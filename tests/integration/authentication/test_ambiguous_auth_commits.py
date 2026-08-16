"""Ambiguous commit acknowledgements on a real stack (spec 12.2, 13.4).

The network, not the database, decides whether the client sees the response
of a committed exchange or rotation. Every test drives the real
:class:`DeviceTokenService` exchange and rotation transactions over a
disposable PostgreSQL 18.4 stack upgraded to the authentication head and
injects the ambiguous case directly: the commit succeeded, the client lost
the acknowledgement, and the retry arrives. The tests prove the binding
resolution contracts of design sections 12.2 and 13.4: a lost exchange
acknowledgement re-derives the byte-identical credentials with the original
anchored timestamps and commits no second row; a lost refresh acknowledgement
re-renders the exact committed successor with no duplicate rows or extra
audit; and a genuinely new rotation identity on a rotated predecessor — the
one ambiguous case that cannot replay — resolves deterministically as
confirmed reuse with the family revoked, never as a silent second successor.
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
from postgresql_source_store.device_authorization_store import (
    DEVICE_TOKEN_FAMILY_CREATED_AUDIT_ACTION,
    DeviceAuthorizationStore,
)
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
        return True

    def needs_rehash(self, password_hash: str) -> bool:
        return False


class StaticHmacCrypto:
    """Deterministic crypto double deriving stable subkeys."""

    def derive_subkey(self, *, master_key: bytes, label: str) -> bytes:
        return hashlib.sha256(label.encode("ascii") + master_key).digest()

    def hmac_sha256(self, *, key: bytes, message: bytes) -> bytes:
        import hmac

        return hmac.new(key, message, hashlib.sha256).digest()

    def seal_secret(self, *, key: bytes, plaintext: bytes) -> tuple[bytes, bytes]:
        raise AssertionError("the ambiguity harness never seals")

    def open_secret(self, *, key: bytes, nonce: bytes, ciphertext: bytes) -> bytes:
        raise AssertionError("the ambiguity harness never opens")


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


class AmbiguityHarness:
    """Real exchange/rotation services over the disposable stack."""

    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine
        self.clock = FixedClock()
        self.crypto = StaticHmacCrypto()
        self.grant_store = DeviceAuthorizationStore(engine)
        self.token_store = DeviceTokenStore(engine)
        self.session_service = SessionService(
            sessions=WebSessionStore(engine),
            hasher=StaticPasswordHasher(),
            crypto=self.crypto,
            master_key=_MASTER_KEY,
            clock=self.clock,
        )
        self.grant_service = DeviceAuthorizationService(
            grants=self.grant_store,
            session_service=self.session_service,
            crypto=self.crypto,
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
            keyring=FixedKeyring(),
            crypto=self.crypto,
            clock=self.clock,
        )

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
                    user_id=user_id,
                    username=f"ambiguity-owner-{uuid4().hex[:10]}",
                    display_name="Ambiguity Owner",
                )
            )
            await connection.execute(
                sa.insert(workspaces).values(
                    workspace_id=workspace_id,
                    owner_user_id=user_id,
                    workspace_key=f"ws-{uuid4().hex[:12]}",
                    display_name="Ambiguity Workspace",
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
                        f"ambiguity-session-{web_session_id.hex}".encode()
                    ).hexdigest(),
                    csrf_secret_hash=hashlib.sha256(
                        f"ambiguity-csrf-{web_session_id.hex}".encode()
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

    async def create_approved_grant(self, account: SeededAccount) -> Any:
        created = await self.grant_service.create_grant(
            client_instance_id=uuid4(),
            device_name="Ambiguity Desktop",
            platform_class=DevicePlatformClass.OBSIDIAN_DESKTOP,
            platform_name="windows",
            plugin_version="1.4.0",
            requested_scope=DeviceScope.OBSIDIAN_SYNC,
            source_bucket=f"ambiguity-source-{uuid4().hex[:10]}",
        )
        await self.grant_store.approve_grant(
            ApproveGrantCommand(
                grant_id=created.grant_id,
                user_id=account.user_id,
                workspace_id=account.workspace_id,
                web_session_id=account.web_session_id,
                database_now=self.database_now,
                diagnostic_context=self.diagnostic_context(),
            )
        )
        return created

    async def poll(self, grant_id: UUID, polling_credential: str) -> Any:
        return await self.token_service.exchange_grant(
            grant_id=grant_id,
            polling_credential=polling_credential,
            diagnostic_context=self.diagnostic_context(),
        )

    async def refresh(self, refresh_credential: str, rotation_id: UUID) -> Any:
        return await self.token_service.refresh(
            refresh_credential=refresh_credential,
            rotation_id=rotation_id,
            diagnostic_context=self.diagnostic_context(),
        )

    async def scalar_count(self, statement: sa.sql.Select) -> int:
        async with self.engine.connect() as connection:
            return int(await connection.scalar(statement) or 0)

    async def token_rows(self, token_family_id: UUID) -> list[Any]:
        statement = sa.select(device_tokens).where(
            device_tokens.c.token_family_id == token_family_id
        )
        async with self.engine.connect() as connection:
            return list((await connection.execute(statement)).all())

    async def family_row(self, token_family_id: UUID) -> Any:
        async with self.engine.connect() as connection:
            return (
                await connection.execute(
                    sa.select(device_token_families).where(
                        device_token_families.c.token_family_id == token_family_id
                    )
                )
            ).one_or_none()

    async def device_count(self, workspace_id: UUID) -> int:
        return await self.scalar_count(
            sa.select(sa.func.count())
            .select_from(devices)
            .where(devices.c.workspace_id == workspace_id)
        )

    async def family_audit_rows(self, token_family_id: UUID) -> list[Any]:
        statement = sa.select(audit_events).where(audit_events.c.target_id == token_family_id)
        async with self.engine.connect() as connection:
            return list((await connection.execute(statement)).all())


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
async def harness(upgraded_authentication_stack: Any) -> AmbiguityHarness:
    settings: DatabaseRuntimeSettings = load_database_runtime_settings(
        environ=upgraded_authentication_stack.alembic_env
    )
    password = SecretStr(upgraded_authentication_stack.password.get_secret_value())
    engine = create_source_store_engine(settings, password)
    try:
        yield AmbiguityHarness(engine)
    finally:
        await dispose_source_store_engine(engine)


@pytest.mark.asyncio
async def test_lost_exchange_acknowledgement_replays_without_duplicates(
    harness: AmbiguityHarness,
) -> None:
    account = await harness.seed_account()
    created = await harness.create_approved_grant(account)

    committed = await harness.poll(created.grant_id, created.polling_secret)
    # The commit succeeded but the acknowledgement was lost; the client
    # re-polls, later in time, with the same one-time polling credential.
    harness.clock.database_now_value += timedelta(minutes=4)
    retried = await harness.poll(created.grant_id, created.polling_secret)

    assert retried.access_credential == committed.access_credential
    assert retried.refresh_credential == committed.refresh_credential
    assert retried.access_expires_at == committed.access_expires_at
    assert retried.refresh_expires_at == committed.refresh_expires_at
    assert retried.device_id == committed.device_id
    assert retried.refresh_generation == committed.refresh_generation

    assert await harness.device_count(account.workspace_id) == 1
    token_rows = await harness.token_rows(committed.token_family_id)
    assert len(token_rows) == 2
    # Only hashes exist: neither credential string is stored anywhere.
    for row in token_rows:
        assert len(row.secret_hash) == 64
        assert committed.access_credential not in row.secret_hash
        assert committed.refresh_credential not in row.secret_hash
    assert [row.action for row in await harness.family_audit_rows(committed.token_family_id)] == [
        DEVICE_TOKEN_FAMILY_CREATED_AUDIT_ACTION
    ]


@pytest.mark.asyncio
async def test_lost_refresh_acknowledgement_replays_the_committed_successor(
    harness: AmbiguityHarness,
) -> None:
    account = await harness.seed_account()
    created = await harness.create_approved_grant(account)
    exchanged = await harness.poll(created.grant_id, created.polling_secret)
    predecessor_credential = exchanged.refresh_credential

    rotated = await harness.refresh(predecessor_credential, ROTATION_A)
    assert rotated.refresh_generation == 2
    # The rotation committed but the acknowledgement was lost; the retry
    # presents the same predecessor under the same rotation identity.
    harness.clock.database_now_value += timedelta(minutes=3)
    retried = await harness.refresh(predecessor_credential, ROTATION_A)

    assert retried.access_credential == rotated.access_credential
    assert retried.refresh_credential == rotated.refresh_credential
    assert retried.access_expires_at == rotated.access_expires_at
    assert retried.refresh_expires_at == rotated.refresh_expires_at
    assert retried.refresh_generation == rotated.refresh_generation == 2

    # Exactly one predecessor and one successor of each kind: four token
    # rows, no duplicates, and no second family-creation audit.
    token_rows = await harness.token_rows(exchanged.token_family_id)
    assert len(token_rows) == 4
    assert [row.action for row in await harness.family_audit_rows(exchanged.token_family_id)] == [
        DEVICE_TOKEN_FAMILY_CREATED_AUDIT_ACTION
    ]
    family = await harness.family_row(exchanged.token_family_id)
    assert family is not None
    assert family.state == "active"
    assert family.current_refresh_generation == 2


@pytest.mark.asyncio
async def test_new_rotation_identity_after_a_lost_acknowledgement_is_confirmed_reuse(
    harness: AmbiguityHarness,
) -> None:
    account = await harness.seed_account()
    created = await harness.create_approved_grant(account)
    exchanged = await harness.poll(created.grant_id, created.polling_secret)
    predecessor_credential = exchanged.refresh_credential

    rotated = await harness.refresh(predecessor_credential, ROTATION_A)
    # The client lost the acknowledgement, abandoned the retry identity and
    # minted a fresh one: the only ambiguous presentation that cannot replay
    # resolves as confirmed reuse, never as a silent second successor.
    with pytest.raises(AuthenticationError) as raised:
        await harness.refresh(predecessor_credential, ROTATION_B)
    assert raised.value.error_code is ErrorCode.DEVICE_TOKEN_REUSE_DETECTED

    family = await harness.family_row(exchanged.token_family_id)
    assert family is not None
    assert family.state == "revoked"
    # The reuse revocation committed before the rejection surfaced, so the
    # committed successor is as dead as its predecessor.
    with pytest.raises(AuthenticationError) as successor_rejected:
        await harness.refresh(str(rotated.refresh_credential), ROTATION_B)
    assert successor_rejected.value.error_code in (
        ErrorCode.DEVICE_REVOKED,
        ErrorCode.DEVICE_TOKEN_REUSE_DETECTED,
    )
    token_rows = await harness.token_rows(exchanged.token_family_id)
    assert len(token_rows) == 4
    assert {str(row.state) for row in token_rows} <= {"revoked", "rotated"}
