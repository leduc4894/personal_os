"""Pure login/session transition logic and the services that orchestrate it.

This module owns the closed password-login, session and password-change
choreography of design sections 8 and 9 as pure functions, typed commands and
results, and three services (:class:`LoginService`, :class:`SessionService`,
:class:`PasswordChangeService`) that depend only on the authentication ports:
the credential/web-session transaction ports implemented by the PostgreSQL
adapter, the password-hasher and crypto ports, and the transaction clock.
Every persisted timestamp and expiry comparison uses the single
``database_now`` of one service invocation; Argon2id verification, secret
generation and hashing always happen outside the database transactions, which
commit once.

The session secret is an opaque 256-bit value of which PostgreSQL stores only
a SHA-256 selector hash; the CSRF token is a separate 256-bit value of which
only an HMAC under the ``auth/csrf/v1`` subkey is stored (spec 9.1, 9.3,
20.1). Username and source throttle material is HMACed under the
``auth/throttle/v1`` subkey before it can reach a row (spec 8.3). Unknown user
and wrong password share one public error and one hasher call through the
fixed dummy Argon2id selection. The module imports no infrastructure SDK,
composition root or web framework.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final, Protocol, runtime_checkable
from uuid import UUID, uuid7

from personal_os.authentication.contracts import (
    AUTHENTICATED_WEB_SCOPES,
    AuthenticatedWebContext,
    WebSessionState,
)
from personal_os.authentication.crypto import CSRF_HASH_LABEL, THROTTLE_HMAC_LABEL
from personal_os.authentication.errors import AuthenticationError
from personal_os.authentication.passwords import (
    PasswordBlocklist,
    validate_new_password,
)
from personal_os.authentication.ports import (
    AuthenticationCryptoPort,
    PasswordHasherPort,
)
from personal_os.diagnostics.context import DiagnosticContext
from personal_os.error_contracts.codes import ErrorCode
from personal_os.identity.contracts import IDENTITY_KEY_PATTERN

#: Login failures allowed per username/source bucket before the lock (spec 8.3).
LOGIN_FAILURE_THRESHOLD: Final[int] = 5

#: Failure-counting window and lock duration per bucket (spec 8.3).
LOGIN_FAILURE_WINDOW: Final[timedelta] = timedelta(minutes=15)
LOGIN_LOCK_DURATION: Final[timedelta] = timedelta(minutes=15)

#: Session lifetimes (spec 9.2, Global Constraints).
PENDING_TOTP_SESSION_TTL: Final[timedelta] = timedelta(minutes=5)
ACTIVE_SESSION_IDLE_TTL: Final[timedelta] = timedelta(hours=12)
ACTIVE_SESSION_ABSOLUTE_TTL: Final[timedelta] = timedelta(days=7)
RECENT_REAUTHENTICATION_WINDOW: Final[timedelta] = timedelta(minutes=5)

#: Cookie/CSRF secret entropy: at least 256 bits each (spec 9.1, 9.3).
SESSION_SECRET_ENTROPY_BYTES: Final[int] = 32
CSRF_SECRET_ENTROPY_BYTES: Final[int] = 32

#: Fixed dummy Argon2id selection with the pinned work parameters: unknown
#: usernames verify against this value so both rejection paths run identical
#: hasher work (spec 8.2 step 4).
DUMMY_LOGIN_PHC_HASH: Final[str] = (
    "$argon2id$v=19$m=65536,t=3,p=1$ZyqiURof2+fcFllZ9PIv5A"
    "$hR4E6R1PUELUnuUnCrBoOGRYf2XyXUmFntxajMPgtDI"
)

#: Logout revocation reason token written to ``web_sessions``.
LOGOUT_REVOCATION_REASON: Final[str] = "logout"

#: Password-change revocation reason for every other session (spec 9.5).
REVOCATION_REASON_PASSWORD_CHANGED: Final[str] = "password_changed"

#: The one authentication method a password login can start (binding decision 2).
PASSWORD_AUTHENTICATION_METHOD: Final[str] = "password"

#: Session states whose binding may still drive their own challenge routes
#: and logout (spec 9.2): every unrevoked state, because ``pending_totp`` and
#: ``recovery_limited`` never authenticate yet may verify their challenge or
#: log out.
CHALLENGE_ELIGIBLE_SESSION_STATES: Final[frozenset[WebSessionState]] = frozenset(
    {
        WebSessionState.PENDING_TOTP,
        WebSessionState.ACTIVE,
        WebSessionState.RECOVERY_LIMITED,
    }
)


class ThrottleBucketKind(StrEnum):
    """Closed throttle-bucket kinds (spec 8.3, binding decision 2)."""

    LOGIN_USERNAME = "login_username"
    LOGIN_SOURCE = "login_source"
    GRANT_CREATION = "grant_creation"
    USER_CODE_LOOKUP = "user_code_lookup"
    TOTP_VERIFICATION = "totp_verification"
    RECOVERY_VERIFICATION = "recovery_verification"


class SessionRotationCause(StrEnum):
    """Closed authentication events that rotate the session binding (spec 9.2)."""

    SESSION_ACTIVATION = "session_activation"
    RECENT_REAUTHENTICATION = "recent_reauthentication"
    RECOVERY_COMPLETED = "recovery_completed"


@dataclass(frozen=True, slots=True)
class ThrottleWindowPolicy:
    """The login throttle contract: threshold, window and lock bounds."""

    failure_threshold: int = LOGIN_FAILURE_THRESHOLD
    window_duration: timedelta = LOGIN_FAILURE_WINDOW
    lock_duration: timedelta = LOGIN_LOCK_DURATION


@dataclass(frozen=True, slots=True)
class SessionWindowPolicy:
    """The session lifetime contract (spec 9.2, 9.4)."""

    pending_totp_ttl: timedelta = PENDING_TOTP_SESSION_TTL
    idle_ttl: timedelta = ACTIVE_SESSION_IDLE_TTL
    absolute_ttl: timedelta = ACTIVE_SESSION_ABSOLUTE_TTL
    reauthentication_window: timedelta = RECENT_REAUTHENTICATION_WINDOW


# --- pure throttle transitions ---------------------------------------------------


@dataclass(frozen=True, slots=True)
class ThrottleBucketState:
    """One persisted throttle bucket's counting state."""

    window_started_at: datetime
    failed_attempt_count: int
    locked_until: datetime | None


