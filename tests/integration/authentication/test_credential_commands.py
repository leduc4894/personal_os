"""Protected credential enrollment, status and emergency reset over the real schema.

Every test drives the real :class:`CredentialStore` protected-operation methods
(and, end to end, the real ``api_runtime.command`` subprocess shell with a
password file beneath the stack secret root) over a disposable PostgreSQL 18.4
stack upgraded to the authentication head. The tests prove the binding
contracts of design sections 7.1 and 7.2: enrollment locks the canonical
identity, inserts revision 1 with its audit row and refuses any later
enrollment attempt; status resolves only the enrollment flag and credential
revision; and the emergency reset closes every authentication surface in one
transaction — password and revision replaced, TOTP and recovery state
disabled, every session revoked with cleared authenticated timestamps, every
device, token family and token revoked, pending grants denied, one reset
audit row with closed counts, including exact zero counts while the device and
grant surfaces are still empty. The closed rejections are pinned too: a reset
on a workspace with no enrollment and a reset of an archived workspace both
answer the generic ``authentication_failed`` rejection (the CLI maps it to
exit 78), and a closed confirmation prompt aborts the CLI reset as operator
input (exit 2), never an internal error.
"""

from __future__ import annotations

import hashlib
import hmac
import subprocess
import sys
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from personal_os.authentication.errors import AuthenticationError
from personal_os.authentication.passwords import (
    PasswordBlocklist,
    validate_new_password,
)
from personal_os.authentication.sessions import LoginService, SessionService
from personal_os.diagnostics.context import DiagnosticContext, create_diagnostic_context
from personal_os.error_contracts.codes import ErrorCode
from postgresql_source_store.authentication_credentials import (
    CredentialStore,
    EnrollWebCredentialCommand,
    ResetWebAuthenticationCommand,
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
    device_token_families,
    device_tokens,
    devices,
    totp_credentials,
    totp_recovery_codes,
    user_credentials,
    users,
    web_sessions,
    workspaces,
)
from postgresql_source_store.web_session_store import WebSessionStore

pytestmark = pytest.mark.local_stack

_WORKTREE_ROOT: Path = Path(__file__).resolve().parents[3]
_SECRET_ROOT: Path = (_WORKTREE_ROOT / ".local" / "stack-secrets").resolve()

_DATABASE_NOW = datetime(2026, 8, 16, 12, 0, 0, tzinfo=UTC)
_MASTER_KEY = bytes(range(32))
_ENROLLMENT_PASSWORD = "enrollment-passphrase-value"
_RESET_PASSWORD = "new secure password"
_CLI_PASSWORD_FILE_NAME = "web-credential-cli-password"

#: The reset confirmation is read through ``getpass``, which reads the
#: Windows console (msvcrt) whenever the child interpreter's ``sys.stdin`` is
#: the original handle — piped automation input then never reaches the prompt
#: on a Windows host. Replacing ``sys.stdin`` inside the child selects
#: getpass's documented fallback reader — the same one a Linux CI host
#: without a controlling tty already gets — which reads the pipe and raises
#: ``EOFError`` once it closes.
_RESET_CONFIRMATION_STDIN_SHIM: Final[str] = (
    "import sys; sys.stdin = open(0, closefd=False); from api_runtime.command import main; main()"
)

ENROLLMENT_AUDIT_ACTION = "authentication.web_credential_enrolled"
RESET_AUDIT_ACTION = "authentication.web_authentication_reset"


class HarnessHasher:
    """Hasher double emitting constraint-valid PHC strings for one password."""

    def __init__(self) -> None:
        self.accepted_password: str = _ENROLLMENT_PASSWORD

    def hash_password(self, password: str) -> str:
        digest = hashlib.sha256(password.encode("utf-8")).hexdigest()[:16]
        return f"$argon2id$v=19$m=65536,t=3,p=1${digest}$harnesssecretvalue"

    def verify_password(self, password_hash: str, password: str) -> bool:
        del password_hash
        return password == self.accepted_password

    def needs_rehash(self, password_hash: str) -> bool:
        return False


