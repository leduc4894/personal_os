"""Keyring rotation, TOTP re-encryption and coverage refusal on a real stack.

Every test drives the real keyring-aware adapters over a disposable
PostgreSQL 18.4 stack upgraded to the authentication head: the real
AES-256-GCM/HKDF codec adapter behind :class:`KeyringTotpSecretCodec`, the
real :class:`TotpStore` re-encryption transaction and the real
:class:`CredentialStore.required_key_ids` coverage read that the serve
process consults before its listening socket is exposed (spec 20.1). The
tests prove the rotation lifecycle of design sections 15.9 and 20.1: a
previous-key TOTP secret is decrypted with its anchored key and re-sealed
under the current key inside the same locked verification transaction; a
device family anchored to a previous derivation key keeps rendering its
byte-identical credentials after the current key rotated; every referenced
key ID — active TOTP ciphertext, unexpired refresh tokens and unexpired
grants with replay state — enters the required set; and a keyring omitting
one referenced key refuses startup with the fixed safe reason token while a
complete keyring passes. Once the previous key's last reference is
re-encrypted, the required set drops it and the key may be removed.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from api_runtime.authentication_composition import (
    KeyringTotpSecretCodec,
    assert_keyring_covers_required_key_ids,
    verify_keyring_covers_required_key_ids,
)
from api_runtime.authentication_crypto import (
    AuthenticationKeyring,
    CryptographyAuthenticationCrypto,
)
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from personal_os.authentication.contracts import DeviceScope
from personal_os.authentication.crypto import TOTP_SECRET_AEAD_LABEL
from personal_os.authentication.device_authorization import (
    ApproveGrantCommand,
    DeviceAuthorizationService,
    DevicePlatformClass,
    PluginVersionBounds,
)
from personal_os.authentication.device_tokens import DeviceTokenService
from personal_os.authentication.sessions import DUMMY_LOGIN_PHC_HASH, SessionService
from personal_os.authentication.totp import (
    SealedTotpSecret,
    VerifyTotpCommand,
    time_step_of,
    totp_code,
)
from personal_os.diagnostics.context import create_diagnostic_context
from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import ConfigurationError
from postgresql_source_store.authentication_credentials import CredentialStore
from postgresql_source_store.device_authorization_store import DeviceAuthorizationStore
from postgresql_source_store.device_token_store import DeviceTokenStore
from postgresql_source_store.engine import (
    create_source_store_engine,
    dispose_source_store_engine,
)
from postgresql_source_store.settings import (
    DatabaseRuntimeSettings,
    load_database_runtime_settings,
)
from postgresql_source_store.tables import (
    device_authorization_grants,
    device_tokens,
    totp_credentials,
    user_credentials,
    users,
    web_sessions,
    workspaces,
)
from postgresql_source_store.totp_store import TotpStore
from postgresql_source_store.web_session_store import WebSessionStore

pytestmark = pytest.mark.local_stack

_WORKTREE_ROOT: Path = Path(__file__).resolve().parents[3]

_DATABASE_NOW = datetime(2026, 8, 16, 12, 0, 0, tzinfo=UTC)
_DATABASE_NOW_UNIX_SECONDS = int(_DATABASE_NOW.timestamp())
_CURRENT_STEP = time_step_of(unix_time_seconds=_DATABASE_NOW_UNIX_SECONDS)
_TOTP_SECRET = bytes(range(20, 40))
_CODE_AT_NOW = totp_code(secret=_TOTP_SECRET, unix_time_seconds=_DATABASE_NOW_UNIX_SECONDS)
_ROTATED_KEY_ID = "authkey-rotated-2081"
_RETIRED_KEY_ID = "authkey-retired-2077"
_ROTATED_MASTER_KEY = bytes(range(32))
_RETIRED_MASTER_KEY = bytes(range(64, 96))


class FixedClock:
    """Clock double pinning one controllable transaction timestamp."""

    def __init__(self) -> None:
        self.database_now_value = _DATABASE_NOW

    async def database_now(self) -> datetime:
        return self.database_now_value


class StaticPasswordHasher:
    """Hasher double satisfying the service constructors only."""

    def hash_password(self, password: str) -> str:
        return f"static${password}"

    def verify_password(self, password_hash: str, password: str) -> bool:
        return password_hash == f"static${password}"

    def needs_rehash(self, password_hash: str) -> bool:
        return False


class StaticHmacCrypto:
    """Deterministic crypto double deriving stable subkeys per master key."""

    def derive_subkey(self, *, master_key: bytes, label: str) -> bytes:
        import hashlib

        return hashlib.sha256(label.encode("ascii") + master_key).digest()

    def hmac_sha256(self, *, key: bytes, message: bytes) -> bytes:
        import hashlib
        import hmac

        return hmac.new(key, message, hashlib.sha256).digest()

    def seal_secret(self, *, key: bytes, plaintext: bytes) -> tuple[bytes, bytes]:
        raise AssertionError("sealing goes through the TOTP codec adapter")

    def open_secret(self, *, key: bytes, nonce: bytes, ciphertext: bytes) -> bytes:
        raise AssertionError("opening goes through the TOTP codec adapter")


def _two_key_keyring(*, current_key_id: str) -> AuthenticationKeyring:
    return AuthenticationKeyring(
        current_key_id=current_key_id,
        keys_by_id=MappingProxyType(
            {
                _RETIRED_KEY_ID: _RETIRED_MASTER_KEY,
                _ROTATED_KEY_ID: _ROTATED_MASTER_KEY,
            }
        ),
    )


class TwoKeyTokenKeyring:
    """Token-derivation keyring view carrying both rotation generations."""

    def __init__(self, current_key_id: str) -> None:
        self._keyring = _two_key_keyring(current_key_id=current_key_id)

    def current_key_id(self) -> str:
        return self._keyring.current_key_id

    def keys_by_id(self) -> MappingProxyType[str, bytes]:
        return self._keyring.keys_by_id


@dataclass(frozen=True, slots=True)
class SeededAccount:
    """The trusted user/workspace/credential graph one test operates on."""

    user_id: UUID
    workspace_id: UUID
    web_session_id: UUID


class KeyRotationHarness:
    """Real keyring-aware stores and services over the disposable stack."""

    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine
        self.clock = FixedClock()
        self.hasher = StaticPasswordHasher()
        self.aead_crypto = CryptographyAuthenticationCrypto()
        self.secret_codec = KeyringTotpSecretCodec(
            self.aead_crypto, _two_key_keyring(current_key_id=_ROTATED_KEY_ID)
        )
        self.totp_store = TotpStore(engine, secret_codec=self.secret_codec)
        self.credentials = CredentialStore(engine)
        self.username = f"rotation-owner-{uuid4().hex[:10]}"

    @property
    def database_now(self) -> datetime:
        return self.clock.database_now_value

    @staticmethod
    def diagnostic_context() -> Any:
        return create_diagnostic_context().context

    def seal_under_key(self, *, key_id: str, plaintext: bytes) -> SealedTotpSecret:
        """Seal one secret under an explicit keyring key, mirroring the codec."""
        master_key = _two_key_keyring(current_key_id=key_id).keys_by_id[key_id]
        subkey = self.aead_crypto.derive_subkey(master_key=master_key, label=TOTP_SECRET_AEAD_LABEL)
        import base64

        nonce, ciphertext = self.aead_crypto.seal_secret(key=subkey, plaintext=plaintext)
        return SealedTotpSecret(
            key_id=key_id,
            nonce=base64.b64encode(nonce).decode("ascii"),
            ciphertext=base64.b64encode(ciphertext).decode("ascii"),
        )

    def open_sealed(self, sealed: SealedTotpSecret) -> bytes:
        master_key = _two_key_keyring(current_key_id=_ROTATED_KEY_ID).keys_by_id[sealed.key_id]
        subkey = self.aead_crypto.derive_subkey(master_key=master_key, label=TOTP_SECRET_AEAD_LABEL)
        import base64

        return self.aead_crypto.open_secret(
            key=subkey,
            nonce=base64.b64decode(sealed.nonce.encode("ascii")),
            ciphertext=base64.b64decode(sealed.ciphertext.encode("ascii")),
        )

    async def seed_account(self) -> SeededAccount:
        import hashlib

        user_id = uuid4()
        workspace_id = uuid4()
        web_session_id = uuid4()
        async with self.engine.begin() as connection:
            await connection.execute(
                sa.insert(users).values(
                    user_id=user_id, username=self.username, display_name="Rotation Owner"
                )
            )
            await connection.execute(
                sa.insert(workspaces).values(
                    workspace_id=workspace_id,
                    owner_user_id=user_id,
                    workspace_key=f"ws-{uuid4().hex[:12]}",
                    display_name="Rotation Workspace",
                )
            )
            await connection.execute(
                sa.insert(user_credentials).values(
                    user_id=user_id,
                    workspace_id=workspace_id,
                    password_hash=DUMMY_LOGIN_PHC_HASH,
                    credential_revision=1,
                    created_at=self.database_now,
                    updated_at=self.database_now,
                    password_changed_at=self.database_now,
                )
            )
            await connection.execute(
                sa.insert(web_sessions).values(
                    web_session_id=web_session_id,
                    user_id=user_id,
                    workspace_id=workspace_id,
                    session_secret_hash=hashlib.sha256(
                        f"rotation-session-{web_session_id.hex}".encode()
                    ).hexdigest(),
                    csrf_secret_hash=hashlib.sha256(
                        f"rotation-csrf-{web_session_id.hex}".encode()
                    ).hexdigest(),
                    state="active",
                    credential_revision=1,
                    authentication_method="password",
                    created_at=self.database_now,
                    authenticated_at=self.database_now,
                    reauthenticated_at=self.database_now,
                    idle_expires_at=self.database_now + timedelta(hours=12),
                    absolute_expires_at=self.database_now + timedelta(days=7),
                )
            )
        return SeededAccount(
            user_id=user_id, workspace_id=workspace_id, web_session_id=web_session_id
        )

    async def insert_active_totp_under_retired_key(self, account: SeededAccount) -> UUID:
        """Insert one active TOTP row whose ciphertext anchors the retired key."""
        sealed = self.seal_under_key(key_id=_RETIRED_KEY_ID, plaintext=_TOTP_SECRET)
        totp_credential_id = uuid4()
        async with self.engine.begin() as connection:
            await connection.execute(
                sa.insert(totp_credentials).values(
                    totp_credential_id=totp_credential_id,
                    user_id=account.user_id,
                    workspace_id=account.workspace_id,
                    state="active",
                    secret_ciphertext=sealed.ciphertext,
                    secret_nonce=sealed.nonce,
                    key_id=_RETIRED_KEY_ID,
                    algorithm="SHA1",
                    digits=6,
                    period_seconds=30,
                    revision=1,
                    created_at=self.database_now - timedelta(days=1),
                    activated_at=self.database_now - timedelta(days=1),
                )
            )
        return totp_credential_id

    async def exchange_device_under_retired_key(self, account: SeededAccount) -> tuple[Any, str]:
        """Exchange one grant whose derivation anchors the retired key."""
        grant_store = DeviceAuthorizationStore(self.engine)
        token_store = DeviceTokenStore(self.engine)
        crypto = StaticHmacCrypto()
        session_service = SessionService(
            sessions=WebSessionStore(self.engine),
            hasher=self.hasher,
            crypto=crypto,
            master_key=_RETIRED_MASTER_KEY,
            clock=self.clock,
        )
        grant_service = DeviceAuthorizationService(
            grants=grant_store,
            session_service=session_service,
            crypto=crypto,
            master_key=_RETIRED_MASTER_KEY,
            clock=self.clock,
            plugin_version_bounds=PluginVersionBounds.from_strings(
                minimum_plugin_version="1.0.0", maximum_plugin_version="2.0.0"
            ),
            verification_base_url="https://web-admin.example",
        )
        created = await grant_service.create_grant(
            client_instance_id=uuid4(),
            device_name="Rotation Desktop",
            platform_class=DevicePlatformClass.OBSIDIAN_DESKTOP,
            platform_name="windows",
            plugin_version="1.4.0",
            requested_scope=DeviceScope.OBSIDIAN_SYNC,
            source_bucket=f"rotation-source-{uuid4().hex[:10]}",
        )
        await grant_store.approve_grant(
            ApproveGrantCommand(
                grant_id=created.grant_id,
                user_id=account.user_id,
                workspace_id=account.workspace_id,
                web_session_id=account.web_session_id,
                database_now=self.database_now,
                diagnostic_context=self.diagnostic_context(),
            )
        )
        token_service = DeviceTokenService(
            exchange=grant_store,
            tokens=token_store,
            keyring=TwoKeyTokenKeyring(current_key_id=_RETIRED_KEY_ID),
            crypto=crypto,
            clock=self.clock,
        )
        exchanged = await token_service.exchange_grant(
            grant_id=created.grant_id,
            polling_credential=created.polling_secret,
            diagnostic_context=self.diagnostic_context(),
        )
        return created, exchanged.refresh_credential

    async def required_key_ids(self) -> frozenset[str]:
        return await self.credentials.required_key_ids(database_now=datetime.now(UTC))

    async def totp_row(self, totp_credential_id: UUID) -> Any:
        async with self.engine.connect() as connection:
            return (
                await connection.execute(
                    sa.select(totp_credentials).where(
                        totp_credentials.c.totp_credential_id == totp_credential_id
                    )
                )
            ).one()

    async def refresh_rows_key_ids(self, account: SeededAccount) -> set[str]:
        statement = sa.select(device_tokens.c.derivation_key_id).where(
            device_tokens.c.user_id == account.user_id,
            device_tokens.c.token_kind == "refresh",
        )
        async with self.engine.connect() as connection:
            return {str(row.derivation_key_id) for row in (await connection.execute(statement))}

    async def grant_derivation_key_ids(self, account: SeededAccount) -> set[str]:
        """The derivation keys of this account's grants (order-independent)."""
        statement = sa.select(device_authorization_grants.c.derivation_key_id).where(
            device_authorization_grants.c.approved_by_user_id == account.user_id,
            device_authorization_grants.c.derivation_key_id.is_not(None),
        )
        async with self.engine.connect() as connection:
            return {
                str(row.derivation_key_id)
                for row in (await connection.execute(statement))
                if row.derivation_key_id is not None
            }