@dataclass(frozen=True, slots=True)
class ThrottleFailureTransition:
    """The next bucket state produced by recording one more failure."""

    window_started_at: datetime
    failed_attempt_count: int
    locked_until: datetime | None
    became_locked: bool


def is_throttle_bucket_locked(
    state: ThrottleBucketState | None, *, database_now: datetime
) -> bool:
    """Return whether the bucket still locks attempts at ``database_now``."""
    if state is None or state.locked_until is None:
        return False
    return database_now < state.locked_until


def next_login_failure_transition(
    previous: ThrottleBucketState | None,
    *,
    database_now: datetime,
    policy: ThrottleWindowPolicy,
) -> ThrottleFailureTransition:
    """Compute the bucket state one more recorded failure produces.

    A missing bucket starts counting at one; a bucket whose counting window
    or lock has elapsed restarts at one in a fresh window; a bucket locked by
    a concurrent failure keeps its state unchanged (the lock cap holds and
    attempts during a lock never extend it); otherwise the count advances
    inside the running window and reaching the threshold locks the bucket for
    exactly the lock duration. Pure: the caller persists it under the bucket
    row lock.
    """
    if previous is None or database_now >= previous.window_started_at + policy.window_duration:
        return ThrottleFailureTransition(
            window_started_at=database_now,
            failed_attempt_count=1,
            locked_until=None,
            became_locked=False,
        )
    if is_throttle_bucket_locked(previous, database_now=database_now):
        return ThrottleFailureTransition(
            window_started_at=previous.window_started_at,
            failed_attempt_count=previous.failed_attempt_count,
            locked_until=previous.locked_until,
            became_locked=False,
        )
    next_attempt_count = previous.failed_attempt_count + 1
    becomes_locked = next_attempt_count >= policy.failure_threshold
    return ThrottleFailureTransition(
        window_started_at=previous.window_started_at,
        failed_attempt_count=next_attempt_count,
        locked_until=database_now + policy.lock_duration if becomes_locked else None,
        became_locked=becomes_locked,
    )


def successful_authentication_reset(*, database_now: datetime) -> ThrottleBucketState:
    """The bucket state a successful authentication resets the streak to."""
    return ThrottleBucketState(
        window_started_at=database_now, failed_attempt_count=0, locked_until=None
    )


# --- pure session decisions --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StoredWebSession:
    """Typed view of one ``web_sessions`` row; both hashes never render."""

    web_session_id: UUID
    user_id: UUID
    workspace_id: UUID
    session_secret_hash: str = field(repr=False)
    csrf_secret_hash: str = field(repr=False)
    state: WebSessionState
    credential_revision: int
    authentication_method: str
    created_at: datetime
    authenticated_at: datetime | None
    reauthenticated_at: datetime | None
    last_seen_at: datetime | None
    idle_expires_at: datetime
    absolute_expires_at: datetime
    revoked_at: datetime | None
    revocation_reason: str | None


@dataclass(frozen=True, slots=True)
class SessionAuthenticationDecision:
    """The outcome of evaluating one session row against the expiry contract."""

    is_authenticated: bool
    rejection_code: ErrorCode | None
    should_advance_activity: bool
    next_idle_expires_at: datetime | None


