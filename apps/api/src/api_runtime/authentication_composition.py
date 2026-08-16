"""Composition of the web authentication runtime: serve graph and offline graph.

:func:`compose_web_authentication` builds the real service graph the serve
process runs: the PostgreSQL credential and session stores over the shared
engine, the reviewed Argon2id and HKDF/HMAC/AES-GCM adapters, the offline
password blocklist, a transaction clock reading the database timestamp, and
the stored-hash CSRF verifier derived under the ``auth/csrf/v1`` subkey of the
current master key (spec 8, 9.3, 20.1).

:func:`compose_offline_web_authentication` builds the deterministic offline
graph used by the OpenAPI export and by unit tests: fixed key material, a
fixed clock, in-memory credential/session state and stdlib digest doubles. It
reads no environment value, no secret file and no database, so the offline
contract document stays byte-deterministic.

:func:`verify_keyring_covers_required_key_ids` implements the spec 20.1
startup refusal: when PostgreSQL still references a key ID the configured
keyring does not carry, the caller refuses startup before the listening
socket is exposed.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Final, cast
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from api_runtime.authentication_crypto import (
    Argon2PasswordHasher,
    AuthenticationKeyring,
    CryptographyAuthenticationCrypto,
)
from api_runtime.authentication_dependencies import (
    SessionCookieContract,
    build_session_cookie_contract,
)
from api_runtime.authentication_settings import AuthenticationSettings
from personal_os.authentication.contracts import WebSessionState
from personal_os.authentication.errors import AuthenticationError
from personal_os.authentication.passwords import (
    PasswordBlocklist,
    load_common_password_blocklist,
)
from personal_os.authentication.ports import AuthenticationCryptoPort
from personal_os.authentication.sessions import (
    ChangedPassword,
    ChangePasswordCommand,
    CommitLoginSuccessCommand,
    CommittedLoginSuccess,
    CredentialTransactionPort,
    LoginService,
    PasswordChangeService,
    RecordedLoginFailure,
    RecordLoginFailureCommand,
    ResolvedLoginMaterial,
    ResolvedWebSession,
    RevokedWebSession,
    RevokeWebSessionCommand,
    RotatedWebSessionSecrets,
    RotateWebSessionSecretsCommand,
    SessionService,
    SessionWindowPolicy,
    StoredWebSession,
    ThrottleBucketState,
    ThrottleWindowPolicy,
    WebSessionTransactionPort,
    derive_csrf_hmac_key,
    evaluate_session_authentication,
    is_challenge_eligible_session,
    next_login_failure_transition,
)
from personal_os.diagnostics.events import SafeToken
from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import ConfigurationError
from personal_os.runtime_configuration.models import RuntimeEnvironment
from postgresql_source_store.authentication_credentials import (
    CredentialStore,
    run_authentication_transaction,
)
from postgresql_source_store.web_session_store import WebSessionStore

#: The exact origin the offline composition accepts. It never enters the
#: contract document and never names a deployment machine.
OFFLINE_WEB_ALLOWED_ORIGIN: Final[str] = "https://web-admin.example"

#: Deterministic offline key material and transaction timestamp.
_OFFLINE_MASTER_KEY: Final[bytes] = bytes(range(32))
_OFFLINE_DATABASE_NOW: Final[datetime] = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)

#: The one offline enrolled account and its canonical ids.
OFFLINE_USERNAME: Final[str] = "admin"
OFFLINE_PASSWORD: Final[str] = "correct-horse-battery-staple"
_OFFLINE_USER_ID: Final[UUID] = UUID("00000000-0000-7000-8000-000000000001")
_OFFLINE_WORKSPACE_ID: Final[UUID] = UUID("00000000-0000-7000-8000-000000000002")

#: The offline graph pins the domain default window policies.
_OFFLINE_THROTTLE_POLICY: Final[ThrottleWindowPolicy] = ThrottleWindowPolicy()
_OFFLINE_SESSION_POLICY: Final[SessionWindowPolicy] = SessionWindowPolicy()

#: Safe reason token of the spec 20.1 startup refusal.
_MISSING_REFERENCED_KEY_REASON: Final[SafeToken] = SafeToken.parse(
    "keyring_missing_referenced_key"
)


@dataclass(frozen=True, slots=True)
class WebAuthenticationRuntime:
    """One composed authentication runtime the session routes consume."""

    allowed_origin: str
    cookie_contract: SessionCookieContract
    login_service: LoginService
    session_service: SessionService
    password_change_service: PasswordChangeService
    verify_csrf_token: Callable[[str, str], bool]


def _build_csrf_verifier(
    crypto: AuthenticationCryptoPort, master_key: bytes
) -> Callable[[str, str], bool]:
    """Bind the stored-hash CSRF comparison under the ``auth/csrf/v1`` subkey."""
    csrf_hmac_key = derive_csrf_hmac_key(crypto, master_key)

    def verify_csrf_token(presented_token: str, stored_hash: str) -> bool:
        computed = crypto.hmac_sha256(
            key=csrf_hmac_key, message=presented_token.encode("utf-8")
        ).hex()
        return hmac.compare_digest(computed, stored_hash)

    return verify_csrf_token


# --- the serve composition -------------------------------------------------------------


class DatabaseAuthenticationClock:
    """Transaction clock reading the canonical database timestamp (spec 8.2)."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def database_now(self) -> datetime:
        """Return one database timestamp through the bounded transaction runner."""

        async def read_now(connection: AsyncConnection) -> datetime:
            return cast("datetime", await connection.scalar(sa.text("SELECT now()")))

        return await run_authentication_transaction(self._engine, read_now)