class DeterministicCrypto:
    """Crypto double deriving stable subkeys and real stdlib HMAC digests."""

    def derive_subkey(self, *, master_key: bytes, label: str) -> bytes:
        return hashlib.sha256(label.encode("ascii") + master_key).digest()

    def hmac_sha256(self, *, key: bytes, message: bytes) -> bytes:
        return hmac.new(key, message, hashlib.sha256).digest()

    def seal_secret(self, *, key: bytes, plaintext: bytes) -> tuple[bytes, bytes]:
        raise AssertionError("sealing is outside these transactions")

    def open_secret(self, *, key: bytes, nonce: bytes, ciphertext: bytes) -> bytes:
        raise AssertionError("opening is outside these transactions")


class FixedClock:
    """Clock double pinning one transaction timestamp."""

    def __init__(self) -> None:
        self.database_now_value = _DATABASE_NOW

    async def database_now(self) -> datetime:
        return self.database_now_value


@dataclass(frozen=True, slots=True)
class SeededAccount:
    """The canonical user/workspace graph one test operates on."""

    user_id: UUID
    workspace_id: UUID
    username: str


@dataclass(frozen=True, slots=True)
class AuthFixture:
    """Real store and services over the disposable stack, pinned clock.

    Every fixture carries its own canonical username so the module-scoped
    shared stack stays isolated per test; the facade methods mirror the
    protected CLI boundary — confirmation and password validation happen
    before the one store transaction, and hashing stays outside it.
    """

    engine: AsyncEngine
    credentials: CredentialStore
    hasher: HarnessHasher
    clock: FixedClock

    @property
    def database_now(self) -> datetime:
        return self.clock.database_now_value

    @staticmethod
    def diagnostic_context() -> DiagnosticContext:
        return create_diagnostic_context().context

    async def seed_canonical_account(self, username: str) -> SeededAccount:
        user_id = uuid4()
        workspace_id = uuid4()
        async with self.engine.begin() as connection:
            await connection.execute(
                sa.insert(users).values(
                    user_id=user_id, username=username, display_name="Credential Owner"
                )
            )
            await connection.execute(
                sa.insert(workspaces).values(
                    workspace_id=workspace_id,
                    owner_user_id=user_id,
                    workspace_key=f"ws-{uuid4().hex[:12]}",
                    display_name="Credential Workspace",
                )
            )
        return SeededAccount(user_id=user_id, workspace_id=workspace_id, username=username)

    async def archive_workspace(self, workspace_id: UUID) -> None:
        """Archive one seeded workspace through the same direct-write seam."""
        async with self.engine.begin() as connection:
            await connection.execute(
                sa.update(workspaces)
                .values(status="archived")
                .where(workspaces.c.workspace_id == workspace_id)
            )

    async def enroll_web_credential(
        self, *, username: str, password: str = _ENROLLMENT_PASSWORD
    ) -> Any:
        self.hasher.accepted_password = password
        return await self.credentials.enroll_web_credential(
            EnrollWebCredentialCommand(
                username=username,
                password_hash=self.hasher.hash_password(password),
                database_now=self.database_now,
                diagnostic_context=self.diagnostic_context(),
            )
        )

    async def web_credential_status(self, *, username: str) -> Any:
        return await self.credentials.resolve_web_credential_status(username=username)

    async def reset_web_authentication(
        self, *, username: str, new_password: str, confirmation: str
    ) -> Any:
        if confirmation != username:
            raise AuthenticationError(ErrorCode.AUTHENTICATION_FAILED)
        validate_new_password(new_password, PasswordBlocklist(digests=()))
        self.hasher.accepted_password = new_password
        return await self.credentials.reset_web_authentication(
            ResetWebAuthenticationCommand(
                username=username,
                new_password_hash=self.hasher.hash_password(new_password),
                database_now=self.database_now,
                diagnostic_context=self.diagnostic_context(),
            )
        )

    async def login(
        self, *, username: str, password: str = _ENROLLMENT_PASSWORD, source_bucket: str
    ) -> Any:
        login_service = LoginService(
            credentials=self.credentials,
            hasher=self.hasher,
            crypto=DeterministicCrypto(),
            master_key=_MASTER_KEY,
            clock=self.clock,
        )
        return await login_service.login(
            username=username,
            password=password,
            source_bucket=source_bucket,
            diagnostic_context=self.diagnostic_context(),
        )

    async def seed_active_totp_and_recovery(self, account: SeededAccount) -> UUID:
        """One active TOTP credential with two unused recovery codes."""
        totp_id = uuid4()
        async with self.engine.begin() as connection:
            await connection.execute(
                sa.insert(totp_credentials).values(
                    totp_credential_id=totp_id,
                    user_id=account.user_id,
                    workspace_id=account.workspace_id,
                    state="active",
                    secret_ciphertext="QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWQ",
                    secret_nonce="c2FsdHNhbHQ",
                    key_id="authkey-reset-active",
                    algorithm="SHA1",
                    digits=6,
                    period_seconds=30,
                    revision=1,
                    created_at=self.database_now - timedelta(days=1),
                    activated_at=self.database_now - timedelta(days=1),
                )
            )
            for code_index in range(2):
                await connection.execute(
                    sa.insert(totp_recovery_codes).values(
                        recovery_code_id=uuid4(),
                        totp_credential_id=totp_id,
                        user_id=account.user_id,
                        workspace_id=account.workspace_id,
                        revision=1,
                        code_hash=hashlib.sha256(
                            f"recovery-{code_index}-{uuid4().hex}".encode()
                        ).hexdigest(),
                        created_at=self.database_now - timedelta(days=1),
                    )
                )
        return totp_id

    async def seed_device_surfaces(self, account: SeededAccount) -> tuple[UUID, UUID, UUID]:
        """One active device with its token family and refresh token."""
        device_id = uuid4()
        family_id = uuid4()
        token_id = uuid4()
        async with self.engine.begin() as connection:
            await connection.execute(
                sa.insert(devices).values(
                    device_id=device_id,
                    workspace_id=account.workspace_id,
                    user_id=account.user_id,
                    device_name="Reset Surface Device",
                    device_kind="obsidian",
                    registered_at=self.database_now - timedelta(days=1),
                )
            )
            await connection.execute(
                sa.insert(device_token_families).values(
                    token_family_id=family_id,
                    user_id=account.user_id,
                    workspace_id=account.workspace_id,
                    device_id=device_id,
                    state="active",
                    current_refresh_generation=1,
                    created_at=self.database_now - timedelta(days=1),
                    last_refreshed_at=self.database_now - timedelta(days=1),
                    inactivity_expires_at=self.database_now + timedelta(days=30),
                    absolute_expires_at=self.database_now + timedelta(days=90),
                )
            )
            await connection.execute(
                sa.insert(device_tokens).values(
                    device_token_id=token_id,
                    token_family_id=family_id,
                    user_id=account.user_id,
                    workspace_id=account.workspace_id,
                    device_id=device_id,
                    token_kind="refresh",
                    generation=1,
                    secret_hash=hashlib.sha256(uuid4().hex.encode("utf-8")).hexdigest(),
                    state="active",
                    derivation_key_id="authkey-reset-refresh",
                    issued_at=self.database_now - timedelta(days=1),
                    expires_at=self.database_now + timedelta(days=30),
                )
            )
        return device_id, family_id, token_id

    async def seed_pending_grant(self) -> UUID:
        grant_id = uuid4()
        async with self.engine.begin() as connection:
            await connection.execute(
                sa.insert(device_authorization_grants).values(
                    grant_id=grant_id,
                    user_code_hash=hashlib.sha256(uuid4().hex.encode("utf-8")).hexdigest(),
                    polling_secret_hash=hashlib.sha256(uuid4().hex.encode("utf-8")).hexdigest(),
                    client_instance_id=uuid4(),
                    device_name="Reset Grant Device",
                    platform_class="obsidian_desktop",
                    platform_name="desktop",
                    plugin_version="1.13.1",
                    requested_scope="obsidian_sync",
                    state="pending",
                    created_at=self.database_now - timedelta(hours=1),
                    expires_at=self.database_now + timedelta(hours=1),
                )
            )
        return grant_id

    async def fetch_one_row(self, statement: sa.Select[tuple[Any]]) -> Any:
        async with self.engine.connect() as connection:
            return (await connection.execute(statement)).one_or_none()

    async def fetch_all_rows(self, statement: sa.Select[tuple[Any]]) -> list[Any]:
        async with self.engine.connect() as connection:
            return list((await connection.execute(statement)).all())

    async def audit_rows(self, action: str, workspace_id: UUID) -> list[Any]:
        return await self.fetch_all_rows(
            sa.select(audit_events).where(
                audit_events.c.action == action,
                audit_events.c.workspace_id == workspace_id,
            )
        )


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
async def database(upgraded_authentication_stack: Any) -> Any:
    settings: DatabaseRuntimeSettings = load_database_runtime_settings(
        environ=upgraded_authentication_stack.alembic_env
    )
    password = SecretStr(upgraded_authentication_stack.password.get_secret_value())
    engine = create_source_store_engine(settings, password)
    try:
        yield AuthFixture(
            engine=engine,
            credentials=CredentialStore(engine),
            hasher=HarnessHasher(),
            clock=FixedClock(),
        )
    finally:
        await dispose_source_store_engine(engine)