def evaluate_session_authentication(
    session: StoredWebSession,
    *,
    current_credential_revision: int,
    database_now: datetime,
    policy: SessionWindowPolicy,
) -> SessionAuthenticationDecision:
    """Decide one session row's authenticity at ``database_now`` (spec 9.2).

    Only ``active`` authenticates; the row's credential revision must equal the
    credential's current revision; both the idle and the absolute expiry must
    still be in the future. An authenticated session slides its idle window to
    ``database_now + idle_ttl`` clamped to the absolute expiry.
    """
    if (
        session.state is not WebSessionState.ACTIVE
        or session.credential_revision != current_credential_revision
        or database_now >= session.absolute_expires_at
        or database_now >= session.idle_expires_at
    ):
        return SessionAuthenticationDecision(
            is_authenticated=False,
            rejection_code=ErrorCode.AUTHENTICATION_REQUIRED,
            should_advance_activity=False,
            next_idle_expires_at=None,
        )
    return SessionAuthenticationDecision(
        is_authenticated=True,
        rejection_code=None,
        should_advance_activity=True,
        next_idle_expires_at=clamp_idle_expiry(
            database_now + policy.idle_ttl, session.absolute_expires_at
        ),
    )


def clamp_idle_expiry(
    candidate_idle_expiry: datetime, absolute_expires_at: datetime
) -> datetime:
    """Return the idle expiry that never passes the absolute boundary."""
    return min(candidate_idle_expiry, absolute_expires_at)


def is_challenge_eligible_session(
    session: StoredWebSession, *, database_now: datetime
) -> bool:
    """Whether one session binding may drive its own challenge routes (spec 9.2).

    Every unrevoked state qualifies while both expiry boundaries hold, so the
    TOTP/recovery verification and logout routes can resolve ``pending_totp``
    and ``recovery_limited`` bindings that never authenticate. Unlike
    :func:`evaluate_session_authentication` this does not require ``active``
    and never slides the idle window: presenting a challenge or logging out is
    not session activity.
    """
    return (
        session.state in CHALLENGE_ELIGIBLE_SESSION_STATES
        and database_now < session.idle_expires_at
        and database_now < session.absolute_expires_at
    )


def recent_authentication_moment(session: StoredWebSession) -> datetime | None:
    """The most recent password-step or re-authentication moment of a session."""
    moments = [
        moment
        for moment in (session.authenticated_at, session.reauthenticated_at)
        if moment is not None
    ]
    return max(moments) if moments else None


def is_recently_authenticated(
    session: StoredWebSession,
    *,
    database_now: datetime,
    policy: SessionWindowPolicy,
) -> bool:
    """Whether the session authenticated within the re-auth window (spec 9.4)."""
    moment = recent_authentication_moment(session)
    if moment is None:
        return False
    return database_now < moment + policy.reauthentication_window


# --- transaction ports ---------------------------------------------------------------


@runtime_checkable
class AuthenticationClockPort(Protocol):
    """The single transaction-timestamp source of one service invocation."""

    async def database_now(self) -> datetime: ...


@dataclass(frozen=True, slots=True)
class ResolvedLoginMaterial:
    """One username's credential, trust and throttle state (spec 8.2 step 2-4)."""

    user_id: UUID | None
    workspace_id: UUID | None
    is_trusted_account: bool
    password_hash: str | None = field(repr=False)
    credential_revision: int | None
    username_bucket: ThrottleBucketState | None
    source_bucket: ThrottleBucketState | None


@dataclass(frozen=True, slots=True)
class RecordLoginFailureCommand:
    """One rejected password attempt's transactional write."""

    username_bucket_hash: str = field(repr=False)
    source_bucket_hash: str = field(repr=False)
    user_id: UUID | None
    workspace_id: UUID | None
    database_now: datetime
    diagnostic_context: DiagnosticContext


@dataclass(frozen=True, slots=True)
class RecordedLoginFailure:
    """The committed bucket states and audit decision of one failure."""

    username_bucket: ThrottleBucketState
    source_bucket: ThrottleBucketState
    was_audited: bool


@dataclass(frozen=True, slots=True)
class CommitLoginSuccessCommand:
    """One accepted password attempt's transactional write.

    Both candidate idle expiries travel with the command because only the
    transaction, after rechecking the TOTP state under the credential row
    lock, decides whether the session starts ``active`` or ``pending_totp``
    (spec 8.2 step 9).
    """

    user_id: UUID
    workspace_id: UUID
    expected_credential_revision: int
    username_bucket_hash: str = field(repr=False)
    web_session_id: UUID
    session_secret_hash: str = field(repr=False)
    csrf_secret_hash: str = field(repr=False)
    authentication_method: str
    database_now: datetime
    active_idle_expires_at: datetime
    pending_totp_idle_expires_at: datetime
    absolute_expires_at: datetime
    upgraded_password_hash: str | None = field(repr=False)
    diagnostic_context: DiagnosticContext