def compose_web_authentication(
    *,
    settings: AuthenticationSettings,
    keyring: AuthenticationKeyring,
    engine: AsyncEngine,
) -> WebAuthenticationRuntime:
    """Build the real authentication runtime of one serve process."""
    hasher = Argon2PasswordHasher()
    crypto = CryptographyAuthenticationCrypto()
    clock = DatabaseAuthenticationClock(engine)
    credentials: CredentialTransactionPort = CredentialStore(engine)
    sessions: WebSessionTransactionPort = WebSessionStore(engine)
    master_key = keyring.current_key()
    session_service = SessionService(
        sessions=sessions, hasher=hasher, crypto=crypto, master_key=master_key, clock=clock
    )
    return WebAuthenticationRuntime(
        allowed_origin=settings.allowed_origin,
        cookie_contract=build_session_cookie_contract(
            settings.allowed_origin, settings.environment
        ),
        login_service=LoginService(
            credentials=credentials,
            hasher=hasher,
            crypto=crypto,
            master_key=master_key,
            clock=clock,
        ),
        session_service=session_service,
        password_change_service=PasswordChangeService(
            session_service=session_service,
            credentials=credentials,
            hasher=hasher,
            blocklist=load_common_password_blocklist(),
        ),
        verify_csrf_token=_build_csrf_verifier(crypto, master_key),
    )


def assert_keyring_covers_required_key_ids(
    required_key_ids: frozenset[str], keyring: AuthenticationKeyring
) -> None:
    """Refuse startup when PostgreSQL references a key the keyring omits.

    The refusal carries only the fixed safe reason token — never a key ID,
    file name or count.
    """
    missing_key_ids = required_key_ids - set(keyring.keys_by_id)
    if missing_key_ids:
        raise ConfigurationError(
            ErrorCode.CONFIGURATION_SECRET_INVALID,
            safe_details={"reason": _MISSING_REFERENCED_KEY_REASON},
        )


async def verify_keyring_covers_required_key_ids(
    *, engine: AsyncEngine, keyring: AuthenticationKeyring
) -> None:
    """Read every referenced key ID and enforce the coverage refusal (spec 20.1).

    The composition root calls this before the listening socket is exposed:
    Uvicorn runs the application lifespan startup before binding, so the
    raised :class:`ConfigurationError` aborts startup.
    """
    required_key_ids = await CredentialStore(engine).required_key_ids(
        database_now=datetime.now(UTC)
    )
    assert_keyring_covers_required_key_ids(required_key_ids, keyring)


# --- the offline composition -----------------------------------------------------------


