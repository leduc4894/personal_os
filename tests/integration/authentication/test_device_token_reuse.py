"""Confirmed device-token reuse revocation on a real stack (spec 13.5, 13.1).

Every test drives the real :class:`DeviceAuthorizationStore` exchange
transition and the real :class:`DeviceTokenStore` rotation and
access-authentication transactions through
:class:`personal_os.authentication.device_tokens.DeviceTokenService` over a
disposable PostgreSQL 18.4 stack upgraded to the authentication head. Only
the non-PostgreSQL ports stay deterministic doubles. The tests prove the
binding contracts of design sections 13.5 and 13.1: presenting one rotated
predecessor with a different rotation identity — or a predecessor whose
successor has already rotated again, or an expired current credential —
revokes the whole family: family row, every usable token and the device,
exactly one reuse audit row, and the closed ``device_token_reuse_detected``
rejection; a revoked family keeps answering with the same terminal code
without a second revocation; access authentication verifies the hash and the
token/family/device/user/workspace state on every request and updates the
device last-seen stamp at most once per five minutes.
"""

from __future__ import annotations

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
from personal_os.authentication.crypto import parse_refresh_credential
from personal_os.authentication.device_authorization import (
    ApproveGrantCommand,
    DeviceAuthorizationService,
    DevicePlatformClass,
    PluginVersionBounds,
)
from personal_os.authentication.device_tokens import DeviceTokenService
from personal_os.authentication.errors import AuthenticationError
from personal_os.authentication.sessions import DUMMY_LOGIN_PHC_HASH, SessionService
from personal_os.diagnostics.context import DiagnosticContext, create_diagnostic_context
from personal_os.error_contracts.codes import ErrorCode
from postgresql_source_store.device_authorization_store import DeviceAuthorizationStore
from postgresql_source_store.device_token_store import (
    DEVICE_TOKEN_REUSE_DETECTED_AUDIT_ACTION,
    DeviceTokenStore,
)
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
        raise AssertionError("the reuse harness never calls hmac_sha256")

    def seal_secret(self, *, key: bytes, plaintext: bytes) -> tuple[bytes, bytes]:
        raise AssertionError("the reuse harness never seals")

    def open_secret(self, *, key: bytes, nonce: bytes, ciphertext: bytes) -> bytes:
        raise AssertionError("the reuse harness never opens")


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