@dataclass(frozen=True, slots=True)
class CommittedLoginSuccess:
    """The committed session identity of one accepted login."""

    web_session_id: UUID
    user_id: UUID
    workspace_id: UUID
    state: WebSessionState
    credential_revision: int
    authenticated_at: datetime | None
    idle_expires_at: datetime
    absolute_expires_at: datetime
    database_now: datetime


@dataclass(frozen=True, slots=True)
class ChangePasswordCommand:
    """One password change's transactional write (spec 9.5)."""

    user_id: UUID
    workspace_id: UUID
    current_web_session_id: UUID
    prior_session_secret_hash: str = field(repr=False)
    expected_credential_revision: int
    new_password_hash: str = field(repr=False)
    new_session_secret_hash: str = field(repr=False)
    new_csrf_secret_hash: str = field(repr=False)
    database_now: datetime
    diagnostic_context: DiagnosticContext


@dataclass(frozen=True, slots=True)
class ChangedPassword:
    """The committed outcome of one password change."""

    current_web_session_id: UUID
    credential_revision: int
    revoked_session_count: int
    database_now: datetime


@runtime_checkable
class CredentialTransactionPort(Protocol):
    """The credential/throttle transaction surface the services orchestrate."""

    async def resolve_login_material(
        self, *, username: str, username_bucket_hash: str, source_bucket_hash: str
    ) -> ResolvedLoginMaterial: ...

    async def record_login_failure(
        self, command: RecordLoginFailureCommand
    ) -> RecordedLoginFailure: ...

    async def commit_login_success(
        self, command: CommitLoginSuccessCommand
    ) -> CommittedLoginSuccess: ...

    async def change_password(self, command: ChangePasswordCommand) -> ChangedPassword: ...


@dataclass(frozen=True, slots=True)
class ResolvedWebSession:
    """One resolved session row joined with its current credential state."""

    session: StoredWebSession
    current_credential_revision: int
    password_hash: str | None = field(repr=False)
    database_now: datetime


@dataclass(frozen=True, slots=True)
class RotateWebSessionSecretsCommand:
    """One authentication event's session-binding rotation."""

    web_session_id: UUID
    prior_session_secret_hash: str = field(repr=False)
    new_session_secret_hash: str = field(repr=False)
    new_csrf_secret_hash: str = field(repr=False)
    cause: SessionRotationCause
    target_authentication_method: str
    database_now: datetime


@dataclass(frozen=True, slots=True)
class RotatedWebSessionSecrets:
    """The committed state of one rotated session."""

    web_session_id: UUID
    state: WebSessionState
    database_now: datetime


@dataclass(frozen=True, slots=True)
class RevokeWebSessionCommand:
    """One logout's revocation write, keyed by the presented secret hash."""

    session_secret_hash: str = field(repr=False)
    revocation_reason: str
    database_now: datetime


@dataclass(frozen=True, slots=True)
class RevokedWebSession:
    """The committed revocation moment."""

    web_session_id: UUID
    revoked_at: datetime


@runtime_checkable
class WebSessionTransactionPort(Protocol):
    """The session-lifecycle transaction surface the services orchestrate."""

    async def resolve_session(
        self, *, session_secret_hash: str, database_now: datetime
    ) -> ResolvedWebSession: ...

    async def resolve_challenge_eligible_session(
        self, *, session_secret_hash: str, database_now: datetime
    ) -> ResolvedWebSession: ...

    async def rotate_session_secrets(
        self, command: RotateWebSessionSecretsCommand
    ) -> RotatedWebSessionSecrets: ...

    async def revoke_session(self, command: RevokeWebSessionCommand) -> RevokedWebSession: ...


# --- service results ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StartedWebSession:
    """A freshly created session: ids, lifetimes and the one-time secrets."""

    web_session_id: UUID
    user_id: UUID
    workspace_id: UUID
    state: WebSessionState
    authentication_method: str
    credential_revision: int
    session_secret: str = field(repr=False)
    csrf_secret: str = field(repr=False)
    session_secret_hash: str = field(repr=False)
    csrf_secret_hash: str = field(repr=False)
    idle_expires_at: datetime
    absolute_expires_at: datetime
    database_now: datetime


@dataclass(frozen=True, slots=True)
class LoginOutcome:
    """One login attempt's public result; secrets only on success."""

    public_error: ErrorCode | None
    locked_until: datetime | None
    started_session: StartedWebSession | None


@dataclass(frozen=True, slots=True)
class AuthenticatedSession:
    """One authenticated request's resolved session view."""

    context: AuthenticatedWebContext
    csrf_secret_hash: str = field(repr=False)
    session: StoredWebSession
    database_now: datetime


