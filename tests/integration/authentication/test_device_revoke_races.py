"""Device revocation races and transitions on a real stack (spec 13.4, 14).

Every test drives the real :class:`DeviceAuthorizationStore` exchange, the
real :class:`DeviceTokenStore` rotation, self-revoke and Admin revocation
transactions, and the real domain services over a disposable PostgreSQL 18.4
stack upgraded to the authentication head. Only the non-PostgreSQL ports stay
deterministic doubles. The tests prove the binding contracts of design
sections 14.1 and 14.2: one refresh rotation racing one Admin revoke — in
either commit order — always leaves the family revoked, the device revoked and
no usable token; the Admin revocation refuses a mismatched display-name
confirmation with the closed conflict code, denies grants that claim the
revoked device identity, writes exactly one device-revoke audit row and stays
idempotent on the read-only revoked row; the plugin self-revoke authenticates
the current refresh credential, revokes the family and every usable token
while the device record itself keeps its spec-14.2 state; and the Admin list
excludes the system bootstrap device while joining the exchanged grant's
validated platform/plugin metadata.
"""

from __future__ import annotations

import asyncio
import hashlib
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from personal_os.authentication.contracts import DeviceScope
from personal_os.authentication.device_authorization import (
    ApproveGrantCommand,
    DeviceAuthorizationService,
    DevicePlatformClass,
    PluginVersionBounds,
)
from personal_os.authentication.device_tokens import (
    DEVICE_REVOKED_AUDIT_ACTION,
    DEVICE_TOKEN_FAMILY_REVOKED_AUDIT_ACTION,
    DeviceAdministrationService,
    DeviceTokenService,
)
from personal_os.authentication.errors import AuthenticationError
from personal_os.authentication.sessions import DUMMY_LOGIN_PHC_HASH, SessionService
from personal_os.diagnostics.context import DiagnosticContext, create_diagnostic_context
from personal_os.error_contracts.codes import ErrorCode
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
    audit_events,
    device_authorization_grants,
    device_token_families,
    device_tokens,
    devices,
    user_credentials,
    users,
    web_sessions,
    workspaces,
)
from postgresql_source_store.web_session_store import WebSessionStore

pytestmark = pytest.mark.local_stack

_WORKTREE_ROOT: Path = Path(__file__).resolve().parents[3]

_DATABASE_NOW = datetime(2026, 8, 16, 12, 0, 0, tzinfo=UTC)
_CORRECT_PASSWORD = "correct-horse-battery-staple"
_MASTER_KEY = bytes(range(32))
_DERIVATION_KEY_ID = "auth-key-current"
_VERIFICATION_BASE_URL = "https://web-admin.example"

ROTATION_A = UUID("00000000-0000-0000-0000-0000000000aa")
ROTATION_B = UUID("00000000-0000-0000-0000-0000000000bb")


class FixedClock:
    """Clock double pinning one controllable transaction timestamp."""

    def __init__(self) -> None:
        self.database_now_value = _DATABASE_NOW

    async def database_now(self) -> datetime:
        return self.database_now_value


class StaticPasswordHasher:
    """Hasher double satisfying the session-service constructor only."""

    def hash_password(self, password: str) -> str:
        return f"static${hashlib.sha256(password.encode('utf-8')).hexdigest()}"

    def verify_password(self, password_hash: str, password: str) -> bool:
        del password_hash
        return password == _CORRECT_PASSWORD

    def needs_rehash(self, password_hash: str) -> bool:
        return False


class StaticHmacCrypto:
    """Crypto double deriving stdlib subkeys for the service wiring."""

    def derive_subkey(self, *, master_key: bytes, label: str) -> bytes:
        return hashlib.sha256(label.encode("ascii") + master_key).digest()

    def hmac_sha256(self, *, key: bytes, message: bytes) -> bytes:
        raise AssertionError("the revoke harness never calls hmac_sha256")

    def seal_secret(self, *, key: bytes, plaintext: bytes) -> tuple[bytes, bytes]:
        raise AssertionError("the revoke harness never seals")

    def open_secret(self, *, key: bytes, nonce: bytes, ciphertext: bytes) -> bytes:
        raise AssertionError("the revoke harness never opens")


class FixedKeyring:
    """Keyring double anchoring every derivation to the fixed master key."""

    def current_key_id(self) -> str:
        return _DERIVATION_KEY_ID

    def keys_by_id(self) -> dict[str, bytes]:
        return {_DERIVATION_KEY_ID: _MASTER_KEY}


