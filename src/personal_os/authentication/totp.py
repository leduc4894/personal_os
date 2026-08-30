"""Pure TOTP computation, enrollment/recovery transitions and the services.

This module owns the closed TOTP and recovery choreography of design sections
10.1-10.3 as pure functions, typed commands and results, the AEAD codec seam
for TOTP-secret ciphertext (spec 20.1) and :class:`TotpService`, which depends
only on the authentication ports: the TOTP transaction port implemented by the
PostgreSQL adapter, the web-session transaction port, the password-hasher and
crypto ports, the transaction clock and the secret codec. Every persisted
timestamp and step decision uses the single ``database_now`` of one service
invocation; Argon2id verification, secret generation, sealing and recovery-code
hashing always happen outside the database transactions — only the spec 20.1
re-encryption of a previous-key secret happens inside the store transaction
while it holds the credential row lock.

The interoperable contract is RFC 6238 HMAC-SHA-1, six digits and a 30-second
period; the verifier accepts only the previous, current or next time step and
a step at or behind the stored replay marker is a replay. Recovery codes are
ten one-use values of twelve Base32 characters hashed under the
``auth/recovery/v1`` subkey. The module imports no infrastructure SDK,
composition root or crypto implementation package.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets as secrets_module
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final, Protocol, runtime_checkable
from urllib.parse import quote
from uuid import UUID

from personal_os.authentication.contracts import WebSessionState
from personal_os.authentication.crypto import RECOVERY_CODE_HASH_LABEL
from personal_os.authentication.errors import AuthenticationError
from personal_os.authentication.ports import (
    AuthenticationCryptoPort,
    PasswordHasherPort,
)
from personal_os.authentication.sessions import (
    AuthenticationClockPort,
    ResolvedWebSession,
    RotatedCurrentSession,
    RotateWebSessionSecretsCommand,
    SessionRotationCause,
    SessionWindowPolicy,
    StoredWebSession,
    ThrottleBucketKind,
    ThrottleBucketState,
    ThrottleFailureTransition,
    ThrottleWindowPolicy,
    WebSessionTransactionPort,
    clamp_idle_expiry,
    derive_csrf_hmac_key,
    derive_throttle_hmac_key,
    generate_session_secret_material,
    is_recently_authenticated,
    session_secret_hash_of,
    throttle_bucket_hash,
)
from personal_os.diagnostics.context import DiagnosticContext
from personal_os.error_contracts.codes import ErrorCode

#: The one pinned algorithm (spec 10.1); the schema check pins the same value.
TOTP_ALGORITHM: Final[str] = "SHA1"

#: The interoperable six-digit, thirty-second contract (spec 10.1).
TOTP_DIGITS: Final[int] = 6
TOTP_PERIOD_SECONDS: Final[int] = 30

#: The verifier accepts the previous, current or next time step only (spec 10.1).
TOTP_ACCEPTED_STEP_WINDOW: Final[int] = 1

#: Fresh secrets carry 160 bits of entropy (spec 10.1).
TOTP_SECRET_ENTROPY_BYTES: Final[int] = 20

#: A pending enrollment expires after ten minutes (spec 10.1).
TOTP_ENROLLMENT_EXPIRY: Final[timedelta] = timedelta(minutes=10)

#: The fixed product issuer of the provisioning label (spec 10.1).
TOTP_PROVISIONING_ISSUER: Final[str] = "Personal Knowledge OS"

#: Recovery codes: ten one-use values of twelve Base32 characters (spec 10.3).
RECOVERY_CODE_COUNT: Final[int] = 10
RECOVERY_CODE_LENGTH_CHARACTERS: Final[int] = 12
RECOVERY_CODE_GROUP_SIZE: Final[int] = 4
RECOVERY_CODE_ALPHABET: Final[str] = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"

#: The session authentication methods a TOTP flow can write (schema vocabulary).
TOTP_AUTHENTICATION_METHOD: Final[str] = "password_totp"
RECOVERY_AUTHENTICATION_METHOD: Final[str] = "recovery_code"

#: Revocation reason of the other sessions an ordinary disable closes (10.3).
TOTP_DISABLED_REVOCATION_REASON: Final[str] = "totp_disabled"

#: Session states that may drive their own TOTP replacement: an active session
#: enrolls the optional credential for the first time, a recovery-limited
#: binding replaces the lost one (spec 10.3).
REPLACEMENT_ELIGIBLE_SESSION_STATES: Final[frozenset[WebSessionState]] = frozenset(
    {WebSessionState.ACTIVE, WebSessionState.RECOVERY_LIMITED}
)


class TotpEnrollmentAction(StrEnum):
    """The strict discriminated enrollment action vocabulary (spec 10.1)."""

    START = "start"
    DISMISS_INITIAL_OFFER = "dismiss_initial_offer"


# --- pure computation --------------------------------------------------------------------


def totp_code(
    *,
    secret: bytes,
    unix_time_seconds: int,
    digits: int = TOTP_DIGITS,
    period_seconds: int = TOTP_PERIOD_SECONDS,
) -> str:
    """Compute one RFC 6238 HMAC-SHA-1 code for a secret and moment."""
    step = unix_time_seconds // period_seconds
    counter = step.to_bytes(8, "big")
    digest = hmac.new(secret, counter, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    binary = (
        (digest[offset] & 0x7F) << 24
        | digest[offset + 1] << 16
        | digest[offset + 2] << 8
        | digest[offset + 3]
    )
    return f"{binary % (10**digits):0{digits}d}"


def time_step_of(*, unix_time_seconds: int, period_seconds: int = TOTP_PERIOD_SECONDS) -> int:
    """Map one unix moment onto its time-step index."""
    return unix_time_seconds // period_seconds


def resolve_totp_step(
    *,
    submitted_code: str,
    secret: bytes,
    last_accepted_time_step: int | None,
    unix_time_seconds: int,
    digits: int = TOTP_DIGITS,
    period_seconds: int = TOTP_PERIOD_SECONDS,
) -> int | None:
    """Return the accepted time step of one submitted code, or ``None``.

    Only the previous, current or next step of ``unix_time_seconds`` can match,
    and only a step strictly newer than ``last_accepted_time_step`` is
    accepted — the same step twice is a replay and drift beyond the ±1 window
    fails safely (spec 10.1, 10.2). The smallest accepted matching step wins,
    so the replay marker advances exactly as far as the submission proves.
    """
    current_step = time_step_of(unix_time_seconds=unix_time_seconds, period_seconds=period_seconds)
    for offset in range(-TOTP_ACCEPTED_STEP_WINDOW, TOTP_ACCEPTED_STEP_WINDOW + 1):
        candidate_step = current_step + offset
        if last_accepted_time_step is not None and candidate_step <= last_accepted_time_step:
            continue
        candidate_code = totp_code(
            secret=secret,
            unix_time_seconds=candidate_step * period_seconds,
            digits=digits,
            period_seconds=period_seconds,
        )
        if hmac.compare_digest(candidate_code, submitted_code):
            return candidate_step
    return None


def generate_totp_secret() -> bytes:
    """Generate one fresh 160-bit TOTP secret (spec 10.1)."""
    return secrets_module.token_bytes(TOTP_SECRET_ENTROPY_BYTES)


def totp_provisioning_uri(
    *,
    secret: bytes,
    username: str,
    issuer: str = TOTP_PROVISIONING_ISSUER,
    digits: int = TOTP_DIGITS,
    period_seconds: int = TOTP_PERIOD_SECONDS,
) -> str:
    """Build the one-time provisioning URI rendered into a local QR (10.1).

    The label carries exactly the fixed issuer and the canonical username —
    no path, workspace identifier, secret or arbitrary user text — and the
    query carries the Base32 secret with the pinned parameters.
    """
    secret_base32 = base64.b32encode(secret).decode("ascii")
    label = f"{quote(issuer, safe='')}:{quote(username, safe='')}"
    return (
        f"otpauth://totp/{label}"
        f"?secret={secret_base32}"
        f"&issuer={quote(issuer, safe='')}"
        f"&algorithm={TOTP_ALGORITHM}"
        f"&digits={digits}"
        f"&period={period_seconds}"
    )


# --- recovery values -----------------------------------------------------------------------


def generate_recovery_codes(*, count: int = RECOVERY_CODE_COUNT) -> tuple[str, ...]:
    """Generate one fresh set of grouped one-use recovery codes (spec 10.3)."""
    return tuple(
        "-".join(
            "".join(
                secrets_module.choice(RECOVERY_CODE_ALPHABET)
                for _ in range(RECOVERY_CODE_GROUP_SIZE)
            )
            for _ in range(RECOVERY_CODE_LENGTH_CHARACTERS // RECOVERY_CODE_GROUP_SIZE)
        )
        for _ in range(count)
    )


def normalize_recovery_code(value: str) -> str:
    """Normalize one pasted recovery-code spelling to its stored form.

    Grouping hyphens and spaces plus letter case are presentation; the stored
    hash covers the twelve uppercase Base32 characters only. Foreign grammar
    fails closed as the generic authentication failure without retaining the
    rejected value.
    """
    normalized = value.replace("-", "").replace(" ", "").upper()
    if len(normalized) != RECOVERY_CODE_LENGTH_CHARACTERS or any(
        character not in RECOVERY_CODE_ALPHABET for character in normalized
    ):
        raise AuthenticationError(ErrorCode.AUTHENTICATION_FAILED)
    return normalized


def derive_recovery_code_hmac_key(crypto: AuthenticationCryptoPort, master_key: bytes) -> bytes:
    """Derive the ``auth/recovery/v1`` HMAC subkey (spec 10.3, 20.1)."""
    return crypto.derive_subkey(master_key=master_key, label=RECOVERY_CODE_HASH_LABEL)


def recovery_code_hash(*, hmac_key: bytes, normalized_code: str) -> str:
    """HMAC one normalized recovery code into the stored 64-hex digest."""
    return hmac.new(hmac_key, normalized_code.encode("ascii"), hashlib.sha256).hexdigest()


# --- the AEAD codec seam ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SealedTotpSecret:
    """One TOTP secret's AEAD material as the store rows carry it.

    ``nonce`` and ``ciphertext`` are Base64 strings matching the schema
    column bounds; both are secret-bearing and never render.
    """

    key_id: str
    nonce: str = field(repr=False)
    ciphertext: str = field(repr=False)


@runtime_checkable
class TotpSecretCodecPort(Protocol):
    """AEAD seam over the versioned keyring for TOTP-secret ciphertext.

    ``seal_secret`` always uses the current key; ``open_secret`` resolves the
    key ID the row references so a previous-key secret stays decryptable until
    re-encryption (spec 20.1).
    """

    def current_key_id(self) -> str: ...

    def seal_secret(self, *, plaintext: bytes) -> SealedTotpSecret: ...

    def open_secret(self, *, sealed: SealedTotpSecret) -> bytes: ...


# --- transaction commands and results ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InsertPendingEnrollmentCommand:
    """One enrollment start's transactional write (spec 10.1).

    The secret is sealed by the caller outside the transaction;
    ``allow_active_credential`` is true only for the replacement path a
    recovery-limited session drives.
    """

    user_id: UUID
    allow_active_credential: bool
    sealed_secret: SealedTotpSecret = field(repr=False)
    enrollment_expires_at: datetime
    database_now: datetime
    diagnostic_context: DiagnosticContext


@dataclass(frozen=True, slots=True)
class InsertedPendingEnrollment:
    """The committed identity of one pending enrollment row."""

    totp_credential_id: UUID
    enrollment_expires_at: datetime
    username: str
    database_now: datetime


@dataclass(frozen=True, slots=True)
class VerifyTotpCommand:
    """One replay-locked TOTP verification against the active credential."""

    user_id: UUID
    submitted_code: str
    unix_time_seconds: int
    database_now: datetime
    reset_bucket_hash: str | None = field(repr=False)
    diagnostic_context: DiagnosticContext


@dataclass(frozen=True, slots=True)
class TotpVerified:
    """The committed outcome of one accepted TOTP verification."""

    totp_credential_id: UUID
    accepted_time_step: int
    was_reencrypted: bool
    database_now: datetime


@dataclass(frozen=True, slots=True)
class ActivateEnrollmentCommand:
    """One enrollment verification's transactional write (spec 10.1).

    The recovery-code hashes are computed by the caller outside the
    transaction; ``complete_recovery_session`` drives the spec 10.3
    recovery-completed session rotation inside the same commit.
    """

    user_id: UUID
    enrollment_id: UUID
    submitted_code: str
    unix_time_seconds: int
    recovery_code_hashes: tuple[str, ...] = field(repr=False)
    complete_recovery_session: bool
    current_web_session_id: UUID
    prior_session_secret_hash: str = field(repr=False)
    new_session_secret_hash: str = field(repr=False)
    new_csrf_secret_hash: str = field(repr=False)
    database_now: datetime
    diagnostic_context: DiagnosticContext


@dataclass(frozen=True, slots=True)
class ActivatedTotpEnrollment:
    """The committed outcome of one enrollment verification."""

    totp_credential_id: UUID
    recovery_code_revision: int
    replaced_previous_credential: bool
    database_now: datetime


@dataclass(frozen=True, slots=True)
class RecoverSessionCommand:
    """One recovery entry's transactional write (spec 10.3).

    Exactly one unused code hash is consumed under the credential and code row
    locks while the presented binding rotates into ``recovery_limited``.
    """

    user_id: UUID
    current_web_session_id: UUID
    prior_session_secret_hash: str = field(repr=False)
    new_session_secret_hash: str = field(repr=False)
    new_csrf_secret_hash: str = field(repr=False)
    recovery_code_hash: str = field(repr=False)
    database_now: datetime
    diagnostic_context: DiagnosticContext


@dataclass(frozen=True, slots=True)
class RecoveredSession:
    """The committed outcome of one accepted recovery entry."""

    web_session_id: UUID
    state: WebSessionState
    database_now: datetime


@dataclass(frozen=True, slots=True)
class RegenerateRecoveryCodesCommand:
    """One regeneration's transactional write (spec 10.3)."""

    user_id: UUID
    workspace_id: UUID
    recovery_code_hashes: tuple[str, ...] = field(repr=False)
    database_now: datetime
    diagnostic_context: DiagnosticContext


