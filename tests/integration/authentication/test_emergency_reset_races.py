"""Emergency reset racing every live credential path on a real stack (spec 7.2).

Every test drives the real :class:`CredentialStore.reset_web_authentication`
transaction over a disposable PostgreSQL 18.4 stack upgraded to the
authentication head while another real authentication path runs concurrently
through ``asyncio.gather``: password login, device refresh rotation and grant
approval. Only the non-PostgreSQL ports stay deterministic doubles: the
hasher accepts one fixed password and the clock pins one transaction
timestamp. The tests prove the binding invariant of spec 24.2 — emergency
reset racing active credential use leaves no usable credential — under every
interleaving the row locks admit: racing logins never commit a session that
outlives the reset, a racing refresh either rotates into an already-revoked
family or fails closed, both generations of a rotated family die with the
reset, and a racing grant approval can never leave an approved grant: the
reset denies every pending and approved-unexchanged grant, and an approval
that committed first is denied by the sweep, not orphaned.
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
from personal_os.authentication.device_tokens import DeviceTokenService
from personal_os.authentication.errors import AuthenticationError
from personal_os.authentication.sessions import (
    LoginService,
    SessionService,
)
from personal_os.diagnostics.context import DiagnosticContext, create_diagnostic_context
from personal_os.error_contracts.codes import ErrorCode
from postgresql_source_store.authentication_credentials import (
    CredentialStore,
    ResetWebAuthenticationCommand,
)
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
    device_token_families,
    device_tokens,
    user_credentials,
    users,
    web_sessions,
    workspaces,
)
from postgresql_source_store.web_session_store import WebSessionStore

pytestmark = pytest.mark.local_stack

_WORKTREE_ROOT: Path = Path(__file__).resolve().parents[3]

_DATABASE_NOW = datetime(2026, 8, 16, 12, 0, 0, tzinfo=UTC)
_OLD_PASSWORD = "correct-horse-battery-staple"
_RESET_PASSWORD = "fresh-reset-trebuchet-phrase"
_MASTER_KEY = bytes(range(32))
_DERIVATION_KEY_ID = "auth-key-current"
_VERIFICATION_BASE_URL = "https://web-admin.example"

ROTATION_A = UUID("00000000-0000-0000-0000-0000000000aa")


class FixedClock:
    """Clock double pinning one controllable transaction timestamp."""

    def __init__(self) -> None:
        self.database_now_value = _DATABASE_NOW

    async def database_now(self) -> datetime:
        return self.database_now_value


class FixedPasswordHasher:
    """Hasher double rendering PHC-shaped digests of one fixed alphabet.

    The stored hash follows the schema check constraint; verification stays
    digest equality, so the pre-reset password dies exactly when the reset
    installs the new hash.
    """

    def __init__(self) -> None:
        self.reset_password_hash = self.hash_password(_RESET_PASSWORD)

    def hash_password(self, password: str) -> str:
        digest = hashlib.sha256(password.encode("utf-8")).hexdigest()
        return f"$argon2id$v=19$m=65536,t=3,p=1${digest[:22]}${digest}"

    def verify_password(self, password_hash: str, password: str) -> bool:
        return password_hash == self.hash_password(password)

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
        raise AssertionError("the reset harness never seals")

    def open_secret(self, *, key: bytes, nonce: bytes, ciphertext: bytes) -> bytes:
        raise AssertionError("the reset harness never opens")


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


class ResetRaceHarness:
    """Real reset/login/grant/token services over the disposable stack."""

    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine
        self.clock = FixedClock()
        self.hasher = FixedPasswordHasher()
        self.crypto = StaticHmacCrypto()
        self.credentials = CredentialStore(engine)
        self.username = f"reset-owner-{uuid4().hex[:10]}"
        self.login_service = LoginService(
            credentials=self.credentials,
            hasher=self.hasher,
            crypto=self.crypto,
            master_key=_MASTER_KEY,
            clock=self.clock,
        )
        self.session_service = SessionService(
            sessions=WebSessionStore(engine),
            hasher=self.hasher,
            crypto=self.crypto,
            master_key=_MASTER_KEY,
            clock=self.clock,
        )
        self.grant_store = DeviceAuthorizationStore(engine)
        self.token_service = DeviceTokenService(
            exchange=self.grant_store,
            tokens=DeviceTokenStore(engine),
            keyring=FixedKeyring(),
            crypto=self.crypto,
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
        session_secret = f"reset-session-{uuid4().hex}"
        # Each seeded account owns its canonical username: the later login
        # and reset calls of a test address the most recently seeded one.
        self.username = f"reset-owner-{uuid4().hex[:10]}"
        async with self.engine.begin() as connection:
            await connection.execute(
                sa.insert(users).values(
                    user_id=user_id, username=self.username, display_name="Reset Owner"
                )
            )
            await connection.execute(
                sa.insert(workspaces).values(
                    workspace_id=workspace_id,
                    owner_user_id=user_id,
                    workspace_key=f"ws-{uuid4().hex[:12]}",
                    display_name="Reset Workspace",
                )
            )
            await connection.execute(
                sa.insert(user_credentials).values(
                    user_id=user_id,
                    workspace_id=workspace_id,
                    password_hash=self.hasher.hash_password(_OLD_PASSWORD),
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
                    session_secret_hash=hashlib.sha256(session_secret.encode("utf-8")).hexdigest(),
                    csrf_secret_hash=hashlib.sha256(
                        (session_secret + "csrf").encode("utf-8")
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
            user_id=user_id,
            workspace_id=workspace_id,
            web_session_id=web_session_id,
            session_secret=session_secret,
        )

    async def login(self, *, password: str) -> Any:
        return await self.login_service.login(
            username=self.username,
            password=password,
            source_bucket="reset-race-source",
            diagnostic_context=self.diagnostic_context(),
        )

    async def reset(self) -> None:
        await self.credentials.reset_web_authentication(
            ResetWebAuthenticationCommand(
                username=self.username,
                new_password_hash=self.hasher.reset_password_hash,
                database_now=self.database_now + timedelta(seconds=1),
                diagnostic_context=self.diagnostic_context(),
            )
        )

    async def exchange_device(self, account: SeededAccount) -> str:
        """Create, approve and exchange one grant; return the refresh credential."""
        created = await self.grant_service.create_grant(
            client_instance_id=uuid4(),
            device_name="Reset Race Desktop",
            platform_class=DevicePlatformClass.OBSIDIAN_DESKTOP,
            platform_name="windows",
            plugin_version="1.4.0",
            requested_scope=DeviceScope.OBSIDIAN_SYNC,
            source_bucket=f"reset-source-{uuid4().hex[:10]}",
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
        exchanged = await self.token_service.exchange_grant(
            grant_id=created.grant_id,
            polling_credential=created.polling_secret,
            diagnostic_context=self.diagnostic_context(),
        )
        return str(exchanged.refresh_credential)

    async def refresh(self, refresh_credential: str, rotation_id: UUID) -> Any:
        return await self.token_service.refresh(
            refresh_credential=refresh_credential,
            rotation_id=rotation_id,
            diagnostic_context=self.diagnostic_context(),
        )

    async def create_pending_grant(self) -> UUID:
        created = await self.grant_service.create_grant(
            client_instance_id=uuid4(),
            device_name="Reset Race Desktop",
            platform_class=DevicePlatformClass.OBSIDIAN_DESKTOP,
            platform_name="windows",
            plugin_version="1.4.0",
            requested_scope=DeviceScope.OBSIDIAN_SYNC,
            source_bucket=f"reset-source-{uuid4().hex[:10]}",
        )
        return created.grant_id

    async def approve(self, account: SeededAccount, grant_id: UUID) -> Any:
        return await self.grant_store.approve_grant(
            ApproveGrantCommand(
                grant_id=grant_id,
                user_id=account.user_id,
                workspace_id=account.workspace_id,
                web_session_id=account.web_session_id,
                database_now=self.database_now + timedelta(seconds=1),
                diagnostic_context=self.diagnostic_context(),
            )
        )

    # -- row inspection ----------------------------------------------------------------

    async def active_session_count(self, user_id: UUID) -> int:
        statement = (
            sa.select(sa.func.count())
            .select_from(web_sessions)
            .where(web_sessions.c.user_id == user_id, web_sessions.c.state != "revoked")
        )
        async with self.engine.connect() as connection:
            return int(await connection.scalar(statement) or 0)

    async def family_rows(self, user_id: UUID) -> list[Any]:
        statement = sa.select(device_token_families).where(
            device_token_families.c.user_id == user_id
        )
        async with self.engine.connect() as connection:
            return list((await connection.execute(statement)).all())

    async def token_states(self, user_id: UUID) -> list[str]:
        statement = sa.select(device_tokens.c.state).where(device_tokens.c.user_id == user_id)
        async with self.engine.connect() as connection:
            return [str(row.state) for row in (await connection.execute(statement))]

    async def grant_row(self, grant_id: UUID) -> Any:
        async with self.engine.connect() as connection:
            return (
                await connection.execute(
                    sa.select(device_authorization_grants).where(
                        device_authorization_grants.c.grant_id == grant_id
                    )
                )
            ).one_or_none()


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
async def harness(upgraded_authentication_stack: Any) -> ResetRaceHarness:
    settings: DatabaseRuntimeSettings = load_database_runtime_settings(
        environ=upgraded_authentication_stack.alembic_env
    )
    password = SecretStr(upgraded_authentication_stack.password.get_secret_value())
    engine = create_source_store_engine(settings, password)
    try:
        yield ResetRaceHarness(engine)
    finally:
        await dispose_source_store_engine(engine)


@pytest.mark.asyncio
async def test_reset_racing_login_leaves_no_usable_credential(
    harness: ResetRaceHarness,
) -> None:
    account = await harness.seed_account()

    racing_logins = [harness.login(password=_OLD_PASSWORD) for _ in range(4)]
    outcomes = await asyncio.gather(harness.reset(), *racing_logins, return_exceptions=True)
    reset_outcome = outcomes[0]
    assert not isinstance(reset_outcome, BaseException), reset_outcome

    # No interleaving left a session that outlives the reset.
    assert await harness.active_session_count(account.user_id) == 0

    # The pre-reset session cookie no longer authenticates.
    with pytest.raises(AuthenticationError) as rejected:
        await harness.session_service.authenticate(session_secret=account.session_secret)
    assert rejected.value.error_code is ErrorCode.AUTHENTICATION_REQUIRED

    # The replaced password is dead; the reset password works and its fresh
    # session is the only usable credential afterwards.
    failed = await harness.login(password=_OLD_PASSWORD)
    assert failed.public_error is ErrorCode.AUTHENTICATION_FAILED
    succeeded = await harness.login(password=_RESET_PASSWORD)
    assert succeeded.started_session is not None
    assert await harness.active_session_count(account.user_id) == 1


@pytest.mark.asyncio
async def test_reset_racing_refresh_leaves_no_usable_refresh_credential(
    harness: ResetRaceHarness,
) -> None:
    account = await harness.seed_account()
    refresh_credential = await harness.exchange_device(account)

    outcomes = await asyncio.gather(
        harness.refresh(refresh_credential, ROTATION_A),
        harness.reset(),
        return_exceptions=True,
    )
    refresh_outcome, reset_outcome = outcomes
    assert not isinstance(reset_outcome, BaseException), reset_outcome

    successor_credential: str | None = None
    if not isinstance(refresh_outcome, BaseException):
        successor_credential = str(refresh_outcome.refresh_credential)

    # Every family of the user is revoked by the reset, whichever side won.
    families = await harness.family_rows(account.user_id)
    assert families
    for family in families:
        assert family.state == "revoked"
        assert family.revocation_reason == "emergency_reset"
    assert set(await harness.token_states(account.user_id)) <= {"revoked", "rotated"}

    presentations = [refresh_credential]
    if successor_credential is not None:
        presentations.append(successor_credential)
    for presentation in presentations:
        with pytest.raises(AuthenticationError):
            await harness.refresh(presentation, UUID("00000000-0000-0000-0000-0000000000bb"))


@pytest.mark.asyncio
async def test_refresh_then_reset_and_reset_then_refresh_both_close_everything(
    harness: ResetRaceHarness,
) -> None:
    # Interleaving one: the rotation commits, then the reset revokes both
    # generations of the family.
    first_account = await harness.seed_account()
    first_credential = await harness.exchange_device(first_account)
    rotated = await harness.refresh(first_credential, ROTATION_A)
    await harness.reset()
    assert await harness.active_session_count(first_account.user_id) == 0
    for family in await harness.family_rows(first_account.user_id):
        assert family.state == "revoked"
    with pytest.raises(AuthenticationError) as successor_rejected:
        await harness.refresh(str(rotated.refresh_credential), uuid4())
    # The successor died with the family: presenting it under a fresh
    # rotation identity is either the revoked-family rejection or the
    # confirmed-reuse classification of a rotated predecessor.
    assert successor_rejected.value.error_code in (
        ErrorCode.DEVICE_REVOKED,
        ErrorCode.DEVICE_TOKEN_REUSE_DETECTED,
    )

    # Interleaving two: the reset commits first, so the rotation and the
    # predecessor presentation both fail closed.
    second_account = await harness.seed_account()
    second_credential = await harness.exchange_device(second_account)
    await harness.reset()
    with pytest.raises(AuthenticationError) as rotation_rejected:
        await harness.refresh(second_credential, ROTATION_A)
    # The reset-revoked predecessor is terminal whichever classification the
    # presentation takes: the revoked family or the reuse detector answers.
    assert rotation_rejected.value.error_code in (
        ErrorCode.DEVICE_REVOKED,
        ErrorCode.DEVICE_TOKEN_REUSE_DETECTED,
    )
    with pytest.raises(AuthenticationError) as predecessor_rejected:
        await harness.refresh(second_credential, uuid4())
    assert predecessor_rejected.value.error_code in (
        ErrorCode.DEVICE_REVOKED,
        ErrorCode.DEVICE_TOKEN_REUSE_DETECTED,
    )


@pytest.mark.asyncio
async def test_reset_racing_grant_approval_never_leaves_an_approved_grant(
    harness: ResetRaceHarness,
) -> None:
    account = await harness.seed_account()
    grant_id = await harness.create_pending_grant()

    outcomes = await asyncio.gather(
        harness.approve(account, grant_id),
        harness.reset(),
        return_exceptions=True,
    )
    approve_outcome, reset_outcome = outcomes
    assert not isinstance(reset_outcome, BaseException), reset_outcome
    if isinstance(approve_outcome, BaseException):
        # The reset committed first: the approving session was revoked or the
        # grant was already denied deployment-wide.
        assert isinstance(approve_outcome, AuthenticationError)

    grant = await harness.grant_row(grant_id)
    assert grant is not None
    # Whether the approval won the race (the reset then denies the
    # approved-unexchanged grant) or lost it, no live grant survives.
    assert grant.state == "denied"
    assert await harness.active_session_count(account.user_id) == 0
