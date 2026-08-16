"""TOTP enrollment, replay, recovery and disable transactions on a real stack.

Every test drives the real :class:`TotpStore`, the real domain
:class:`personal_os.authentication.totp.TotpService` and the real
AES-256-GCM/HKDF codec adapter over a disposable PostgreSQL 18.4 stack
upgraded to the authentication head. Only the non-PostgreSQL ports stay
deterministic doubles: the hasher counts verifier calls and the clock pins one
transaction timestamp so expiry, enrollment and replay assertions are exact.
The tests prove the binding contracts of design sections 10.1-10.3: a pending
enrollment expires after ten minutes; the same TOTP step is accepted exactly
once under a real concurrent race (``asyncio.gather`` against the row lock);
the ±1 window holds and drift beyond it fails safely; a previous-key secret is
re-encrypted with the current key under the same lock before commit;
activation creates ten hashed one-use recovery codes; recovery consumes
exactly one code under a concurrent race and transitions only into
``recovery_limited``; regeneration invalidates the prior revision; and disable
revokes every recovery code, increments the credential revision, revokes the
other Web sessions and rotates the current session to password-only.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
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
from api_runtime.authentication_composition import KeyringTotpSecretCodec
from api_runtime.authentication_crypto import (
    AuthenticationKeyring,
    CryptographyAuthenticationCrypto,
)
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from personal_os.authentication.crypto import TOTP_SECRET_AEAD_LABEL
from personal_os.authentication.errors import AuthenticationError
from personal_os.authentication.sessions import (
    DUMMY_LOGIN_PHC_HASH,
    session_secret_hash_of,
)
from personal_os.authentication.totp import (
    RECOVERY_CODE_COUNT,
    TOTP_ENROLLMENT_EXPIRY,
    ActivateEnrollmentCommand,
    DisableTotpCommand,
    InsertPendingEnrollmentCommand,
    RecoveredSession,
    RecoverSessionCommand,
    RegenerateRecoveryCodesCommand,
    SealedTotpSecret,
    TotpService,
    TotpVerified,
    VerifyTotpCommand,
    derive_recovery_code_hmac_key,
    generate_recovery_codes,
    normalize_recovery_code,
    recovery_code_hash,
    time_step_of,
    totp_code,
)
from personal_os.diagnostics.context import DiagnosticContext, create_diagnostic_context
from personal_os.error_contracts.codes import ErrorCode
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
    totp_credentials,
    totp_recovery_codes,
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
_CORRECT_PASSWORD = "correct-horse-battery-staple"
_MASTER_KEY = bytes(range(32))
_CURRENT_KEY_ID = "authkey-current"
_PREVIOUS_KEY_ID = "authkey-previous"
_SECRET = bytes(range(20))
_ALTERNATE_SECRET = bytes(range(20, 40))
_ENROLLMENT_STARTED_AUDIT_ACTION = "authentication.totp_enrollment_started"
_TOTP_ACTIVATED_AUDIT_ACTION = "authentication.totp_activated"
_TOTP_DISABLED_AUDIT_ACTION = "authentication.totp_disabled"


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


class DeterministicHmacCrypto:
    """Crypto double deriving the recovery-code HMAC subkey with stdlib only."""

    def __init__(self) -> None:
        import hmac as hmac_module

        self._hmac_module = hmac_module

    def derive_subkey(self, *, master_key: bytes, label: str) -> bytes:
        return hashlib.sha256(label.encode("ascii") + master_key).digest()

    def hmac_sha256(self, *, key: bytes, message: bytes) -> bytes:
        return self._hmac_module.new(key, message, hashlib.sha256).digest()

    def seal_secret(self, *, key: bytes, plaintext: bytes) -> tuple[bytes, bytes]:
        raise AssertionError("sealing goes through the TOTP codec adapter")

    def open_secret(self, *, key: bytes, nonce: bytes, ciphertext: bytes) -> bytes:
        raise AssertionError("opening goes through the TOTP codec adapter")


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


def _build_keyring() -> AuthenticationKeyring:
    return AuthenticationKeyring(
        current_key_id=_CURRENT_KEY_ID,
        keys_by_id=MappingProxyType(
            {
                _PREVIOUS_KEY_ID: bytes(range(1, 33)),
                _CURRENT_KEY_ID: bytes(range(32)),
            }
        ),
    )


class TotpTransactionHarness:
    """Real TOTP stores and services over the disposable stack, pinned clock."""

    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine
        self.clock = FixedClock()
        self.hasher = CountingPasswordHasher()
        self.hmac_crypto = DeterministicHmacCrypto()
        self.aead_crypto = CryptographyAuthenticationCrypto()
        self.secret_codec = KeyringTotpSecretCodec(self.aead_crypto, _build_keyring())
        self.totp_store = TotpStore(engine, secret_codec=self.secret_codec)
        self.web_session_store = WebSessionStore(engine)
        unique_suffix = uuid4().hex[:10]
        self.username = f"totp-owner-{unique_suffix}"
        self.workspace_id: UUID | None = None
        self.totp_service = TotpService(
            transactions=self.totp_store,
            sessions=self.web_session_store,
            hasher=self.hasher,
            crypto=self.hmac_crypto,
            master_key=_MASTER_KEY,
            clock=self.clock,
            secret_codec=self.secret_codec,
        )
        self._recovery_hmac_key = derive_recovery_code_hmac_key(
            self.hmac_crypto, _MASTER_KEY
        )

    @property
    def database_now(self) -> datetime:
        return self.clock.database_now_value

    @staticmethod
    def diagnostic_context() -> DiagnosticContext:
        return create_diagnostic_context().context

    def recovery_hash_of(self, code: str) -> str:
        return recovery_code_hash(
            hmac_key=self._recovery_hmac_key, normalized_code=normalize_recovery_code(code)
        )

    def seal_under_key(self, *, key_id: str, plaintext: bytes) -> SealedTotpSecret:
        """Seal one secret under an explicit keyring key, mirroring the codec."""
        master_key = _build_keyring().keys_by_id[key_id]
        subkey = self.aead_crypto.derive_subkey(
            master_key=master_key, label=TOTP_SECRET_AEAD_LABEL
        )
        nonce, ciphertext = self.aead_crypto.seal_secret(key=subkey, plaintext=plaintext)
        return SealedTotpSecret(
            key_id=key_id,
            nonce=base64.b64encode(nonce).decode("ascii"),
            ciphertext=base64.b64encode(ciphertext).decode("ascii"),
        )

    async def seed_account(self) -> SeededAccount:
        user_id = uuid4()
        workspace_id = uuid4()
        self.workspace_id = workspace_id
        async with self.engine.begin() as connection:
            await connection.execute(
                sa.insert(users).values(
                    user_id=user_id, username=self.username, display_name="TOTP Owner"
                )
            )
            await connection.execute(
                sa.insert(workspaces).values(
                    workspace_id=workspace_id,
                    owner_user_id=user_id,
                    workspace_key=f"ws-{uuid4().hex[:12]}",
                    display_name="TOTP Workspace",
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
        return SeededAccount(
            user_id=user_id, workspace_id=workspace_id, username=self.username
        )

    async def insert_active_credential(
        self, account: SeededAccount, *, secret: bytes = _SECRET, key_id: str = _CURRENT_KEY_ID
    ) -> UUID:
        """Insert one already-active TOTP credential row with a real seal."""
        sealed = self.seal_under_key(key_id=key_id, plaintext=secret)
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
                    key_id=key_id,
                    algorithm="SHA1",
                    digits=6,
                    period_seconds=30,
                    revision=1,
                    created_at=self.database_now - timedelta(days=1),
                    activated_at=self.database_now - timedelta(days=1),
                )
            )
        return totp_credential_id

    async def insert_recovery_codes(
        self, account: SeededAccount, totp_credential_id: UUID, *, codes: list[str]
    ) -> None:
        async with self.engine.begin() as connection:
            for code in codes:
                await connection.execute(
                    sa.insert(totp_recovery_codes).values(
                        recovery_code_id=uuid4(),
                        totp_credential_id=totp_credential_id,
                        user_id=account.user_id,
                        workspace_id=account.workspace_id,
                        revision=1,
                        code_hash=self.recovery_hash_of(code),
                        created_at=self.database_now,
                    )
                )

    async def insert_session(
        self, account: SeededAccount, *, state: str
    ) -> tuple[UUID, str]:
        """Insert one session row and return its id and raw cookie secret."""
        web_session_id = uuid4()
        raw_secret = f"session-secret-{web_session_id.hex}"
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
                    state=state,
                    credential_revision=1,
                    authentication_method="password",
                    created_at=self.database_now,
                    authenticated_at=self.database_now if state == "active" else None,
                    idle_expires_at=self.database_now + timedelta(minutes=5),
                    absolute_expires_at=self.database_now + timedelta(days=7),
                )
            )
        return web_session_id, raw_secret

    async def fetch_one_row(self, statement: sa.Select[tuple[Any]]) -> Any:
        async with self.engine.connect() as connection:
            return (await connection.execute(statement)).one_or_none()

    async def credential_row(self, totp_credential_id: UUID) -> Any:
        return await self.fetch_one_row(
            sa.select(totp_credentials).where(
                totp_credentials.c.totp_credential_id == totp_credential_id
            )
        )

    async def pending_credential_row(self, user_id: UUID) -> Any:
        return await self.fetch_one_row(
            sa.select(totp_credentials)
            .where(
                totp_credentials.c.user_id == user_id, totp_credentials.c.state == "pending"
            )
            .limit(1)
        )

    async def recovery_rows(self, totp_credential_id: UUID) -> list[Any]:
        statement = sa.select(totp_recovery_codes).where(
            totp_recovery_codes.c.totp_credential_id == totp_credential_id
        )
        async with self.engine.connect() as connection:
            return list((await connection.execute(statement)).all())

    async def session_row(self, web_session_id: UUID) -> Any:
        return await self.fetch_one_row(
            sa.select(web_sessions).where(web_sessions.c.web_session_id == web_session_id)
        )

    async def audit_rows(self, action: str) -> list[Any]:
        assert self.workspace_id is not None
        statement = sa.select(audit_events).where(
            audit_events.c.action == action,
            audit_events.c.workspace_id == self.workspace_id,
        )
        async with self.engine.connect() as connection:
            return list((await connection.execute(statement)).all())

    def verify_command(self, user_id: UUID, *, code: str) -> VerifyTotpCommand:
        return VerifyTotpCommand(
            user_id=user_id,
            submitted_code=code,
            unix_time_seconds=_DATABASE_NOW_UNIX_SECONDS,
            database_now=self.database_now,
            reset_bucket_hash=None,
            diagnostic_context=self.diagnostic_context(),
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
        yield TotpTransactionHarness(engine)
    finally:
        await dispose_source_store_engine(engine)


def _code_at_step(step: int, *, secret: bytes = _SECRET) -> str:
    return totp_code(secret=secret, unix_time_seconds=step * 30)


# --- enrollment ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pending_enrollment_seals_secret_with_ten_minute_expiry(harness: Any) -> None:
    account = await harness.seed_account()
    started = await harness.totp_store.insert_pending_enrollment(
        InsertPendingEnrollmentCommand(
            user_id=account.user_id,
            allow_active_credential=False,
            sealed_secret=harness.secret_codec.seal_secret(plaintext=_SECRET),
            enrollment_expires_at=harness.database_now + TOTP_ENROLLMENT_EXPIRY,
            database_now=harness.database_now,
            diagnostic_context=harness.diagnostic_context(),
        )
    )
    assert started.username == account.username
    row = await harness.pending_credential_row(account.user_id)
    assert row.totp_credential_id == started.totp_credential_id
    assert row.enrollment_expires_at == harness.database_now + timedelta(minutes=10)
    assert row.key_id == _CURRENT_KEY_ID
    assert row.last_accepted_time_step is None
    reopened = harness.secret_codec.open_secret(
        sealed=SealedTotpSecret(
            key_id=row.key_id, nonce=row.secret_nonce, ciphertext=row.secret_ciphertext
        )
    )
    assert reopened == _SECRET
    assert len(await harness.audit_rows(_ENROLLMENT_STARTED_AUDIT_ACTION)) == 1


@pytest.mark.asyncio
async def test_expired_pending_enrollment_is_rejected(harness: Any) -> None:
    account = await harness.seed_account()
    started = await harness.totp_store.insert_pending_enrollment(
        InsertPendingEnrollmentCommand(
            user_id=account.user_id,
            allow_active_credential=False,
            sealed_secret=harness.secret_codec.seal_secret(plaintext=_SECRET),
            enrollment_expires_at=harness.database_now + TOTP_ENROLLMENT_EXPIRY,
            database_now=harness.database_now,
            diagnostic_context=harness.diagnostic_context(),
        )
    )
    harness.clock.database_now_value = _DATABASE_NOW + timedelta(minutes=11)
    with pytest.raises(AuthenticationError) as rejected:
        await harness.totp_store.activate_enrollment(
            ActivateEnrollmentCommand(
                user_id=account.user_id,
                enrollment_id=started.totp_credential_id,
                submitted_code=_code_at_step(_CURRENT_STEP),
                unix_time_seconds=_DATABASE_NOW_UNIX_SECONDS,
                recovery_code_hashes=("0" * 64,),
                complete_recovery_session=False,
                current_web_session_id=uuid4(),
                prior_session_secret_hash="ab" * 32,
                new_session_secret_hash="cd" * 32,
                new_csrf_secret_hash="ef" * 32,
                database_now=harness.database_now,
                diagnostic_context=harness.diagnostic_context(),
            )
        )
    assert rejected.value.error_code is ErrorCode.TOTP_ENROLLMENT_STATE_INVALID


# --- replay prevention and the window --------------------------------------------------


@pytest.mark.asyncio
async def test_same_totp_step_is_accepted_once_under_race(harness: Any) -> None:
    account = await harness.seed_account()
    await harness.insert_active_credential(account)
    code = _code_at_step(_CURRENT_STEP)
    results = await asyncio.gather(
        harness.totp_store.verify_totp(harness.verify_command(account.user_id, code=code)),
        harness.totp_store.verify_totp(harness.verify_command(account.user_id, code=code)),
        return_exceptions=True,
    )
    accepted = [result for result in results if isinstance(result, TotpVerified)]
    rejected = [result for result in results if isinstance(result, AuthenticationError)]
    assert len(accepted) == 1
    assert len(rejected) == 1
    assert rejected[0].error_code is ErrorCode.AUTHENTICATION_FAILED
    row = await harness.fetch_one_row(
        sa.select(totp_credentials).where(totp_credentials.c.user_id == account.user_id)
    )
    assert row.last_accepted_time_step == _CURRENT_STEP


@pytest.mark.asyncio
async def test_window_accepts_neighbor_steps_then_replays_and_drift_fail(harness: Any) -> None:
    account = await harness.seed_account()
    credential_id = await harness.insert_active_credential(account)

    async def verify(code: str) -> TotpVerified:
        return await harness.totp_store.verify_totp(
            harness.verify_command(account.user_id, code=code)
        )

    previous = await verify(_code_at_step(_CURRENT_STEP - 1))
    assert previous.accepted_time_step == _CURRENT_STEP - 1
    next_step = await verify(_code_at_step(_CURRENT_STEP + 1))
    assert next_step.accepted_time_step == _CURRENT_STEP + 1
    # Every candidate step is now at or behind the marker: replay.
    with pytest.raises(AuthenticationError) as replayed:
        await verify(_code_at_step(_CURRENT_STEP))
    assert replayed.value.error_code is ErrorCode.AUTHENTICATION_FAILED
    # Drift beyond the ±1 window fails safely even with a fresh marker.
    async with harness.engine.begin() as connection:
        await connection.execute(
            sa.update(totp_credentials)
            .values(last_accepted_time_step=None)
            .where(totp_credentials.c.totp_credential_id == credential_id)
        )
    with pytest.raises(AuthenticationError) as drifted:
        await verify(_code_at_step(_CURRENT_STEP + 2))
    assert drifted.value.error_code is ErrorCode.AUTHENTICATION_FAILED


@pytest.mark.asyncio
async def test_previous_key_credential_is_reencrypted_under_the_same_lock(harness: Any) -> None:
    account = await harness.seed_account()
    sealed = harness.seal_under_key(key_id=_PREVIOUS_KEY_ID, plaintext=_SECRET)
    credential_id = uuid4()
    async with harness.engine.begin() as connection:
        await connection.execute(
            sa.insert(totp_credentials).values(
                totp_credential_id=credential_id,
                user_id=account.user_id,
                workspace_id=account.workspace_id,
                state="active",
                secret_ciphertext=sealed.ciphertext,
                secret_nonce=sealed.nonce,
                key_id=_PREVIOUS_KEY_ID,
                algorithm="SHA1",
                digits=6,
                period_seconds=30,
                revision=1,
                created_at=harness.database_now - timedelta(days=1),
                activated_at=harness.database_now - timedelta(days=1),
            )
        )
    verified = await harness.totp_store.verify_totp(
        harness.verify_command(account.user_id, code=_code_at_step(_CURRENT_STEP))
    )
    assert verified.accepted_time_step == _CURRENT_STEP
    assert verified.was_reencrypted is True
    row = await harness.credential_row(credential_id)
    assert row.key_id == _CURRENT_KEY_ID
    reopened = harness.secret_codec.open_secret(
        sealed=SealedTotpSecret(
            key_id=row.key_id, nonce=row.secret_nonce, ciphertext=row.secret_ciphertext
        )
    )
    assert reopened == _SECRET


# --- activation and recovery codes -------------------------------------------------------


@pytest.mark.asyncio
async def test_activation_creates_ten_hashed_one_use_recovery_codes(harness: Any) -> None:
    account = await harness.seed_account()
    started = await harness.totp_store.insert_pending_enrollment(
        InsertPendingEnrollmentCommand(
            user_id=account.user_id,
            allow_active_credential=False,
            sealed_secret=harness.secret_codec.seal_secret(plaintext=_SECRET),
            enrollment_expires_at=harness.database_now + TOTP_ENROLLMENT_EXPIRY,
            database_now=harness.database_now,
            diagnostic_context=harness.diagnostic_context(),
        )
    )
    codes = generate_recovery_codes()
    hashes = tuple(harness.recovery_hash_of(code) for code in codes)
    activated = await harness.totp_store.activate_enrollment(
        ActivateEnrollmentCommand(
            user_id=account.user_id,
            enrollment_id=started.totp_credential_id,
            submitted_code=_code_at_step(_CURRENT_STEP),
            unix_time_seconds=_DATABASE_NOW_UNIX_SECONDS,
            recovery_code_hashes=hashes,
            complete_recovery_session=False,
            current_web_session_id=uuid4(),
            prior_session_secret_hash="ab" * 32,
            new_session_secret_hash="cd" * 32,
            new_csrf_secret_hash="ef" * 32,
            database_now=harness.database_now,
            diagnostic_context=harness.diagnostic_context(),
        )
    )
    assert activated.recovery_code_revision == 1
    rows = await harness.recovery_rows(started.totp_credential_id)
    assert len(rows) == RECOVERY_CODE_COUNT
    assert all(row.used_at is None for row in rows)
    assert {row.code_hash for row in rows} == set(hashes)
    credential = await harness.credential_row(started.totp_credential_id)
    assert credential.state == "active"
    assert credential.activated_at == harness.database_now
    assert credential.enrollment_expires_at is None
    assert credential.last_accepted_time_step == _CURRENT_STEP
    assert len(await harness.audit_rows(_TOTP_ACTIVATED_AUDIT_ACTION)) == 1


# --- recovery ------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recovery_consumes_exactly_one_code_under_race(harness: Any) -> None:
    account = await harness.seed_account()
    credential_id = await harness.insert_active_credential(account)
    codes = list(generate_recovery_codes())
    await harness.insert_recovery_codes(account, credential_id, codes=codes)
    first_session_id, first_secret = await harness.insert_session(
        account, state="pending_totp"
    )
    second_session_id, second_secret = await harness.insert_session(
        account, state="pending_totp"
    )
    code_hash = harness.recovery_hash_of(codes[0])

    async def recover(web_session_id: UUID, raw_secret: str) -> RecoveredSession:
        return await harness.totp_store.recover_session(
            RecoverSessionCommand(
                user_id=account.user_id,
                current_web_session_id=web_session_id,
                prior_session_secret_hash=session_secret_hash_of(raw_secret),
                new_session_secret_hash=web_session_id.hex * 2,
                new_csrf_secret_hash=web_session_id.hex[::-1] * 2,
                recovery_code_hash=code_hash,
                database_now=harness.database_now,
                diagnostic_context=harness.diagnostic_context(),
            )
        )

    results = await asyncio.gather(
        recover(first_session_id, first_secret),
        recover(second_session_id, second_secret),
        return_exceptions=True,
    )
    recovered = [result for result in results if isinstance(result, RecoveredSession)]
    rejected = [result for result in results if isinstance(result, AuthenticationError)]
    assert len(recovered) == 1
    assert len(rejected) == 1
    consumed = [row for row in await harness.recovery_rows(credential_id) if row.used_at]
    assert len(consumed) == 1
    assert consumed[0].used_at == harness.database_now
    winner_id = recovered[0].web_session_id
    winner_row = await harness.session_row(winner_id)
    assert winner_row.state == "recovery_limited"
    assert winner_row.authentication_method == "recovery_code"
    assert winner_row.authenticated_at == harness.database_now
    loser_id = second_session_id if winner_id == first_session_id else first_session_id
    assert (await harness.session_row(loser_id)).state == "pending_totp"


@pytest.mark.asyncio
async def test_consumed_recovery_code_cannot_be_reused(harness: Any) -> None:
    account = await harness.seed_account()
    credential_id = await harness.insert_active_credential(account)
    codes = list(generate_recovery_codes())
    await harness.insert_recovery_codes(account, credential_id, codes=codes)
    session_id, raw_secret = await harness.insert_session(account, state="pending_totp")
    first = await harness.totp_store.recover_session(
        RecoverSessionCommand(
            user_id=account.user_id,
            current_web_session_id=session_id,
            prior_session_secret_hash=session_secret_hash_of(raw_secret),
            new_session_secret_hash="cd" * 32,
            new_csrf_secret_hash="ef" * 32,
            recovery_code_hash=harness.recovery_hash_of(codes[2]),
            database_now=harness.database_now,
            diagnostic_context=harness.diagnostic_context(),
        )
    )
    assert first.web_session_id == session_id
    second_session_id, second_secret = await harness.insert_session(
        account, state="pending_totp"
    )
    with pytest.raises(AuthenticationError) as rejected:
        await harness.totp_store.recover_session(
            RecoverSessionCommand(
                user_id=account.user_id,
                current_web_session_id=second_session_id,
                prior_session_secret_hash=session_secret_hash_of(second_secret),
                new_session_secret_hash="ce" * 32,
                new_csrf_secret_hash="ed" * 32,
                recovery_code_hash=harness.recovery_hash_of(codes[2]),
                database_now=harness.database_now,
                diagnostic_context=harness.diagnostic_context(),
            )
        )
    assert rejected.value.error_code is ErrorCode.AUTHENTICATION_FAILED


# --- regeneration and disable -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_regenerate_invalidates_unused_prior_revision(harness: Any) -> None:
    account = await harness.seed_account()
    credential_id = await harness.insert_active_credential(account)
    await harness.insert_recovery_codes(
        account, credential_id, codes=list(generate_recovery_codes())
    )
    fresh_codes = generate_recovery_codes()
    regenerated = await harness.totp_store.regenerate_recovery_codes(
        RegenerateRecoveryCodesCommand(
            user_id=account.user_id,
            workspace_id=account.workspace_id,
            recovery_code_hashes=tuple(
                harness.recovery_hash_of(code) for code in fresh_codes
            ),
            database_now=harness.database_now,
            diagnostic_context=harness.diagnostic_context(),
        )
    )
    assert regenerated.revision == 2
    assert regenerated.invalidated_code_count == RECOVERY_CODE_COUNT
    rows = await harness.recovery_rows(credential_id)
    assert len(rows) == 2 * RECOVERY_CODE_COUNT
    assert all(row.used_at is not None for row in rows if row.revision == 1)
    assert all(row.used_at is None for row in rows if row.revision == 2)
    assert (await harness.credential_row(credential_id)).revision == 2


@pytest.mark.asyncio
async def test_disable_closes_every_surface_and_rotates_password_only(harness: Any) -> None:
    account = await harness.seed_account()
    credential_id = await harness.insert_active_credential(account)
    await harness.insert_recovery_codes(
        account, credential_id, codes=list(generate_recovery_codes())
    )
    current_session_id, current_secret = await harness.insert_session(
        account, state="active"
    )
    other_session_id, _other_secret = await harness.insert_session(account, state="active")
    disabled = await harness.totp_store.disable_totp(
        DisableTotpCommand(
            user_id=account.user_id,
            workspace_id=account.workspace_id,
            current_web_session_id=current_session_id,
            prior_session_secret_hash=session_secret_hash_of(current_secret),
            new_session_secret_hash=current_session_id.hex * 2,
            new_csrf_secret_hash=current_session_id.hex[::-1] * 2,
            database_now=harness.database_now,
            diagnostic_context=harness.diagnostic_context(),
        )
    )
    assert disabled.credential_revision == 2
    assert disabled.revoked_session_count == 1
    credential = await harness.credential_row(credential_id)
    assert credential.state == "replaced"
    assert credential.replaced_at == harness.database_now
    assert all(row.used_at is not None for row in await harness.recovery_rows(credential_id))
    credential_revision_row = await harness.fetch_one_row(
        sa.select(user_credentials).where(user_credentials.c.user_id == account.user_id)
    )
    assert credential_revision_row.credential_revision == 2
    other_row = await harness.session_row(other_session_id)
    assert other_row.state == "revoked"
    assert other_row.revocation_reason == "totp_disabled"
    assert other_row.authenticated_at is None
    current_row = await harness.session_row(current_session_id)
    assert current_row.state == "active"
    assert current_row.authentication_method == "password"
    assert current_row.credential_revision == 2
    assert current_row.session_secret_hash == current_session_id.hex * 2
    assert len(await harness.audit_rows(_TOTP_DISABLED_AUDIT_ACTION)) == 1


# --- service choreography --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_session_totp_rejects_an_unknown_binding(harness: Any) -> None:
    await harness.seed_account()
    with pytest.raises(AuthenticationError) as rejected:
        await harness.totp_service.verify_session_totp(
            session_secret="missing-secret-value",
            code=_code_at_step(_CURRENT_STEP),
            diagnostic_context=harness.diagnostic_context(),
        )
    assert rejected.value.error_code is ErrorCode.AUTHENTICATION_REQUIRED


@pytest.mark.asyncio
async def test_service_challenge_and_recovery_choreography(harness: Any) -> None:
    account = await harness.seed_account()
    credential_id = await harness.insert_active_credential(
        account, secret=_ALTERNATE_SECRET
    )
    codes = list(generate_recovery_codes())
    await harness.insert_recovery_codes(account, credential_id, codes=codes)
    # The pending_totp challenge resolves to active with rotated secrets.
    challenge_session_id, challenge_secret = await harness.insert_session(
        account, state="pending_totp"
    )
    verified = await harness.totp_service.verify_session_totp(
        session_secret=challenge_secret,
        code=_code_at_step(_CURRENT_STEP, secret=_ALTERNATE_SECRET),
        diagnostic_context=harness.diagnostic_context(),
    )
    assert verified.public_error is None
    assert verified.verified is not None
    challenge_row = await harness.session_row(challenge_session_id)
    assert challenge_row.state == "active"
    assert challenge_row.authentication_method == "password_totp"
    assert challenge_row.authenticated_at == harness.database_now
    # Password plus one recovery code enters recovery_limited.
    recovery_session_id, recovery_secret = await harness.insert_session(
        account, state="pending_totp"
    )
    recovery = await harness.totp_service.recover_with_code(
        session_secret=recovery_secret,
        password=_CORRECT_PASSWORD,
        recovery_code=codes[3],
        diagnostic_context=harness.diagnostic_context(),
    )
    assert recovery.public_error is None
    assert recovery.entered is not None
    recovery_row = await harness.session_row(recovery_session_id)
    assert recovery_row.state == "recovery_limited"
    assert recovery_row.session_secret_hash == session_secret_hash_of(
        recovery.entered.rotated_session.session_secret
    )
    # A wrong password never reaches the code consumption.
    _wrong_session_id, wrong_secret = await harness.insert_session(
        account, state="pending_totp"
    )
    wrong_password = await harness.totp_service.recover_with_code(
        session_secret=wrong_secret,
        password="sentinel-wrong-password-value",
        recovery_code=codes[4],
        diagnostic_context=harness.diagnostic_context(),
    )
    assert wrong_password.public_error is ErrorCode.AUTHENTICATION_FAILED