@pytest.fixture(scope="module")
def upgraded_authentication_stack(authentication_schema_stack: Any) -> Any:
    """Upgrade the disposable stack to the authentication head once per module."""
    upgrade = subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        cwd=str(_WORKTREE_ROOT),
        env=authentication_schema_stack.alembic_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert upgrade.returncode == 0, f"alembic upgrade head failed:{upgrade.stdout}{upgrade.stderr}"
    return authentication_schema_stack


@pytest_asyncio.fixture
async def harness(upgraded_authentication_stack: Any) -> KeyRotationHarness:
    settings: DatabaseRuntimeSettings = load_database_runtime_settings(
        environ=upgraded_authentication_stack.alembic_env
    )
    password = SecretStr(upgraded_authentication_stack.password.get_secret_value())
    engine = create_source_store_engine(settings, password)
    try:
        yield KeyRotationHarness(engine)
    finally:
        await dispose_source_store_engine(engine)


@pytest.mark.asyncio
async def test_previous_key_totp_secret_reencrypts_under_the_current_key(
    harness: KeyRotationHarness,
) -> None:
    account = await harness.seed_account()
    credential_id = await harness.insert_active_totp_under_retired_key(account)

    verified = await harness.totp_store.verify_totp(
        VerifyTotpCommand(
            user_id=account.user_id,
            submitted_code=_CODE_AT_NOW,
            unix_time_seconds=_DATABASE_NOW_UNIX_SECONDS,
            database_now=harness.database_now,
            reset_bucket_hash=None,
            diagnostic_context=harness.diagnostic_context(),
        )
    )
    assert verified.accepted_time_step == _CURRENT_STEP
    assert verified.was_reencrypted is True

    row = await harness.totp_row(credential_id)
    assert row.key_id == _ROTATED_KEY_ID
    reopened = harness.open_sealed(
        SealedTotpSecret(
            key_id=row.key_id, nonce=row.secret_nonce, ciphertext=row.secret_ciphertext
        )
    )
    assert reopened == _TOTP_SECRET