class OfflinePasswordHasher:
    """Deterministic hasher double: digests only, no native library."""

    def hash_password(self, password: str) -> str:
        digest = hashlib.sha256(password.encode("utf-8")).hexdigest()
        return f"offline${digest}"

    def verify_password(self, password_hash: str, password: str) -> bool:
        if not password_hash.startswith("offline$"):
            return False
        digest = hashlib.sha256(password.encode("utf-8")).hexdigest()
        return hmac.compare_digest(password_hash, f"offline${digest}")

    def needs_rehash(self, password_hash: str) -> bool:
        del password_hash
        return False


class OfflineAuthenticationCrypto:
    """Deterministic crypto double deriving stable subkeys and stdlib HMAC."""

    def derive_subkey(self, *, master_key: bytes, label: str) -> bytes:
        return hashlib.sha256(label.encode("ascii") + master_key).digest()

    def hmac_sha256(self, *, key: bytes, message: bytes) -> bytes:
        return hmac.new(key, message, hashlib.sha256).digest()

    def seal_secret(self, *, key: bytes, plaintext: bytes) -> tuple[bytes, bytes]:
        del key, plaintext
        raise AssertionError("the offline composition never seals secrets")

    def open_secret(self, *, key: bytes, nonce: bytes, ciphertext: bytes) -> bytes:
        del key, nonce, ciphertext
        raise AssertionError("the offline composition never opens secrets")


class OfflineAuthenticationClock:
    """Fixed transaction clock; tests may advance the pinned timestamp."""

    def __init__(self) -> None:
        self.database_now_value: datetime = _OFFLINE_DATABASE_NOW

    async def database_now(self) -> datetime:
        return self.database_now_value


class OfflineAuthenticationState:
    """In-memory credential, throttle and session state of the offline graph."""

    def __init__(self, *, totp_active: bool) -> None:
        self.totp_active = totp_active
        self.credential_revision = 1
        self.password_hash = OfflinePasswordHasher().hash_password(OFFLINE_PASSWORD)
        self.sessions_by_secret_hash: dict[str, StoredWebSession] = {}
        self.buckets: dict[str, ThrottleBucketState] = {}