@dataclass(frozen=True, slots=True)
class SeededAccount:
    """The trusted user/workspace/credential graph one test operates on."""

    user_id: UUID
    workspace_id: UUID
    web_session_id: UUID
    session_secret: str


class RevokeRaceHarness:
    """Real grant/token/admin stores and services over the disposable stack."""

    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine
        self.clock = FixedClock()
        self.keyring = FixedKeyring()
        self.grant_store = DeviceAuthorizationStore(engine)
        self.token_store = DeviceTokenStore(engine)
        crypto = StaticHmacCrypto()
        self.crypto = crypto
        self.session_service = SessionService(
            sessions=WebSessionStore(engine),
            hasher=StaticPasswordHasher(),
            crypto=crypto,
            master_key=_MASTER_KEY,
            clock=self.clock,
        )
        self.grant_service = DeviceAuthorizationService(
            grants=self.grant_store,
            session_service=self.session_service,
            crypto=crypto,
            master_key=_MASTER_KEY,
            clock=self.clock,
            plugin_version_bounds=PluginVersionBounds.from_strings(
                minimum_plugin_version="1.0.0", maximum_plugin_version="2.0.0"
            ),
            verification_base_url=_VERIFICATION_BASE_URL,
        )
        self.token_service = DeviceTokenService(
            exchange=self.grant_store,
            tokens=self.token_store,
            keyring=self.keyring,
            crypto=crypto,
            clock=self.clock,
        )
        self.admin_service = DeviceAdministrationService(
            tokens=self.token_store,
            session_service=self.session_service,
            clock=self.clock,
        )
        self.username = f"revoke-owner-{uuid4().hex[:10]}"
        self.account: SeededAccount | None = None
        self.session_secret = f"session-{uuid4().hex}"
        self.access_credential: str | None = None
        self.refresh_credential: str | None = None
        self.device_id: UUID | None = None
        self.token_family_id: UUID | None = None

    @property
    def database_now(self) -> datetime:
        return self.clock.database_now_value

    @staticmethod
    def diagnostic_context() -> DiagnosticContext:
        return create_diagnostic_context().context

    async def seed_account(self) -> SeededAccount:
        user_id = uuid4()
        workspace_id = uuid4()
        web_session_id = uuid4()
        async with self.engine.begin() as connection:
            await connection.execute(
                sa.insert(users).values(
                    user_id=user_id, username=self.username, display_name="Revoke Owner"
                )
            )
            await connection.execute(
                sa.insert(workspaces).values(
                    workspace_id=workspace_id,
                    owner_user_id=user_id,
                    workspace_key=f"ws-{uuid4().hex[:12]}",
                    display_name="Revoke Workspace",
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
                        self.session_secret.encode("utf-8")
                    ).hexdigest(),
                    csrf_secret_hash=hashlib.sha256(
                        f"csrf-{web_session_id.hex}".encode()
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
        account = SeededAccount(
            user_id=user_id,
            workspace_id=workspace_id,
            web_session_id=web_session_id,
            session_secret=self.session_secret,
        )
        self.account = account
        return account

    async def seed_system_device(self) -> UUID:
        """Insert one bootstrap-kind device row the list must exclude."""
        assert self.account is not None
        device_id = uuid4()
        async with self.engine.begin() as connection:
            await connection.execute(
                sa.insert(devices).values(
                    device_id=device_id,
                    workspace_id=self.account.workspace_id,
                    user_id=self.account.user_id,
                    device_name="System bootstrap",
                    device_kind="system",
                    status="active",
                    registered_at=self.database_now,
                )
            )
        return device_id

    async def exchange(
        self, *, device_name: str = "Personal desktop", claimed_device_id: UUID | None = None
    ) -> Any:
        """Create, approve and exchange one grant; remember the credentials."""
        assert self.account is not None
        created = await self.grant_service.create_grant(
            client_instance_id=uuid4(),
            device_name=device_name,
            platform_class=DevicePlatformClass.OBSIDIAN_DESKTOP,
            platform_name="windows",
            plugin_version="1.4.0",
            requested_scope=DeviceScope.OBSIDIAN_SYNC,
            source_bucket=f"revoke-source-{uuid4().hex[:10]}",
            claimed_device_id=claimed_device_id,
        )
        await self.grant_store.approve_grant(
            ApproveGrantCommand(
                grant_id=created.grant_id,
                user_id=self.account.user_id,
                workspace_id=self.account.workspace_id,
                web_session_id=self.account.web_session_id,
                database_now=self.database_now,
                diagnostic_context=self.diagnostic_context(),
            )
        )
        exchanged = await self.token_service.exchange_grant(
            grant_id=created.grant_id,
            polling_credential=created.polling_secret,
            diagnostic_context=self.diagnostic_context(),
        )
        self.access_credential = exchanged.access_credential
        self.refresh_credential = exchanged.refresh_credential
        self.device_id = exchanged.device_id
        self.token_family_id = exchanged.token_family_id
        return exchanged

    async def refresh(self, rotation_id: UUID, refresh_credential: str | None = None) -> Any:
        """Present the remembered — or given — refresh credential for rotation."""
        presented = refresh_credential or self.refresh_credential
        assert presented is not None, "exchange before refreshing"
        rotated = await self.token_service.refresh(
            refresh_credential=presented,
            rotation_id=rotation_id,
            diagnostic_context=self.diagnostic_context(),
        )
        self.access_credential = rotated.access_credential
        self.refresh_credential = rotated.refresh_credential
        return rotated

    async def self_revoke(self, refresh_credential: str | None = None) -> Any:
        """Present the current refresh credential for self-revoke (14.2)."""
        presented = refresh_credential or self.refresh_credential
        assert presented is not None, "exchange before self-revoking"
        return await self.token_service.revoke_current(
            refresh_credential=presented,
            diagnostic_context=self.diagnostic_context(),
        )

    async def admin_revoke(
        self, *, device_name_confirmation: str = "Personal desktop", device_id: UUID | None = None
    ) -> Any:
        """Run one Admin device revocation behind the seeded session (14.1)."""
        assert self.account is not None
        target_device_id = device_id or self.device_id
        assert target_device_id is not None, "exchange before admin-revoking"
        return await self.admin_service.revoke_device(
            device_id=target_device_id,
            session_secret=self.account.session_secret,
            device_name_confirmation=device_name_confirmation,
            diagnostic_context=self.diagnostic_context(),
        )

    async def admin_list(self) -> Any:
        """List the workspace devices behind the seeded session."""
        assert self.account is not None
        return await self.admin_service.list_devices(session_secret=self.account.session_secret)

    async def any_usable_token(self) -> bool:
        """Whether the remembered access credential still authenticates."""
        presented = self.access_credential
        assert presented is not None, "exchange before checking usability"
        try:
            await self.token_service.authenticate_access(access_credential=presented)
        except AuthenticationError:
            return False
        return True

    async def family_row(self, token_family_id: UUID | None = None) -> Any:
        target_family_id = token_family_id or self.token_family_id
        assert target_family_id is not None
        async with self.engine.connect() as connection:
            return (
                await connection.execute(
                    sa.select(device_token_families).where(
                        device_token_families.c.token_family_id == target_family_id
                    )
                )
            ).one_or_none()

    async def device_row(self, device_id: UUID | None = None) -> Any:
        target_device_id = device_id or self.device_id
        assert target_device_id is not None
        async with self.engine.connect() as connection:
            return (
                await connection.execute(
                    sa.select(devices).where(devices.c.device_id == target_device_id)
                )
            ).one_or_none()

    async def token_states(self, token_family_id: UUID | None = None) -> list[str]:
        target_family_id = token_family_id or self.token_family_id
        assert target_family_id is not None
        async with self.engine.connect() as connection:
            return list(
                (
                    await connection.execute(
                        sa.select(device_tokens.c.state).where(
                            device_tokens.c.token_family_id == target_family_id
                        )
                    )
                ).scalars()
            )

    async def audit_actions(self, target_id: UUID) -> list[str]:
        statement = sa.select(audit_events.c.action).where(audit_events.c.target_id == target_id)
        async with self.engine.connect() as connection:
            return list((await connection.execute(statement)).scalars())

    async def grants_claiming_device(self, device_id: UUID | None = None) -> list[Any]:
        target_device_id = device_id or self.device_id
        assert target_device_id is not None
        async with self.engine.connect() as connection:
            return list(
                (
                    await connection.execute(
                        sa.select(device_authorization_grants).where(
                            device_authorization_grants.c.claimed_device_id == target_device_id
                        )
                    )
                ).all()
            )


async def race(*awaitables: Any) -> list[Any]:
    """Run the transitions concurrently, keeping rejections as values."""
    return list(await asyncio.gather(*awaitables, return_exceptions=True))


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
    assert upgrade.returncode == 0, f"alembic upgrade head failed: {upgrade.stdout}{upgrade.stderr}"
    return authentication_schema_stack


@pytest_asyncio.fixture
async def harness(upgraded_authentication_stack: Any) -> RevokeRaceHarness:
    settings: DatabaseRuntimeSettings = load_database_runtime_settings(
        environ=upgraded_authentication_stack.alembic_env
    )
    password = SecretStr(upgraded_authentication_stack.password.get_secret_value())
    engine = create_source_store_engine(settings, password)
    revoke_harness = RevokeRaceHarness(engine)
    try:
        await revoke_harness.seed_account()
        yield revoke_harness
    finally:
        await dispose_source_store_engine(engine)


@pytest_asyncio.fixture
async def exchanged(harness: RevokeRaceHarness) -> Any:
    """One exchanged family with its credentials remembered on the harness."""
    return await harness.exchange()


# --- refresh racing Admin revoke --------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_racing_admin_revoke_leaves_no_usable_token(
    harness: RevokeRaceHarness, exchanged: Any
) -> None:
    del exchanged
    outcomes = await race(harness.refresh(ROTATION_A), harness.admin_revoke())
    refresh_outcome, revoke_outcome = outcomes
    assert not isinstance(revoke_outcome, BaseException), revoke_outcome
    assert isinstance(refresh_outcome, AuthenticationError) or not isinstance(
        refresh_outcome, BaseException
    )

    family = await harness.family_row()
    assert family is not None
    assert family.state == "revoked"
    device = await harness.device_row()
    assert device is not None
    assert device.status == "revoked"
    assert "active" not in await harness.token_states()
    assert await harness.any_usable_token() is False


@pytest.mark.asyncio
async def test_refresh_after_admin_revoke_is_terminal_reuse(
    harness: RevokeRaceHarness, exchanged: Any
) -> None:
    del exchanged
    await harness.admin_revoke()
    with pytest.raises(AuthenticationError) as raised:
        await harness.refresh(ROTATION_A)
    assert raised.value.error_code is ErrorCode.DEVICE_TOKEN_REUSE_DETECTED
    assert await harness.any_usable_token() is False


@pytest.mark.asyncio
async def test_admin_revoke_after_refresh_still_kills_every_token(
    harness: RevokeRaceHarness, exchanged: Any
) -> None:
    del exchanged
    rotated = await harness.refresh(ROTATION_A)
    assert await harness.any_usable_token() is True
    await harness.admin_revoke()
    family = await harness.family_row()
    assert family is not None
    assert family.state == "revoked"
    assert family.revocation_reason == "admin_revoked"
    assert "active" not in await harness.token_states()
    assert await harness.any_usable_token() is False
    del rotated


# --- Admin revoke contract --------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_revoke_requires_the_exact_stored_device_name(
    harness: RevokeRaceHarness, exchanged: Any
) -> None:
    del exchanged
    with pytest.raises(AuthenticationError) as raised:
        await harness.admin_revoke(device_name_confirmation="wrong")
    assert raised.value.error_code is ErrorCode.DEVICE_REVOCATION_CONFIRMATION_INVALID
    family = await harness.family_row()
    assert family is not None
    assert family.state == "active"


