"""Password/session transactions against the real migrated authentication schema.

Every test drives the real :class:`CredentialStore`/:class:`WebSessionStore`
and the real domain services over a disposable PostgreSQL 18.4 stack upgraded
to the authentication head. Only the four ports that are not PostgreSQL stay
deterministic doubles: the hasher counts verifier calls, the crypto adapter
derives stable HMAC subkeys, and the clock pins one transaction timestamp so
expiry and lock assertions are exact. The tests prove the binding contracts:
unknown user and wrong password are indistinguishable and cost exactly one
hasher call each; the fifth failure locks both buckets for exactly fifteen
minutes and a locked bucket rejects before verifier work, recording the
dedicated ``authentication.login_locked_out`` audit action for both a correct
and a wrong password while the unlocked wrong-password attempts keep the
generic ``authentication.login_rejected`` rows; success resets the
streak and inserts an ``active`` or ``pending_totp`` row with the exact idle
and absolute expiry; session resolution slides the idle window without ever
passing the absolute boundary; logout clears the authenticated timestamps the
schema matrix demands; password change bumps the revision, revokes the other
sessions and rotates the current binding; and ``required_key_ids`` returns
exactly the referenced key set.
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

from personal_os.authentication.errors import AuthenticationError
from personal_os.authentication.passwords import PasswordBlocklist
from personal_os.authentication.sessions import (
    DUMMY_LOGIN_PHC_HASH,
    LoginService,
    PasswordChangeService,
    RecordLoginFailureCommand,
    SessionService,
    ThrottleBucketKind,
    derive_throttle_hmac_key,
    throttle_bucket_hash,
)
from personal_os.diagnostics.context import DiagnosticContext, create_diagnostic_context
from personal_os.error_contracts.codes import ErrorCode
from postgresql_source_store.authentication_credentials import CredentialStore
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
    authentication_throttle_buckets,
    device_token_families,
    device_tokens,
    devices,
    totp_credentials,
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
_WRONG_PASSWORD = "sentinel-wrong-password-value"
_MASTER_KEY = bytes(range(32))


class CountingPasswordHasher:
    """Hasher double counting verifier calls against one accepted password."""

    def __init__(self) -> None:
        self.verify_calls = 0

    def hash_password(self, password: str) -> str:
        digest = hashlib.sha256(password.encode("utf-8")).hexdigest()[:16]
        return f"$argon2id$v=19$m=65536,t=3,p=1${digest}$rehashedsecretvalue"

    def verify_password(self, password_hash: str, password: str) -> bool:
        self.verify_calls += 1
        return password == _CORRECT_PASSWORD

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
    """Clock double pinning one controllable transaction timestamp."""

    def __init__(self) -> None:
        self.database_now_value = _DATABASE_NOW

    async def database_now(self) -> datetime:
        return self.database_now_value


@dataclass(frozen=True, slots=True)
class SeededAccount:
    """The trusted user/workspace/credential graph one test operates on."""

    user_id: UUID
    workspace_id: UUID
    username: str


class TransactionHarness:
    """Real stores and services over the disposable stack, pinned clock.

    Every harness instance carries its own canonical username, source bucket
    and workspace so the module-scoped shared stack stays isolated per test;
    the row-inspection helpers scope to exactly those identities.
    """

    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine
        self.clock = FixedClock()
        self.hasher = CountingPasswordHasher()
        self.crypto = DeterministicCrypto()
        self.credentials = CredentialStore(engine)
        self.web_sessions = WebSessionStore(engine)
        unique_suffix = uuid4().hex[:10]
        self.username = f"tx-owner-{unique_suffix}"
        self.source_bucket = f"source-{unique_suffix}"
        self.workspace_id: UUID | None = None
        self.login_service = LoginService(
            credentials=self.credentials,
            hasher=self.hasher,
            crypto=self.crypto,
            master_key=_MASTER_KEY,
            clock=self.clock,
        )
        self.session_service = SessionService(
            sessions=self.web_sessions,
            hasher=self.hasher,
            crypto=self.crypto,
            master_key=_MASTER_KEY,
            clock=self.clock,
        )
        self.password_service = PasswordChangeService(
            session_service=self.session_service,
            credentials=self.credentials,
            hasher=self.hasher,
            blocklist=PasswordBlocklist(digests=()),
        )

    @property
    def database_now(self) -> datetime:
        return self.clock.database_now_value

    @property
    def bucket_hashes(self) -> tuple[str, str]:
        """This harness's username and source bucket hashes."""
        throttle_key = derive_throttle_hmac_key(self.crypto, _MASTER_KEY)
        return (
            throttle_bucket_hash(
                hmac_key=throttle_key,
                bucket_kind=ThrottleBucketKind.LOGIN_USERNAME,
                bucket_material=self.username,
            ),
            throttle_bucket_hash(
                hmac_key=throttle_key,
                bucket_kind=ThrottleBucketKind.LOGIN_SOURCE,
                bucket_material=self.source_bucket,
            ),
        )

    async def seed_account(self) -> SeededAccount:
        user_id = uuid4()
        workspace_id = uuid4()
        self.workspace_id = workspace_id
        async with self.engine.begin() as connection:
            await connection.execute(
                sa.insert(users).values(
                    user_id=user_id, username=self.username, display_name="Transaction Owner"
                )
            )
            await connection.execute(
                sa.insert(workspaces).values(
                    workspace_id=workspace_id,
                    owner_user_id=user_id,
                    workspace_key=f"ws-{uuid4().hex[:12]}",
                    display_name="Transaction Workspace",
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

    async def login(
        self,
        *,
        username: str | None = None,
        password: str = _CORRECT_PASSWORD,
        source_bucket: str | None = None,
    ) -> Any:
        return await self.login_service.login(
            username=username if username is not None else self.username,
            password=password,
            source_bucket=source_bucket if source_bucket is not None else self.source_bucket,
            diagnostic_context=self.diagnostic_context(),
        )

    async def reject_login(self) -> Any:
        return await self.login(password=_WRONG_PASSWORD)

    @staticmethod
    def diagnostic_context() -> DiagnosticContext:
        return create_diagnostic_context().context

    async def fetch_one_row(self, statement: sa.Select[tuple[Any]]) -> Any:
        async with self.engine.connect() as connection:
            return (await connection.execute(statement)).one_or_none()

    async def throttle_rows(self) -> list[Any]:
        username_bucket_hash, source_bucket_hash = self.bucket_hashes
        statement = (
            sa.select(authentication_throttle_buckets)
            .where(
                authentication_throttle_buckets.c.bucket_hash.in_(
                    (username_bucket_hash, source_bucket_hash)
                )
            )
            .order_by(authentication_throttle_buckets.c.bucket_kind)
        )
        async with self.engine.connect() as connection:
            return list((await connection.execute(statement)).all())

    async def audit_rows(self, action: str) -> list[Any]:
        assert self.workspace_id is not None, "seed_account must run first"
        statement = sa.select(audit_events).where(
            audit_events.c.action == action,
            audit_events.c.workspace_id == self.workspace_id,
        )
        async with self.engine.connect() as connection:
            return list((await connection.execute(statement)).all())

    async def session_row(self, web_session_id: UUID) -> Any:
        return await self.fetch_one_row(
            sa.select(web_sessions).where(web_sessions.c.web_session_id == web_session_id)
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
async def harness(upgraded_authentication_stack: Any) -> Any:
    settings: DatabaseRuntimeSettings = load_database_runtime_settings(
        environ=upgraded_authentication_stack.alembic_env
    )
    password = SecretStr(upgraded_authentication_stack.password.get_secret_value())
    engine = create_source_store_engine(settings, password)
    try:
        yield TransactionHarness(engine)
    finally:
        await dispose_source_store_engine(engine)


# --- login choreography over the real schema ------------------------------------


@pytest.mark.asyncio
async def test_unknown_and_wrong_password_both_call_hasher_once(harness: Any) -> None:
    await harness.seed_account()
    unknown = await harness.login(username="missing", password="sentinel")
    wrong = await harness.login(password="sentinel")
    assert unknown.public_error == wrong.public_error == ErrorCode.AUTHENTICATION_FAILED
    assert harness.hasher.verify_calls == 2
    # The rejected audit exists only behind the trusted account boundary: the
    # unknown username wrote no audit row, the trusted one exactly one.
    rejected_audits = await harness.audit_rows("authentication.login_rejected")
    assert len(rejected_audits) == 1
    assert rejected_audits[0].actor_id is not None
    # The known username bucket counted once and the shared source bucket
    # counted both rejected attempts.
    buckets = await harness.throttle_rows()
    assert len(buckets) == 2
    source_rows = [row for row in buckets if row.bucket_kind == "login_source"]
    username_rows = [row for row in buckets if row.bucket_kind == "login_username"]
    assert len(source_rows) == 1
    assert len(username_rows) == 1
    assert source_rows[0].failed_attempt_count == 2
    assert username_rows[0].failed_attempt_count == 1
    for row in buckets:
        assert row.locked_until is None
        assert len(row.bucket_hash) == 64
        assert row.bucket_hash != hashlib.sha256(harness.source_bucket.encode("utf-8")).hexdigest()
        assert row.bucket_hash != hashlib.sha256(harness.username.encode("utf-8")).hexdigest()


@pytest.mark.asyncio
async def test_fifth_failure_locks_bucket_for_fifteen_minutes(harness: Any) -> None:
    await harness.seed_account()
    outcomes = [await harness.reject_login() for _ in range(5)]
    assert outcomes[-1].locked_until == harness.database_now + timedelta(minutes=15)
    buckets = await harness.throttle_rows()
    assert len(buckets) == 2
    for row in buckets:
        assert row.failed_attempt_count == 5
        assert row.window_started_at == harness.database_now
        assert row.locked_until == harness.database_now + timedelta(minutes=15)
    # A locked bucket rejects before any verifier work.
    locked = await harness.login()
    assert locked.public_error is ErrorCode.AUTHENTICATION_RATE_LIMITED
    assert locked.locked_until == harness.database_now + timedelta(minutes=15)
    assert harness.hasher.verify_calls == 5


@pytest.mark.asyncio
async def test_locked_rejections_use_the_dedicated_audit_action(harness: Any) -> None:
    await harness.seed_account()
    for _ in range(5):
        await harness.reject_login()
    # Locked with the correct password and locked with a wrong password both
    # reject rate-limited and both record the dedicated lockout action.
    locked_correct = await harness.login()
    locked_wrong = await harness.login(password=_WRONG_PASSWORD)
    assert locked_correct.public_error is ErrorCode.AUTHENTICATION_RATE_LIMITED
    assert locked_wrong.public_error is ErrorCode.AUTHENTICATION_RATE_LIMITED
    locked_out_audits = await harness.audit_rows("authentication.login_locked_out")
    assert len(locked_out_audits) == 2
    for row in locked_out_audits:
        assert row.result == "rejected"
        assert row.reason_code is None
        assert row.actor_id is not None
    # The pre-lock wrong-password attempts on the unlocked account stay the
    # only generic rejection rows: unlocked+wrong → login_rejected.
    rejected_audits = await harness.audit_rows("authentication.login_rejected")
    assert len(rejected_audits) == 5
    # The locked branch never touches the throttle rows it read.
    for row in await harness.throttle_rows():
        assert row.failed_attempt_count == 5
        assert row.window_started_at == harness.database_now
        assert row.locked_until == harness.database_now + timedelta(minutes=15)


@pytest.mark.asyncio
async def test_concurrent_cold_bucket_first_failures_settle_one_row(harness: Any) -> None:
    # Two unknown-account first failures race on the same cold bucket: each
    # ``record_login_failure`` transaction runs on its own connection, both see
    # no row under the lock, and both try the first insert. Both attempts must
    # settle — the insert loser re-locks the winner's row and continues through
    # the update path — leaving exactly one row per bucket with both strikes.
    username_bucket_hash, source_bucket_hash = harness.bucket_hashes
    command = RecordLoginFailureCommand(
        username_bucket_hash=username_bucket_hash,
        source_bucket_hash=source_bucket_hash,
        user_id=None,
        workspace_id=None,
        database_now=harness.database_now,
        diagnostic_context=harness.diagnostic_context(),
    )
    recorded = await asyncio.gather(
        harness.credentials.record_login_failure(command),
        harness.credentials.record_login_failure(command),
    )
    # The loser must not escape as internal_error: both transactions return.
    assert sorted(outcome.username_bucket.failed_attempt_count for outcome in recorded) == [1, 2]
    assert sorted(outcome.source_bucket.failed_attempt_count for outcome in recorded) == [1, 2]
    rows = await harness.throttle_rows()
    assert len(rows) == 2
    for row in rows:
        assert row.failed_attempt_count == 2
        assert row.locked_until is None


@pytest.mark.asyncio
async def test_successful_login_resets_streak_and_starts_active_session(harness: Any) -> None:
    await harness.seed_account()
    await harness.reject_login()
    await harness.reject_login()
    outcome = await harness.login()
    assert outcome.public_error is None
    started = outcome.started_session
    assert started is not None
    row = await harness.session_row(started.web_session_id)
    assert row.state == "active"
    assert row.authentication_method == "password"
    assert row.credential_revision == 1
    assert row.authenticated_at == harness.database_now
    assert row.created_at == harness.database_now
    assert row.idle_expires_at == harness.database_now + timedelta(hours=12)
    assert row.absolute_expires_at == harness.database_now + timedelta(days=7)
    assert (
        row.session_secret_hash
        == hashlib.sha256(started.session_secret.encode("utf-8")).hexdigest()
    )
    assert row.csrf_secret_hash == started.csrf_secret_hash
    buckets = await harness.throttle_rows()
    username_bucket = next(row for row in buckets if row.bucket_kind == "login_username")
    source_bucket = next(row for row in buckets if row.bucket_kind == "login_source")
    assert username_bucket.failed_attempt_count == 0
    assert username_bucket.locked_until is None
    # Only the credential streak resets; the source streak persists.
    assert source_bucket.failed_attempt_count == 2
    succeeded_audits = await harness.audit_rows("authentication.login_succeeded")
    assert len(succeeded_audits) == 1


@pytest.mark.asyncio
async def test_login_with_active_totp_starts_pending_totp_session(harness: Any) -> None:
    account = await harness.seed_account()
    async with harness.engine.begin() as connection:
        await connection.execute(
            sa.insert(totp_credentials).values(
                totp_credential_id=uuid4(),
                user_id=account.user_id,
                workspace_id=account.workspace_id,
                state="active",
                secret_ciphertext="QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWQ",
                secret_nonce="c2FsdHNhbHQ",
                key_id="authkey-current",
                algorithm="SHA1",
                digits=6,
                period_seconds=30,
                revision=1,
                created_at=harness.database_now - timedelta(minutes=1),
                activated_at=harness.database_now - timedelta(minutes=1),
            )
        )
    outcome = await harness.login()
    started = outcome.started_session
    assert started is not None
    row = await harness.session_row(started.web_session_id)
    assert row.state == "pending_totp"
    assert row.authenticated_at is None
    assert row.idle_expires_at == harness.database_now + timedelta(minutes=5)
    assert row.absolute_expires_at == harness.database_now + timedelta(days=7)


# --- session lifecycle ------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_authentication_slides_idle_capped_at_absolute(harness: Any) -> None:
    await harness.seed_account()
    started = (await harness.login()).started_session
    assert started is not None

    # Shrink the absolute boundary to seventeen hours so the twelve-hour idle
    # slide would cross it: the slide must clamp to the absolute expiry.
    absolute_expiry = _DATABASE_NOW + timedelta(hours=17)
    async with harness.engine.begin() as connection:
        await connection.execute(
            sa.update(web_sessions)
            .values(absolute_expires_at=absolute_expiry)
            .where(web_sessions.c.web_session_id == started.web_session_id)
        )
    harness.clock.database_now_value = _DATABASE_NOW + timedelta(hours=6)
    authenticated = await harness.session_service.authenticate(
        session_secret=started.session_secret
    )
    assert authenticated.context.web_session_id == started.web_session_id
    from personal_os.authentication.contracts import AUTHENTICATED_WEB_SCOPES

    assert authenticated.context.scopes == AUTHENTICATED_WEB_SCOPES
    row = await harness.session_row(started.web_session_id)
    assert row.last_seen_at == harness.database_now
    assert row.idle_expires_at == absolute_expiry
    assert row.absolute_expires_at == absolute_expiry

    harness.clock.database_now_value = absolute_expiry + timedelta(seconds=1)
    with pytest.raises(AuthenticationError) as expired:
        await harness.session_service.authenticate(session_secret=started.session_secret)
    assert expired.value.error_code is ErrorCode.AUTHENTICATION_REQUIRED


@pytest.mark.asyncio
async def test_logout_revocation_clears_authenticated_timestamps(harness: Any) -> None:
    await harness.seed_account()
    started = (await harness.login()).started_session
    assert started is not None
    revoked_at = await harness.session_service.revoke(session_secret=started.session_secret)
    assert revoked_at == harness.database_now
    row = await harness.session_row(started.web_session_id)
    assert row.state == "revoked"
    assert row.revoked_at == harness.database_now
    assert row.revocation_reason == "logout"
    assert row.authenticated_at is None
    assert row.reauthenticated_at is None
    with pytest.raises(AuthenticationError):
        await harness.session_service.authenticate(session_secret=started.session_secret)


@pytest.mark.asyncio
async def test_reauthenticate_rotates_and_records_the_moment(harness: Any) -> None:
    await harness.seed_account()
    started = (await harness.login()).started_session
    assert started is not None
    failed = await harness.session_service.reauthenticate(
        session_secret=started.session_secret, password=_WRONG_PASSWORD
    )
    assert failed.public_error is ErrorCode.AUTHENTICATION_FAILED
    succeeded = await harness.session_service.reauthenticate(
        session_secret=started.session_secret, password=_CORRECT_PASSWORD
    )
    assert succeeded.public_error is None
    rotated = succeeded.rotated_session
    assert rotated is not None
    assert rotated.session_secret != started.session_secret
    row = await harness.session_row(started.web_session_id)
    assert row.reauthenticated_at == harness.database_now
    assert row.session_secret_hash == rotated.session_secret_hash
    with pytest.raises(AuthenticationError):
        await harness.session_service.authenticate(session_secret=started.session_secret)
    authenticated = await harness.session_service.authenticate(
        session_secret=rotated.session_secret
    )
    assert authenticated.context.web_session_id == started.web_session_id


# --- password change ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_password_change_revokes_others_and_rotates_current(harness: Any) -> None:
    await harness.seed_account()
    current = (await harness.login()).started_session
    other = (await harness.login(source_bucket="198.51.100.9")).started_session
    assert current is not None
    assert other is not None
    result = await harness.password_service.change_password(
        session_secret=current.session_secret,
        new_password="fresh-rotation-passphrase-value",
        diagnostic_context=harness.diagnostic_context(),
    )
    assert result.public_error is None
    assert result.credential_revision == 2
    assert result.revoked_session_count == 1
    rotated = result.rotated_session
    assert rotated is not None
    credential_row = await harness.fetch_one_row(
        sa.select(user_credentials).where(user_credentials.c.user_id == current.user_id)
    )
    assert credential_row.credential_revision == 2
    assert credential_row.password_changed_at == harness.database_now
    other_row = await harness.session_row(other.web_session_id)
    assert other_row.state == "revoked"
    assert other_row.revocation_reason == "password_changed"
    assert other_row.authenticated_at is None
    assert other_row.reauthenticated_at is None
    current_row = await harness.session_row(current.web_session_id)
    assert current_row.state == "active"
    assert current_row.credential_revision == 2
    assert current_row.session_secret_hash == rotated.session_secret_hash
    with pytest.raises(AuthenticationError):
        await harness.session_service.authenticate(session_secret=other.session_secret)
    with pytest.raises(AuthenticationError):
        await harness.session_service.authenticate(session_secret=current.session_secret)
    authenticated = await harness.session_service.authenticate(
        session_secret=rotated.session_secret
    )
    assert authenticated.context.credential_revision == 2
    changed_audits = await harness.audit_rows("authentication.password_changed")
    assert len(changed_audits) == 1


# --- keyring reference resolution ----------------------------------------------------


@pytest.mark.asyncio
async def test_required_key_ids_returns_referenced_keys_only(harness: Any) -> None:
    account = await harness.seed_account()
    async with harness.engine.begin() as connection:
        active_totp_id = uuid4()
        replaced_totp_id = uuid4()
        for totp_id, state in ((active_totp_id, "active"), (replaced_totp_id, "replaced")):
            await connection.execute(
                sa.insert(totp_credentials).values(
                    totp_credential_id=totp_id,
                    user_id=account.user_id,
                    workspace_id=account.workspace_id,
                    state=state,
                    secret_ciphertext="QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWQ",
                    secret_nonce="c2FsdHNhbHQ",
                    key_id=(
                        "authkey-totp-active" if state == "active" else "authkey-totp-replaced"
                    ),
                    algorithm="SHA1",
                    digits=6,
                    period_seconds=30,
                    revision=1,
                    created_at=harness.database_now - timedelta(days=2),
                    activated_at=harness.database_now - timedelta(days=2),
                    replaced_at=(
                        harness.database_now - timedelta(days=1) if state == "replaced" else None
                    ),
                )
            )
        active_family_id = uuid4()
        expired_family_id = uuid4()
        device_ids: dict[UUID, UUID] = {}
        for family_id, device_suffix in (
            (active_family_id, "active"),
            (expired_family_id, "expired"),
        ):
            device_id = uuid4()
            device_ids[family_id] = device_id
            await connection.execute(
                sa.insert(devices).values(
                    device_id=device_id,
                    workspace_id=account.workspace_id,
                    user_id=account.user_id,
                    device_name=f"Key Reference Device {device_suffix}",
                    device_kind="obsidian",
                    registered_at=harness.database_now - timedelta(days=1),
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
                    created_at=harness.database_now - timedelta(days=1),
                    last_refreshed_at=harness.database_now - timedelta(days=1),
                    inactivity_expires_at=harness.database_now + timedelta(days=30),
                    absolute_expires_at=harness.database_now + timedelta(days=90),
                )
            )
        for family_id, derivation_key_id, expires_at in (
            (
                active_family_id,
                "authkey-refresh-active",
                harness.database_now + timedelta(days=30),
            ),
            (
                expired_family_id,
                "authkey-refresh-expired",
                harness.database_now - timedelta(days=1),
            ),
        ):
            await connection.execute(
                sa.insert(device_tokens).values(
                    device_token_id=uuid4(),
                    token_family_id=family_id,
                    user_id=account.user_id,
                    workspace_id=account.workspace_id,
                    device_id=device_ids[family_id],
                    token_kind="refresh",
                    generation=1,
                    secret_hash=hashlib.sha256(derivation_key_id.encode("utf-8")).hexdigest(),
                    state="active",
                    derivation_key_id=derivation_key_id,
                    issued_at=harness.database_now - timedelta(days=1),
                    expires_at=expires_at,
                )
            )
    required = await harness.credentials.required_key_ids(database_now=harness.database_now)
    # The query is global over shared state, so assert on this test's own
    # referenced and excluded keys rather than the whole set.
    assert "authkey-totp-active" in required
    assert "authkey-refresh-active" in required
    assert "authkey-totp-replaced" not in required
    assert "authkey-refresh-expired" not in required