class ReuseHarness:
    """Real grant and token stores and services over the disposable stack."""

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
        self.username = f"reuse-owner-{uuid4().hex[:10]}"
        self.access_credential: str | None = None
        self.refresh_credential: str | None = None

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
                    user_id=user_id, username=self.username, display_name="Reuse Owner"
                )
            )
            await connection.execute(
                sa.insert(workspaces).values(
                    workspace_id=workspace_id,
                    owner_user_id=user_id,
                    workspace_key=f"ws-{uuid4().hex[:12]}",
                    display_name="Reuse Workspace",
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
                        f"session-{web_session_id.hex}".encode()
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
        return SeededAccount(
            user_id=user_id, workspace_id=workspace_id, web_session_id=web_session_id
        )

    async def exchange(self) -> Any:
        """Create, approve and exchange one grant; remember the credentials."""
        created = await self.grant_service.create_grant(
            client_instance_id=uuid4(),
            device_name="Personal desktop",
            platform_class=DevicePlatformClass.OBSIDIAN_DESKTOP,
            platform_name="windows",
            plugin_version="1.4.0",
            requested_scope=DeviceScope.OBSIDIAN_SYNC,
            source_bucket=f"reuse-source-{uuid4().hex[:10]}",
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
        return exchanged

    async def refresh(self, *, rotation_id: UUID, refresh_credential: str | None = None) -> Any:
        """Present one refresh credential for rotation without advancing it.

        The reuse module deliberately keeps the remembered predecessor: every
        confirmed-reuse case presents a rotated credential again.
        """
        presented = refresh_credential or self.refresh_credential
        assert presented is not None, "exchange before refreshing"
        return await self.token_service.refresh(
            refresh_credential=presented,
            rotation_id=rotation_id,
            diagnostic_context=self.diagnostic_context(),
        )

    async def authenticate(self, access_credential: str | None = None) -> Any:
        presented = access_credential or self.access_credential
        assert presented is not None, "exchange before authenticating"
        return await self.token_service.authenticate_access(access_credential=presented)

    async def current_family_row(self) -> Any:
        """The family row of the currently remembered refresh credential."""
        assert self.refresh_credential is not None
        parsed = parse_refresh_credential(self.refresh_credential)
        async with self.engine.connect() as connection:
            token_row = (
                await connection.execute(
                    sa.select(device_tokens.c.token_family_id, device_tokens.c.generation).where(
                        device_tokens.c.device_token_id == parsed.lookup_id
                    )
                )
            ).one()
            return (
                await connection.execute(
                    sa.select(device_token_families).where(
                        device_token_families.c.token_family_id == token_row.token_family_id
                    )
                )
            ).one_or_none()

    async def device_row(self, device_id: UUID) -> Any:
        async with self.engine.connect() as connection:
            return (
                await connection.execute(sa.select(devices).where(devices.c.device_id == device_id))
            ).one_or_none()

    async def audit_actions(self, target_id: UUID) -> list[str]:
        statement = sa.select(audit_events.c.action).where(audit_events.c.target_id == target_id)
        async with self.engine.connect() as connection:
            return list((await connection.execute(statement)).scalars())


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
async def harness(upgraded_authentication_stack: Any) -> ReuseHarness:
    settings: DatabaseRuntimeSettings = load_database_runtime_settings(
        environ=upgraded_authentication_stack.alembic_env
    )
    password = SecretStr(upgraded_authentication_stack.password.get_secret_value())
    engine = create_source_store_engine(settings, password)
    reuse_harness = ReuseHarness(engine)
    try:
        reuse_harness.account = await reuse_harness.seed_account()
        yield reuse_harness
    finally:
        await dispose_source_store_engine(engine)


@pytest_asyncio.fixture
async def exchanged(harness: ReuseHarness) -> Any:
    """One exchanged family with its credentials remembered on the harness."""
    return await harness.exchange()


# --- confirmed reuse -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_same_predecessor_different_rotation_revokes_family(
    harness: ReuseHarness, exchanged: Any
) -> None:
    del exchanged
    await harness.refresh(rotation_id=ROTATION_A)
    with pytest.raises(AuthenticationError) as raised:
        await harness.refresh(rotation_id=ROTATION_B)
    assert raised.value.error_code is ErrorCode.DEVICE_TOKEN_REUSE_DETECTED
    family = await harness.current_family_row()
    assert family is not None
    assert family.state == "revoked"


@pytest.mark.asyncio
async def test_reuse_revokes_every_usable_token_and_the_device_once(
    harness: ReuseHarness, exchanged: Any
) -> None:
    first_refresh = await harness.refresh(rotation_id=ROTATION_A)

    with pytest.raises(AuthenticationError) as raised:
        await harness.refresh(rotation_id=ROTATION_B)
    assert raised.value.error_code is ErrorCode.DEVICE_TOKEN_REUSE_DETECTED

    device = await harness.device_row(exchanged.device_id)
    assert device is not None
    assert device.status == "revoked"
    assert device.revoked_at == harness.database_now

    async with harness.engine.connect() as connection:
        token_states = list(
            (
                await connection.execute(
                    sa.select(device_tokens.c.state).where(
                        device_tokens.c.token_family_id == exchanged.token_family_id
                    )
                )
            ).scalars()
        )
    assert token_states
    # Every usable token is revoked; the already-rotated predecessor keeps its
    # terminal rotated state because the schema forbids concurrent rotated and
    # revoked timestamps on one row.
    assert "active" not in token_states
    assert set(token_states) <= {"revoked", "rotated"}
    assert "revoked" in token_states

    family = await harness.current_family_row()
    assert family is not None
    assert family.state == "revoked"
    assert family.revocation_reason == "token_reuse"
    # Exactly one reuse audit targets the family next to its exchange-time
    # family-creation row.
    assert [
        action
        for action in await harness.audit_actions(exchanged.token_family_id)
        if action == DEVICE_TOKEN_REUSE_DETECTED_AUDIT_ACTION
    ] == [DEVICE_TOKEN_REUSE_DETECTED_AUDIT_ACTION]

    # A further presentation keeps the terminal code without a second audit row.
    with pytest.raises(AuthenticationError) as repeated:
        await harness.refresh(rotation_id=ROTATION_B)
    assert repeated.value.error_code is ErrorCode.DEVICE_TOKEN_REUSE_DETECTED
    assert [
        action
        for action in await harness.audit_actions(exchanged.token_family_id)
        if action == DEVICE_TOKEN_REUSE_DETECTED_AUDIT_ACTION
    ] == [DEVICE_TOKEN_REUSE_DETECTED_AUDIT_ACTION]
    del first_refresh


@pytest.mark.asyncio
async def test_predecessor_after_successor_rotated_again_is_reuse(
    harness: ReuseHarness, exchanged: Any
) -> None:
    del exchanged
    first_refresh = await harness.refresh(rotation_id=ROTATION_A)
    # Rotate the successor once more; the replay window of the first rotation
    # closed with it.
    await harness.refresh(
        rotation_id=ROTATION_B, refresh_credential=first_refresh.refresh_credential
    )

    with pytest.raises(AuthenticationError) as raised:
        await harness.refresh(
            rotation_id=ROTATION_A, refresh_credential=first_refresh.refresh_credential
        )
    assert raised.value.error_code is ErrorCode.DEVICE_TOKEN_REUSE_DETECTED
    family = await harness.current_family_row()
    assert family is not None
    assert family.state == "revoked"


@pytest.mark.asyncio
async def test_expired_current_predecessor_presentation_revokes_family(
    harness: ReuseHarness, exchanged: Any
) -> None:
    del exchanged
    harness.clock.database_now_value += timedelta(days=31)

    with pytest.raises(AuthenticationError) as raised:
        await harness.refresh(rotation_id=ROTATION_A)
    assert raised.value.error_code is ErrorCode.DEVICE_TOKEN_REUSE_DETECTED
    family = await harness.current_family_row()
    assert family is not None
    assert family.state == "revoked"


# --- access authentication --------------------------------------------------------------


@pytest.mark.asyncio
async def test_access_authentication_resolves_context_and_throttles_last_seen(
    harness: ReuseHarness, exchanged: Any
) -> None:
    first = await harness.authenticate()
    assert first.context.device_id == exchanged.device_id
    assert first.context.scope.value == "obsidian_sync"
    device = await harness.device_row(exchanged.device_id)
    assert device is not None
    assert device.last_seen_at == harness.database_now

    harness.clock.database_now_value += timedelta(minutes=2)
    await harness.authenticate()
    unchanged = await harness.device_row(exchanged.device_id)
    assert unchanged is not None
    assert unchanged.last_seen_at == device.last_seen_at

    harness.clock.database_now_value += timedelta(minutes=4)
    await harness.authenticate()
    updated = await harness.device_row(exchanged.device_id)
    assert updated is not None
    assert updated.last_seen_at == harness.database_now


@pytest.mark.asyncio
async def test_access_authentication_closes_expired_credentials(
    harness: ReuseHarness, exchanged: Any
) -> None:
    del exchanged
    harness.clock.database_now_value += timedelta(minutes=16)

    with pytest.raises(AuthenticationError) as raised:
        await harness.authenticate()
    assert raised.value.error_code is ErrorCode.DEVICE_CREDENTIAL_INVALID


@pytest.mark.asyncio
async def test_access_authentication_reports_revoked_surfaces(
    harness: ReuseHarness, exchanged: Any
) -> None:
    await harness.refresh(rotation_id=ROTATION_A)
    with pytest.raises(AuthenticationError):
        await harness.refresh(rotation_id=ROTATION_B)

    with pytest.raises(AuthenticationError) as raised:
        await harness.authenticate(exchanged.access_credential)
    assert raised.value.error_code is ErrorCode.DEVICE_REVOKED


@pytest.mark.asyncio
async def test_refresh_rejects_unknown_credentials_without_revoking(
    harness: ReuseHarness, exchanged: Any
) -> None:
    del exchanged
    unknown = f"rt1.{uuid4()}.{bytes(range(32)).hex()}"
    with pytest.raises(AuthenticationError) as raised:
        await harness.refresh(rotation_id=ROTATION_A, refresh_credential=unknown)
    assert raised.value.error_code is ErrorCode.DEVICE_CREDENTIAL_INVALID
    family = await harness.current_family_row()
    assert family is not None
    assert family.state == "active"