@pytest.mark.asyncio
async def test_admin_revoke_denies_grants_claiming_the_device_identity(
    harness: RevokeRaceHarness, exchanged: Any
) -> None:
    assert harness.device_id is not None
    claimed = await harness.exchange(device_name="Second device")
    # Point one fresh pending grant at the first device's identity.
    pending_claim = await harness.grant_service.create_grant(
        client_instance_id=uuid4(),
        device_name="Third device",
        platform_class=DevicePlatformClass.OBSIDIAN_MOBILE,
        platform_name="android",
        plugin_version="1.4.0",
        requested_scope=DeviceScope.OBSIDIAN_SYNC,
        source_bucket=f"revoke-claim-{uuid4().hex[:10]}",
        claimed_device_id=exchanged.device_id,
    )
    revoked = await harness.admin_revoke(device_id=exchanged.device_id)
    assert revoked.revoked_at == harness.database_now

    claiming = await harness.grants_claiming_device(exchanged.device_id)
    assert [row.grant_id for row in claiming] == [pending_claim.grant_id]
    assert claiming[0].state == "denied"

    device = await harness.device_row(exchanged.device_id)
    assert device is not None
    assert device.status == "revoked"
    assert [
        action
        for action in await harness.audit_actions(exchanged.device_id)
        if action == DEVICE_REVOKED_AUDIT_ACTION
    ] == [DEVICE_REVOKED_AUDIT_ACTION]
    del claimed