@dataclass(frozen=True, slots=True)
class RegeneratedRecoveryCodes:
    """The committed outcome of one regeneration."""

    revision: int
    invalidated_code_count: int
    database_now: datetime


@dataclass(frozen=True, slots=True)
class DisableTotpCommand:
    """One ordinary disable's transactional writes (spec 10.3)."""

    user_id: UUID
    workspace_id: UUID
    current_web_session_id: UUID
    prior_session_secret_hash: str = field(repr=False)
    new_session_secret_hash: str = field(repr=False)
    new_csrf_secret_hash: str = field(repr=False)
    database_now: datetime
    diagnostic_context: DiagnosticContext


@dataclass(frozen=True, slots=True)
class DisabledTotp:
    """The committed outcome of one ordinary disable."""

    credential_revision: int
    revoked_session_count: int
    database_now: datetime


@runtime_checkable
class TotpTransactionPort(Protocol):
    """The TOTP transaction surface the service orchestrates."""

    async def resolve_verification_bucket(
        self, *, bucket_kind: ThrottleBucketKind, bucket_hash: str
    ) -> ThrottleBucketState | None: ...

    async def record_verification_failure(
        self, *, bucket_kind: ThrottleBucketKind, bucket_hash: str, database_now: datetime
    ) -> ThrottleFailureTransition: ...

    async def has_active_totp(self, *, user_id: UUID) -> bool: ...

    async def record_prompt_dismissal(
        self, *, user_id: UUID, workspace_id: UUID, database_now: datetime
    ) -> datetime: ...

    async def insert_pending_enrollment(
        self, command: InsertPendingEnrollmentCommand
    ) -> InsertedPendingEnrollment: ...

    async def verify_totp(self, command: VerifyTotpCommand) -> TotpVerified: ...

    async def activate_enrollment(
        self, command: ActivateEnrollmentCommand
    ) -> ActivatedTotpEnrollment: ...

    async def recover_session(self, command: RecoverSessionCommand) -> RecoveredSession: ...

    async def regenerate_recovery_codes(
        self, command: RegenerateRecoveryCodesCommand
    ) -> RegeneratedRecoveryCodes: ...

    async def disable_totp(self, command: DisableTotpCommand) -> DisabledTotp: ...