# --- enrollment and status ------------------------------------------------------------


@pytest.mark.asyncio
async def test_enrollment_inserts_revision_one_and_refuses_existing(database: Any) -> None:
    account = await database.seed_canonical_account("enroll-owner")
    before = await database.web_credential_status(username="enroll-owner")
    assert before.credential_revision is None

    enrolled = await database.enroll_web_credential(username="enroll-owner")
    assert enrolled.credential_revision == 1
    assert enrolled.user_id == account.user_id
    assert enrolled.workspace_id == account.workspace_id

    status = await database.web_credential_status(username="enroll-owner")
    assert status.credential_revision == 1
    credential_row = await database.fetch_one_row(
        sa.select(user_credentials).where(user_credentials.c.user_id == account.user_id)
    )
    assert credential_row.credential_revision == 1
    assert credential_row.password_changed_at == database.database_now
    enrolled_audits = await database.audit_rows(ENROLLMENT_AUDIT_ACTION, account.workspace_id)
    assert len(enrolled_audits) == 1
    assert enrolled_audits[0].result == "succeeded"
    assert enrolled_audits[0].actor_id == account.user_id

    with pytest.raises(AuthenticationError) as refused:
        await database.enroll_web_credential(username="enroll-owner")
    assert refused.value.error_code is ErrorCode.AUTHENTICATION_FAILED
    unchanged = await database.fetch_one_row(
        sa.select(user_credentials).where(user_credentials.c.user_id == account.user_id)
    )
    assert unchanged.password_hash == credential_row.password_hash
    assert unchanged.credential_revision == 1
    assert len(await database.audit_rows(ENROLLMENT_AUDIT_ACTION, account.workspace_id)) == 1