@pytest.mark.asyncio
async def test_admin_revoke_is_idempotent_on_the_revoked_row(
    harness: RevokeRaceHarness, exchanged: Any
) -> None:
    first = await harness.admin_revoke()
    second = await harness.admin_revoke()
    assert second.revoked_at == first.revoked_at
    assert [
        action
        for action in await harness.audit_actions(exchanged.device_id)
        if action == DEVICE_REVOKED_AUDIT_ACTION
    ] == [DEVICE_REVOKED_AUDIT_ACTION]


@pytest.mark.asyncio
async def test_admin_revoke_of_an_unknown_device_fails_closed(harness: RevokeRaceHarness) -> None:
    with pytest.raises(AuthenticationError) as raised:
        await harness.admin_revoke(device_id=uuid4(), device_name_confirmation="any")
    assert raised.value.error_code is ErrorCode.DEVICE_CREDENTIAL_INVALID


# --- plugin self-revoke -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_self_revoke_revokes_the_family_but_keeps_the_device_record(
    harness: RevokeRaceHarness, exchanged: Any
) -> None:
    revoked = await harness.self_revoke()
    assert revoked.token_family_id == exchanged.token_family_id

    family = await harness.family_row()
    assert family is not None
    assert family.state == "revoked"
    assert family.revocation_reason == "self_revoked"
    assert "active" not in await harness.token_states()
    assert [
        action
        for action in await harness.audit_actions(exchanged.token_family_id)
        if action == DEVICE_TOKEN_FAMILY_REVOKED_AUDIT_ACTION
    ] == [DEVICE_TOKEN_FAMILY_REVOKED_AUDIT_ACTION]

    # Spec 14.2 revokes the device family; the device record itself keeps its
    # state for the Admin surface and audit lineage.
    device = await harness.device_row(exchanged.device_id)
    assert device is not None
    assert device.status == "active"

    assert await harness.any_usable_token() is False
    with pytest.raises(AuthenticationError) as repeated:
        await harness.self_revoke()
    assert repeated.value.error_code is ErrorCode.DEVICE_REVOKED


