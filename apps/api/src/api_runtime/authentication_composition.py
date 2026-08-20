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

import asyncio
import base64
import hashlib
import hmac
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Final, cast
from uuid import UUID, uuid7

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from api_runtime.authentication_crypto import (
    Argon2PasswordHasher,
    AuthenticationKeyring,
    CryptographyAuthenticationCrypto,
)
from api_runtime.authentication_dependencies import (
    ClientAddressResolver,
    SessionCookieContract,
    build_session_cookie_contract,
    create_client_address_resolver,
)
from api_runtime.authentication_settings import AuthenticationSettings
from personal_os.authentication.contracts import (
    FIXED_DEVICE_SCOPE,
    AuthenticatedDeviceContext,
    DeviceAuthorizationGrantState,
    DeviceTokenFamilyState,
    DeviceTokenKind,
    DeviceTokenState,
    TotpCredentialState,
    WebSessionState,
)
from personal_os.authentication.crypto import (
    TOTP_SECRET_AEAD_LABEL,
    assert_crypto_domain_label,
)
from personal_os.authentication.device_authorization import (
    POLL_INTERVAL_SECONDS,
    ApprovedGrant,
    ApproveGrantCommand,
    DeniedGrant,
    DenyGrantCommand,
    DeviceAuthorizationService,
    DeviceAuthorizationTransactionPort,
    InsertedPendingGrant,
    InsertPendingGrantCommand,
    LiveGrantWindow,
    PluginVersionBounds,
    StoredDeviceAuthorizationGrant,
    resolve_terminal_rejection_code,
)
from personal_os.authentication.device_tokens import (
    ADMIN_REVOCATION_REASON,
    DEVICE_REVOKED_AUDIT_ACTION,
    DEVICE_TOKEN_FAMILY_REVOKED_AUDIT_ACTION,
    INITIAL_REFRESH_GENERATION,
    REFRESH_INACTIVITY_LIFETIME,
    SELF_REVOCATION_REASON,
    AccessTokenAuthenticationCommand,
    AdminRevokedDevice,
    AdminRevokeDeviceCommand,
    AuthenticatedAccessToken,
    CommittedRefreshRotation,
    DeviceAdministrationService,
    DeviceTokenService,
    ExchangeGrantCommand,
    ExchangeProvisioning,
    ListedAdminDevice,
    RefreshPresentationKind,
    RefreshRotationCommand,
    RevokeCurrentRefreshCommand,
    RevokedCurrentTokenFamily,
    StoredDeviceToken,
    StoredTokenFamily,
    classify_refresh_presentation,
)
from personal_os.authentication.errors import AuthenticationError
from personal_os.authentication.passwords import (
    PasswordBlocklist,
    load_common_password_blocklist,
)
from personal_os.authentication.ports import AuthenticationCryptoPort
from personal_os.authentication.sessions import (
    PASSWORD_AUTHENTICATION_METHOD,
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
    SessionRotationCause,
    SessionService,
    SessionWindowPolicy,
    StoredWebSession,
    ThrottleBucketKind,
    ThrottleBucketState,
    ThrottleFailureTransition,
    ThrottleWindowPolicy,
    WebSessionTransactionPort,
    clamp_idle_expiry,
    derive_csrf_hmac_key,
    evaluate_session_authentication,
    is_challenge_eligible_session,
    next_login_failure_transition,
)
from personal_os.authentication.totp import (
    RECOVERY_AUTHENTICATION_METHOD,
    TOTP_AUTHENTICATION_METHOD,
    TOTP_DISABLED_REVOCATION_REASON,
    ActivatedTotpEnrollment,
    ActivateEnrollmentCommand,
    DisabledTotp,
    DisableTotpCommand,
    InsertedPendingEnrollment,
    InsertPendingEnrollmentCommand,
    RecoveredSession,
    RecoverSessionCommand,
    RegeneratedRecoveryCodes,
    RegenerateRecoveryCodesCommand,
    SealedTotpSecret,
    TotpService,
    TotpTransactionPort,
    TotpVerified,
    VerifyTotpCommand,
    resolve_totp_step,
)
from personal_os.diagnostics.events import SafeToken
from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import (
    ConfigurationError,
    InternalApplicationError,
)
from personal_os.runtime_configuration.models import RuntimeEnvironment
from postgresql_source_store.authentication_credentials import (
    CredentialStore,
    run_authentication_transaction,
)
from postgresql_source_store.device_authorization_store import DeviceAuthorizationStore
from postgresql_source_store.device_token_store import DeviceTokenStore
from postgresql_source_store.totp_store import TotpStore
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

#: The fixed secret of the offline seeded active TOTP credential; tests
#: compute challenge codes from it. It never renders in the contract document.
OFFLINE_TOTP_SECRET: Final[bytes] = b"offline-totp-secret-"

#: The approved plugin version window of the offline graph.
OFFLINE_PLUGIN_VERSION_BOUNDS: Final[PluginVersionBounds] = PluginVersionBounds(
    minimum=(1, 0, 0), maximum=(2, 0, 0)
)

#: The offline graph pins the domain default window policies.
_OFFLINE_THROTTLE_POLICY: Final[ThrottleWindowPolicy] = ThrottleWindowPolicy()
_OFFLINE_SESSION_POLICY: Final[SessionWindowPolicy] = SessionWindowPolicy()

#: Safe reason token of the spec 20.1 startup refusal.
_MISSING_REFERENCED_KEY_REASON: Final[SafeToken] = SafeToken.parse("keyring_missing_referenced_key")


@dataclass(frozen=True, slots=True)
class WebAuthenticationRuntime:
    """One composed authentication runtime the session routes consume."""

    allowed_origin: str
    cookie_contract: SessionCookieContract
    resolve_client_address: ClientAddressResolver
    login_service: LoginService
    session_service: SessionService
    password_change_service: PasswordChangeService
    totp_service: TotpService
    device_authorization_service: DeviceAuthorizationService
    device_token_service: DeviceTokenService
    device_administration_service: DeviceAdministrationService
    verify_csrf_token: Callable[[str, str], bool]


class KeyringDeviceTokenKeyring:
    """Versioned-keyring adapter view for device-token derivations (20.1).

    The domain service resolves the current key and the key that anchored a
    committed derivation through this structural view; the concrete keyring
    keeps ownership of the key material.
    """

    def __init__(self, keyring: AuthenticationKeyring) -> None:
        self._keyring = keyring

    def current_key_id(self) -> str:
        return self._keyring.current_key_id

    def keys_by_id(self) -> Mapping[str, bytes]:
        return self._keyring.keys_by_id