@pytest.mark.asyncio
async def test_status_of_unknown_username_fails_closed(database: Any) -> None:
    with pytest.raises(AuthenticationError) as rejected:
        await database.web_credential_status(username="missing-owner")
    assert rejected.value.error_code is ErrorCode.AUTHENTICATION_FAILED


# --- emergency reset -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reset_before_any_surfaces_exist_closes_every_count_at_zero(
    database: Any,
) -> None:
    account = await database.seed_canonical_account("empty-reset-owner")
    await database.enroll_web_credential(username="empty-reset-owner")

    result = await database.reset_web_authentication(
        username="empty-reset-owner",
        new_password=_RESET_PASSWORD,
        confirmation="empty-reset-owner",
    )
    assert result.credential_revision == 2
    assert result.revoked_web_session_count == 0
    assert result.revoked_device_count == 0
    assert result.revoked_token_family_count == 0
    assert result.revoked_device_token_count == 0
    assert result.replaced_totp_credential_count == 0
    assert result.disabled_recovery_code_count == 0
    assert result.denied_grant_count == 0
    reset_audits = await database.audit_rows(RESET_AUDIT_ACTION, account.workspace_id)
    assert len(reset_audits) == 1
    assert reset_audits[0].result == "succeeded"

    # A workspace with no enrollment at all refuses the reset closed — the
    # operation is never create-or-return, so enrollment stays its only door.
    await database.seed_canonical_account("unenrolled-reset-owner")
    with pytest.raises(AuthenticationError) as unenrolled:
        await database.reset_web_authentication(
            username="unenrolled-reset-owner",
            new_password=_RESET_PASSWORD,
            confirmation="unenrolled-reset-owner",
        )
    assert unenrolled.value.error_code is ErrorCode.AUTHENTICATION_FAILED
    unenrolled_status = await database.web_credential_status(username="unenrolled-reset-owner")
    assert unenrolled_status.credential_revision is None