@pytest.mark.asyncio
async def test_self_revoke_of_a_stale_predecessor_confirms_reuse(
    harness: RevokeRaceHarness, exchanged: Any
) -> None:
    predecessor_credential = exchanged.refresh_credential
    rotated = await harness.refresh(ROTATION_A)
    del rotated
    with pytest.raises(AuthenticationError) as raised:
        await harness.self_revoke(refresh_credential=predecessor_credential)
    assert raised.value.error_code is ErrorCode.DEVICE_TOKEN_REUSE_DETECTED
    family = await harness.family_row(exchanged.token_family_id)
    assert family is not None
    assert family.state == "revoked"
    device = await harness.device_row(exchanged.device_id)
    assert device is not None
    assert device.status == "revoked"


# --- the Admin device list ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_list_excludes_the_bootstrap_and_joins_the_grant_metadata(
    harness: RevokeRaceHarness, exchanged: Any
) -> None:
    system_device_id = await harness.seed_system_device()
    listed = await harness.admin_list()
    device_ids = [entry.device_id for entry in listed]
    assert exchanged.device_id in device_ids
    assert system_device_id not in device_ids

    entry = next(candidate for candidate in listed if candidate.device_id == exchanged.device_id)
    assert entry.device_name == "Personal desktop"
    assert entry.platform_class == "obsidian_desktop"
    assert entry.platform_name == "windows"
    assert entry.plugin_version == "1.4.0"
    assert entry.status == "active"
    assert entry.registered_at == harness.database_now
    assert entry.last_seen_at is None
    assert entry.revoked_at is None
    assert entry.family_absolute_expires_at is not None


@pytest.mark.asyncio
async def test_admin_list_marks_revoked_devices_read_only(
    harness: RevokeRaceHarness, exchanged: Any
) -> None:
    await harness.admin_revoke()
    listed = await harness.admin_list()
    entry = next(candidate for candidate in listed if candidate.device_id == exchanged.device_id)
    assert entry.status == "revoked"
    assert entry.revoked_at == harness.database_now