@dataclass(frozen=True, slots=True)
class RotatedCurrentSession:
    """The freshly rotated binding of the caller's own session."""

    web_session_id: UUID
    session_secret: str = field(repr=False)
    csrf_secret: str = field(repr=False)
    session_secret_hash: str = field(repr=False)
    csrf_secret_hash: str = field(repr=False)
    database_now: datetime


@dataclass(frozen=True, slots=True)
class ReauthenticationOutcome:
    """One recent-re-authentication attempt's public result (spec 9.4)."""

    public_error: ErrorCode | None
    rotated_session: RotatedCurrentSession | None


@dataclass(frozen=True, slots=True)
class PasswordChangeOutcome:
    """One password change's public result (spec 9.5)."""

    public_error: ErrorCode | None
    rotated_session: RotatedCurrentSession | None
    credential_revision: int | None
    revoked_session_count: int
    database_now: datetime | None


# --- secret material helpers -------------------------------------------------------------


def session_secret_hash_of(session_secret: str) -> str:
    """The SHA-256 selector hash stored for one opaque session secret."""
    return hashlib.sha256(session_secret.encode("utf-8")).hexdigest()


def derive_throttle_hmac_key(
    crypto: AuthenticationCryptoPort, master_key: bytes
) -> bytes:
    """Derive the ``auth/throttle/v1`` HMAC subkey (spec 8.3, 20.1)."""
    return crypto.derive_subkey(master_key=master_key, label=THROTTLE_HMAC_LABEL)


def derive_csrf_hmac_key(crypto: AuthenticationCryptoPort, master_key: bytes) -> bytes:
    """Derive the ``auth/csrf/v1`` HMAC subkey (spec 9.3, 20.1)."""
    return crypto.derive_subkey(master_key=master_key, label=CSRF_HASH_LABEL)


def throttle_bucket_hash(
    *, hmac_key: bytes, bucket_kind: ThrottleBucketKind, bucket_material: str
) -> str:
    """HMAC one bucket's raw material into the stored 64-hex digest.

    The kind prefix keeps the two login bucket families from ever colliding on
    equal material, and no raw username or source value reaches a row.
    """
    message = bucket_kind.value.encode("ascii") + b"\x00" + bucket_material.encode("utf-8")
    return hmac.new(hmac_key, message, hashlib.sha256).hexdigest()


def generate_session_secret_material(
    *, csrf_hmac_key: bytes
) -> tuple[str, str, str, str]:
    """Generate one session's opaque secrets and their two stored hashes.

    Returns ``(session_secret, csrf_secret, session_secret_hash,
    csrf_secret_hash)``. The session secret hashes with plain SHA-256 — it is
    a lookup selector over 256 bits of entropy — while the CSRF token hashes
    under the ``auth/csrf/v1`` HMAC subkey per the closed label vocabulary.
    """
    session_secret = secrets.token_urlsafe(SESSION_SECRET_ENTROPY_BYTES)
    csrf_secret = secrets.token_urlsafe(CSRF_SECRET_ENTROPY_BYTES)
    return (
        session_secret,
        csrf_secret,
        session_secret_hash_of(session_secret),
        hmac.new(
            csrf_hmac_key, csrf_secret.encode("utf-8"), hashlib.sha256
        ).hexdigest(),
    )


# --- services --------------------------------------------------------------------------