class OfflineCredentialStore:
    """In-memory credential transaction double behind the login services."""

    def __init__(self, state: OfflineAuthenticationState) -> None:
        self._state = state

    async def resolve_login_material(
        self, *, username: str, username_bucket_hash: str, source_bucket_hash: str
    ) -> ResolvedLoginMaterial:
        is_enrolled_account = username == OFFLINE_USERNAME
        return ResolvedLoginMaterial(
            user_id=_OFFLINE_USER_ID if is_enrolled_account else None,
            workspace_id=_OFFLINE_WORKSPACE_ID if is_enrolled_account else None,
            is_trusted_account=is_enrolled_account,
            password_hash=self._state.password_hash if is_enrolled_account else None,
            credential_revision=(
                self._state.credential_revision if is_enrolled_account else None
            ),
            username_bucket=self._state.buckets.get(username_bucket_hash),
            source_bucket=self._state.buckets.get(source_bucket_hash),
        )

    async def record_login_failure(
        self, command: RecordLoginFailureCommand
    ) -> RecordedLoginFailure:
        transition = next_login_failure_transition(
            self._state.buckets.get(command.username_bucket_hash),
            database_now=command.database_now,
            policy=_OFFLINE_THROTTLE_POLICY,
        )
        username_bucket = ThrottleBucketState(
            window_started_at=transition.window_started_at,
            failed_attempt_count=transition.failed_attempt_count,
            locked_until=transition.locked_until,
        )
        source_bucket = ThrottleBucketState(
            window_started_at=transition.window_started_at,
            failed_attempt_count=transition.failed_attempt_count,
            locked_until=transition.locked_until,
        )
        self._state.buckets[command.username_bucket_hash] = username_bucket
        self._state.buckets[command.source_bucket_hash] = source_bucket
        return RecordedLoginFailure(
            username_bucket=username_bucket,
            source_bucket=source_bucket,
            was_audited=command.user_id is not None,
        )

    async def commit_login_success(
        self, command: CommitLoginSuccessCommand
    ) -> CommittedLoginSuccess:
        state = (
            WebSessionState.PENDING_TOTP
            if self._state.totp_active
            else WebSessionState.ACTIVE
        )
        session = StoredWebSession(
            web_session_id=command.web_session_id,
            user_id=command.user_id,
            workspace_id=command.workspace_id,
            session_secret_hash=command.session_secret_hash,
            csrf_secret_hash=command.csrf_secret_hash,
            state=state,
            credential_revision=command.expected_credential_revision,
            authentication_method="password",
            created_at=command.database_now,
            authenticated_at=None if self._state.totp_active else command.database_now,
            reauthenticated_at=None,
            last_seen_at=None,
            idle_expires_at=(
                command.pending_totp_idle_expires_at
                if self._state.totp_active
                else command.active_idle_expires_at
            ),
            absolute_expires_at=command.absolute_expires_at,
            revoked_at=None,
            revocation_reason=None,
        )
        self._state.sessions_by_secret_hash[command.session_secret_hash] = session
        return CommittedLoginSuccess(
            web_session_id=command.web_session_id,
            user_id=command.user_id,
            workspace_id=command.workspace_id,
            state=state,
            credential_revision=command.expected_credential_revision,
            authenticated_at=session.authenticated_at,
            idle_expires_at=session.idle_expires_at,
            absolute_expires_at=session.absolute_expires_at,
            database_now=command.database_now,
        )

    async def change_password(self, command: ChangePasswordCommand) -> ChangedPassword:
        session = self._state.sessions_by_secret_hash.get(command.prior_session_secret_hash)
        if session is None or session.web_session_id != command.current_web_session_id:
            raise AuthenticationError(ErrorCode.AUTHENTICATION_REQUIRED)
        del self._state.sessions_by_secret_hash[command.prior_session_secret_hash]
        self._state.password_hash = command.new_password_hash
        next_revision = command.expected_credential_revision + 1
        self._state.credential_revision = next_revision
        revoked_session_count = 0
        for secret_hash, other_session in self._state.sessions_by_secret_hash.items():
            if other_session.web_session_id == command.current_web_session_id:
                continue
            self._state.sessions_by_secret_hash[secret_hash] = replace(
                other_session,
                state=WebSessionState.REVOKED,
                revoked_at=command.database_now,
                revocation_reason="password_changed",
                credential_revision=next_revision,
            )
            revoked_session_count += 1
        self._state.sessions_by_secret_hash[command.new_session_secret_hash] = replace(
            session,
            session_secret_hash=command.new_session_secret_hash,
            csrf_secret_hash=command.new_csrf_secret_hash,
            credential_revision=next_revision,
        )
        return ChangedPassword(
            current_web_session_id=command.current_web_session_id,
            credential_revision=next_revision,
            revoked_session_count=revoked_session_count,
            database_now=command.database_now,
        )