@pytest.mark.asyncio
async def test_reset_confirmation_must_equal_canonical_username(database: Any) -> None:
    await database.seed_canonical_account("confirm-owner")
    await database.enroll_web_credential(username="confirm-owner")
    with pytest.raises(AuthenticationError) as rejected:
        await database.reset_web_authentication(
            username="confirm-owner", new_password=_RESET_PASSWORD, confirmation="typo"
        )
    assert rejected.value.error_code is ErrorCode.AUTHENTICATION_FAILED
    # The refused reset left the enrolled credential untouched.
    status = await database.web_credential_status(username="confirm-owner")
    assert status.credential_revision == 1


@pytest.mark.asyncio
async def test_reset_on_archived_workspace_is_authentication_failed(
    database: Any, upgraded_authentication_stack: Any
) -> None:
    """Every protected surface of an archived workspace fails closed.

    The archived workspace drops out of the canonical identity resolution, so
    both the store transaction and the real CLI reset answer the generic
    ``authentication_failed`` rejection — the CLI as exit code 78 — while the
    enrolled credential row stays exactly as it was.
    """
    username = f"archived-owner-{uuid4().hex[:10]}"
    account = await database.seed_canonical_account(username)
    await database.enroll_web_credential(username=username)
    await database.archive_workspace(account.workspace_id)

    with pytest.raises(AuthenticationError) as rejected:
        await database.reset_web_authentication(
            username=username, new_password=_RESET_PASSWORD, confirmation=username
        )
    assert rejected.value.error_code is ErrorCode.AUTHENTICATION_FAILED

    environment = dict(upgraded_authentication_stack.alembic_env)
    password_file = _SECRET_ROOT / _CLI_PASSWORD_FILE_NAME
    password_file.write_text("cli-emergency-passphrase\n", encoding="utf-8")
    try:
        reset = subprocess.run(
            [
                sys.executable,
                "-c",
                _RESET_CONFIRMATION_STDIN_SHIM,
                "reset-web-authentication",
                "--username",
                username,
                "--password-file-name",
                _CLI_PASSWORD_FILE_NAME,
            ],
            cwd=str(_WORKTREE_ROOT),
            env=environment,
            capture_output=True,
            text=True,
            input=f"{username}\n",
            check=False,
        )
        assert reset.returncode == 78
        assert "authentication_failed" in reset.stderr
        assert "reset=true" not in reset.stdout
        assert "cli-emergency-passphrase" not in reset.stdout + reset.stderr
    finally:
        with suppress(OSError):
            password_file.unlink()

    status = await database.web_credential_status(username=username)
    assert status.credential_revision == 1