class LoginService:
    """The password-login choreography of spec 8.2.

    One invocation acquires one ``database_now`` and one read of the resolved
    material, verifies the password against the stored or the fixed dummy
    Argon2id selection outside every transaction, and then commits exactly one
    write: either the throttled rejection with its audit (trusted accounts
    only) or the session-creating success with its streak reset, optional hash
    upgrade and audit.
    """

    def __init__(
        self,
        *,
        credentials: CredentialTransactionPort,
        hasher: PasswordHasherPort,
        crypto: AuthenticationCryptoPort,
        master_key: bytes,
        clock: AuthenticationClockPort,
        throttle_policy: ThrottleWindowPolicy | None = None,
        session_policy: SessionWindowPolicy | None = None,
    ) -> None:
        self._credentials = credentials
        self._hasher = hasher
        self._clock = clock
        self._throttle_policy = (
            throttle_policy if throttle_policy is not None else ThrottleWindowPolicy()
        )
        self._session_policy = (
            session_policy if session_policy is not None else SessionWindowPolicy()
        )
        self._throttle_hmac_key = derive_throttle_hmac_key(crypto, master_key)
        self._csrf_hmac_key = derive_csrf_hmac_key(crypto, master_key)

    async def login(
        self,
        *,
        username: str,
        password: str,
        source_bucket: str,
        diagnostic_context: DiagnosticContext,
    ) -> LoginOutcome:
        """Run one login attempt; never reveal account existence."""
        database_now = await self._clock.database_now()
        username_bucket_hash = throttle_bucket_hash(
            hmac_key=self._throttle_hmac_key,
            bucket_kind=ThrottleBucketKind.LOGIN_USERNAME,
            bucket_material=username,
        )
        source_bucket_hash = throttle_bucket_hash(
            hmac_key=self._throttle_hmac_key,
            bucket_kind=ThrottleBucketKind.LOGIN_SOURCE,
            bucket_material=source_bucket,
        )
        if IDENTITY_KEY_PATTERN.fullmatch(username) is None:
            # Grammar-invalid usernames can never resolve: verify once against
            # the dummy selection for uniform work, then fail without touching
            # PostgreSQL (no bucket rows for unbounded hostile input).
            self._hasher.verify_password(DUMMY_LOGIN_PHC_HASH, password)
            return LoginOutcome(
                public_error=ErrorCode.AUTHENTICATION_FAILED,
                locked_until=None,
                started_session=None,
            )
        material = await self._credentials.resolve_login_material(
            username=username,
            username_bucket_hash=username_bucket_hash,
            source_bucket_hash=source_bucket_hash,
        )
        locked_until = _earliest_active_lock(material, database_now=database_now)
        if locked_until is not None:
            return LoginOutcome(
                public_error=ErrorCode.AUTHENTICATION_RATE_LIMITED,
                locked_until=locked_until,
                started_session=None,
            )
        selected_hash = (
            material.password_hash
            if material.password_hash is not None
            else DUMMY_LOGIN_PHC_HASH
        )
        is_password_valid = self._hasher.verify_password(selected_hash, password)
        if not is_password_valid:
            recorded = await self._credentials.record_login_failure(
                RecordLoginFailureCommand(
                    username_bucket_hash=username_bucket_hash,
                    source_bucket_hash=source_bucket_hash,
                    user_id=material.user_id,
                    workspace_id=material.workspace_id,
                    database_now=database_now,
                    diagnostic_context=diagnostic_context,
                )
            )
            return LoginOutcome(
                public_error=ErrorCode.AUTHENTICATION_FAILED,
                locked_until=_newly_set_lock(recorded),
                started_session=None,
            )
        if material.user_id is None or material.workspace_id is None:
            # A verified password without a resolved trusted account cannot
            # happen: no credential row means the dummy selection ran.
            raise AuthenticationError(ErrorCode.AUTHENTICATION_FAILED)
        assert material.credential_revision is not None
        upgraded_password_hash = (
            self._hasher.hash_password(password)
            if self._hasher.needs_rehash(selected_hash)
            else None
        )
        (
            session_secret,
            csrf_secret,
            session_secret_hash_value,
            csrf_secret_hash_value,
        ) = generate_session_secret_material(csrf_hmac_key=self._csrf_hmac_key)
        committed = await self._credentials.commit_login_success(
            CommitLoginSuccessCommand(
                user_id=material.user_id,
                workspace_id=material.workspace_id,
                expected_credential_revision=material.credential_revision,
                username_bucket_hash=username_bucket_hash,
                web_session_id=uuid7(),
                session_secret_hash=session_secret_hash_value,
                csrf_secret_hash=csrf_secret_hash_value,
                authentication_method=PASSWORD_AUTHENTICATION_METHOD,
                database_now=database_now,
                active_idle_expires_at=database_now + self._session_policy.idle_ttl,
                pending_totp_idle_expires_at=(
                    database_now + self._session_policy.pending_totp_ttl
                ),
                absolute_expires_at=database_now + self._session_policy.absolute_ttl,
                upgraded_password_hash=upgraded_password_hash,
                diagnostic_context=diagnostic_context,
            )
        )
        return LoginOutcome(
            public_error=None,
            locked_until=None,
            started_session=StartedWebSession(
                web_session_id=committed.web_session_id,
                user_id=committed.user_id,
                workspace_id=committed.workspace_id,
                state=committed.state,
                authentication_method=PASSWORD_AUTHENTICATION_METHOD,
                credential_revision=committed.credential_revision,
                session_secret=session_secret,
                csrf_secret=csrf_secret,
                session_secret_hash=session_secret_hash_value,
                csrf_secret_hash=csrf_secret_hash_value,
                idle_expires_at=committed.idle_expires_at,
                absolute_expires_at=committed.absolute_expires_at,
                database_now=database_now,
            ),
        )


def _earliest_active_lock(
    material: ResolvedLoginMaterial, *, database_now: datetime
) -> datetime | None:
    active_locks = [
        locked_until
        for locked_until in (
            _active_lock_of(material.username_bucket, database_now=database_now),
            _active_lock_of(material.source_bucket, database_now=database_now),
        )
        if locked_until is not None
    ]
    return max(active_locks) if active_locks else None