@pytest.mark.asyncio
async def test_retired_key_leaves_required_coverage_after_reencryption(
    harness: KeyRotationHarness,
) -> None:
    account = await harness.seed_account()
    credential_id = await harness.insert_active_totp_under_retired_key(account)

    required_before = await harness.required_key_ids()
    assert _RETIRED_KEY_ID in required_before

    await harness.totp_store.verify_totp(
        VerifyTotpCommand(
            user_id=account.user_id,
            submitted_code=_CODE_AT_NOW,
            unix_time_seconds=_DATABASE_NOW_UNIX_SECONDS,
            database_now=harness.database_now,
            reset_bucket_hash=None,
            diagnostic_context=harness.diagnostic_context(),
        )
    )
    del credential_id

    required_after = await harness.required_key_ids()
    # No earlier test in this module leaves a retired-key reference (the
    # re-encryption test re-sealed its row under the rotated key), and this
    # account just dropped the last one: the retired key is removable.
    assert _RETIRED_KEY_ID not in required_after
    # The re-encrypted row now references the rotated key only.
    assert required_after <= {_ROTATED_KEY_ID, _RETIRED_KEY_ID}
    assert _ROTATED_KEY_ID in required_after


@pytest.mark.asyncio
async def test_required_key_ids_cover_totp_refresh_tokens_and_grants(
    harness: KeyRotationHarness,
) -> None:
    account = await harness.seed_account()
    await harness.insert_active_totp_under_retired_key(account)
    _created, refresh_credential = await harness.exchange_device_under_retired_key(account)
    assert refresh_credential.startswith("rt1.")

    required = await harness.required_key_ids()
    assert _RETIRED_KEY_ID in required

    # The refresh row and the grant both anchor the retired derivation key,
    # and the TOTP ciphertext references it too: every surface agrees.
    assert await harness.refresh_rows_key_ids(account) == {_RETIRED_KEY_ID}
    assert await harness.grant_derivation_key_ids(account) == {_RETIRED_KEY_ID}