@pytest.mark.asyncio
async def test_emergency_reset_revokes_every_auth_surface(database: Any) -> None:
    account = await database.seed_canonical_account("owner")
    await database.enroll_web_credential(username="owner")
    first_login = await database.login(username="owner", source_bucket="198.51.100.10")
    second_login = await database.login(username="owner", source_bucket="198.51.100.11")
    assert first_login.public_error is None
    assert second_login.public_error is None
    totp_id = await database.seed_active_totp_and_recovery(account)
    device_id, family_id, token_id = await database.seed_device_surfaces(account)
    grant_id = await database.seed_pending_grant()

    result = await database.reset_web_authentication(
        username="owner", new_password=_RESET_PASSWORD, confirmation="owner"
    )
    assert result.revoked_web_session_count == 2
    assert result.revoked_device_count == 1
    assert result.denied_grant_count == 1
    assert result.credential_revision == 2
    assert result.revoked_token_family_count == 1
    assert result.revoked_device_token_count == 1
    assert result.replaced_totp_credential_count == 1
    assert result.disabled_recovery_code_count == 2

    credential_row = await database.fetch_one_row(
        sa.select(user_credentials).where(user_credentials.c.user_id == account.user_id)
    )
    assert credential_row.credential_revision == 2
    assert credential_row.password_hash == database.hasher.hash_password(_RESET_PASSWORD)
    assert credential_row.password_changed_at == database.database_now

    session_rows = await database.fetch_all_rows(
        sa.select(web_sessions).where(web_sessions.c.user_id == account.user_id)
    )
    assert len(session_rows) == 2
    for row in session_rows:
        assert row.state == "revoked"
        assert row.revoked_at == database.database_now
        assert row.revocation_reason == "emergency_reset"
        assert row.authenticated_at is None
        assert row.reauthenticated_at is None

    device_row = await database.fetch_one_row(
        sa.select(devices).where(devices.c.device_id == device_id)
    )
    assert device_row.status == "revoked"
    assert device_row.revoked_at == database.database_now
    family_row = await database.fetch_one_row(
        sa.select(device_token_families).where(device_token_families.c.token_family_id == family_id)
    )
    assert family_row.state == "revoked"
    assert family_row.revocation_reason == "emergency_reset"
    token_row = await database.fetch_one_row(
        sa.select(device_tokens).where(device_tokens.c.device_token_id == token_id)
    )
    assert token_row.state == "revoked"
    assert token_row.revoked_at == database.database_now
    grant_row = await database.fetch_one_row(
        sa.select(device_authorization_grants).where(
            device_authorization_grants.c.grant_id == grant_id
        )
    )
    assert grant_row.state == "denied"
    assert grant_row.denied_at == database.database_now
    totp_row = await database.fetch_one_row(
        sa.select(totp_credentials).where(totp_credentials.c.totp_credential_id == totp_id)
    )
    assert totp_row.state == "replaced"
    assert totp_row.replaced_at == database.database_now
    assert totp_row.enrollment_expires_at is None
    recovery_rows = await database.fetch_all_rows(
        sa.select(totp_recovery_codes).where(totp_recovery_codes.c.totp_credential_id == totp_id)
    )
    assert len(recovery_rows) == 2
    assert all(row.used_at == database.database_now for row in recovery_rows)

    assert len(await database.audit_rows(RESET_AUDIT_ACTION, account.workspace_id)) == 1

    # The revoked session no longer authenticates, and the new password logs
    # in at the bumped revision.
    session_service = SessionService(
        sessions=WebSessionStore(database.engine),
        hasher=database.hasher,
        crypto=DeterministicCrypto(),
        master_key=_MASTER_KEY,
        clock=database.clock,
    )
    with pytest.raises(AuthenticationError):
        await session_service.authenticate(
            session_secret=first_login.started_session.session_secret
        )
    relogin = await database.login(
        username="owner", password=_RESET_PASSWORD, source_bucket="198.51.100.12"
    )
    assert relogin.public_error is None
    assert relogin.started_session is not None
    assert relogin.started_session.credential_revision == 2