def _active_lock_of(
    state: ThrottleBucketState | None, *, database_now: datetime
) -> datetime | None:
    if state is None or state.locked_until is None:
        return None
    return state.locked_until if database_now < state.locked_until else None


def _newly_set_lock(recorded: RecordedLoginFailure) -> datetime | None:
    locks = [
        state.locked_until
        for state in (recorded.username_bucket, recorded.source_bucket)
        if state.locked_until is not None
    ]
    return max(locks) if locks else None


class SessionService:
    """The session resolution, rotation and revocation choreography of spec 9.

    ``resolve``/``authenticate`` resolve one cookie secret to the full-scope
    :class:`AuthenticatedWebContext`, sliding the idle window inside the same
    transaction without ever passing the absolute expiry. ``reauthenticate``
    verifies the password against the stored hash outside the transaction and
    rotates the session binding with ``reauthenticated_at``. ``revoke``
    implements logout's row revocation. ``prepare_rotation_material``
    generates one rotation's fresh secrets for a caller that commits them
    inside its own transaction (the password change).
    """

    def __init__(
        self,
        *,
        sessions: WebSessionTransactionPort,
        hasher: PasswordHasherPort,
        crypto: AuthenticationCryptoPort,
        master_key: bytes,
        clock: AuthenticationClockPort,
        session_policy: SessionWindowPolicy | None = None,
    ) -> None:
        self._sessions = sessions
        self._hasher = hasher
        self._clock = clock
        self.session_policy = (
            session_policy if session_policy is not None else SessionWindowPolicy()
        )
        self._csrf_hmac_key = derive_csrf_hmac_key(crypto, master_key)

    async def database_now(self) -> datetime:
        """One transaction timestamp shared with co-orchestrating services."""
        return await self._clock.database_now()

    async def resolve(
        self,
        *,
        session_secret: str,
        database_now: datetime | None = None,
    ) -> AuthenticatedSession:
        """Resolve one session secret or reject with ``authentication_required``.

        ``database_now`` lets a co-orchestrating service (the password change)
        share its single clock read with this resolution so the whole
        invocation decides and persists against one transaction timestamp;
        omitted, the resolution takes its own read.
        """
        transaction_now = (
            database_now
            if database_now is not None
            else await self._clock.database_now()
        )
        resolved = await self._sessions.resolve_session(
            session_secret_hash=session_secret_hash_of(session_secret),
            database_now=transaction_now,
        )
        return self._authenticated_session_of(resolved.session, database_now=transaction_now)

    async def authenticate(self, *, session_secret: str) -> AuthenticatedSession:
        """Resolve one session secret to its authenticated web context."""
        return await self.resolve(session_secret=session_secret)

    async def resolve_challenge_eligible(
        self, *, session_secret: str
    ) -> AuthenticatedSession:
        """Resolve one session secret tolerating the pending/recovery states.

        The state-tolerant resolution of spec 9.2: a binding in any unrevoked,
        unexpired state still resolves for its own challenge routes — TOTP and
        recovery verification, logout — even though only ``active``
        authenticates; the idle window never slides because presenting a
        challenge or logging out is not session activity.
        """
        transaction_now = await self._clock.database_now()
        resolved = await self._sessions.resolve_challenge_eligible_session(
            session_secret_hash=session_secret_hash_of(session_secret),
            database_now=transaction_now,
        )
        return self._authenticated_session_of(resolved.session, database_now=transaction_now)

    @staticmethod
    def _authenticated_session_of(
        session: StoredWebSession, *, database_now: datetime
    ) -> AuthenticatedSession:
        """Build the resolved view of one session row for its state.

        Only an ``active`` row carries the granted web scopes; the strict
        resolution never resolves anything else, so the empty-scope branch is
        reached only by the challenge resolution of non-authenticating states.
        """
        return AuthenticatedSession(
            context=AuthenticatedWebContext(
                user_id=session.user_id,
                workspace_id=session.workspace_id,
                web_session_id=session.web_session_id,
                credential_revision=session.credential_revision,
                scopes=(
                    AUTHENTICATED_WEB_SCOPES
                    if session.state is WebSessionState.ACTIVE
                    else frozenset()
                ),
            ),
            csrf_secret_hash=session.csrf_secret_hash,
            session=session,
            database_now=database_now,
        )

    async def reauthenticate(
        self, *, session_secret: str, password: str
    ) -> ReauthenticationOutcome:
        """Verify the password again and rotate the session (spec 9.4)."""
        database_now = await self._clock.database_now()
        resolved = await self._sessions.resolve_session(
            session_secret_hash=session_secret_hash_of(session_secret),
            database_now=database_now,
        )
        if resolved.password_hash is None or not self._hasher.verify_password(
            resolved.password_hash, password
        ):
            return ReauthenticationOutcome(
                public_error=ErrorCode.AUTHENTICATION_FAILED, rotated_session=None
            )
        prepared = self.prepare_rotation_material(
            web_session_id=resolved.session.web_session_id,
            database_now=database_now,
        )
        await self._sessions.rotate_session_secrets(
            RotateWebSessionSecretsCommand(
                web_session_id=resolved.session.web_session_id,
                prior_session_secret_hash=resolved.session.session_secret_hash,
                new_session_secret_hash=prepared.session_secret_hash,
                new_csrf_secret_hash=prepared.csrf_secret_hash,
                cause=SessionRotationCause.RECENT_REAUTHENTICATION,
                target_authentication_method=resolved.session.authentication_method,
                database_now=database_now,
            )
        )
        return ReauthenticationOutcome(public_error=None, rotated_session=prepared)

    async def revoke(self, *, session_secret: str) -> datetime:
        """Revoke the session row of one logout (spec 9.2).

        The store resolves and revokes behind the presented secret hash in one
        transaction; logout is also reachable from ``pending_totp`` and
        ``recovery_limited`` states.
        """
        database_now = await self._clock.database_now()
        revoked = await self._sessions.revoke_session(
            RevokeWebSessionCommand(
                session_secret_hash=session_secret_hash_of(session_secret),
                revocation_reason=LOGOUT_REVOCATION_REASON,
                database_now=database_now,
            )
        )
        return revoked.revoked_at

    def prepare_rotation_material(
        self, *, web_session_id: UUID, database_now: datetime
    ) -> RotatedCurrentSession:
        """Generate one rotation's fresh secrets outside any transaction."""
        session_secret, csrf_secret, session_secret_hash_value, csrf_secret_hash_value = (
            generate_session_secret_material(csrf_hmac_key=self._csrf_hmac_key)
        )
        return RotatedCurrentSession(
            web_session_id=web_session_id,
            session_secret=session_secret,
            csrf_secret=csrf_secret,
            session_secret_hash=session_secret_hash_value,
            csrf_secret_hash=csrf_secret_hash_value,
            database_now=database_now,
        )