# --- service payloads ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StartedTotpEnrollment:
    """One enrollment offer: the provisioning material shown exactly once."""

    enrollment_id: UUID
    username: str
    provisioning_uri: str = field(repr=False)
    secret_base32: str = field(repr=False)
    enrollment_expires_at: datetime
    database_now: datetime


@dataclass(frozen=True, slots=True)
class IssuedRecoveryCodes:
    """One recovery-code set displayed exactly once (spec 10.3)."""

    codes: tuple[str, ...] = field(repr=False)
    revision: int


@dataclass(frozen=True, slots=True)
class VerifiedSessionTOTP:
    """The activated session of one completed login challenge."""

    rotated_session: RotatedCurrentSession
    idle_expires_at: datetime
    absolute_expires_at: datetime
    database_now: datetime


@dataclass(frozen=True, slots=True)
class VerifiedTotpEnrollment:
    """The committed outcome of one enrollment verification."""

    issued_codes: IssuedRecoveryCodes
    rotated_session: RotatedCurrentSession | None
    idle_expires_at: datetime
    absolute_expires_at: datetime
    database_now: datetime


@dataclass(frozen=True, slots=True)
class EnteredRecoveryLimitedSession:
    """The recovery-limited binding one accepted recovery produced."""

    rotated_session: RotatedCurrentSession
    idle_expires_at: datetime
    absolute_expires_at: datetime
    database_now: datetime