# --- the real CLI end to end ------------------------------------------------------------


@pytest.mark.asyncio
async def test_cli_enroll_status_and_reset_end_to_end(
    database: Any, upgraded_authentication_stack: Any
) -> None:
    username = f"cli-owner-{uuid4().hex[:10]}"
    await database.seed_canonical_account(username)
    environment = dict(upgraded_authentication_stack.alembic_env)
    password_file = _SECRET_ROOT / _CLI_PASSWORD_FILE_NAME
    password_file.write_text("cli-emergency-passphrase\n", encoding="utf-8")
    try:
        enroll = subprocess.run(
            [
                sys.executable,
                "-m",
                "api_runtime.command",
                "enroll-web-credential",
                "--username",
                username,
                "--password-file-name",
                _CLI_PASSWORD_FILE_NAME,
            ],
            cwd=str(_WORKTREE_ROOT),
            env=environment,
            capture_output=True,
            text=True,
            input="",
            check=False,
        )
        assert enroll.returncode == 0, enroll.stderr
        assert "enrolled=true credential_revision=1" in enroll.stdout
        assert "cli-emergency-passphrase" not in enroll.stdout + enroll.stderr

        status = subprocess.run(
            [
                sys.executable,
                "-m",
                "api_runtime.command",
                "web-credential-status",
                "--username",
                username,
            ],
            cwd=str(_WORKTREE_ROOT),
            env=environment,
            capture_output=True,
            text=True,
            input="",
            check=False,
        )
        assert status.returncode == 0, status.stderr
        assert "enrolled=true credential_revision=1" in status.stdout

        # A closed confirmation prompt is a typed abort (exit 2), never an
        # internal error, and leaves the enrolled credential untouched.
        aborted_reset = subprocess.run(
            [
                sys.executable,
                "-c",
                _RESET_CONFIRMATION_STDIN_SHIM,
                "reset-web-authentication",
                "--username",
                username,
                "--password-file-name",
                _CLI_PASSWORD_FILE_NAME,
            ],
            cwd=str(_WORKTREE_ROOT),
            env=environment,
            capture_output=True,
            text=True,
            input="",
            check=False,
        )
        assert aborted_reset.returncode == 2
        assert "reset confirmation input closed" in aborted_reset.stderr
        assert "internal_error" not in aborted_reset.stderr

        reset = subprocess.run(
            [
                sys.executable,
                "-c",
                _RESET_CONFIRMATION_STDIN_SHIM,
                "reset-web-authentication",
                "--username",
                username,
                "--password-file-name",
                _CLI_PASSWORD_FILE_NAME,
            ],
            cwd=str(_WORKTREE_ROOT),
            env=environment,
            capture_output=True,
            text=True,
            input=f"{username}\n",
            check=False,
        )
        assert reset.returncode == 0, reset.stderr
        assert "credential_revision=2" in reset.stdout
        assert "cli-emergency-passphrase" not in reset.stdout + reset.stderr

        reset_status = subprocess.run(
            [
                sys.executable,
                "-m",
                "api_runtime.command",
                "web-credential-status",
                "--username",
                username,
            ],
            cwd=str(_WORKTREE_ROOT),
            env=environment,
            capture_output=True,
            text=True,
            input="",
            check=False,
        )
        assert reset_status.returncode == 0, reset_status.stderr
        assert "enrolled=true credential_revision=2" in reset_status.stdout
    finally:
        with suppress(OSError):
            password_file.unlink()