class PasswordChangeService:
    """The password-change choreography of spec 9.5.

    Requires a recently re-authenticated session, validates and hashes the new
    password outside the transaction, then commits one credential-anchored
    transaction that bumps the revision, revokes every other session and
    rotates the current binding. One clock read drives the re-auth gate, the
    session resolution and every persisted write of the invocation. Obsidian
    devices are never touched.
    """

    def __init__(
        self,
        *,
        session_service: SessionService,
        credentials: CredentialTransactionPort,
        hasher: PasswordHasherPort,
        blocklist: PasswordBlocklist,
    ) -> None:
        self._session_service = session_service
        self._credentials = credentials
        self._hasher = hasher
        self._blocklist = blocklist

    async def change_password(
        self,
        *,
        session_secret: str,
        new_password: str,
        diagnostic_context: DiagnosticContext,
    ) -> PasswordChangeOutcome:
        """Run one password change; device state is never touched."""
        database_now = await self._session_service.database_now()
        resolved = await self._session_service.resolve(
            session_secret=session_secret, database_now=database_now
        )
        session = resolved.session
        if not is_recently_authenticated(
            session,
            database_now=database_now,
            policy=self._session_service.session_policy,
        ):
            return PasswordChangeOutcome(
                public_error=ErrorCode.RECENT_AUTHENTICATION_REQUIRED,
                rotated_session=None,
                credential_revision=None,
                revoked_session_count=0,
                database_now=database_now,
            )
        try:
            validate_new_password(new_password, self._blocklist)
        except AuthenticationError:
            return PasswordChangeOutcome(
                public_error=ErrorCode.AUTHENTICATION_FAILED,
                rotated_session=None,
                credential_revision=None,
                revoked_session_count=0,
                database_now=database_now,
            )
        new_password_hash = self._hasher.hash_password(new_password)
        rotated_session = self._session_service.prepare_rotation_material(
            web_session_id=session.web_session_id, database_now=database_now
        )
        changed = await self._credentials.change_password(
            ChangePasswordCommand(
                user_id=session.user_id,
                workspace_id=session.workspace_id,
                current_web_session_id=session.web_session_id,
                prior_session_secret_hash=session.session_secret_hash,
                expected_credential_revision=session.credential_revision,
                new_password_hash=new_password_hash,
                new_session_secret_hash=rotated_session.session_secret_hash,
                new_csrf_secret_hash=rotated_session.csrf_secret_hash,
                database_now=database_now,
                diagnostic_context=diagnostic_context,
            )
        )
        return PasswordChangeOutcome(
            public_error=None,
            rotated_session=rotated_session,
            credential_revision=changed.credential_revision,
            revoked_session_count=changed.revoked_session_count,
            database_now=changed.database_now,
        )