@dataclass(frozen=True, slots=True)
class DisabledTotpCredential:
    """The password-only binding one ordinary disable produced."""

    rotated_session: RotatedCurrentSession
    credential_revision: int
    revoked_session_count: int
    idle_expires_at: datetime
    absolute_expires_at: datetime
    database_now: datetime


@dataclass(frozen=True, slots=True)
class TotpSessionVerificationOutcome:
    """One login-challenge verification's public result."""

    public_error: ErrorCode | None
    locked_until: datetime | None
    verified: VerifiedSessionTOTP | None
    limited_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class TotpEnrollmentActionOutcome:
    """One enrollment-action request's public result."""

    public_error: ErrorCode | None
    started: StartedTotpEnrollment | None
    dismissed_at: datetime | None


@dataclass(frozen=True, slots=True)
class TotpEnrollmentVerificationOutcome:
    """One enrollment verification's public result."""

    public_error: ErrorCode | None
    locked_until: datetime | None
    verified: VerifiedTotpEnrollment | None
    limited_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class TotpRecoveryOutcome:
    """One recovery attempt's public result."""

    public_error: ErrorCode | None
    locked_until: datetime | None
    entered: EnteredRecoveryLimitedSession | None
    limited_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class TotpRegenerationOutcome:
    """One regeneration request's public result."""

    public_error: ErrorCode | None
    locked_until: datetime | None
    issued_codes: IssuedRecoveryCodes | None
    limited_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class TotpProofFailure:
    """The public rejection of one failed password-or-TOTP proof."""

    error_code: ErrorCode
    locked_until: datetime | None
    limited_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class TotpDisableOutcome:
    """One ordinary disable request's public result."""

    public_error: ErrorCode | None
    locked_until: datetime | None
    disabled: DisabledTotpCredential | None
    limited_at: datetime | None = None


# --- the service -------------------------------------------------------------------------------


