"""Device-authorization grant races and transitions on a real stack.

Every test drives the real :class:`DeviceAuthorizationStore` and the real
domain :class:`personal_os.authentication.device_authorization.DeviceAuthorizationService`
over a disposable PostgreSQL 18.4 stack upgraded to the authentication head.
Only the non-PostgreSQL ports stay deterministic doubles: the crypto adapter
derives stdlib subkeys/HMACs and the clock pins one transaction timestamp so
expiry, throttle and replay assertions are exact. The tests prove the binding
contracts of design sections 11.1-11.3: creation persists only HMAC digests
under the exact-replay label with the 600-second lifetime; approve racing
deny (and approve racing approve) yields exactly one terminal winner and one
closed state-invalid rejection with exactly one audit row; expired grants
refuse both decisions with the closed expired code; and the browser lookup
resolves a fresh pending grant, reports terminal states through their closed
codes and throttles repeated unknown-code attempts.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
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
from personal_os.authentication.crypto import GRANT_REPLAY_DERIVATION_LABEL
from personal_os.authentication.device_authorization import (
    ApproveGrantCommand,
    DenyGrantCommand,
    DeviceAuthorizationService,
    DevicePlatformClass,
    PluginVersionBounds,
    polling_credential_hash_of,
    user_code_hash_of,
)
from personal_os.authentication.errors import AuthenticationError
from personal_os.authentication.sessions import (
    DUMMY_LOGIN_PHC_HASH,
    SessionService,
    session_secret_hash_of,
)
from personal_os.diagnostics.context import DiagnosticContext, create_diagnostic_context
from personal_os.error_contracts.codes import ErrorCode
from postgresql_source_store.device_authorization_store import (
    DEVICE_AUTHORIZATION_APPROVED_AUDIT_ACTION,
    DEVICE_AUTHORIZATION_DENIED_AUDIT_ACTION,
    DeviceAuthorizationStore,
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
    device_authorization_grants,
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
_VERIFICATION_BASE_URL = "https://web-admin.example"


class FixedClock:
    """Clock double pinning one controllable transaction timestamp."""

    def __init__(self) -> None:
        self.database_now_value = _DATABASE_NOW

    async def database_now(self) -> datetime:
        return self.database_now_value


class DeterministicHmacCrypto:
    """Crypto double deriving grant subkeys with stdlib only."""

    def derive_subkey(self, *, master_key: bytes, label: str) -> bytes:
        return hashlib.sha256(label.encode("ascii") + master_key).digest()

    def hmac_sha256(self, *, key: bytes, message: bytes) -> bytes:
        return hmac.new(key, message, hashlib.sha256).digest()

    def seal_secret(self, *, key: bytes, plaintext: bytes) -> tuple[bytes, bytes]:
        raise AssertionError("grant transactions never seal")

    def open_secret(self, *, key: bytes, nonce: bytes, ciphertext: bytes) -> bytes:
        raise AssertionError("grant transactions never open")


class StaticPasswordHasher:
    """Hasher double satisfying the session-service constructor only."""

    def hash_password(self, password: str) -> str:
        return f"static${hashlib.sha256(password.encode('utf-8')).hexdigest()}"

    def verify_password(self, password_hash: str, password: str) -> bool:
        del password_hash
        return password == _CORRECT_PASSWORD

    def needs_rehash(self, password_hash: str) -> bool:
        return False


@dataclass(frozen=True, slots=True)
class SeededAccount:
    """The trusted user/workspace/credential graph one test operates on."""

    user_id: UUID
    workspace_id: UUID
    username: str


class GrantTransactionHarness:
    """Real grant store and services over the disposable stack, pinned clock."""

    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine
        self.clock = FixedClock()
        self.crypto = DeterministicHmacCrypto()
        self.grant_store = DeviceAuthorizationStore(engine)
        self.web_session_store = WebSessionStore(engine)
        self.session_service = SessionService(
            sessions=self.web_session_store,
            hasher=StaticPasswordHasher(),
            crypto=self.crypto,
            master_key=_MASTER_KEY,
            clock=self.clock,
        )
        self.service = DeviceAuthorizationService(
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
        self.grant_hmac_key = self.crypto.derive_subkey(
            master_key=_MASTER_KEY, label=GRANT_REPLAY_DERIVATION_LABEL
        )
        self.workspace_id: UUID | None = None
        self.username = f"grant-owner-{uuid4().hex[:10]}"

    @property
    def database_now(self) -> datetime:
        return self.clock.database_now_value

    @staticmethod
    def diagnostic_context() -> DiagnosticContext:
        return create_diagnostic_context().context

    async def seed_account(self) -> SeededAccount:
        user_id = uuid4()
        workspace_id = uuid4()
        self.workspace_id = workspace_id
        async with self.engine.begin() as connection:
            await connection.execute(
                sa.insert(users).values(
                    user_id=user_id, username=self.username, display_name="Grant Owner"
                )
            )
            await connection.execute(
                sa.insert(workspaces).values(
                    workspace_id=workspace_id,
                    owner_user_id=user_id,
                    workspace_key=f"ws-{uuid4().hex[:12]}",
                    display_name="Grant Workspace",
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
        return SeededAccount(user_id=user_id, workspace_id=workspace_id, username=self.username)

    async def seed_session(
        self,
        account: SeededAccount,
        *,
        authenticated_at: datetime | None,
        reauthenticated_at: datetime | None,
    ) -> tuple[UUID, str]:
        """Insert one active session row; return its id and raw secret."""
        web_session_id = uuid4()
        raw_secret = f"session-secret-{web_session_id.hex}"
        anchor = authenticated_at if authenticated_at is not None else self.database_now
        async with self.engine.begin() as connection:
            await connection.execute(
                sa.insert(web_sessions).values(
                    web_session_id=web_session_id,
                    user_id=account.user_id,
                    workspace_id=account.workspace_id,
                    session_secret_hash=session_secret_hash_of(raw_secret),
                    csrf_secret_hash=hashlib.sha256(
                        (raw_secret + "csrf").encode("utf-8")
                    ).hexdigest(),
                    state="active",
                    credential_revision=1,
                    authentication_method="password",
                    created_at=anchor,
                    authenticated_at=authenticated_at,
                    reauthenticated_at=reauthenticated_at,
                    idle_expires_at=self.database_now + timedelta(hours=12),
                    absolute_expires_at=self.database_now + timedelta(days=7),
                )
            )
        return web_session_id, raw_secret

    async def create_grant(self) -> Any:
        return await self.service.create_grant(
            client_instance_id=uuid4(),
            device_name="Personal desktop",
            platform_class=DevicePlatformClass.OBSIDIAN_DESKTOP,
            platform_name="windows",
            plugin_version="1.4.0",
            requested_scope=DeviceScope.OBSIDIAN_SYNC,
            source_bucket=f"grant-source-{uuid4().hex[:10]}",
            diagnostic_context=self.diagnostic_context(),
        )

    def approve_command(
        self, grant_id: UUID, account: SeededAccount, web_session_id: UUID
    ) -> ApproveGrantCommand:
        return ApproveGrantCommand(
            grant_id=grant_id,
            user_id=account.user_id,
            workspace_id=account.workspace_id,
            web_session_id=web_session_id,
            database_now=self.database_now,
            diagnostic_context=self.diagnostic_context(),
        )

    def deny_command(
        self, grant_id: UUID, account: SeededAccount, web_session_id: UUID
    ) -> DenyGrantCommand:
        return DenyGrantCommand(
            grant_id=grant_id,
            user_id=account.user_id,
            workspace_id=account.workspace_id,
            web_session_id=web_session_id,
            database_now=self.database_now,
            diagnostic_context=self.diagnostic_context(),
        )

    async def grant_row(self, grant_id: UUID) -> Any:
        async with self.engine.connect() as connection:
            return (
                await connection.execute(
                    sa.select(device_authorization_grants).where(
                        device_authorization_grants.c.grant_id == grant_id
                    )
                )
            ).one_or_none()

    async def grant_audit_rows(self, grant_id: UUID) -> list[Any]:
        statement = sa.select(audit_events).where(audit_events.c.target_id == grant_id)
        async with self.engine.connect() as connection:
            return list((await connection.execute(statement)).all())


async def race(*awaitables: Any) -> list[Any]:
    """Run the transitions concurrently, keeping rejections as values."""
    return list(await asyncio.gather(*awaitables, return_exceptions=True))


def outcome_kinds(outcomes: list[Any]) -> list[str]:
    """Map each race outcome to its closed kind token.

    A committed transition maps to its terminal state token (``approved`` or
    ``denied``); a lost race maps to the closed registry rejection code, whose
    token is ``device_authorization_state_invalid`` for the terminal conflict.
    """
    kinds: list[str] = []
    for outcome in outcomes:
        if isinstance(outcome, AuthenticationError):
            kinds.append(outcome.error_code.value)
        else:
            kinds.append(outcome.state.value)
    return sorted(kinds)


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
async def harness(upgraded_authentication_stack: Any) -> GrantTransactionHarness:
    settings: DatabaseRuntimeSettings = load_database_runtime_settings(
        environ=upgraded_authentication_stack.alembic_env
    )
    password = SecretStr(upgraded_authentication_stack.password.get_secret_value())
    engine = create_source_store_engine(settings, password)
    try:
        yield GrantTransactionHarness(engine)
    finally:
        await dispose_source_store_engine(engine)


# --- creation -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_created_grant_persists_only_hmac_digests(
    harness: GrantTransactionHarness,
) -> None:
    created = await harness.create_grant()

    assert created.user_code
    assert created.polling_secret.startswith("pg1.")
    assert created.expires_in_seconds == 600
    assert created.poll_interval_seconds == 5
    assert created.verification_uri == f"{_VERIFICATION_BASE_URL}/device/approve"
    assert created.verification_uri_complete.endswith(f"#{created.user_code}")
    row = await harness.grant_row(created.grant_id)
    assert row is not None
    assert row.state == "pending"
    assert row.expires_at == harness.database_now + timedelta(seconds=600)
    assert row.user_code_hash == user_code_hash_of(
        hmac_key=harness.grant_hmac_key, user_code=created.user_code
    )
    assert row.polling_secret_hash == polling_credential_hash_of(
        hmac_key=harness.grant_hmac_key, polling_credential=created.polling_secret
    )
    assert created.user_code not in row.user_code_hash
    assert created.polling_secret not in row.polling_secret_hash
    assert await harness.grant_audit_rows(created.grant_id) == []


@pytest.mark.asyncio
async def test_unsupported_plugin_versions_are_refused_before_secrets(
    harness: GrantTransactionHarness,
) -> None:
    with pytest.raises(AuthenticationError) as raised:
        await harness.service.create_grant(
            client_instance_id=uuid4(),
            device_name="Personal desktop",
            platform_class=DevicePlatformClass.OBSIDIAN_DESKTOP,
            platform_name="windows",
            plugin_version="2.0.1",
            requested_scope=DeviceScope.OBSIDIAN_SYNC,
            source_bucket=f"grant-source-{uuid4().hex[:10]}",
            diagnostic_context=harness.diagnostic_context(),
        )
    assert raised.value.error_code is ErrorCode.PLUGIN_VERSION_UNSUPPORTED
    assert [str(bound) for bound in raised.value.safe_details["approved_version_bounds"]] == [
        "1.0.0",
        "2.0.0",
    ]


# --- races ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approve_racing_deny_has_one_terminal_winner(
    harness: GrantTransactionHarness,
) -> None:
    account = await harness.seed_account()
    web_session_id, _secret = await harness.seed_session(
        account,
        authenticated_at=harness.database_now,
        reauthenticated_at=harness.database_now,
    )
    created = await harness.create_grant()

    outcomes = await race(
        harness.grant_store.approve_grant(
            harness.approve_command(created.grant_id, account, web_session_id)
        ),
        harness.grant_store.deny_grant(
            harness.deny_command(created.grant_id, account, web_session_id)
        ),
    )

    # One terminal winner plus one closed terminal-conflict rejection.
    assert outcome_kinds(outcomes) in (
        ["approved", "device_authorization_state_invalid"],
        ["denied", "device_authorization_state_invalid"],
    )
    row = await harness.grant_row(created.grant_id)
    assert row is not None
    assert row.state in ("approved", "denied")
    assert len(await harness.grant_audit_rows(created.grant_id)) == 1


@pytest.mark.asyncio
async def test_approve_racing_approve_has_one_winner(
    harness: GrantTransactionHarness,
) -> None:
    account = await harness.seed_account()
    web_session_id, _secret = await harness.seed_session(
        account,
        authenticated_at=harness.database_now,
        reauthenticated_at=harness.database_now,
    )
    created = await harness.create_grant()
    command = harness.approve_command(created.grant_id, account, web_session_id)

    outcomes = await race(
        harness.grant_store.approve_grant(command), harness.grant_store.approve_grant(command)
    )

    assert outcome_kinds(outcomes) == [
        "approved",
        "device_authorization_state_invalid",
    ]
    row = await harness.grant_row(created.grant_id)
    assert row is not None
    assert row.state == "approved"
    assert row.approved_by_user_id == account.user_id
    assert row.approved_web_session_id == web_session_id
    audit_rows = await harness.grant_audit_rows(created.grant_id)
    assert len(audit_rows) == 1
    assert audit_rows[0].action == DEVICE_AUTHORIZATION_APPROVED_AUDIT_ACTION


@pytest.mark.asyncio
async def test_denial_records_the_terminal_state_with_one_audit_row(
    harness: GrantTransactionHarness,
) -> None:
    account = await harness.seed_account()
    web_session_id, _secret = await harness.seed_session(
        account, authenticated_at=harness.database_now, reauthenticated_at=None
    )
    created = await harness.create_grant()

    denied = await harness.grant_store.deny_grant(
        harness.deny_command(created.grant_id, account, web_session_id)
    )

    assert denied.state == "denied"
    row = await harness.grant_row(created.grant_id)
    assert row is not None
    assert row.state == "denied"
    assert row.denied_at == harness.database_now
    audit_rows = await harness.grant_audit_rows(created.grant_id)
    assert len(audit_rows) == 1
    assert audit_rows[0].action == DEVICE_AUTHORIZATION_DENIED_AUDIT_ACTION


@pytest.mark.asyncio
async def test_expired_grants_refuse_both_decisions_with_the_expired_code(
    harness: GrantTransactionHarness,
) -> None:
    account = await harness.seed_account()
    web_session_id, _secret = await harness.seed_session(
        account,
        authenticated_at=harness.database_now,
        reauthenticated_at=harness.database_now,
    )
    created = await harness.create_grant()
    harness.clock.database_now_value += timedelta(minutes=11)

    with pytest.raises(AuthenticationError) as approve_rejection:
        await harness.grant_store.approve_grant(
            harness.approve_command(created.grant_id, account, web_session_id)
        )
    assert approve_rejection.value.error_code is ErrorCode.DEVICE_AUTHORIZATION_EXPIRED
    with pytest.raises(AuthenticationError) as deny_rejection:
        await harness.grant_store.deny_grant(
            harness.deny_command(created.grant_id, account, web_session_id)
        )
    assert deny_rejection.value.error_code is ErrorCode.DEVICE_AUTHORIZATION_EXPIRED
    assert await harness.grant_audit_rows(created.grant_id) == []


# --- browser lookup -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lookup_resolves_the_pending_grant_display_context(
    harness: GrantTransactionHarness,
) -> None:
    account = await harness.seed_account()
    _web_session_id, _secret = await harness.seed_session(
        account, authenticated_at=harness.database_now, reauthenticated_at=None
    )
    created = await harness.create_grant()

    resolved = await harness.service.lookup_grant(
        user_code=created.user_code,
        user_id=account.user_id,
        diagnostic_context=harness.diagnostic_context(),
    )

    assert resolved.grant_id == created.grant_id
    assert resolved.user_code == created.user_code
    assert resolved.device_name == "Personal desktop"
    assert resolved.platform_class == "obsidian_desktop"
    assert resolved.platform_name == "windows"
    assert resolved.plugin_version == "1.4.0"
    assert resolved.requested_scope == "obsidian_sync"
    assert resolved.expires_at == created.expires_at


@pytest.mark.asyncio
async def test_lookup_of_expired_and_denied_grants_reports_closed_codes(
    harness: GrantTransactionHarness,
) -> None:
    account = await harness.seed_account()
    _web_session_id, _secret = await harness.seed_session(
        account, authenticated_at=harness.database_now, reauthenticated_at=None
    )
    expired_grant = await harness.create_grant()
    harness.clock.database_now_value += timedelta(minutes=11)
    with pytest.raises(AuthenticationError) as expired:
        await harness.service.lookup_grant(
            user_code=expired_grant.user_code,
            user_id=account.user_id,
            diagnostic_context=harness.diagnostic_context(),
        )
    assert expired.value.error_code is ErrorCode.DEVICE_AUTHORIZATION_EXPIRED

    denied_grant = await harness.create_grant()
    denial_session_id, _denial_secret = await harness.seed_session(
        account, authenticated_at=harness.database_now, reauthenticated_at=None
    )
    await harness.grant_store.deny_grant(
        harness.deny_command(denied_grant.grant_id, account, denial_session_id)
    )
    with pytest.raises(AuthenticationError) as denied:
        await harness.service.lookup_grant(
            user_code=denied_grant.user_code,
            user_id=account.user_id,
            diagnostic_context=harness.diagnostic_context(),
        )
    assert denied.value.error_code is ErrorCode.DEVICE_AUTHORIZATION_DENIED


@pytest.mark.asyncio
async def test_unknown_user_code_lookup_fails_closed_and_throttles(
    harness: GrantTransactionHarness,
) -> None:
    account = await harness.seed_account()
    _web_session_id, _secret = await harness.seed_session(
        account, authenticated_at=harness.database_now, reauthenticated_at=None
    )
    for _ in range(5):
        with pytest.raises(AuthenticationError) as rejected:
            await harness.service.lookup_grant(
                user_code="ZZZZZZZ-Z",
                user_id=account.user_id,
                diagnostic_context=harness.diagnostic_context(),
            )
        assert rejected.value.error_code is ErrorCode.DEVICE_CREDENTIAL_INVALID
    with pytest.raises(AuthenticationError) as locked:
        await harness.service.lookup_grant(
            user_code="ZZZZZZZ-Z",
            user_id=account.user_id,
            diagnostic_context=harness.diagnostic_context(),
        )
    assert locked.value.error_code is ErrorCode.AUTHENTICATION_RATE_LIMITED
    assert locked.value.safe_details["retry_after_seconds"]


# --- approval choreography through the service ----------------------------------------


@pytest.mark.asyncio
async def test_service_approve_requires_recent_reauthentication(
    harness: GrantTransactionHarness,
) -> None:
    account = await harness.seed_account()
    _web_session_id, raw_secret = await harness.seed_session(
        account,
        authenticated_at=harness.database_now - timedelta(minutes=6),
        reauthenticated_at=None,
    )
    created = await harness.create_grant()

    with pytest.raises(AuthenticationError) as stale:
        await harness.service.approve_grant(
            grant_id=created.grant_id,
            session_secret=raw_secret,
            diagnostic_context=harness.diagnostic_context(),
        )
    assert stale.value.error_code is ErrorCode.RECENT_AUTHENTICATION_REQUIRED
    assert await harness.grant_audit_rows(created.grant_id) == []


@pytest.mark.asyncio
async def test_service_approve_commits_after_a_recent_reauthentication(
    harness: GrantTransactionHarness,
) -> None:
    account = await harness.seed_account()
    web_session_id, raw_secret = await harness.seed_session(
        account,
        authenticated_at=harness.database_now,
        reauthenticated_at=harness.database_now,
    )
    created = await harness.create_grant()

    approved = await harness.service.approve_grant(
        grant_id=created.grant_id,
        session_secret=raw_secret,
        diagnostic_context=harness.diagnostic_context(),
    )

    assert approved.state == "approved"
    row = await harness.grant_row(created.grant_id)
    assert row is not None
    assert row.state == "approved"
    assert row.approved_web_session_id == web_session_id