class OfflineSessionStore:
    """In-memory session transaction double behind the session services."""

    def __init__(self, state: OfflineAuthenticationState) -> None:
        self._state = state

    def _session_by_id(self, web_session_id: UUID) -> StoredWebSession | None:
        return next(
            (
                candidate
                for candidate in self._state.sessions_by_secret_hash.values()
                if candidate.web_session_id == web_session_id
            ),
            None,
        )

    async def resolve_session(
        self, *, session_secret_hash: str, database_now: datetime
    ) -> ResolvedWebSession:
        session = self._state.sessions_by_secret_hash.get(session_secret_hash)
        if session is None:
            raise AuthenticationError(ErrorCode.AUTHENTICATION_REQUIRED)
        decision = evaluate_session_authentication(
            session,
            current_credential_revision=self._state.credential_revision,
            database_now=database_now,
            policy=_OFFLINE_SESSION_POLICY,
        )
        if not decision.is_authenticated:
            raise AuthenticationError(ErrorCode.AUTHENTICATION_REQUIRED)
        if decision.next_idle_expires_at is not None:
            session = replace(
                session,
                last_seen_at=database_now,
                idle_expires_at=decision.next_idle_expires_at,
            )
            self._state.sessions_by_secret_hash[session_secret_hash] = session
        return ResolvedWebSession(
            session=session,
            current_credential_revision=self._state.credential_revision,
            password_hash=self._state.password_hash,
            database_now=database_now,
        )

    async def resolve_challenge_eligible_session(
        self, *, session_secret_hash: str, database_now: datetime
    ) -> ResolvedWebSession:
        session = self._state.sessions_by_secret_hash.get(session_secret_hash)
        if session is None or not is_challenge_eligible_session(
            session, database_now=database_now
        ):
            raise AuthenticationError(ErrorCode.AUTHENTICATION_REQUIRED)
        return ResolvedWebSession(
            session=session,
            current_credential_revision=self._state.credential_revision,
            password_hash=self._state.password_hash,
            database_now=database_now,
        )

    async def rotate_session_secrets(
        self, command: RotateWebSessionSecretsCommand
    ) -> RotatedWebSessionSecrets:
        session = self._session_by_id(command.web_session_id)
        if (
            session is None
            or session.session_secret_hash != command.prior_session_secret_hash
        ):
            raise AuthenticationError(ErrorCode.AUTHENTICATION_REQUIRED)
        rotated = replace(
            session,
            session_secret_hash=command.new_session_secret_hash,
            csrf_secret_hash=command.new_csrf_secret_hash,
            reauthenticated_at=command.database_now,
        )
        del self._state.sessions_by_secret_hash[command.prior_session_secret_hash]
        self._state.sessions_by_secret_hash[command.new_session_secret_hash] = rotated
        return RotatedWebSessionSecrets(
            web_session_id=command.web_session_id,
            state=rotated.state,
            database_now=command.database_now,
        )

    async def revoke_session(self, command: RevokeWebSessionCommand) -> RevokedWebSession:
        session = self._state.sessions_by_secret_hash.get(command.session_secret_hash)
        if session is None or session.state is WebSessionState.REVOKED:
            raise AuthenticationError(ErrorCode.AUTHENTICATION_REQUIRED)
        self._state.sessions_by_secret_hash[command.session_secret_hash] = replace(
            session,
            state=WebSessionState.REVOKED,
            revoked_at=command.database_now,
            revocation_reason=command.revocation_reason,
            authenticated_at=None,
            reauthenticated_at=None,
        )
        return RevokedWebSession(
            web_session_id=session.web_session_id, revoked_at=command.database_now
        )


def compose_offline_web_authentication(
    *,
    totp_active: bool = False,
    clock: OfflineAuthenticationClock | None = None,
    state: OfflineAuthenticationState | None = None,
) -> WebAuthenticationRuntime:
    """Build the deterministic offline runtime for export and tests.

    An injected ``state`` replaces the default construction — ``totp_active``
    then only seeds the default — so tests can pre-seed or restamp session
    rows (for example a ``recovery_limited`` binding) while every secret and
    the fixed clock stay deterministic.
    """
    offline_state = (
        state if state is not None else OfflineAuthenticationState(totp_active=totp_active)
    )
    hasher = OfflinePasswordHasher()
    crypto = OfflineAuthenticationCrypto()
    offline_clock = clock if clock is not None else OfflineAuthenticationClock()
    credentials: CredentialTransactionPort = OfflineCredentialStore(offline_state)
    sessions: WebSessionTransactionPort = OfflineSessionStore(offline_state)
    session_service = SessionService(
        sessions=sessions,
        hasher=hasher,
        crypto=crypto,
        master_key=_OFFLINE_MASTER_KEY,
        clock=offline_clock,
        session_policy=_OFFLINE_SESSION_POLICY,
    )
    return WebAuthenticationRuntime(
        allowed_origin=OFFLINE_WEB_ALLOWED_ORIGIN,
        cookie_contract=build_session_cookie_contract(
            OFFLINE_WEB_ALLOWED_ORIGIN, RuntimeEnvironment.TEST
        ),
        login_service=LoginService(
            credentials=credentials,
            hasher=hasher,
            crypto=crypto,
            master_key=_OFFLINE_MASTER_KEY,
            clock=offline_clock,
            throttle_policy=_OFFLINE_THROTTLE_POLICY,
            session_policy=_OFFLINE_SESSION_POLICY,
        ),
        session_service=session_service,
        password_change_service=PasswordChangeService(
            session_service=session_service,
            credentials=credentials,
            hasher=hasher,
            blocklist=PasswordBlocklist(digests=()),
        ),
        verify_csrf_token=_build_csrf_verifier(crypto, _OFFLINE_MASTER_KEY),
    )