@pytest.mark.asyncio
async def test_missing_referenced_key_refuses_startup_before_bind(
    harness: KeyRotationHarness,
) -> None:
    account = await harness.seed_account()
    await harness.insert_active_totp_under_retired_key(account)
    required = await harness.required_key_ids()
    assert _RETIRED_KEY_ID in required

    incomplete_keyring = AuthenticationKeyring(
        current_key_id=_ROTATED_KEY_ID,
        keys_by_id=MappingProxyType({_ROTATED_KEY_ID: _ROTATED_MASTER_KEY}),
    )
    with pytest.raises(ConfigurationError) as raised:
        assert_keyring_covers_required_key_ids(required, incomplete_keyring)
    assert raised.value.error_code is ErrorCode.CONFIGURATION_SECRET_INVALID
    safe_details = dict(raised.value.safe_details)
    assert str(safe_details.get("reason")) == "keyring_missing_referenced_key"

    with pytest.raises(ConfigurationError):
        await verify_keyring_covers_required_key_ids(
            engine=harness.engine, keyring=incomplete_keyring
        )

    complete_keyring = _two_key_keyring(current_key_id=_ROTATED_KEY_ID)
    await verify_keyring_covers_required_key_ids(engine=harness.engine, keyring=complete_keyring)


@pytest.mark.asyncio
async def test_device_family_survives_a_current_key_transition(
    harness: KeyRotationHarness,
) -> None:
    account = await harness.seed_account()
    _created, refresh_credential = await harness.exchange_device_under_retired_key(account)

    # The deployment rotates: the retired derivation key is still carried,
    # but every NEW derivation now runs under the rotated current key.
    token_service = DeviceTokenService(
        exchange=DeviceAuthorizationStore(harness.engine),
        tokens=DeviceTokenStore(harness.engine),
        keyring=TwoKeyTokenKeyring(current_key_id=_ROTATED_KEY_ID),
        crypto=StaticHmacCrypto(),
        clock=harness.clock,
    )
    rotated = await token_service.refresh(
        refresh_credential=refresh_credential,
        rotation_id=UUID("00000000-0000-0000-0000-0000000000aa"),
        diagnostic_context=harness.diagnostic_context(),
    )
    assert rotated.refresh_generation == 2

    # The anchored predecessor replays through the retired key and the new
    # successor was derived under the rotated key: both render byte-exactly.
    replayed = await token_service.refresh(
        refresh_credential=refresh_credential,
        rotation_id=UUID("00000000-0000-0000-0000-0000000000aa"),
        diagnostic_context=harness.diagnostic_context(),
    )
    assert replayed.refresh_credential == rotated.refresh_credential
    assert replayed.access_credential == rotated.access_credential
    assert await harness.refresh_rows_key_ids(account) == {_RETIRED_KEY_ID, _ROTATED_KEY_ID}
