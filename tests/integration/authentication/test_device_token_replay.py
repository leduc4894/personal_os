"""Exact-replay exchange and refresh rotation on a real stack (spec 12.2, 13.4).

Every test drives the real :class:`DeviceAuthorizationStore` exchange
transition and the real :class:`DeviceTokenStore` rotation transactions
through :class:`personal_os.authentication.device_tokens.DeviceTokenService`
over a disposable PostgreSQL 18.4 stack upgraded to the authentication head.
Only the non-PostgreSQL ports stay deterministic doubles: derivations use the
pure stdlib HKDF/HMAC domain functions and the clock pins one transaction
timestamp so replay assertions are byte-exact. The tests prove the binding
contracts of design sections 12 and 13.4: one exchange creates exactly one
device, family, access token and refresh token with the grant anchors and the
two registration audit rows; a lost commit acknowledgement — the same polling
secret polled again — re-derives byte-identical credentials with the original
anchored timestamps and creates no new row; a keyring rotation between two
polls of the same grant keeps that replay exact instead of failing the
credential; the replay window closes once the
initial refresh generation rotates; a refresh retry with the same rotation
identity returns the exact committed successor with anchored timestamps and
no duplicate rows; and the poll route semantics report the five-second
pending interval and the closed slow-down outcome.
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
from personal_os.authentication.device_tokens import (
    INITIAL_REFRESH_GENERATION,
    DeviceTokenService,
)
from personal_os.authentication.errors import AuthenticationError
from personal_os.authentication.sessions import DUMMY_LOGIN_PHC_HASH, SessionService
from personal_os.error_contracts.codes import ErrorCode
from postgresql_source_store.device_authorization_store import (
    DEVICE_AUTHORIZATION_APPROVED_AUDIT_ACTION,
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
_ROTATED_DERIVATION_KEY_ID = "auth-key-rotated"
_ROTATED_MASTER_KEY = bytes(range(64, 96))
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


class FixedKeyring:
    """Keyring double anchoring derivations to the fixed key pair.

    ``rotate_current_key`` flips the current key to the rotated one while
    both keys stay resolvable through ``keys_by_id``, mirroring the two-key
    rotation model of the deployment keyring.
    """

    def __init__(self) -> None:
        self._current_key_id = _DERIVATION_KEY_ID

    def rotate_current_key(self) -> None:
        """Rotate once: the fixed key stays retained for anchored rows."""
        self._current_key_id = _ROTATED_DERIVATION_KEY_ID

    def current_key_id(self) -> str:
        return self._current_key_id

    def keys_by_id(self) -> dict[str, bytes]:
        return {
            _DERIVATION_KEY_ID: _MASTER_KEY,
            _ROTATED_DERIVATION_KEY_ID: _ROTATED_MASTER_KEY,
        }


@dataclass(frozen=True, slots=True)
class SeededAccount:
    """The trusted user/workspace/credential graph one test operates on."""

    user_id: UUID
    workspace_id: UUID
    web_session_id: UUID


class TokenHarness:
    """Real exchange/rotation stores and services over the disposable stack."""

    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine
        self.clock = FixedClock()
        self.keyring = FixedKeyring()
        self.crypto = _StaticHmacCrypto()
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
        self.token_service = self._build_token_service()
        self.username = f"token-owner-{uuid4().hex[:10]}"
        self.access_credential: str | None = None
        self.refresh_credential: str | None = None

    def _build_token_service(
        self, *, previous_master_key: bytes | None = None
    ) -> DeviceTokenService:
        return DeviceTokenService(
            exchange=self.grant_store,
            tokens=self.token_store,
            keyring=self.keyring,
            crypto=self.crypto,
            clock=self.clock,
            previous_master_key=previous_master_key,
        )

    def rotate_keyring(self) -> None:
        """Rotate the keyring mid-grant, retaining the previous master key.

        The deployment-level two-key rotation: the current key flips while
        the grant-issuing key stays retained, so a poll of a grant issued
        under the previous key must keep matching its stored digest.
        """
        self.keyring.rotate_current_key()
        self.token_service = self._build_token_service(previous_master_key=_MASTER_KEY)

    @property
    def database_now(self) -> datetime:
        return self.clock.database_now_value

    @staticmethod
    def diagnostic_context() -> Any:
        from personal_os.diagnostics.context import create_diagnostic_context

        return create_diagnostic_context().context

    async def seed_account(self) -> SeededAccount:
        user_id = uuid4()
        workspace_id = uuid4()
        web_session_id = uuid4()
        async with self.engine.begin() as connection:
            await connection.execute(
                sa.insert(users).values(
                    user_id=user_id, username=self.username, display_name="Token Owner"
                )
            )
            await connection.execute(
                sa.insert(workspaces).values(
                    workspace_id=workspace_id,
                    owner_user_id=user_id,
                    workspace_key=f"ws-{uuid4().hex[:12]}",
                    display_name="Token Workspace",
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

    async def create_approved_grant(self, account: SeededAccount) -> Any:
        """Create one grant and approve it through the real transition."""
        created = await self.grant_service.create_grant(
            client_instance_id=uuid4(),
            device_name="Personal desktop",
            platform_class=DevicePlatformClass.OBSIDIAN_DESKTOP,
            platform_name="windows",
            plugin_version="1.4.0",
            requested_scope=DeviceScope.OBSIDIAN_SYNC,
            source_bucket=f"token-source-{uuid4().hex[:10]}",
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
        """Poll the exchange once through the real service."""
        return await self.token_service.exchange_grant(
            grant_id=grant_id,
            polling_credential=polling_credential,
            diagnostic_context=self.diagnostic_context(),
        )

    async def refresh(
        self,
        *,
        rotation_id: UUID,
        refresh_credential: str | None = None,
    ) -> Any:
        """Rotate the current refresh credential once through the real service.

        The remembered credentials advance to the committed successor, so a
        follow-up rotation rotates the new current generation.
        """
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

    async def authenticate(self, access_credential: str | None = None) -> Any:
        presented = access_credential or self.access_credential
        assert presented is not None, "exchange before authenticating"
        return await self.token_service.authenticate_access(access_credential=presented)

    async def exchange(self, account: SeededAccount) -> tuple[Any, Any]:
        created = await self.create_approved_grant(account)
        exchanged = await self.poll(created.grant_id, created.polling_secret)
        self.access_credential = exchanged.access_credential
        self.refresh_credential = exchanged.refresh_credential
        return created, exchanged

    async def scalar_count(self, statement: sa.sql.Select) -> int:
        async with self.engine.connect() as connection:
            return int(await connection.scalar(statement) or 0)

    async def family_row(self, token_family_id: UUID) -> Any:
        async with self.engine.connect() as connection:
            return (
                await connection.execute(
                    sa.select(device_token_families).where(
                        device_token_families.c.token_family_id == token_family_id
                    )
                )
            ).one_or_none()

    async def family_state(self) -> str:
        assert self.refresh_credential is not None
        from personal_os.authentication.crypto import parse_refresh_credential

        parsed = parse_refresh_credential(self.refresh_credential)
        async with self.engine.connect() as connection:
            token_row = (
                await connection.execute(
                    sa.select(device_tokens.c.token_family_id, device_tokens.c.generation).where(
                        device_tokens.c.device_token_id == parsed.lookup_id
                    )
                )
            ).one()
        family = await self.family_row(token_row.token_family_id)
        assert family is not None
        return str(family.state)

    async def token_rows(self, token_family_id: UUID) -> list[Any]:
        statement = sa.select(device_tokens).where(
            device_tokens.c.token_family_id == token_family_id
        )
        async with self.engine.connect() as connection:
            return list((await connection.execute(statement)).all())

    async def audit_rows(self, target_id: UUID) -> list[Any]:
        statement = sa.select(audit_events).where(audit_events.c.target_id == target_id)
        async with self.engine.connect() as connection:
            return list((await connection.execute(statement)).all())

    async def device_row(self, device_id: UUID) -> Any:
        async with self.engine.connect() as connection:
            return (
                await connection.execute(sa.select(devices).where(devices.c.device_id == device_id))
            ).one_or_none()

    async def grant_row(self, grant_id: UUID) -> Any:
        async with self.engine.connect() as connection:
            return (
                await connection.execute(
                    sa.select(device_authorization_grants).where(
                        device_authorization_grants.c.grant_id == grant_id
                    )
                )
            ).one_or_none()


class _StaticHmacCrypto:
    """Crypto double deriving stdlib subkeys for the grant service wiring."""

    def derive_subkey(self, *, master_key: bytes, label: str) -> bytes:
        return hashlib.sha256(label.encode("ascii") + master_key).digest()

    def hmac_sha256(self, *, key: bytes, message: bytes) -> bytes:
        raise AssertionError("the token harness never calls hmac_sha256")

    def seal_secret(self, *, key: bytes, plaintext: bytes) -> tuple[bytes, bytes]:
        raise AssertionError("the token harness never seals")

    def open_secret(self, *, key: bytes, nonce: bytes, ciphertext: bytes) -> bytes:
        raise AssertionError("the token harness never opens")


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
async def harness(upgraded_authentication_stack: Any) -> TokenHarness:
    settings: DatabaseRuntimeSettings = load_database_runtime_settings(
        environ=upgraded_authentication_stack.alembic_env
    )
    password = SecretStr(upgraded_authentication_stack.password.get_secret_value())
    engine = create_source_store_engine(settings, password)
    try:
        yield TokenHarness(engine)
    finally:
        await dispose_source_store_engine(engine)


# --- exchange and exact exchange replay ------------------------------------------------


@pytest.mark.asyncio
async def test_exchange_creates_exactly_one_device_family_and_token_pair(
    harness: TokenHarness,
) -> None:
    account = await harness.seed_account()
    created, exchanged = await harness.exchange(account)

    grant = await harness.grant_row(created.grant_id)
    assert grant is not None
    assert grant.state == "exchanged"
    assert grant.exchanged_at == harness.database_now
    assert grant.device_id == exchanged.device_id
    assert grant.token_family_id == exchanged.token_family_id
    assert grant.initial_access_token_id is not None
    assert grant.initial_refresh_token_id is not None
    assert grant.derivation_key_id == _DERIVATION_KEY_ID

    assert exchanged.refresh_generation == INITIAL_REFRESH_GENERATION
    assert exchanged.access_credential.startswith("at1.")
    assert exchanged.refresh_credential.startswith("rt1.")
    assert exchanged.access_expires_at == harness.database_now + timedelta(minutes=15)

    assert (
        await harness.scalar_count(
            sa.select(sa.func.count())
            .select_from(devices)
            .where(devices.c.device_id == exchanged.device_id)
        )
        == 1
    )
    family_rows = await harness.token_rows(exchanged.token_family_id)
    assert len(family_rows) == 2
    kinds = sorted(row.token_kind for row in family_rows)
    assert kinds == ["access", "refresh"]
    assert len(await harness.audit_rows(exchanged.device_id)) == 1
    assert len(await harness.audit_rows(exchanged.token_family_id)) == 1
    # Only hashes are persisted: neither credential string appears anywhere.
    async with harness.engine.connect() as connection:
        stored_hashes = list(
            (await connection.execute(sa.select(device_tokens.c.secret_hash))).scalars()
        )
    for stored_hash in stored_hashes:
        assert len(stored_hash) == 64
        assert exchanged.access_credential not in stored_hash
        assert exchanged.refresh_credential not in stored_hash


@pytest.mark.asyncio
async def test_lost_exchange_acknowledgement_replays_identical_credentials(
    harness: TokenHarness,
) -> None:
    account = await harness.seed_account()
    created, exchanged = await harness.exchange(account)

    # The commit succeeded but the client lost the acknowledgement and retries.
    retried = await harness.poll(created.grant_id, created.polling_secret)

    assert retried.access_credential == exchanged.access_credential
    assert retried.refresh_credential == exchanged.refresh_credential
    assert retried.access_expires_at == exchanged.access_expires_at
    assert retried.refresh_expires_at == exchanged.refresh_expires_at
    assert retried.device_id == exchanged.device_id
    assert retried.token_family_id == exchanged.token_family_id
    assert retried.refresh_generation == exchanged.refresh_generation
    # Advancing the clock does not move the anchored timestamps.
    harness.clock.database_now_value += timedelta(minutes=4)
    replayed_again = await harness.poll(created.grant_id, created.polling_secret)
    assert replayed_again.access_credential == exchanged.access_credential
    assert replayed_again.access_expires_at == exchanged.access_expires_at
    assert (
        await harness.scalar_count(
            sa.select(sa.func.count())
            .select_from(devices)
            .where(devices.c.device_id == exchanged.device_id)
        )
        == 1
    )
    assert len(await harness.token_rows(exchanged.token_family_id)) == 2
    assert len(await harness.audit_rows(exchanged.device_id)) == 1
    assert len(await harness.audit_rows(exchanged.token_family_id)) == 1


@pytest.mark.asyncio
async def test_keyring_rotation_mid_grant_preserves_exact_poll_replay(
    harness: TokenHarness,
) -> None:
    """A keyring rotation between two polls of the same grant keeps the
    exchange an exact replay instead of an invalid credential (BACKLOG §9)."""
    account = await harness.seed_account()
    created, exchanged = await harness.exchange(account)

    # The deployment rotates the keyring between two polls of this grant.
    harness.rotate_keyring()

    retried = await harness.poll(created.grant_id, created.polling_secret)

    assert retried.access_credential == exchanged.access_credential
    assert retried.refresh_credential == exchanged.refresh_credential
    assert retried.access_expires_at == exchanged.access_expires_at
    assert retried.refresh_expires_at == exchanged.refresh_expires_at
    assert retried.device_id == exchanged.device_id
    assert retried.token_family_id == exchanged.token_family_id
    assert retried.refresh_generation == exchanged.refresh_generation
    # Advancing the clock does not move the anchored timestamps.
    harness.clock.database_now_value += timedelta(minutes=4)
    replayed_again = await harness.poll(created.grant_id, created.polling_secret)
    assert replayed_again.access_credential == exchanged.access_credential
    assert replayed_again.access_expires_at == exchanged.access_expires_at
    assert (
        await harness.scalar_count(
            sa.select(sa.func.count())
            .select_from(devices)
            .where(devices.c.device_id == exchanged.device_id)
        )
        == 1
    )
    assert len(await harness.token_rows(exchanged.token_family_id)) == 2
    assert len(await harness.audit_rows(exchanged.device_id)) == 1
    assert len(await harness.audit_rows(exchanged.token_family_id)) == 1


@pytest.mark.asyncio
async def test_exchange_replay_is_terminated_after_first_rotation(
    harness: TokenHarness,
) -> None:
    account = await harness.seed_account()
    created, _exchanged = await harness.exchange(account)

    rotated = await harness.refresh(rotation_id=ROTATION_A)
    assert rotated.refresh_generation == 2

    with pytest.raises(AuthenticationError) as raised:
        await harness.poll(created.grant_id, created.polling_secret)
    assert raised.value.error_code is ErrorCode.DEVICE_AUTHORIZATION_STATE_INVALID


@pytest.mark.asyncio
async def test_expired_approved_grant_refuses_the_exchange_without_rows(
    harness: TokenHarness,
) -> None:
    account = await harness.seed_account()
    created = await harness.create_approved_grant(account)
    harness.clock.database_now_value += timedelta(minutes=11)

    with pytest.raises(AuthenticationError) as raised:
        await harness.poll(created.grant_id, created.polling_secret)
    assert raised.value.error_code is ErrorCode.DEVICE_AUTHORIZATION_EXPIRED

    # The grant keeps its approved state with unset anchors, and no device,
    # family, token or audit row exists for the workspace the exchange would
    # have used.
    grant = await harness.grant_row(created.grant_id)
    assert grant is not None
    assert grant.state == "approved"
    assert grant.exchanged_at is None
    assert grant.device_id is None
    assert grant.token_family_id is None
    assert grant.initial_access_token_id is None
    assert grant.initial_refresh_token_id is None
    async with harness.engine.connect() as connection:
        device_count = await connection.scalar(
            sa.select(sa.func.count())
            .select_from(devices)
            .where(devices.c.workspace_id == account.workspace_id)
        )
        family_count = await connection.scalar(
            sa.select(sa.func.count())
            .select_from(device_token_families)
            .where(device_token_families.c.workspace_id == account.workspace_id)
        )
        token_count = await connection.scalar(
            sa.select(sa.func.count())
            .select_from(device_tokens)
            .where(device_tokens.c.workspace_id == account.workspace_id)
        )
    assert device_count == 0
    assert family_count == 0
    assert token_count == 0
    # Only the approval's own audit row targets the grant; the refused
    # exchange writes none.
    assert [row.action for row in await harness.audit_rows(created.grant_id)] == [
        DEVICE_AUTHORIZATION_APPROVED_AUDIT_ACTION
    ]


# --- refresh exact replay ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_lost_refresh_acknowledgement_replays_identical_successor(
    harness: TokenHarness,
) -> None:
    account = await harness.seed_account()
    _created, exchanged = await harness.exchange(account)
    predecessor_credential = exchanged.refresh_credential

    rotated = await harness.refresh(rotation_id=ROTATION_A)
    # The commit succeeded but the client lost the acknowledgement and retries
    # the same predecessor with the same rotation identity.
    retried = await harness.refresh(
        rotation_id=ROTATION_A, refresh_credential=predecessor_credential
    )

    assert retried.access_credential == rotated.access_credential
    assert retried.refresh_credential == rotated.refresh_credential
    assert retried.access_expires_at == rotated.access_expires_at
    assert retried.refresh_expires_at == rotated.refresh_expires_at
    assert retried.refresh_generation == rotated.refresh_generation == 2
    harness.clock.database_now_value += timedelta(minutes=3)
    replayed_again = await harness.refresh(
        rotation_id=ROTATION_A, refresh_credential=predecessor_credential
    )
    assert replayed_again.refresh_credential == rotated.refresh_credential
    assert replayed_again.access_expires_at == rotated.access_expires_at

    assert len(await harness.token_rows(exchanged.token_family_id)) == 4
    # Only the exchange-time family-creation audit targets the family: the
    # replaying rotations and the ambiguous-commit retries write nothing.
    assert [row.action for row in await harness.audit_rows(exchanged.token_family_id)] == [
        DEVICE_TOKEN_FAMILY_CREATED_AUDIT_ACTION
    ]


@pytest.mark.asyncio
async def test_rotation_advances_inactivity_without_extending_absolute_expiry(
    harness: TokenHarness,
) -> None:
    account = await harness.seed_account()
    _created, exchanged = await harness.exchange(account)
    del account
    family = await harness.family_row(exchanged.token_family_id)
    assert family is not None
    anchored_absolute_expiry = family.absolute_expires_at
    assert family.inactivity_expires_at == harness.database_now + timedelta(days=30)
    assert anchored_absolute_expiry == harness.database_now + timedelta(days=90)

    # Three rotations, each inside the running 30-day inactivity window; the
    # third would reach past the anchored absolute expiry and clamps to it.
    harness.clock.database_now_value += timedelta(days=29)
    await harness.refresh(rotation_id=ROTATION_A)
    harness.clock.database_now_value += timedelta(days=29)
    await harness.refresh(rotation_id=ROTATION_B)
    harness.clock.database_now_value += timedelta(days=29)
    rotated = await harness.refresh(rotation_id=UUID("00000000-0000-0000-0000-0000000000cc"))
    advanced = await harness.family_row(exchanged.token_family_id)
    assert advanced is not None
    assert advanced.current_refresh_generation == 4
    # 30 days from the last rotation, but clamped to the anchored expiry.
    assert advanced.inactivity_expires_at == anchored_absolute_expiry
    assert advanced.absolute_expires_at == anchored_absolute_expiry
    assert rotated.refresh_expires_at == anchored_absolute_expiry


# --- poll semantics ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pending_poll_reports_the_five_second_interval_then_slows_down(
    harness: TokenHarness,
) -> None:
    await harness.seed_account()
    pending_created = await harness.grant_service.create_grant(
        client_instance_id=uuid4(),
        device_name="Personal desktop",
        platform_class=DevicePlatformClass.OBSIDIAN_DESKTOP,
        platform_name="windows",
        plugin_version="1.4.0",
        requested_scope=DeviceScope.OBSIDIAN_SYNC,
        source_bucket=f"token-source-{uuid4().hex[:10]}",
    )
    with pytest.raises(AuthenticationError) as pending:
        await harness.poll(pending_created.grant_id, pending_created.polling_secret)
    assert pending.value.error_code is ErrorCode.DEVICE_AUTHORIZATION_PENDING
    assert pending.value.safe_details["retry_after_seconds"] == 5

    with pytest.raises(AuthenticationError) as slow:
        await harness.poll(pending_created.grant_id, pending_created.polling_secret)
    assert slow.value.error_code is ErrorCode.DEVICE_AUTHORIZATION_SLOW_DOWN
    assert slow.value.safe_details["retry_after_seconds"] >= 1

    harness.clock.database_now_value += timedelta(seconds=10)
    with pytest.raises(AuthenticationError) as allowed:
        await harness.poll(pending_created.grant_id, pending_created.polling_secret)
    assert allowed.value.error_code is ErrorCode.DEVICE_AUTHORIZATION_PENDING


@pytest.mark.asyncio
async def test_denied_and_expired_polls_report_their_closed_codes(
    harness: TokenHarness,
) -> None:
    from personal_os.authentication.device_authorization import DenyGrantCommand

    denied_created = await harness.grant_service.create_grant(
        client_instance_id=uuid4(),
        device_name="Personal desktop",
        platform_class=DevicePlatformClass.OBSIDIAN_DESKTOP,
        platform_name="windows",
        plugin_version="1.4.0",
        requested_scope=DeviceScope.OBSIDIAN_SYNC,
        source_bucket=f"token-source-{uuid4().hex[:10]}",
    )
    denial_account = await harness.seed_account()
    await harness.grant_store.deny_grant(
        DenyGrantCommand(
            grant_id=denied_created.grant_id,
            user_id=denial_account.user_id,
            workspace_id=denial_account.workspace_id,
            web_session_id=denial_account.web_session_id,
            database_now=harness.database_now,
            diagnostic_context=harness.diagnostic_context(),
        )
    )
    with pytest.raises(AuthenticationError) as denied:
        await harness.poll(denied_created.grant_id, denied_created.polling_secret)
    assert denied.value.error_code is ErrorCode.DEVICE_AUTHORIZATION_DENIED

    pending_created = await harness.grant_service.create_grant(
        client_instance_id=uuid4(),
        device_name="Personal desktop",
        platform_class=DevicePlatformClass.OBSIDIAN_DESKTOP,
        platform_name="windows",
        plugin_version="1.4.0",
        requested_scope=DeviceScope.OBSIDIAN_SYNC,
        source_bucket=f"token-source-{uuid4().hex[:10]}",
    )
    harness.clock.database_now_value += timedelta(minutes=11)
    with pytest.raises(AuthenticationError) as expired:
        await harness.poll(pending_created.grant_id, pending_created.polling_secret)
    assert expired.value.error_code is ErrorCode.DEVICE_AUTHORIZATION_EXPIRED


@pytest.mark.asyncio
async def test_unknown_polling_credential_fails_closed(harness: TokenHarness) -> None:
    with pytest.raises(AuthenticationError) as raised:
        await harness.poll(uuid4(), "pg1.00000000-0000-0000-0000-0000000000ff.neverseen")
    assert raised.value.error_code is ErrorCode.DEVICE_CREDENTIAL_INVALID