class KeyringTotpSecretCodec:
    """Versioned-keyring AEAD adapter for TOTP-secret ciphertext (spec 20.1).

    Sealing always derives the ``auth/totp-secret/v1`` subkey of the current
    master key; opening resolves the subkey of the key ID the row references,
    so a previous-key secret stays decryptable until its re-encryption. Every
    decrypt or parameter failure fails closed as the safe ``internal_error``
    without crypto text.
    """

    def __init__(
        self, crypto: CryptographyAuthenticationCrypto, keyring: AuthenticationKeyring
    ) -> None:
        self._crypto = crypto
        self._keyring = keyring

    def current_key_id(self) -> str:
        return self._keyring.current_key_id

    def seal_secret(self, *, plaintext: bytes) -> SealedTotpSecret:
        key_id = self.current_key_id()
        subkey = self._crypto.derive_subkey(
            master_key=self._keyring.keys_by_id[key_id], label=TOTP_SECRET_AEAD_LABEL
        )
        nonce, ciphertext = self._crypto.seal_secret(key=subkey, plaintext=plaintext)
        return SealedTotpSecret(
            key_id=key_id,
            nonce=base64.b64encode(nonce).decode("ascii"),
            ciphertext=base64.b64encode(ciphertext).decode("ascii"),
        )

    def open_secret(self, *, sealed: SealedTotpSecret) -> bytes:
        master_key = self._keyring.keys_by_id.get(sealed.key_id)
        if master_key is None:
            raise InternalApplicationError(ErrorCode.INTERNAL_ERROR)
        subkey = self._crypto.derive_subkey(master_key=master_key, label=TOTP_SECRET_AEAD_LABEL)
        return self._crypto.open_secret(
            key=subkey,
            nonce=base64.b64decode(sealed.nonce.encode("ascii")),
            ciphertext=base64.b64decode(sealed.ciphertext.encode("ascii")),
        )


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
    totp_transactions: TotpTransactionPort = TotpStore(
        engine, secret_codec=KeyringTotpSecretCodec(crypto, keyring)
    )
    master_key = keyring.current_key()
    totp_service = TotpService(
        transactions=totp_transactions,
        sessions=sessions,
        hasher=hasher,
        crypto=crypto,
        master_key=master_key,
        clock=clock,
        secret_codec=KeyringTotpSecretCodec(crypto, keyring),
    )
    session_service = SessionService(
        sessions=sessions,
        hasher=hasher,
        crypto=crypto,
        master_key=master_key,
        clock=clock,
        totp_leg=totp_service,
    )
    device_grant_store = DeviceAuthorizationStore(engine)
    device_tokens_store = DeviceTokenStore(engine)
    return WebAuthenticationRuntime(
        allowed_origin=settings.allowed_origin,
        cookie_contract=build_session_cookie_contract(
            settings.allowed_origin, settings.environment
        ),
        resolve_client_address=create_client_address_resolver(settings.trusted_proxy_cidrs),
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
        totp_service=totp_service,
        device_authorization_service=DeviceAuthorizationService(
            grants=device_grant_store,
            session_service=session_service,
            crypto=crypto,
            master_key=master_key,
            clock=clock,
            plugin_version_bounds=PluginVersionBounds.from_strings(
                minimum_plugin_version=settings.minimum_plugin_version,
                maximum_plugin_version=settings.maximum_plugin_version,
            ),
            verification_base_url=settings.allowed_origin,
        ),
        device_token_service=DeviceTokenService(
            exchange=device_grant_store,
            tokens=device_tokens_store,
            keyring=KeyringDeviceTokenKeyring(keyring),
            crypto=crypto,
            clock=clock,
        ),
        device_administration_service=DeviceAdministrationService(
            tokens=device_tokens_store,
            session_service=session_service,
            clock=clock,
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
    *,
    engine: AsyncEngine,
    keyring: AuthenticationKeyring,
    clock: DatabaseAuthenticationClock,
) -> None:
    """Read every referenced key ID and enforce the coverage refusal (spec 20.1).

    The composition root calls this before the listening socket is exposed:
    Uvicorn runs the application lifespan startup before binding, so the
    raised :class:`ConfigurationError` aborts startup.
    """
    required_key_ids = await CredentialStore(engine).required_key_ids(
        database_now=await clock.database_now()
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
    """Deterministic crypto double deriving stable subkeys and stdlib HMAC.

    Mirrors the production vocabulary check at ``assert_crypto_domain_label``;
    rejects labels outside ``CRYPTO_DOMAIN_LABELS`` as ``INTERNAL_ERROR``.
    """

    def derive_subkey(self, *, master_key: bytes, label: str) -> bytes:
        assert_crypto_domain_label(label)
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


class OfflineTotpCredentialRow:
    """In-memory ``totp_credentials`` row of the offline graph."""

    def __init__(
        self,
        *,
        totp_credential_id: UUID,
        user_id: UUID,
        workspace_id: UUID,
        state: TotpCredentialState,
        sealed: SealedTotpSecret,
        last_accepted_time_step: int | None,
        enrollment_expires_at: datetime | None,
        revision: int,
        created_at: datetime,
        activated_at: datetime | None,
    ) -> None:
        self.totp_credential_id = totp_credential_id
        self.user_id = user_id
        self.workspace_id = workspace_id
        self.state = state
        self.sealed = sealed
        self.last_accepted_time_step = last_accepted_time_step
        self.enrollment_expires_at = enrollment_expires_at
        self.revision = revision
        self.created_at = created_at
        self.activated_at = activated_at


class OfflineRecoveryCodeRow:
    """In-memory ``totp_recovery_codes`` row of the offline graph."""

    def __init__(
        self,
        *,
        recovery_code_id: UUID,
        totp_credential_id: UUID,
        revision: int,
        code_hash: str,
        created_at: datetime,
    ) -> None:
        self.recovery_code_id = recovery_code_id
        self.totp_credential_id = totp_credential_id
        self.revision = revision
        self.code_hash = code_hash
        self.created_at = created_at
        self.used_at: datetime | None = None


class OfflineRegisteredDeviceRow:
    """In-memory ``devices`` row of one offline exchange."""

    def __init__(
        self,
        *,
        device_id: UUID,
        workspace_id: UUID,
        user_id: UUID,
        device_name: str,
        registered_at: datetime,
        device_kind: str = "obsidian",
    ) -> None:
        self.device_id = device_id
        self.workspace_id = workspace_id
        self.user_id = user_id
        self.device_name = device_name
        self.device_kind = device_kind
        self.status = "active"
        self.registered_at = registered_at
        self.last_seen_at: datetime | None = None
        self.revoked_at: datetime | None = None


class OfflineTokenFamilyRow:
    """In-memory ``device_token_families`` row of one offline exchange."""

    def __init__(
        self,
        *,
        token_family_id: UUID,
        workspace_id: UUID,
        user_id: UUID,
        device_id: UUID,
        inactivity_expires_at: datetime,
        absolute_expires_at: datetime,
        created_at: datetime,
    ) -> None:
        self.token_family_id = token_family_id
        self.workspace_id = workspace_id
        self.user_id = user_id
        self.device_id = device_id
        self.state = "active"
        self.current_refresh_generation = INITIAL_REFRESH_GENERATION
        self.created_at = created_at
        self.last_refreshed_at = created_at
        self.inactivity_expires_at = inactivity_expires_at
        self.absolute_expires_at = absolute_expires_at
        self.revoked_at: datetime | None = None
        self.revocation_reason: str | None = None


class OfflineDeviceTokenRow:
    """In-memory ``device_tokens`` row of one offline exchange."""

    def __init__(
        self,
        *,
        device_token_id: UUID,
        token_family_id: UUID,
        workspace_id: UUID,
        user_id: UUID,
        device_id: UUID,
        token_kind: str,
        secret_hash: str,
        expires_at: datetime,
        issued_at: datetime,
        derivation_key_id: str,
    ) -> None:
        self.device_token_id = device_token_id
        self.token_family_id = token_family_id
        self.workspace_id = workspace_id
        self.user_id = user_id
        self.device_id = device_id
        self.token_kind = token_kind
        self.generation = INITIAL_REFRESH_GENERATION
        self.secret_hash = secret_hash
        self.state = "active"
        self.predecessor_token_id: UUID | None = None
        self.successor_token_id: UUID | None = None
        self.rotation_id: UUID | None = None
        self.derivation_key_id = derivation_key_id
        self.issued_at = issued_at
        self.expires_at = expires_at
        self.rotated_at: datetime | None = None
        self.revoked_at: datetime | None = None


class OfflineDeviceGrantRow:
    """In-memory ``device_authorization_grants`` row of the offline graph."""

    def __init__(self, command: InsertPendingGrantCommand) -> None:
        self.grant_id = command.grant_id
        self.user_code_hash = command.user_code_hash
        self.polling_secret_hash = command.polling_secret_hash
        self.client_instance_id = command.client_instance_id
        self.claimed_device_id = command.claimed_device_id
        self.device_name = command.device_name
        self.platform_class = command.platform_class
        self.platform_name = command.platform_name
        self.plugin_version = command.plugin_version
        self.requested_scope = command.requested_scope
        self.state: DeviceAuthorizationGrantState = DeviceAuthorizationGrantState.PENDING
        self.created_at = command.database_now
        self.expires_at = command.expires_at
        self.approved_at: datetime | None = None
        self.denied_at: datetime | None = None
        self.exchanged_at: datetime | None = None
        self.approved_by_user_id: UUID | None = None
        self.approved_web_session_id: UUID | None = None
        self.device_id: UUID | None = None
        self.token_family_id: UUID | None = None
        self.initial_access_token_id: UUID | None = None
        self.initial_refresh_token_id: UUID | None = None
        self.derivation_key_id: str | None = None


class OfflineTotpSecretCodec:
    """Deterministic AEAD double: reversible transform, fixed key id."""

    _CURRENT_KEY_ID: Final[str] = "offline-totp-key-current"

    def current_key_id(self) -> str:
        return self._CURRENT_KEY_ID

    def seal_secret(self, *, plaintext: bytes) -> SealedTotpSecret:
        return SealedTotpSecret(
            key_id=self._CURRENT_KEY_ID,
            nonce=hashlib.sha256(plaintext).hexdigest()[:16],
            ciphertext=base64.b64encode(bytes(reversed(plaintext))).decode("ascii"),
        )

    def open_secret(self, *, sealed: SealedTotpSecret) -> bytes:
        return bytes(reversed(base64.b64decode(sealed.ciphertext.encode("ascii"))))


class OfflineAuthenticationState:
    """In-memory credential, throttle, TOTP and session state of the offline graph."""

    def __init__(self, *, totp_active: bool) -> None:
        self.totp_active = totp_active
        self.credential_revision = 1
        self.password_hash = OfflinePasswordHasher().hash_password(OFFLINE_PASSWORD)
        self.sessions_by_secret_hash: dict[str, StoredWebSession] = {}
        self.buckets: dict[str, ThrottleBucketState] = {}
        self.login_buckets: dict[str, ThrottleBucketState] = {}
        self.source_buckets: dict[str, ThrottleBucketState] = {}
        self.totp_prompt_dismissed_at: datetime | None = None
        self.totp_credential_rows: list[OfflineTotpCredentialRow] = []
        self.recovery_code_rows: list[OfflineRecoveryCodeRow] = []
        self.device_grant_rows: list[OfflineDeviceGrantRow] = []
        self.device_grant_audit_actions: list[str] = []
        self.device_rows: list[OfflineRegisteredDeviceRow] = []
        self.device_family_rows: list[OfflineTokenFamilyRow] = []
        self.device_token_rows: list[OfflineDeviceTokenRow] = []
        self.device_exchange_audit_actions: list[str] = []
        self.device_revoke_audit_actions: list[str] = []
        # The system bootstrap device of the canonical baseline: the Admin
        # device surface excludes it by its kind marker (spec 14.1).
        self.device_rows.append(
            OfflineRegisteredDeviceRow(
                device_id=UUID("00000000-0000-7000-8000-0000000000dd"),
                workspace_id=_OFFLINE_WORKSPACE_ID,
                user_id=_OFFLINE_USER_ID,
                device_name="System bootstrap",
                registered_at=_OFFLINE_DATABASE_NOW,
                device_kind="system",
            )
        )
        if totp_active:
            self.totp_credential_rows.append(
                OfflineTotpCredentialRow(
                    totp_credential_id=UUID("00000000-0000-7000-8000-0000000000aa"),
                    user_id=_OFFLINE_USER_ID,
                    workspace_id=_OFFLINE_WORKSPACE_ID,
                    state=TotpCredentialState.ACTIVE,
                    sealed=OfflineTotpSecretCodec().seal_secret(plaintext=OFFLINE_TOTP_SECRET),
                    last_accepted_time_step=None,
                    enrollment_expires_at=None,
                    revision=1,
                    created_at=_OFFLINE_DATABASE_NOW,
                    activated_at=_OFFLINE_DATABASE_NOW,
                )
            )

    def has_active_totp_credential(self) -> bool:
        return any(
            row.state is TotpCredentialState.ACTIVE
            for row in self.totp_credential_rows
            if row.user_id == _OFFLINE_USER_ID
        )


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
            credential_revision=(self._state.credential_revision if is_enrolled_account else None),
            username_bucket=self._state.login_buckets.get(username_bucket_hash),
            source_bucket=self._state.source_buckets.get(source_bucket_hash),
        )

    async def record_login_failure(
        self, command: RecordLoginFailureCommand
    ) -> RecordedLoginFailure:
        transition = next_login_failure_transition(
            self._state.login_buckets.get(command.username_bucket_hash),
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
        self._state.login_buckets[command.username_bucket_hash] = username_bucket
        self._state.source_buckets[command.source_bucket_hash] = source_bucket
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
            if self._state.has_active_totp_credential()
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
            authenticated_at=(
                None if state is WebSessionState.PENDING_TOTP else command.database_now
            ),
            reauthenticated_at=None,
            last_seen_at=None,
            idle_expires_at=(
                command.pending_totp_idle_expires_at
                if state is WebSessionState.PENDING_TOTP
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
        if session is None or not is_challenge_eligible_session(session, database_now=database_now):
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
        if session is None or session.session_secret_hash != command.prior_session_secret_hash:
            raise AuthenticationError(ErrorCode.AUTHENTICATION_REQUIRED)
        if command.cause is SessionRotationCause.RECENT_REAUTHENTICATION:
            if session.state is not WebSessionState.ACTIVE:
                raise AuthenticationError(ErrorCode.AUTHENTICATION_REQUIRED)
            rotated = replace(
                session,
                session_secret_hash=command.new_session_secret_hash,
                csrf_secret_hash=command.new_csrf_secret_hash,
                reauthenticated_at=command.database_now,
            )
            next_state = rotated.state
        else:
            source_state = (
                WebSessionState.PENDING_TOTP
                if command.cause is SessionRotationCause.SESSION_ACTIVATION
                else WebSessionState.RECOVERY_LIMITED
            )
            if session.state is not source_state:
                raise AuthenticationError(ErrorCode.AUTHENTICATION_REQUIRED)
            rotated = replace(
                session,
                session_secret_hash=command.new_session_secret_hash,
                csrf_secret_hash=command.new_csrf_secret_hash,
                state=WebSessionState.ACTIVE,
                authentication_method=command.target_authentication_method,
                authenticated_at=command.database_now,
                reauthenticated_at=None,
                idle_expires_at=clamp_idle_expiry(
                    command.database_now + _OFFLINE_SESSION_POLICY.idle_ttl,
                    session.absolute_expires_at,
                ),
            )
            next_state = WebSessionState.ACTIVE
        del self._state.sessions_by_secret_hash[command.prior_session_secret_hash]
        self._state.sessions_by_secret_hash[command.new_session_secret_hash] = rotated
        return RotatedWebSessionSecrets(
            web_session_id=command.web_session_id,
            state=next_state,
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


class OfflineTotpStore:
    """In-memory TOTP transaction double mirroring the store contracts.

    One ``asyncio.Lock`` serializes every operation, so the offline graph
    reproduces the row-lock serialization points of the real store — the same
    TOTP step accepted once, one recovery code consumed once — with the real
    pure domain logic and the same closed rejections.
    """

    def __init__(self, state: OfflineAuthenticationState) -> None:
        self._state = state
        self._codec = OfflineTotpSecretCodec()
        self._lock = asyncio.Lock()

    def _active_credential(self) -> OfflineTotpCredentialRow | None:
        return next(
            (
                row
                for row in self._state.totp_credential_rows
                if row.user_id == _OFFLINE_USER_ID and row.state is TotpCredentialState.ACTIVE
            ),
            None,
        )

    def _pending_credential(self, enrollment_id: UUID) -> OfflineTotpCredentialRow | None:
        return next(
            (
                row
                for row in self._state.totp_credential_rows
                if row.totp_credential_id == enrollment_id
                and row.user_id == _OFFLINE_USER_ID
                and row.state is TotpCredentialState.PENDING
            ),
            None,
        )

    def _bucket(self, bucket_kind: ThrottleBucketKind, bucket_hash: str) -> str:
        return f"{bucket_kind.value}:{bucket_hash}"

    async def resolve_verification_bucket(
        self, *, bucket_kind: ThrottleBucketKind, bucket_hash: str
    ) -> ThrottleBucketState | None:
        async with self._lock:
            return self._state.buckets.get(self._bucket(bucket_kind, bucket_hash))

    async def record_verification_failure(
        self, *, bucket_kind: ThrottleBucketKind, bucket_hash: str, database_now: datetime
    ) -> ThrottleFailureTransition:
        async with self._lock:
            key = self._bucket(bucket_kind, bucket_hash)
            transition = next_login_failure_transition(
                self._state.buckets.get(key),
                database_now=database_now,
                policy=_OFFLINE_THROTTLE_POLICY,
            )
            self._state.buckets[key] = ThrottleBucketState(
                window_started_at=transition.window_started_at,
                failed_attempt_count=transition.failed_attempt_count,
                locked_until=transition.locked_until,
            )
            return transition

    async def has_active_totp(self, *, user_id: UUID) -> bool:
        async with self._lock:
            return any(
                row.user_id == user_id and row.state is TotpCredentialState.ACTIVE
                for row in self._state.totp_credential_rows
            )

    async def record_prompt_dismissal(
        self, *, user_id: UUID, workspace_id: UUID, database_now: datetime
    ) -> datetime:
        del user_id, workspace_id
        async with self._lock:
            self._state.totp_prompt_dismissed_at = database_now
            return database_now

    async def insert_pending_enrollment(
        self, command: InsertPendingEnrollmentCommand
    ) -> InsertedPendingEnrollment:
        async with self._lock:
            active = self._active_credential()
            if active is not None and not command.allow_active_credential:
                raise AuthenticationError(ErrorCode.TOTP_ENROLLMENT_STATE_INVALID)
            for row in self._state.totp_credential_rows:
                if row.state is TotpCredentialState.PENDING:
                    row.state = TotpCredentialState.REPLACED
                    row.enrollment_expires_at = None
            totp_credential_id = uuid7()
            self._state.totp_credential_rows.append(
                OfflineTotpCredentialRow(
                    totp_credential_id=totp_credential_id,
                    user_id=command.user_id,
                    workspace_id=_OFFLINE_WORKSPACE_ID,
                    state=TotpCredentialState.PENDING,
                    sealed=command.sealed_secret,
                    last_accepted_time_step=None,
                    enrollment_expires_at=command.enrollment_expires_at,
                    revision=1,
                    created_at=command.database_now,
                    activated_at=None,
                )
            )
            return InsertedPendingEnrollment(
                totp_credential_id=totp_credential_id,
                enrollment_expires_at=command.enrollment_expires_at,
                username=OFFLINE_USERNAME,
                database_now=command.database_now,
            )

    async def verify_totp(self, command: VerifyTotpCommand) -> TotpVerified:
        async with self._lock:
            credential = self._active_credential()
            if credential is None:
                raise AuthenticationError(ErrorCode.AUTHENTICATION_FAILED)
            secret = self._codec.open_secret(sealed=credential.sealed)
            accepted_step = resolve_totp_step(
                submitted_code=command.submitted_code,
                secret=secret,
                last_accepted_time_step=credential.last_accepted_time_step,
                unix_time_seconds=command.unix_time_seconds,
            )
            if accepted_step is None:
                raise AuthenticationError(ErrorCode.AUTHENTICATION_FAILED)
            credential.last_accepted_time_step = accepted_step
            was_reencrypted = False
            if credential.sealed.key_id != self._codec.current_key_id():
                credential.sealed = self._codec.seal_secret(plaintext=secret)
                was_reencrypted = True
            if command.reset_bucket_hash is not None:
                self._state.buckets[
                    self._bucket(ThrottleBucketKind.TOTP_VERIFICATION, command.reset_bucket_hash)
                ] = ThrottleBucketState(
                    window_started_at=command.database_now,
                    failed_attempt_count=0,
                    locked_until=None,
                )
            return TotpVerified(
                totp_credential_id=credential.totp_credential_id,
                accepted_time_step=accepted_step,
                was_reencrypted=was_reencrypted,
                database_now=command.database_now,
            )

    async def activate_enrollment(
        self, command: ActivateEnrollmentCommand
    ) -> ActivatedTotpEnrollment:
        async with self._lock:
            pending = self._pending_credential(command.enrollment_id)
            if (
                pending is None
                or pending.enrollment_expires_at is None
                or pending.enrollment_expires_at <= command.database_now
            ):
                raise AuthenticationError(ErrorCode.TOTP_ENROLLMENT_STATE_INVALID)
            secret = self._codec.open_secret(sealed=pending.sealed)
            accepted_step = resolve_totp_step(
                submitted_code=command.submitted_code,
                secret=secret,
                last_accepted_time_step=pending.last_accepted_time_step,
                unix_time_seconds=command.unix_time_seconds,
            )
            if accepted_step is None:
                raise AuthenticationError(ErrorCode.AUTHENTICATION_FAILED)
            replaced_previous = False
            for row in self._state.totp_credential_rows:
                if row.state is TotpCredentialState.ACTIVE:
                    row.state = TotpCredentialState.REPLACED
                    row.enrollment_expires_at = None
                    replaced_previous = True
            pending.state = TotpCredentialState.ACTIVE
            pending.activated_at = command.database_now
            pending.enrollment_expires_at = None
            pending.last_accepted_time_step = accepted_step
            for code_hash in command.recovery_code_hashes:
                self._state.recovery_code_rows.append(
                    OfflineRecoveryCodeRow(
                        recovery_code_id=uuid7(),
                        totp_credential_id=pending.totp_credential_id,
                        revision=pending.revision,
                        code_hash=code_hash,
                        created_at=command.database_now,
                    )
                )
            if command.complete_recovery_session:
                session = next(
                    (
                        candidate
                        for candidate in self._state.sessions_by_secret_hash.values()
                        if candidate.web_session_id == command.current_web_session_id
                    ),
                    None,
                )
                if (
                    session is None
                    or session.session_secret_hash != command.prior_session_secret_hash
                    or session.state is not WebSessionState.RECOVERY_LIMITED
                ):
                    raise AuthenticationError(ErrorCode.AUTHENTICATION_REQUIRED)
                rotated = replace(
                    session,
                    session_secret_hash=command.new_session_secret_hash,
                    csrf_secret_hash=command.new_csrf_secret_hash,
                    state=WebSessionState.ACTIVE,
                    authentication_method=TOTP_AUTHENTICATION_METHOD,
                    authenticated_at=command.database_now,
                    reauthenticated_at=None,
                    idle_expires_at=clamp_idle_expiry(
                        command.database_now + _OFFLINE_SESSION_POLICY.idle_ttl,
                        session.absolute_expires_at,
                    ),
                )
                del self._state.sessions_by_secret_hash[command.prior_session_secret_hash]
                self._state.sessions_by_secret_hash[command.new_session_secret_hash] = rotated
            return ActivatedTotpEnrollment(
                totp_credential_id=pending.totp_credential_id,
                recovery_code_revision=pending.revision,
                replaced_previous_credential=replaced_previous,
                database_now=command.database_now,
            )

    async def recover_session(self, command: RecoverSessionCommand) -> RecoveredSession:
        async with self._lock:
            credential = self._active_credential()
            if credential is None:
                raise AuthenticationError(ErrorCode.AUTHENTICATION_FAILED)
            matching = next(
                (
                    row
                    for row in self._state.recovery_code_rows
                    if row.totp_credential_id == credential.totp_credential_id
                    and row.code_hash == command.recovery_code_hash
                    and row.used_at is None
                ),
                None,
            )
            if matching is None:
                raise AuthenticationError(ErrorCode.AUTHENTICATION_FAILED)
            matching.used_at = command.database_now
            session = next(
                (
                    candidate
                    for candidate in self._state.sessions_by_secret_hash.values()
                    if candidate.web_session_id == command.current_web_session_id
                ),
                None,
            )
            if (
                session is None
                or session.session_secret_hash != command.prior_session_secret_hash
                or session.state not in (WebSessionState.PENDING_TOTP, WebSessionState.ACTIVE)
            ):
                raise AuthenticationError(ErrorCode.AUTHENTICATION_REQUIRED)
            rotated = replace(
                session,
                session_secret_hash=command.new_session_secret_hash,
                csrf_secret_hash=command.new_csrf_secret_hash,
                state=WebSessionState.RECOVERY_LIMITED,
                authentication_method=RECOVERY_AUTHENTICATION_METHOD,
                authenticated_at=command.database_now,
                reauthenticated_at=None,
            )
            del self._state.sessions_by_secret_hash[command.prior_session_secret_hash]
            self._state.sessions_by_secret_hash[command.new_session_secret_hash] = rotated
            return RecoveredSession(
                web_session_id=command.current_web_session_id,
                state=WebSessionState.RECOVERY_LIMITED,
                database_now=command.database_now,
            )

    async def regenerate_recovery_codes(
        self, command: RegenerateRecoveryCodesCommand
    ) -> RegeneratedRecoveryCodes:
        async with self._lock:
            credential = self._active_credential()
            if credential is None:
                raise AuthenticationError(ErrorCode.AUTHENTICATION_FAILED)
            credential.revision += 1
            invalidated = 0
            for row in self._state.recovery_code_rows:
                if row.totp_credential_id == credential.totp_credential_id and row.used_at is None:
                    row.used_at = command.database_now
                    invalidated += 1
            for code_hash in command.recovery_code_hashes:
                self._state.recovery_code_rows.append(
                    OfflineRecoveryCodeRow(
                        recovery_code_id=uuid7(),
                        totp_credential_id=credential.totp_credential_id,
                        revision=credential.revision,
                        code_hash=code_hash,
                        created_at=command.database_now,
                    )
                )
            return RegeneratedRecoveryCodes(
                revision=credential.revision,
                invalidated_code_count=invalidated,
                database_now=command.database_now,
            )

    async def disable_totp(self, command: DisableTotpCommand) -> DisabledTotp:
        async with self._lock:
            if self._active_credential() is None:
                raise AuthenticationError(ErrorCode.AUTHENTICATION_FAILED)
            for row in self._state.totp_credential_rows:
                if row.state is not TotpCredentialState.REPLACED:
                    row.state = TotpCredentialState.REPLACED
                    row.enrollment_expires_at = None
            for code_row in self._state.recovery_code_rows:
                if code_row.used_at is None:
                    code_row.used_at = command.database_now
            self._state.credential_revision += 1
            next_credential_revision = self._state.credential_revision
            revoked_session_count = 0
            rotated_session: StoredWebSession | None = None
            for secret_hash, session in list(self._state.sessions_by_secret_hash.items()):
                if session.web_session_id == command.current_web_session_id:
                    if session.session_secret_hash != command.prior_session_secret_hash:
                        raise AuthenticationError(ErrorCode.AUTHENTICATION_REQUIRED)
                    rotated_session = replace(
                        session,
                        session_secret_hash=command.new_session_secret_hash,
                        csrf_secret_hash=command.new_csrf_secret_hash,
                        credential_revision=next_credential_revision,
                        authentication_method=PASSWORD_AUTHENTICATION_METHOD,
                    )
                    del self._state.sessions_by_secret_hash[secret_hash]
                    self._state.sessions_by_secret_hash[command.new_session_secret_hash] = (
                        rotated_session
                    )
                elif session.state is not WebSessionState.REVOKED:
                    self._state.sessions_by_secret_hash[secret_hash] = replace(
                        session,
                        state=WebSessionState.REVOKED,
                        revoked_at=command.database_now,
                        revocation_reason=TOTP_DISABLED_REVOCATION_REASON,
                        authenticated_at=None,
                        reauthenticated_at=None,
                    )
                    revoked_session_count += 1
            if rotated_session is None:
                raise AuthenticationError(ErrorCode.AUTHENTICATION_REQUIRED)
            return DisabledTotp(
                credential_revision=next_credential_revision,
                revoked_session_count=revoked_session_count,
                database_now=command.database_now,
            )


class OfflineDeviceAuthorizationStore:
    """In-memory grant transaction double mirroring the store contracts.

    One ``asyncio.Lock`` serializes every operation, so the offline graph
    reproduces the row-lock serialization points of the real store — one
    terminal winner per grant, one audit action per committed transition —
    with the real pure domain logic and the same closed rejections.
    """

    def __init__(self, state: OfflineAuthenticationState) -> None:
        self._state = state
        self._lock = asyncio.Lock()

    def _bucket(self, bucket_kind: ThrottleBucketKind, bucket_hash: str) -> str:
        return f"{bucket_kind.value}:{bucket_hash}"

    def _stored_view(self, row: OfflineDeviceGrantRow) -> StoredDeviceAuthorizationGrant:
        return StoredDeviceAuthorizationGrant(
            grant_id=row.grant_id,
            client_instance_id=row.client_instance_id,
            claimed_device_id=row.claimed_device_id,
            device_name=row.device_name,
            platform_class=row.platform_class,
            platform_name=row.platform_name,
            plugin_version=row.plugin_version,
            requested_scope=row.requested_scope,
            state=row.state,
            created_at=row.created_at,
            expires_at=row.expires_at,
            approved_at=row.approved_at,
            denied_at=row.denied_at,
            exchanged_at=row.exchanged_at,
            approved_by_user_id=row.approved_by_user_id,
            approved_web_session_id=row.approved_web_session_id,
        )

    async def resolve_throttle_bucket(
        self, *, bucket_kind: ThrottleBucketKind, bucket_hash: str
    ) -> ThrottleBucketState | None:
        async with self._lock:
            return self._state.buckets.get(self._bucket(bucket_kind, bucket_hash))

    async def record_throttle_attempt(
        self, *, bucket_kind: ThrottleBucketKind, bucket_hash: str, database_now: datetime
    ) -> ThrottleFailureTransition:
        async with self._lock:
            key = self._bucket(bucket_kind, bucket_hash)
            transition = next_login_failure_transition(
                self._state.buckets.get(key),
                database_now=database_now,
                policy=_OFFLINE_THROTTLE_POLICY,
            )
            self._state.buckets[key] = ThrottleBucketState(
                window_started_at=transition.window_started_at,
                failed_attempt_count=transition.failed_attempt_count,
                locked_until=transition.locked_until,
            )
            return transition

    async def live_grant_window(
        self, *, client_instance_id: UUID, database_now: datetime
    ) -> LiveGrantWindow:
        async with self._lock:
            live_rows = [
                row
                for row in self._state.device_grant_rows
                if row.client_instance_id == client_instance_id
                and row.state is DeviceAuthorizationGrantState.PENDING
                and database_now < row.expires_at
            ]
            return LiveGrantWindow(
                live_grant_count=len(live_rows),
                earliest_expires_at=(
                    min(row.expires_at for row in live_rows) if live_rows else None
                ),
            )

    async def insert_pending_grant(
        self, command: InsertPendingGrantCommand
    ) -> InsertedPendingGrant:
        async with self._lock:
            if command.creation_bucket_hash is not None:
                key = self._bucket(ThrottleBucketKind.GRANT_CREATION, command.creation_bucket_hash)
                transition = next_login_failure_transition(
                    self._state.buckets.get(key),
                    database_now=command.database_now,
                    policy=_OFFLINE_THROTTLE_POLICY,
                )
                self._state.buckets[key] = ThrottleBucketState(
                    window_started_at=transition.window_started_at,
                    failed_attempt_count=transition.failed_attempt_count,
                    locked_until=transition.locked_until,
                )
            self._state.device_grant_rows.append(OfflineDeviceGrantRow(command))
            return InsertedPendingGrant(
                grant_id=command.grant_id,
                expires_at=command.expires_at,
                database_now=command.database_now,
            )

    async def lookup_grant_by_user_code(
        self,
        *,
        user_code_hash: str,
        database_now: datetime,
        reset_bucket_hash: str | None = None,
    ) -> StoredDeviceAuthorizationGrant | None:
        async with self._lock:
            row = next(
                (
                    candidate
                    for candidate in self._state.device_grant_rows
                    if candidate.user_code_hash == user_code_hash
                ),
                None,
            )
            if row is None:
                return None
            stored = self._stored_view(row)
            if (
                reset_bucket_hash is not None
                and stored.state is (DeviceAuthorizationGrantState.PENDING)
                and database_now < stored.expires_at
            ):
                self._state.buckets[
                    self._bucket(ThrottleBucketKind.USER_CODE_LOOKUP, reset_bucket_hash)
                ] = ThrottleBucketState(
                    window_started_at=database_now,
                    failed_attempt_count=0,
                    locked_until=None,
                )
            return stored

    async def approve_grant(self, command: ApproveGrantCommand) -> ApprovedGrant:
        committed = await self._terminal_transition(command)
        return ApprovedGrant(
            grant_id=committed.grant_id,
            state=DeviceAuthorizationGrantState.APPROVED,
            approved_at=command.database_now,
            database_now=command.database_now,
        )

    async def deny_grant(self, command: DenyGrantCommand) -> DeniedGrant:
        committed = await self._terminal_transition(command)
        return DeniedGrant(
            grant_id=committed.grant_id,
            state=DeviceAuthorizationGrantState.DENIED,
            denied_at=command.database_now,
            database_now=command.database_now,
        )

    async def _terminal_transition(
        self, command: ApproveGrantCommand | DenyGrantCommand
    ) -> OfflineDeviceGrantRow:
        async with self._lock:
            row = next(
                (
                    candidate
                    for candidate in self._state.device_grant_rows
                    if candidate.grant_id == command.grant_id
                ),
                None,
            )
            rejection_code = resolve_terminal_rejection_code(
                None if row is None else self._stored_view(row),
                database_now=command.database_now,
            )
            if rejection_code is not None:
                raise AuthenticationError(rejection_code)
            assert row is not None
            is_approval = isinstance(command, ApproveGrantCommand)
            if is_approval:
                row.state = DeviceAuthorizationGrantState.APPROVED
                row.approved_at = command.database_now
                row.approved_by_user_id = command.user_id
                row.approved_web_session_id = command.web_session_id
                self._state.device_grant_audit_actions.append(
                    "authentication.device_authorization_approved"
                )
            else:
                row.state = DeviceAuthorizationGrantState.DENIED
                row.denied_at = command.database_now
                self._state.device_grant_audit_actions.append(
                    "authentication.device_authorization_denied"
                )
            return row

    async def poll_exchange(self, command: ExchangeGrantCommand) -> ExchangeProvisioning:
        """Lock-free in-memory exchange mirroring the real transaction.

        One ``asyncio.Lock`` serializes the operation exactly like the row
        lock of the real store: the closed poll outcome vocabulary answers
        pending, denied and expired grants before any write; an approved
        grant commits one device, family and token pair with the grant
        anchors and the two registration audits; an exchanged grant replays
        the anchored identities and timestamps while generation one stays
        current.
        """
        async with self._lock:
            row = next(
                (
                    candidate
                    for candidate in self._state.device_grant_rows
                    if candidate.polling_secret_hash == command.polling_secret_hash
                ),
                None,
            )
            if row is None or row.grant_id != command.grant_id:
                raise AuthenticationError(ErrorCode.DEVICE_CREDENTIAL_INVALID)
            if row.state is DeviceAuthorizationGrantState.DENIED:
                raise AuthenticationError(ErrorCode.DEVICE_AUTHORIZATION_DENIED)
            if row.state is DeviceAuthorizationGrantState.PENDING:
                if command.database_now >= row.expires_at:
                    raise AuthenticationError(ErrorCode.DEVICE_AUTHORIZATION_EXPIRED)
                raise AuthenticationError(
                    ErrorCode.DEVICE_AUTHORIZATION_PENDING,
                    safe_details={"retry_after_seconds": POLL_INTERVAL_SECONDS},
                )
            if row.state is DeviceAuthorizationGrantState.EXCHANGED:
                return self._replay_offline_exchange(row, command)
            row.state = DeviceAuthorizationGrantState.EXCHANGED
            row.exchanged_at = command.database_now
            row.device_id = command.device_id
            row.token_family_id = command.token_family_id
            row.initial_access_token_id = command.access_token_id
            row.initial_refresh_token_id = command.refresh_token_id
            row.derivation_key_id = command.derivation_key_id
            self._state.device_rows.append(
                OfflineRegisteredDeviceRow(
                    device_id=command.device_id,
                    workspace_id=_OFFLINE_WORKSPACE_ID,
                    user_id=_OFFLINE_USER_ID,
                    device_name=row.device_name,
                    registered_at=command.database_now,
                )
            )
            self._state.device_family_rows.append(
                OfflineTokenFamilyRow(
                    token_family_id=command.token_family_id,
                    workspace_id=_OFFLINE_WORKSPACE_ID,
                    user_id=_OFFLINE_USER_ID,
                    device_id=command.device_id,
                    inactivity_expires_at=command.refresh_expires_at,
                    absolute_expires_at=command.family_absolute_expires_at,
                    created_at=command.database_now,
                )
            )
            for token_kind, token_id, secret_hash, expires_at in (
                (
                    "access",
                    command.access_token_id,
                    command.access_secret_hash,
                    command.access_expires_at,
                ),
                (
                    "refresh",
                    command.refresh_token_id,
                    command.refresh_secret_hash,
                    command.refresh_expires_at,
                ),
            ):
                self._state.device_token_rows.append(
                    OfflineDeviceTokenRow(
                        device_token_id=token_id,
                        token_family_id=command.token_family_id,
                        workspace_id=_OFFLINE_WORKSPACE_ID,
                        user_id=_OFFLINE_USER_ID,
                        device_id=command.device_id,
                        token_kind=token_kind,
                        secret_hash=secret_hash,
                        expires_at=expires_at,
                        issued_at=command.database_now,
                        derivation_key_id=command.derivation_key_id,
                    )
                )
            self._state.device_exchange_audit_actions.append("authentication.device_registered")
            self._state.device_exchange_audit_actions.append(
                "authentication.device_token_family_created"
            )
            return ExchangeProvisioning(
                grant_id=command.grant_id,
                device_id=command.device_id,
                token_family_id=command.token_family_id,
                access_token_id=command.access_token_id,
                refresh_token_id=command.refresh_token_id,
                derivation_key_id=command.derivation_key_id,
                refresh_generation=INITIAL_REFRESH_GENERATION,
                access_issued_at=command.database_now,
                access_expires_at=command.access_expires_at,
                refresh_expires_at=command.refresh_expires_at,
                database_now=command.database_now,
            )

    def _replay_offline_exchange(
        self, row: OfflineDeviceGrantRow, command: ExchangeGrantCommand
    ) -> ExchangeProvisioning:
        """Replay the anchored offline exchange while generation one is current."""
        assert row.initial_refresh_token_id is not None
        assert row.initial_access_token_id is not None
        initial_refresh = next(
            candidate
            for candidate in self._state.device_token_rows
            if candidate.device_token_id == row.initial_refresh_token_id
        )
        family = next(
            candidate
            for candidate in self._state.device_family_rows
            if candidate.token_family_id == row.token_family_id
        )
        is_initial_generation_current = (
            initial_refresh.state == "active"
            and family.current_refresh_generation == initial_refresh.generation
        )
        if not is_initial_generation_current:
            raise AuthenticationError(ErrorCode.DEVICE_AUTHORIZATION_STATE_INVALID)
        initial_access = next(
            candidate
            for candidate in self._state.device_token_rows
            if candidate.device_token_id == row.initial_access_token_id
        )
        assert row.device_id is not None
        assert row.token_family_id is not None
        assert row.derivation_key_id is not None
        return ExchangeProvisioning(
            grant_id=row.grant_id,
            device_id=row.device_id,
            token_family_id=row.token_family_id,
            access_token_id=row.initial_access_token_id,
            refresh_token_id=row.initial_refresh_token_id,
            derivation_key_id=row.derivation_key_id,
            refresh_generation=initial_refresh.generation,
            access_issued_at=initial_access.issued_at,
            access_expires_at=initial_access.expires_at,
            refresh_expires_at=initial_refresh.expires_at,
            database_now=command.database_now,
        )


def _stored_token_view(row: OfflineDeviceTokenRow) -> StoredDeviceToken:
    """Build the typed token view of one offline row."""
    return StoredDeviceToken(
        device_token_id=row.device_token_id,
        token_family_id=row.token_family_id,
        user_id=row.user_id,
        workspace_id=row.workspace_id,
        device_id=row.device_id,
        token_kind=DeviceTokenKind(row.token_kind),
        generation=row.generation,
        state=DeviceTokenState(row.state),
        predecessor_token_id=row.predecessor_token_id,
        successor_token_id=row.successor_token_id,
        rotation_id=row.rotation_id,
        derivation_key_id=row.derivation_key_id,
        issued_at=row.issued_at,
        expires_at=row.expires_at,
        rotated_at=row.rotated_at,
        revoked_at=row.revoked_at,
    )


def _stored_family_view(row: OfflineTokenFamilyRow) -> StoredTokenFamily:
    """Build the typed family view of one offline row."""
    return StoredTokenFamily(
        token_family_id=row.token_family_id,
        user_id=row.user_id,
        workspace_id=row.workspace_id,
        device_id=row.device_id,
        state=DeviceTokenFamilyState(row.state),
        current_refresh_generation=row.current_refresh_generation,
        created_at=row.created_at,
        last_refreshed_at=row.last_refreshed_at,
        inactivity_expires_at=row.inactivity_expires_at,
        absolute_expires_at=row.absolute_expires_at,
        revoked_at=row.revoked_at,
        revocation_reason=row.revocation_reason,
    )


class OfflineDeviceTokenStore:
    """In-memory token transaction double mirroring the store contracts.

    One ``asyncio.Lock`` serializes every operation, so the offline graph
    reproduces the row-lock serialization points of the real store with the
    real pure domain logic - the exact replay classification, the rotation
    order and both revoke transitions - and the same closed rejections.
    """

    def __init__(self, state: OfflineAuthenticationState) -> None:
        self._state = state
        self._lock = asyncio.Lock()

    def _token_row(self, token_id: UUID, *, token_kind: str) -> OfflineDeviceTokenRow | None:
        return next(
            (
                row
                for row in self._state.device_token_rows
                if row.device_token_id == token_id and row.token_kind == token_kind
            ),
            None,
        )

    def _family_row(self, token_family_id: UUID) -> OfflineTokenFamilyRow | None:
        return next(
            (
                row
                for row in self._state.device_family_rows
                if row.token_family_id == token_family_id
            ),
            None,
        )

    def _verify_presented_hash(
        self, row: OfflineDeviceTokenRow, hashes_by_key_id: Mapping[str, str]
    ) -> bool:
        presented = hashes_by_key_id.get(row.derivation_key_id)
        return presented is not None and hmac.compare_digest(presented, row.secret_hash)

    async def resolve_refresh_predecessor(self, *, token_id: UUID) -> StoredDeviceToken | None:
        async with self._lock:
            row = self._token_row(token_id, token_kind="refresh")
            return None if row is None else _stored_token_view(row)

    async def refresh_rotation(self, command: RefreshRotationCommand) -> CommittedRefreshRotation:
        async with self._lock:
            predecessor_row = self._token_row(command.predecessor_token_id, token_kind="refresh")
            if predecessor_row is None or not self._verify_presented_hash(
                predecessor_row, command.predecessor_secret_hashes_by_key_id
            ):
                raise AuthenticationError(ErrorCode.DEVICE_CREDENTIAL_INVALID)
            family_row = self._family_row(predecessor_row.token_family_id)
            if family_row is None:
                raise AuthenticationError(ErrorCode.DEVICE_CREDENTIAL_INVALID)

            successor_row = (
                self._token_row(predecessor_row.successor_token_id, token_kind="refresh")
                if predecessor_row.state == "rotated"
                and predecessor_row.successor_token_id is not None
                else None
            )
            presentation = classify_refresh_presentation(
                predecessor=_stored_token_view(predecessor_row),
                successor=None if successor_row is None else _stored_token_view(successor_row),
                family=_stored_family_view(family_row),
                presented_rotation_id=command.rotation_id,
                database_now=command.database_now,
            )
            if presentation is RefreshPresentationKind.REUSE_DETECTED:
                self._revoke_family_for_reuse(family_row, decided_at=command.database_now)
                raise AuthenticationError(ErrorCode.DEVICE_TOKEN_REUSE_DETECTED)
            if presentation is RefreshPresentationKind.EXACT_REPLAY:
                assert successor_row is not None
                assert predecessor_row.rotated_at is not None
                successor_access = next(
                    row
                    for row in self._state.device_token_rows
                    if row.token_family_id == family_row.token_family_id
                    and row.token_kind == "access"
                    and row.generation == successor_row.generation
                )
                return CommittedRefreshRotation(
                    token_family_id=family_row.token_family_id,
                    successor_refresh_token_id=successor_row.device_token_id,
                    successor_access_token_id=successor_access.device_token_id,
                    successor_generation=successor_row.generation,
                    derivation_key_id=successor_row.derivation_key_id,
                    rotated_at=predecessor_row.rotated_at,
                    access_expires_at=successor_access.expires_at,
                    refresh_expires_at=successor_row.expires_at,
                    family_inactivity_expires_at=family_row.inactivity_expires_at,
                    family_absolute_expires_at=family_row.absolute_expires_at,
                    database_now=command.database_now,
                )

            successor_generation = predecessor_row.generation + 1
            assert family_row.absolute_expires_at is not None
            refresh_expires_at = min(
                command.database_now + REFRESH_INACTIVITY_LIFETIME,
                family_row.absolute_expires_at,
            )
            predecessor_row.state = "rotated"
            predecessor_row.rotated_at = command.database_now
            for token_kind, token_id, secret_hash, expires_at in (
                (
                    "refresh",
                    command.successor_refresh_token_id,
                    command.successor_refresh_secret_hash,
                    refresh_expires_at,
                ),
                (
                    "access",
                    command.successor_access_token_id,
                    command.successor_access_secret_hash,
                    command.access_expires_at,
                ),
            ):
                successor_row_offline = OfflineDeviceTokenRow(
                    device_token_id=token_id,
                    token_family_id=family_row.token_family_id,
                    workspace_id=predecessor_row.workspace_id,
                    user_id=predecessor_row.user_id,
                    device_id=predecessor_row.device_id,
                    token_kind=token_kind,
                    secret_hash=secret_hash,
                    expires_at=expires_at,
                    issued_at=command.database_now,
                    derivation_key_id=command.derivation_key_id,
                )
                successor_row_offline.generation = successor_generation
                if token_kind == "refresh":
                    successor_row_offline.predecessor_token_id = predecessor_row.device_token_id
                    successor_row_offline.rotation_id = command.rotation_id
                self._state.device_token_rows.append(successor_row_offline)
            predecessor_row.successor_token_id = command.successor_refresh_token_id
            family_row.current_refresh_generation = successor_generation
            family_row.last_refreshed_at = command.database_now
            family_row.inactivity_expires_at = refresh_expires_at
            return CommittedRefreshRotation(
                token_family_id=family_row.token_family_id,
                successor_refresh_token_id=command.successor_refresh_token_id,
                successor_access_token_id=command.successor_access_token_id,
                successor_generation=successor_generation,
                derivation_key_id=command.derivation_key_id,
                rotated_at=command.database_now,
                access_expires_at=command.access_expires_at,
                refresh_expires_at=refresh_expires_at,
                family_inactivity_expires_at=refresh_expires_at,
                family_absolute_expires_at=family_row.absolute_expires_at,
                database_now=command.database_now,
            )

    def _revoke_family_for_reuse(
        self, family_row: OfflineTokenFamilyRow, *, decided_at: datetime
    ) -> None:
        """Mirror the confirmed-reuse revocation of the real store (13.5)."""
        if family_row.state == "revoked":
            return
        family_row.state = "revoked"
        family_row.revoked_at = decided_at
        family_row.revocation_reason = "token_reuse"
        for row in self._state.device_token_rows:
            if row.token_family_id == family_row.token_family_id and row.state == "active":
                row.state = "revoked"
                row.revoked_at = decided_at
        device_row = next(
            (
                candidate
                for candidate in self._state.device_rows
                if candidate.device_id == family_row.device_id
            ),
            None,
        )
        if device_row is not None and device_row.status == "active":
            device_row.status = "revoked"
            device_row.revoked_at = decided_at
        self._state.device_revoke_audit_actions.append("authentication.device_token_reuse_detected")

    async def revoke_current_refresh(
        self, command: RevokeCurrentRefreshCommand
    ) -> RevokedCurrentTokenFamily:
        async with self._lock:
            predecessor_row = self._token_row(command.refresh_token_id, token_kind="refresh")
            if predecessor_row is None or not self._verify_presented_hash(
                predecessor_row, command.secret_hashes_by_key_id
            ):
                raise AuthenticationError(ErrorCode.DEVICE_CREDENTIAL_INVALID)
            family_row = self._family_row(predecessor_row.token_family_id)
            if family_row is None:
                raise AuthenticationError(ErrorCode.DEVICE_CREDENTIAL_INVALID)
            if family_row.state == "revoked" or predecessor_row.state == "revoked":
                raise AuthenticationError(ErrorCode.DEVICE_REVOKED)
            if (
                predecessor_row.state == "rotated"
                or family_row.current_refresh_generation != predecessor_row.generation
            ):
                self._revoke_family_for_reuse(family_row, decided_at=command.database_now)
                raise AuthenticationError(ErrorCode.DEVICE_TOKEN_REUSE_DETECTED)
            family_row.state = "revoked"
            family_row.revoked_at = command.database_now
            family_row.revocation_reason = SELF_REVOCATION_REASON
            for row in self._state.device_token_rows:
                if row.token_family_id == family_row.token_family_id and row.state == "active":
                    row.state = "revoked"
                    row.revoked_at = command.database_now
            self._state.device_revoke_audit_actions.append(DEVICE_TOKEN_FAMILY_REVOKED_AUDIT_ACTION)
            return RevokedCurrentTokenFamily(
                token_family_id=family_row.token_family_id,
                device_id=predecessor_row.device_id,
                revoked_at=command.database_now,
                database_now=command.database_now,
            )

    async def admin_revoke_device(self, command: AdminRevokeDeviceCommand) -> AdminRevokedDevice:
        async with self._lock:
            device_row = next(
                (
                    candidate
                    for candidate in self._state.device_rows
                    if candidate.device_id == command.device_id
                ),
                None,
            )
            if (
                device_row is None
                or device_row.device_kind == "system"
                or device_row.workspace_id != command.workspace_id
            ):
                raise AuthenticationError(ErrorCode.DEVICE_CREDENTIAL_INVALID)
            if device_row.device_name != command.device_name_confirmation:
                raise AuthenticationError(ErrorCode.DEVICE_REVOCATION_CONFIRMATION_INVALID)
            if device_row.status == "revoked":
                assert device_row.revoked_at is not None
                return AdminRevokedDevice(
                    device_id=command.device_id,
                    revoked_at=device_row.revoked_at,
                    database_now=command.database_now,
                )
            device_row.status = "revoked"
            device_row.revoked_at = command.database_now
            for family_row in self._state.device_family_rows:
                if family_row.device_id != command.device_id or family_row.state != "active":
                    continue
                family_row.state = "revoked"
                family_row.revoked_at = command.database_now
                family_row.revocation_reason = ADMIN_REVOCATION_REASON
            for token_row in self._state.device_token_rows:
                if token_row.device_id == command.device_id and token_row.state == "active":
                    token_row.state = "revoked"
                    token_row.revoked_at = command.database_now
            for grant_row in self._state.device_grant_rows:
                if grant_row.claimed_device_id == command.device_id and grant_row.state in (
                    DeviceAuthorizationGrantState.PENDING,
                    DeviceAuthorizationGrantState.APPROVED,
                ):
                    grant_row.state = DeviceAuthorizationGrantState.DENIED
                    grant_row.denied_at = command.database_now
            self._state.device_revoke_audit_actions.append(DEVICE_REVOKED_AUDIT_ACTION)
            return AdminRevokedDevice(
                device_id=command.device_id,
                revoked_at=command.database_now,
                database_now=command.database_now,
            )

    async def list_admin_devices(self, *, workspace_id: UUID) -> tuple[ListedAdminDevice, ...]:
        async with self._lock:
            exchanged_grants_by_device = {
                grant_row.device_id: grant_row
                for grant_row in self._state.device_grant_rows
                if grant_row.state is DeviceAuthorizationGrantState.EXCHANGED
                and grant_row.device_id is not None
            }
            rows = sorted(
                (
                    candidate
                    for candidate in self._state.device_rows
                    if candidate.workspace_id == workspace_id
                    and candidate.device_kind != "system"
                    and candidate.device_id in exchanged_grants_by_device
                ),
                key=lambda candidate: (candidate.registered_at, candidate.device_id),
                reverse=True,
            )
            return tuple(
                ListedAdminDevice(
                    device_id=row.device_id,
                    device_name=row.device_name,
                    platform_class=exchanged_grants_by_device[row.device_id].platform_class,
                    platform_name=exchanged_grants_by_device[row.device_id].platform_name,
                    plugin_version=exchanged_grants_by_device[row.device_id].plugin_version,
                    status=row.status,
                    registered_at=row.registered_at,
                    last_seen_at=row.last_seen_at,
                    revoked_at=row.revoked_at,
                    family_absolute_expires_at=max(
                        (
                            family.absolute_expires_at
                            for family in self._state.device_family_rows
                            if family.device_id == row.device_id
                            and family.absolute_expires_at is not None
                        ),
                        default=None,
                    ),
                )
                for row in rows
            )

    async def authenticate_access_token(
        self, command: AccessTokenAuthenticationCommand
    ) -> AuthenticatedAccessToken:
        async with self._lock:
            token_row = self._token_row(command.token_id, token_kind="access")
            if token_row is None or not self._verify_presented_hash(
                token_row, command.secret_hashes_by_key_id
            ):
                raise AuthenticationError(ErrorCode.DEVICE_CREDENTIAL_INVALID)
            family_row = self._family_row(token_row.token_family_id)
            if token_row.state == "revoked" or (
                family_row is not None and family_row.state == "revoked"
            ):
                raise AuthenticationError(ErrorCode.DEVICE_REVOKED)
            if token_row.state != "active" or command.database_now >= token_row.expires_at:
                raise AuthenticationError(ErrorCode.DEVICE_CREDENTIAL_INVALID)
            if family_row is None or command.database_now >= family_row.absolute_expires_at:
                raise AuthenticationError(ErrorCode.DEVICE_CREDENTIAL_INVALID)
            device_row = next(
                (
                    candidate
                    for candidate in self._state.device_rows
                    if candidate.device_id == token_row.device_id
                ),
                None,
            )
            if device_row is None or device_row.status == "revoked":
                raise AuthenticationError(ErrorCode.DEVICE_REVOKED)
            return AuthenticatedAccessToken(
                context=AuthenticatedDeviceContext(
                    user_id=token_row.user_id,
                    workspace_id=token_row.workspace_id,
                    device_id=token_row.device_id,
                    scope=FIXED_DEVICE_SCOPE,
                ),
                database_now=command.database_now,
            )


class OfflineDeviceTokenKeyring:
    """Fixed offline keyring view: one deterministic key."""

    _CURRENT_KEY_ID: str = "offline-device-token-key-current"

    def current_key_id(self) -> str:
        return self._CURRENT_KEY_ID

    def keys_by_id(self) -> dict[str, bytes]:
        return {self._CURRENT_KEY_ID: _OFFLINE_MASTER_KEY}


def compose_offline_web_authentication(
    *,
    totp_active: bool = False,
    clock: OfflineAuthenticationClock | None = None,
    state: OfflineAuthenticationState | None = None,
    trusted_proxy_cidrs: Sequence[str] = (),
) -> WebAuthenticationRuntime:
    """Build the deterministic offline runtime for export and tests.

    An injected ``state`` replaces the default construction — ``totp_active``
    then only seeds the default — so tests can pre-seed or restamp session
    rows (for example a ``recovery_limited`` binding) while every secret and
    the fixed clock stay deterministic. The trusted-proxy CIDRs default to
    the fail-closed empty set (socket peer always wins) and are only ever the
    exact explicit values a test passes, so the offline graph stays
    deterministic for the export.
    """
    offline_state = (
        state if state is not None else OfflineAuthenticationState(totp_active=totp_active)
    )
    hasher = OfflinePasswordHasher()
    crypto = OfflineAuthenticationCrypto()
    offline_clock = clock if clock is not None else OfflineAuthenticationClock()
    credentials: CredentialTransactionPort = OfflineCredentialStore(offline_state)
    sessions: WebSessionTransactionPort = OfflineSessionStore(offline_state)
    totp_transactions: TotpTransactionPort = OfflineTotpStore(offline_state)
    totp_service = TotpService(
        transactions=totp_transactions,
        sessions=sessions,
        hasher=hasher,
        crypto=crypto,
        master_key=_OFFLINE_MASTER_KEY,
        clock=offline_clock,
        secret_codec=OfflineTotpSecretCodec(),
        throttle_policy=_OFFLINE_THROTTLE_POLICY,
        session_policy=_OFFLINE_SESSION_POLICY,
    )
    session_service = SessionService(
        sessions=sessions,
        hasher=hasher,
        crypto=crypto,
        master_key=_OFFLINE_MASTER_KEY,
        clock=offline_clock,
        session_policy=_OFFLINE_SESSION_POLICY,
        totp_leg=totp_service,
    )
    device_grants: DeviceAuthorizationTransactionPort = OfflineDeviceAuthorizationStore(
        offline_state
    )
    return WebAuthenticationRuntime(
        allowed_origin=OFFLINE_WEB_ALLOWED_ORIGIN,
        cookie_contract=build_session_cookie_contract(
            OFFLINE_WEB_ALLOWED_ORIGIN, RuntimeEnvironment.TEST
        ),
        resolve_client_address=create_client_address_resolver(trusted_proxy_cidrs),
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
        totp_service=totp_service,
        device_authorization_service=DeviceAuthorizationService(
            grants=device_grants,
            session_service=session_service,
            crypto=crypto,
            master_key=_OFFLINE_MASTER_KEY,
            clock=offline_clock,
            plugin_version_bounds=OFFLINE_PLUGIN_VERSION_BOUNDS,
            verification_base_url=OFFLINE_WEB_ALLOWED_ORIGIN,
            session_policy=_OFFLINE_SESSION_POLICY,
        ),
        device_token_service=DeviceTokenService(
            exchange=OfflineDeviceAuthorizationStore(offline_state),
            tokens=OfflineDeviceTokenStore(offline_state),
            keyring=OfflineDeviceTokenKeyring(),
            crypto=crypto,
            clock=offline_clock,
        ),
        device_administration_service=DeviceAdministrationService(
            tokens=OfflineDeviceTokenStore(offline_state),
            session_service=session_service,
            clock=offline_clock,
        ),
        verify_csrf_token=_build_csrf_verifier(crypto, _OFFLINE_MASTER_KEY),
    )