class TotpService:
    """The TOTP enrollment, verification and recovery choreography (spec 10).

    One invocation of any method takes exactly one ``database_now`` read,
    resolves the presented binding once, verifies passwords and generates or
    hashes secret material outside every transaction, and commits through the
    transaction port's single-purpose transactions. Login-challenge
    verification, recovery entry and enrollment verification carry their own
    closed throttles (spec 8.3); regeneration and disable demand the password
    plus a current TOTP proof.
    """

    def __init__(
        self,
        *,
        transactions: TotpTransactionPort,
        sessions: WebSessionTransactionPort,
        hasher: PasswordHasherPort,
        crypto: AuthenticationCryptoPort,
        master_key: bytes,
        clock: AuthenticationClockPort,
        secret_codec: TotpSecretCodecPort,
        throttle_policy: ThrottleWindowPolicy | None = None,
        session_policy: SessionWindowPolicy | None = None,
    ) -> None:
        self._transactions = transactions
        self._sessions = sessions
        self._hasher = hasher
        self._clock = clock
        self.secret_codec = secret_codec
        self.throttle_policy = (
            throttle_policy if throttle_policy is not None else ThrottleWindowPolicy()
        )
        self.session_policy = (
            session_policy if session_policy is not None else SessionWindowPolicy()
        )
        self._recovery_hmac_key = derive_recovery_code_hmac_key(crypto, master_key)
        self._throttle_hmac_key = derive_throttle_hmac_key(crypto, master_key)
        self._csrf_hmac_key = derive_csrf_hmac_key(crypto, master_key)

    async def database_now(self) -> datetime:
        """One transaction timestamp shared with co-orchestrating services."""
        return await self._clock.database_now()

    # -- login-challenge verification (spec 10.1/10.2) ----------------------------------

    async def verify_session_totp(
        self, *, session_secret: str, code: str, diagnostic_context: DiagnosticContext
    ) -> TotpSessionVerificationOutcome:
        """Verify one login challenge and activate the pending binding."""
        database_now = await self._clock.database_now()
        resolved = await self._resolve_challenge(session_secret, database_now=database_now)
        session = resolved.session
        if session.state is not WebSessionState.PENDING_TOTP:
            raise AuthenticationError(ErrorCode.AUTHENTICATION_REQUIRED)
        bucket_hash = self._bucket_hash(ThrottleBucketKind.TOTP_VERIFICATION, str(session.user_id))
        locked_until = self._active_lock(
            await self._transactions.resolve_verification_bucket(
                bucket_kind=ThrottleBucketKind.TOTP_VERIFICATION, bucket_hash=bucket_hash
            ),
            database_now=database_now,
        )
        if locked_until is not None:
            return TotpSessionVerificationOutcome(
                public_error=ErrorCode.AUTHENTICATION_RATE_LIMITED,
                locked_until=locked_until,
                verified=None,
                limited_at=database_now,
            )
        unix_time_seconds = int(database_now.timestamp())
        try:
            await self._transactions.verify_totp(
                VerifyTotpCommand(
                    user_id=session.user_id,
                    submitted_code=code,
                    unix_time_seconds=unix_time_seconds,
                    database_now=database_now,
                    reset_bucket_hash=bucket_hash,
                    diagnostic_context=diagnostic_context,
                )
            )
        except AuthenticationError as error:
            if error.error_code is not ErrorCode.AUTHENTICATION_FAILED:
                raise
            return TotpSessionVerificationOutcome(
                public_error=ErrorCode.AUTHENTICATION_FAILED,
                locked_until=await self._record_failure(
                    ThrottleBucketKind.TOTP_VERIFICATION, bucket_hash, database_now
                ),
                verified=None,
            )
        rotated = self._prepare_rotation(session.web_session_id, database_now)
        await self._sessions.rotate_session_secrets(
            RotateWebSessionSecretsCommand(
                web_session_id=session.web_session_id,
                prior_session_secret_hash=session.session_secret_hash,
                new_session_secret_hash=rotated.session_secret_hash,
                new_csrf_secret_hash=rotated.csrf_secret_hash,
                cause=SessionRotationCause.SESSION_ACTIVATION,
                target_authentication_method=TOTP_AUTHENTICATION_METHOD,
                database_now=database_now,
            )
        )
        return TotpSessionVerificationOutcome(
            public_error=None,
            locked_until=None,
            verified=VerifiedSessionTOTP(
                rotated_session=rotated,
                idle_expires_at=clamp_idle_expiry(
                    database_now + self.session_policy.idle_ttl,
                    session.absolute_expires_at,
                ),
                absolute_expires_at=session.absolute_expires_at,
                database_now=database_now,
            ),
        )

    # -- enrollment actions (spec 10.1) ---------------------------------------------------

    async def submit_enrollment_action(
        self,
        *,
        session_secret: str,
        action: TotpEnrollmentAction,
        diagnostic_context: DiagnosticContext,
    ) -> TotpEnrollmentActionOutcome:
        """Run one strict enrollment action: ``start`` or the dismissal."""
        database_now = await self._clock.database_now()
        resolved = await self._resolve_challenge(session_secret, database_now=database_now)
        session = resolved.session
        if action is TotpEnrollmentAction.DISMISS_INITIAL_OFFER:
            self._require_current_active_session(session, resolved.current_credential_revision)
            dismissed_at = await self._transactions.record_prompt_dismissal(
                user_id=session.user_id,
                workspace_id=session.workspace_id,
                database_now=database_now,
            )
            return TotpEnrollmentActionOutcome(
                public_error=None, started=None, dismissed_at=dismissed_at
            )
        if session.state not in REPLACEMENT_ELIGIBLE_SESSION_STATES:
            raise AuthenticationError(ErrorCode.AUTHENTICATION_REQUIRED)
        if not is_recently_authenticated(
            session, database_now=database_now, policy=self.session_policy
        ):
            return TotpEnrollmentActionOutcome(
                public_error=ErrorCode.RECENT_AUTHENTICATION_REQUIRED,
                started=None,
                dismissed_at=None,
            )
        secret = generate_totp_secret()
        sealed = self.secret_codec.seal_secret(plaintext=secret)
        inserted = await self._transactions.insert_pending_enrollment(
            InsertPendingEnrollmentCommand(
                user_id=session.user_id,
                allow_active_credential=(session.state is WebSessionState.RECOVERY_LIMITED),
                sealed_secret=sealed,
                enrollment_expires_at=database_now + TOTP_ENROLLMENT_EXPIRY,
                database_now=database_now,
                diagnostic_context=diagnostic_context,
            )
        )
        return TotpEnrollmentActionOutcome(
            public_error=None,
            started=StartedTotpEnrollment(
                enrollment_id=inserted.totp_credential_id,
                username=inserted.username,
                provisioning_uri=totp_provisioning_uri(secret=secret, username=inserted.username),
                secret_base32=base64.b32encode(secret).decode("ascii"),
                enrollment_expires_at=inserted.enrollment_expires_at,
                database_now=database_now,
            ),
            dismissed_at=None,
        )

    async def verify_enrollment(
        self,
        *,
        session_secret: str,
        enrollment_id: UUID,
        code: str,
        diagnostic_context: DiagnosticContext,
    ) -> TotpEnrollmentVerificationOutcome:
        """Verify one submitted enrollment code and activate the credential."""
        database_now = await self._clock.database_now()
        resolved = await self._resolve_challenge(session_secret, database_now=database_now)
        session = resolved.session
        if session.state not in REPLACEMENT_ELIGIBLE_SESSION_STATES:
            raise AuthenticationError(ErrorCode.AUTHENTICATION_REQUIRED)
        bucket_hash = self._bucket_hash(ThrottleBucketKind.TOTP_VERIFICATION, str(session.user_id))
        locked_until = self._active_lock(
            await self._transactions.resolve_verification_bucket(
                bucket_kind=ThrottleBucketKind.TOTP_VERIFICATION, bucket_hash=bucket_hash
            ),
            database_now=database_now,
        )
        if locked_until is not None:
            return TotpEnrollmentVerificationOutcome(
                public_error=ErrorCode.AUTHENTICATION_RATE_LIMITED,
                locked_until=locked_until,
                verified=None,
                limited_at=database_now,
            )
        codes = generate_recovery_codes()
        recovery_hashes = tuple(
            recovery_code_hash(
                hmac_key=self._recovery_hmac_key, normalized_code=normalize_recovery_code(value)
            )
            for value in codes
        )
        completes_recovery = session.state is WebSessionState.RECOVERY_LIMITED
        rotated = (
            self._prepare_rotation(session.web_session_id, database_now)
            if completes_recovery
            else None
        )
        try:
            activated = await self._transactions.activate_enrollment(
                ActivateEnrollmentCommand(
                    user_id=session.user_id,
                    enrollment_id=enrollment_id,
                    submitted_code=code,
                    unix_time_seconds=int(database_now.timestamp()),
                    recovery_code_hashes=recovery_hashes,
                    complete_recovery_session=completes_recovery,
                    current_web_session_id=session.web_session_id,
                    prior_session_secret_hash=session.session_secret_hash,
                    new_session_secret_hash=(
                        rotated.session_secret_hash if rotated is not None else ""
                    ),
                    new_csrf_secret_hash=(rotated.csrf_secret_hash if rotated is not None else ""),
                    database_now=database_now,
                    diagnostic_context=diagnostic_context,
                )
            )
        except AuthenticationError as error:
            if error.error_code is ErrorCode.TOTP_ENROLLMENT_STATE_INVALID:
                return TotpEnrollmentVerificationOutcome(
                    public_error=error.error_code, locked_until=None, verified=None
                )
            if error.error_code is not ErrorCode.AUTHENTICATION_FAILED:
                raise
            return TotpEnrollmentVerificationOutcome(
                public_error=ErrorCode.AUTHENTICATION_FAILED,
                locked_until=await self._record_failure(
                    ThrottleBucketKind.TOTP_VERIFICATION, bucket_hash, database_now
                ),
                verified=None,
            )
        return TotpEnrollmentVerificationOutcome(
            public_error=None,
            locked_until=None,
            verified=VerifiedTotpEnrollment(
                issued_codes=IssuedRecoveryCodes(
                    codes=codes, revision=activated.recovery_code_revision
                ),
                rotated_session=rotated,
                idle_expires_at=clamp_idle_expiry(
                    database_now + self.session_policy.idle_ttl,
                    session.absolute_expires_at,
                ),
                absolute_expires_at=session.absolute_expires_at,
                database_now=database_now,
            ),
        )

    # -- recovery (spec 10.3) ----------------------------------------------------------------

    async def recover_with_code(
        self,
        *,
        session_secret: str,
        password: str,
        recovery_code: str,
        diagnostic_context: DiagnosticContext,
    ) -> TotpRecoveryOutcome:
        """Consume one recovery code and enter the recovery-limited state."""
        database_now = await self._clock.database_now()
        resolved = await self._resolve_challenge(session_secret, database_now=database_now)
        session = resolved.session
        if session.state not in (
            WebSessionState.PENDING_TOTP,
            WebSessionState.ACTIVE,
        ):
            raise AuthenticationError(ErrorCode.AUTHENTICATION_REQUIRED)
        bucket_hash = self._bucket_hash(
            ThrottleBucketKind.RECOVERY_VERIFICATION, str(session.user_id)
        )
        locked_until = self._active_lock(
            await self._transactions.resolve_verification_bucket(
                bucket_kind=ThrottleBucketKind.RECOVERY_VERIFICATION, bucket_hash=bucket_hash
            ),
            database_now=database_now,
        )
        if locked_until is not None:
            return TotpRecoveryOutcome(
                public_error=ErrorCode.AUTHENTICATION_RATE_LIMITED,
                locked_until=locked_until,
                entered=None,
                limited_at=database_now,
            )
        is_password_valid = resolved.password_hash is not None and self._hasher.verify_password(
            resolved.password_hash, password
        )
        try:
            code_hash = recovery_code_hash(
                hmac_key=self._recovery_hmac_key,
                normalized_code=normalize_recovery_code(recovery_code),
            )
        except AuthenticationError:
            is_password_valid = False
            code_hash = ""
        if not is_password_valid:
            return TotpRecoveryOutcome(
                public_error=ErrorCode.AUTHENTICATION_FAILED,
                locked_until=await self._record_failure(
                    ThrottleBucketKind.RECOVERY_VERIFICATION, bucket_hash, database_now
                ),
                entered=None,
            )
        rotated = self._prepare_rotation(session.web_session_id, database_now)
        try:
            await self._transactions.recover_session(
                RecoverSessionCommand(
                    user_id=session.user_id,
                    current_web_session_id=session.web_session_id,
                    prior_session_secret_hash=session.session_secret_hash,
                    new_session_secret_hash=rotated.session_secret_hash,
                    new_csrf_secret_hash=rotated.csrf_secret_hash,
                    recovery_code_hash=code_hash,
                    database_now=database_now,
                    diagnostic_context=diagnostic_context,
                )
            )
        except AuthenticationError as error:
            if error.error_code is not ErrorCode.AUTHENTICATION_FAILED:
                raise
            return TotpRecoveryOutcome(
                public_error=ErrorCode.AUTHENTICATION_FAILED,
                locked_until=await self._record_failure(
                    ThrottleBucketKind.RECOVERY_VERIFICATION, bucket_hash, database_now
                ),
                entered=None,
            )
        return TotpRecoveryOutcome(
            public_error=None,
            locked_until=None,
            entered=EnteredRecoveryLimitedSession(
                rotated_session=rotated,
                idle_expires_at=session.idle_expires_at,
                absolute_expires_at=session.absolute_expires_at,
                database_now=database_now,
            ),
        )

    # -- regeneration and disable (spec 10.3) --------------------------------------------------

    async def regenerate_recovery_codes(
        self,
        *,
        session_secret: str,
        password: str,
        code: str,
        diagnostic_context: DiagnosticContext,
    ) -> TotpRegenerationOutcome:
        """Re-verify password plus current TOTP and issue a fresh code set."""
        database_now = await self._clock.database_now()
        resolved = await self._resolve_challenge(session_secret, database_now=database_now)
        session = resolved.session
        self._require_current_active_session(session, resolved.current_credential_revision)
        proof_error = await self._verify_password_and_totp_proof(
            session=session,
            resolved_password_hash=resolved.password_hash,
            password=password,
            totp_code_value=code,
            database_now=database_now,
            diagnostic_context=diagnostic_context,
        )
        if proof_error is not None:
            return TotpRegenerationOutcome(
                public_error=proof_error.error_code,
                locked_until=proof_error.locked_until,
                issued_codes=None,
                limited_at=proof_error.limited_at,
            )
        codes = generate_recovery_codes()
        regenerated = await self._transactions.regenerate_recovery_codes(
            RegenerateRecoveryCodesCommand(
                user_id=session.user_id,
                workspace_id=session.workspace_id,
                recovery_code_hashes=tuple(
                    recovery_code_hash(
                        hmac_key=self._recovery_hmac_key,
                        normalized_code=normalize_recovery_code(value),
                    )
                    for value in codes
                ),
                database_now=database_now,
                diagnostic_context=diagnostic_context,
            )
        )
        return TotpRegenerationOutcome(
            public_error=None,
            locked_until=None,
            issued_codes=IssuedRecoveryCodes(codes=codes, revision=regenerated.revision),
        )

    async def disable_totp(
        self,
        *,
        session_secret: str,
        password: str,
        code: str,
        diagnostic_context: DiagnosticContext,
    ) -> TotpDisableOutcome:
        """Re-verify password plus current TOTP and close every TOTP surface."""
        database_now = await self._clock.database_now()
        resolved = await self._resolve_challenge(session_secret, database_now=database_now)
        session = resolved.session
        self._require_current_active_session(session, resolved.current_credential_revision)
        proof_error = await self._verify_password_and_totp_proof(
            session=session,
            resolved_password_hash=resolved.password_hash,
            password=password,
            totp_code_value=code,
            database_now=database_now,
            diagnostic_context=diagnostic_context,
        )
        if proof_error is not None:
            return TotpDisableOutcome(
                public_error=proof_error.error_code,
                locked_until=proof_error.locked_until,
                disabled=None,
                limited_at=proof_error.limited_at,
            )
        rotated = self._prepare_rotation(session.web_session_id, database_now)
        disabled = await self._transactions.disable_totp(
            DisableTotpCommand(
                user_id=session.user_id,
                workspace_id=session.workspace_id,
                current_web_session_id=session.web_session_id,
                prior_session_secret_hash=session.session_secret_hash,
                new_session_secret_hash=rotated.session_secret_hash,
                new_csrf_secret_hash=rotated.csrf_secret_hash,
                database_now=database_now,
                diagnostic_context=diagnostic_context,
            )
        )
        return TotpDisableOutcome(
            public_error=None,
            locked_until=None,
            disabled=DisabledTotpCredential(
                rotated_session=rotated,
                credential_revision=disabled.credential_revision,
                revoked_session_count=disabled.revoked_session_count,
                idle_expires_at=clamp_idle_expiry(
                    database_now + self.session_policy.idle_ttl,
                    session.absolute_expires_at,
                ),
                absolute_expires_at=session.absolute_expires_at,
                database_now=database_now,
            ),
        )

    # -- the re-authentication TOTP leg (spec 9.4) ---------------------------------------------

    async def has_active_totp(self, *, user_id: UUID) -> bool:
        """Whether the account currently carries an active TOTP credential."""
        return await self._transactions.has_active_totp(user_id=user_id)

    async def verify_reauthentication_totp(
        self,
        *,
        user_id: UUID,
        code: str | None,
        database_now: datetime,
        diagnostic_context: DiagnosticContext,
    ) -> bool:
        """Verify the optional TOTP leg of one recent re-authentication.

        A missing code fails closed; a wrong or replayed code records one
        ``totp_verification`` bucket failure exactly like the login challenge.
        The boolean outcome lets the re-authentication path keep its single
        public ``authentication_failed`` rejection.
        """
        if code is None:
            return False
        bucket_hash = self._bucket_hash(ThrottleBucketKind.TOTP_VERIFICATION, str(user_id))
        bucket = await self._transactions.resolve_verification_bucket(
            bucket_kind=ThrottleBucketKind.TOTP_VERIFICATION, bucket_hash=bucket_hash
        )
        if self._active_lock(bucket, database_now=database_now) is not None:
            return False
        try:
            await self._transactions.verify_totp(
                VerifyTotpCommand(
                    user_id=user_id,
                    submitted_code=code,
                    unix_time_seconds=int(database_now.timestamp()),
                    database_now=database_now,
                    reset_bucket_hash=bucket_hash,
                    diagnostic_context=diagnostic_context,
                )
            )
        except AuthenticationError as error:
            if error.error_code is not ErrorCode.AUTHENTICATION_FAILED:
                raise
            await self._record_failure(
                ThrottleBucketKind.TOTP_VERIFICATION, bucket_hash, database_now
            )
            return False
        return True

    # -- internal helpers --------------------------------------------------------------------

    async def _resolve_challenge(
        self, session_secret: str, *, database_now: datetime
    ) -> ResolvedWebSession:
        return await self._sessions.resolve_challenge_eligible_session(
            session_secret_hash=session_secret_hash_of(session_secret),
            database_now=database_now,
        )

    def _require_current_active_session(
        self, session: StoredWebSession, current_credential_revision: int
    ) -> None:
        """Enforce the active, revision-current binding of the gated actions."""
        if (
            session.state is not WebSessionState.ACTIVE
            or session.credential_revision != current_credential_revision
        ):
            raise AuthenticationError(ErrorCode.AUTHENTICATION_REQUIRED)

    async def _verify_password_and_totp_proof(
        self,
        *,
        session: StoredWebSession,
        resolved_password_hash: str | None,
        password: str,
        totp_code_value: str,
        database_now: datetime,
        diagnostic_context: DiagnosticContext,
    ) -> TotpProofFailure | None:
        """Verify the password outside locks, then the current TOTP proof.

        Returns ``None`` on success or the public error (with the newly set
        lock moment, if any) of the first failing proof.
        """
        if resolved_password_hash is None or not self._hasher.verify_password(
            resolved_password_hash, password
        ):
            return TotpProofFailure(ErrorCode.AUTHENTICATION_FAILED, None)
        bucket_hash = self._bucket_hash(ThrottleBucketKind.TOTP_VERIFICATION, str(session.user_id))
        locked_until = self._active_lock(
            await self._transactions.resolve_verification_bucket(
                bucket_kind=ThrottleBucketKind.TOTP_VERIFICATION, bucket_hash=bucket_hash
            ),
            database_now=database_now,
        )
        if locked_until is not None:
            return TotpProofFailure(
                ErrorCode.AUTHENTICATION_RATE_LIMITED, locked_until, limited_at=database_now
            )
        try:
            await self._transactions.verify_totp(
                VerifyTotpCommand(
                    user_id=session.user_id,
                    submitted_code=totp_code_value,
                    unix_time_seconds=int(database_now.timestamp()),
                    database_now=database_now,
                    reset_bucket_hash=bucket_hash,
                    diagnostic_context=diagnostic_context,
                )
            )
        except AuthenticationError as error:
            if error.error_code is not ErrorCode.AUTHENTICATION_FAILED:
                raise
            return TotpProofFailure(
                ErrorCode.AUTHENTICATION_FAILED,
                await self._record_failure(
                    ThrottleBucketKind.TOTP_VERIFICATION, bucket_hash, database_now
                ),
            )
        return None

    def _prepare_rotation(
        self, web_session_id: UUID, database_now: datetime
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

    def _bucket_hash(self, bucket_kind: ThrottleBucketKind, bucket_material: str) -> str:
        return throttle_bucket_hash(
            hmac_key=self._throttle_hmac_key,
            bucket_kind=bucket_kind,
            bucket_material=bucket_material,
        )

    async def _record_failure(
        self,
        bucket_kind: ThrottleBucketKind,
        bucket_hash: str,
        database_now: datetime,
    ) -> datetime | None:
        """Record one failed verification and return a newly set lock moment."""
        transition = await self._transactions.record_verification_failure(
            bucket_kind=bucket_kind, bucket_hash=bucket_hash, database_now=database_now
        )
        return transition.locked_until if transition.became_locked else None

    @staticmethod
    def _active_lock(
        state: ThrottleBucketState | None, *, database_now: datetime
    ) -> datetime | None:
        if state is None or state.locked_until is None:
            return None
        return state.locked_until if database_now < state.locked_until else None
