"""TOTP store transaction contracts over a scripted engine double.

The store unit tests double the async engine with a scripted connection that
records every executed statement and returns programmed rows: session TOTP
verification locks the active credential row, decrypts through the codec seam,
advances ``last_accepted_time_step`` only for a newer step and re-encrypts a
previous-key secret with the current key under the same lock; enrollment
insertion refuses an existing active credential unless replacement is allowed
and supersedes a stale pending row; activation flips the pending row, replaces
the previous active credential and inserts the ten hashed recovery rows;
recovery consumes exactly one unused code and transitions the session to
``recovery_limited``; regeneration bumps the credential's recovery revision
and invalidates the unused prior revision; disable replaces the credential,
marks every recovery code used, bumps the credential revision, revokes the
other sessions and rotates the current session to password-only. No database
is touched.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql.elements import TextClause

from personal_os.authentication.errors import AuthenticationError
from personal_os.authentication.sessions import ThrottleBucketKind
from personal_os.authentication.totp import (
    ActivateEnrollmentCommand,
    DisableTotpCommand,
    InsertPendingEnrollmentCommand,
    RecoverSessionCommand,
    RegenerateRecoveryCodesCommand,
    SealedTotpSecret,
    VerifyTotpCommand,
    totp_code,
)
from personal_os.diagnostics.context import DiagnosticContext, create_diagnostic_context
from personal_os.error_contracts.codes import ErrorCode
from postgresql_source_store.totp_store import TotpStore

_DATABASE_NOW = datetime(2026, 8, 16, 12, 0, 0, tzinfo=UTC)
_DATABASE_NOW_UNIX_SECONDS = int(_DATABASE_NOW.timestamp())
_USER_ID = uuid4()
_WORKSPACE_ID = uuid4()
_CREDENTIAL_ID = uuid4()
_ENROLLMENT_ID = uuid4()
_WEB_SESSION_ID = uuid4()
_SECRET = bytes(range(20))
_CURRENT_KEY_ID = "authkey-current"
_PREVIOUS_KEY_ID = "authkey-previous"
_RESEALED = SealedTotpSecret(
    key_id=_CURRENT_KEY_ID, nonce="cmVzZWFsZWQtbm9uY2U", ciphertext="cmVzZWFsZWRjaXBoZXJ0ZXh0"
)
_RECOVERY_HASHES = tuple(f"{index:064x}" for index in range(10))


class ScriptedResult:
    """Programmed result double exposing the executed-statement return shape."""

    def __init__(self, rows: tuple[SimpleNamespace, ...] = (), rowcount: int = -1) -> None:
        self._rows = list(rows)
        self.rowcount = rowcount

    def one_or_none(self) -> SimpleNamespace | None:
        return self._rows[0] if self._rows else None

    def one(self) -> SimpleNamespace:
        assert len(self._rows) == 1, "scripted result must program exactly one row"
        return self._rows[0]

    def all(self) -> list[SimpleNamespace]:
        return list(self._rows)


class ScriptedConnection:
    """Connection double recording statements and popping programmed results."""

    def __init__(self, results: list[ScriptedResult]) -> None:
        self._results = results
        self.executed_statements: list[object] = []

    async def execute(self, statement: object) -> ScriptedResult:
        self.executed_statements.append(statement)
        if isinstance(statement, TextClause):
            return ScriptedResult()
        if not self._results:
            return ScriptedResult()
        return self._results.pop(0)

    async def __aenter__(self) -> ScriptedConnection:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    def begin(self) -> ScriptedConnection:
        return self


class ScriptedEngine:
    """Engine double handing out one scripted connection per connect call."""

    def __init__(self, results: list[ScriptedResult]) -> None:
        self.results = results
        self.connections: list[ScriptedConnection] = []

    def connect(self) -> ScriptedConnection:
        connection = ScriptedConnection(self.results)
        self.connections.append(connection)
        return connection


class ScriptedTotpCodec:
    """Codec double opening every secret to one fixed plaintext."""

    def current_key_id(self) -> str:
        return _CURRENT_KEY_ID

    def seal_secret(self, *, plaintext: bytes) -> SealedTotpSecret:
        del plaintext
        return _RESEALED

    def open_secret(self, *, sealed: SealedTotpSecret) -> bytes:
        del sealed
        return _SECRET


_BINDNAME_SUFFIX_PATTERN = re.compile(r"_\d+$")


def _statement_parameters(statement: object) -> dict[str, object]:
    """Compiled bind parameters with WHERE-suffix aliases added."""
    raw_parameters: dict[str, object] = dict(
        statement.compile().params  # type: ignore[attr-defined]
    )
    resolved = dict(raw_parameters)
    for name, value in raw_parameters.items():
        stripped_name = _BINDNAME_SUFFIX_PATTERN.sub("", name)
        if stripped_name != name and stripped_name not in resolved:
            resolved[stripped_name] = value
    return resolved


def _statements_of(connection: ScriptedConnection, kind: type, table_name: str) -> list[object]:
    return [
        statement
        for statement in connection.executed_statements
        if isinstance(statement, kind) and statement.table.name == table_name  # type: ignore[attr-defined]
    ]


def _current_step_code() -> str:
    return totp_code(secret=_SECRET, unix_time_seconds=_DATABASE_NOW_UNIX_SECONDS)


def _diagnostic_context() -> DiagnosticContext:
    return create_diagnostic_context().context


def _credential_row(
    *,
    state: str = "active",
    key_id: str = _CURRENT_KEY_ID,
    last_accepted_time_step: int | None = None,
    revision: int = 1,
    enrollment_expires_at: datetime | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        totp_credential_id=_CREDENTIAL_ID if state != "pending" else _ENROLLMENT_ID,
        user_id=_USER_ID,
        workspace_id=_WORKSPACE_ID,
        state=state,
        secret_ciphertext="c2VhbGVkLWNpcGhlcnRleHQ",
        secret_nonce="c2VhbGVkLW5vbmNl",
        key_id=key_id,
        algorithm="SHA1",
        digits=6,
        period_seconds=30,
        last_accepted_time_step=last_accepted_time_step,
        enrollment_expires_at=enrollment_expires_at,
        revision=revision,
        created_at=_DATABASE_NOW - timedelta(minutes=1),
        activated_at=_DATABASE_NOW if state == "active" else None,
        replaced_at=None,
    )


def _user_credential_row() -> SimpleNamespace:
    return SimpleNamespace(
        user_id=_USER_ID,
        workspace_id=_WORKSPACE_ID,
        credential_revision=1,
        password_hash="phc-hash",
        username="store-owner",
    )


# --- session TOTP verification -------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_totp_advances_marker_under_lock_and_reseals_previous_key() -> None:
    engine = ScriptedEngine(
        [
            ScriptedResult(rows=(_credential_row(key_id=_PREVIOUS_KEY_ID),)),
            ScriptedResult(rowcount=1),
        ]
    )
    store = TotpStore(engine, secret_codec=ScriptedTotpCodec())
    verified = await store.verify_totp(
        VerifyTotpCommand(
            user_id=_USER_ID,
            submitted_code=_current_step_code(),
            unix_time_seconds=_DATABASE_NOW_UNIX_SECONDS,
            database_now=_DATABASE_NOW,
            reset_bucket_hash=None,
            diagnostic_context=_diagnostic_context(),
        )
    )
    assert verified.accepted_time_step == _DATABASE_NOW_UNIX_SECONDS // 30
    assert verified.was_reencrypted is True
    connection = engine.connections[0]
    marker_updates = _statements_of(connection, sa.sql.dml.Update, "totp_credentials")
    assert len(marker_updates) == 1
    parameters = _statement_parameters(marker_updates[0])
    assert parameters["last_accepted_time_step"] == _DATABASE_NOW_UNIX_SECONDS // 30
    assert parameters["key_id"] == _CURRENT_KEY_ID
    assert parameters["secret_nonce"] == _RESEALED.nonce
    assert parameters["secret_ciphertext"] == _RESEALED.ciphertext
    # The credential row locks before any decision or write: the first
    # non-bounds statement is the locked credential select.
    first_statement = next(
        statement
        for statement in connection.executed_statements
        if not isinstance(statement, TextClause)
    )
    assert isinstance(first_statement, sa.Select)


@pytest.mark.asyncio
async def test_verify_totp_replays_the_same_step_without_a_write() -> None:
    current_step = _DATABASE_NOW_UNIX_SECONDS // 30
    engine = ScriptedEngine(
        [ScriptedResult(rows=(_credential_row(last_accepted_time_step=current_step),))]
    )
    store = TotpStore(engine, secret_codec=ScriptedTotpCodec())
    with pytest.raises(AuthenticationError) as rejected:
        await store.verify_totp(
            VerifyTotpCommand(
                user_id=_USER_ID,
                submitted_code=_current_step_code(),
                unix_time_seconds=_DATABASE_NOW_UNIX_SECONDS,
                database_now=_DATABASE_NOW,
                reset_bucket_hash=None,
                diagnostic_context=_diagnostic_context(),
            )
        )
    assert rejected.value.error_code is ErrorCode.AUTHENTICATION_FAILED
    assert _statements_of(engine.connections[0], sa.sql.dml.Update, "totp_credentials") == []


@pytest.mark.asyncio
async def test_verify_totp_without_an_active_credential_fails_closed() -> None:
    engine = ScriptedEngine([ScriptedResult(rows=())])
    store = TotpStore(engine, secret_codec=ScriptedTotpCodec())
    with pytest.raises(AuthenticationError) as rejected:
        await store.verify_totp(
            VerifyTotpCommand(
                user_id=_USER_ID,
                submitted_code=_current_step_code(),
                unix_time_seconds=_DATABASE_NOW_UNIX_SECONDS,
                database_now=_DATABASE_NOW,
                reset_bucket_hash=None,
                diagnostic_context=_diagnostic_context(),
            )
        )
    assert rejected.value.error_code is ErrorCode.AUTHENTICATION_FAILED


# --- enrollment start ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_insert_pending_enrollment_supersedes_stale_pending_row() -> None:
    engine = ScriptedEngine(
        [
            ScriptedResult(rows=(_user_credential_row(),)),
            ScriptedResult(rows=()),  # no active credential exists
            ScriptedResult(rowcount=1),  # stale pending row superseded
        ]
    )
    store = TotpStore(engine, secret_codec=ScriptedTotpCodec())
    inserted = await store.insert_pending_enrollment(
        InsertPendingEnrollmentCommand(
            user_id=_USER_ID,
            allow_active_credential=False,
            sealed_secret=_RESEALED,
            enrollment_expires_at=_DATABASE_NOW + timedelta(minutes=10),
            database_now=_DATABASE_NOW,
            diagnostic_context=_diagnostic_context(),
        )
    )
    assert inserted.username == "store-owner"
    connection = engine.connections[0]
    supersede_updates = _statements_of(connection, sa.sql.dml.Update, "totp_credentials")
    assert len(supersede_updates) == 1
    assert _statement_parameters(supersede_updates[0])["state"] == "replaced"
    inserts = _statements_of(connection, sa.sql.dml.Insert, "totp_credentials")
    assert len(inserts) == 1
    parameters = _statement_parameters(inserts[0])
    assert parameters["state"] == "pending"
    assert parameters["enrollment_expires_at"] == _DATABASE_NOW + timedelta(minutes=10)
    assert parameters["key_id"] == _CURRENT_KEY_ID
    assert parameters["digits"] == 6
    assert parameters["period_seconds"] == 30
    assert parameters["revision"] == 1


@pytest.mark.asyncio
async def test_insert_pending_enrollment_refuses_an_active_credential() -> None:
    engine = ScriptedEngine(
        [
            ScriptedResult(rows=(_user_credential_row(),)),
            ScriptedResult(rows=(_credential_row(),)),  # active credential exists
        ]
    )
    store = TotpStore(engine, secret_codec=ScriptedTotpCodec())
    with pytest.raises(AuthenticationError) as rejected:
        await store.insert_pending_enrollment(
            InsertPendingEnrollmentCommand(
                user_id=_USER_ID,
                allow_active_credential=False,
                sealed_secret=_RESEALED,
                enrollment_expires_at=_DATABASE_NOW + timedelta(minutes=10),
                database_now=_DATABASE_NOW,
                diagnostic_context=_diagnostic_context(),
            )
        )
    assert rejected.value.error_code is ErrorCode.TOTP_ENROLLMENT_STATE_INVALID
    assert not [
        statement
        for statement in engine.connections[0].executed_statements
        if isinstance(statement, sa.sql.dml.Insert | sa.sql.dml.Update)
    ]


@pytest.mark.asyncio
async def test_record_prompt_dismissal_writes_only_the_dismissal_timestamp() -> None:
    engine = ScriptedEngine(
        [ScriptedResult(rows=(_user_credential_row(),)), ScriptedResult(rowcount=1)]
    )
    store = TotpStore(engine, secret_codec=ScriptedTotpCodec())
    dismissed_at = await store.record_prompt_dismissal(
        user_id=_USER_ID, workspace_id=_WORKSPACE_ID, database_now=_DATABASE_NOW
    )
    assert dismissed_at == _DATABASE_NOW
    connection = engine.connections[0]
    assert _statements_of(connection, sa.sql.dml.Insert, "totp_credentials") == []
    update_parameters = _statement_parameters(
        _statements_of(connection, sa.sql.dml.Update, "user_credentials")[0]
    )
    assert update_parameters["totp_prompt_dismissed_at"] == _DATABASE_NOW


# --- enrollment verification and activation ---------------------------------------------


@pytest.mark.asyncio
async def test_activate_enrollment_replaces_previous_and_inserts_ten_hashed_codes() -> None:
    pending_row = _credential_row(
        state="pending",
        enrollment_expires_at=_DATABASE_NOW + timedelta(minutes=9),
    )
    engine = ScriptedEngine(
        [
            ScriptedResult(rows=(pending_row,)),
            ScriptedResult(rowcount=1),  # previous active credential replaced
            ScriptedResult(rowcount=1),  # pending row activated
            *[ScriptedResult(rowcount=1) for _ in _RECOVERY_HASHES],
        ]
    )
    store = TotpStore(engine, secret_codec=ScriptedTotpCodec())
    activated = await store.activate_enrollment(
        ActivateEnrollmentCommand(
            user_id=_USER_ID,
            enrollment_id=_ENROLLMENT_ID,
            submitted_code=_current_step_code(),
            unix_time_seconds=_DATABASE_NOW_UNIX_SECONDS,
            recovery_code_hashes=_RECOVERY_HASHES,
            complete_recovery_session=False,
            current_web_session_id=_WEB_SESSION_ID,
            prior_session_secret_hash="ab" * 32,
            new_session_secret_hash="cd" * 32,
            new_csrf_secret_hash="ef" * 32,
            database_now=_DATABASE_NOW,
            diagnostic_context=_diagnostic_context(),
        )
    )
    assert activated.recovery_code_revision == 1
    assert activated.replaced_previous_credential is True
    connection = engine.connections[0]
    updates = _statements_of(connection, sa.sql.dml.Update, "totp_credentials")
    activation_parameters = _statement_parameters(updates[-1])
    assert activation_parameters["state"] == "active"
    assert activation_parameters["activated_at"] == _DATABASE_NOW
    assert activation_parameters["enrollment_expires_at"] is None
    assert activation_parameters["last_accepted_time_step"] == _DATABASE_NOW_UNIX_SECONDS // 30
    recovery_inserts = _statements_of(connection, sa.sql.dml.Insert, "totp_recovery_codes")
    assert len(recovery_inserts) == 10
    inserted_hashes = {_statement_parameters(insert)["code_hash"] for insert in recovery_inserts}
    assert inserted_hashes == set(_RECOVERY_HASHES)
    assert _statements_of(connection, sa.sql.dml.Update, "web_sessions") == []


@pytest.mark.asyncio
async def test_activate_enrollment_rejects_an_expired_pending_row() -> None:
    pending_row = _credential_row(
        state="pending",
        enrollment_expires_at=_DATABASE_NOW - timedelta(minutes=1),
    )
    engine = ScriptedEngine([ScriptedResult(rows=(pending_row,))])
    store = TotpStore(engine, secret_codec=ScriptedTotpCodec())
    with pytest.raises(AuthenticationError) as rejected:
        await store.activate_enrollment(
            ActivateEnrollmentCommand(
                user_id=_USER_ID,
                enrollment_id=_ENROLLMENT_ID,
                submitted_code=_current_step_code(),
                unix_time_seconds=_DATABASE_NOW_UNIX_SECONDS,
                recovery_code_hashes=_RECOVERY_HASHES,
                complete_recovery_session=False,
                current_web_session_id=_WEB_SESSION_ID,
                prior_session_secret_hash="ab" * 32,
                new_session_secret_hash="cd" * 32,
                new_csrf_secret_hash="ef" * 32,
                database_now=_DATABASE_NOW,
                diagnostic_context=_diagnostic_context(),
            )
        )
    assert rejected.value.error_code is ErrorCode.TOTP_ENROLLMENT_STATE_INVALID


@pytest.mark.asyncio
async def test_activate_enrollment_rotates_recovery_limited_session_to_active() -> None:
    pending_row = _credential_row(
        state="pending",
        enrollment_expires_at=_DATABASE_NOW + timedelta(minutes=9),
    )
    engine = ScriptedEngine(
        [
            ScriptedResult(rows=(pending_row,)),
            ScriptedResult(rowcount=0),  # no previous active credential
            ScriptedResult(rowcount=1),
            *[ScriptedResult(rowcount=1) for _ in _RECOVERY_HASHES],
            ScriptedResult(rowcount=1),  # recovery-limited session rotated
        ]
    )
    store = TotpStore(engine, secret_codec=ScriptedTotpCodec())
    activated = await store.activate_enrollment(
        ActivateEnrollmentCommand(
            user_id=_USER_ID,
            enrollment_id=_ENROLLMENT_ID,
            submitted_code=_current_step_code(),
            unix_time_seconds=_DATABASE_NOW_UNIX_SECONDS,
            recovery_code_hashes=_RECOVERY_HASHES,
            complete_recovery_session=True,
            current_web_session_id=_WEB_SESSION_ID,
            prior_session_secret_hash="ab" * 32,
            new_session_secret_hash="cd" * 32,
            new_csrf_secret_hash="ef" * 32,
            database_now=_DATABASE_NOW,
            diagnostic_context=_diagnostic_context(),
        )
    )
    assert activated.replaced_previous_credential is False
    session_updates = _statements_of(engine.connections[0], sa.sql.dml.Update, "web_sessions")
    assert len(session_updates) == 1
    parameters = _statement_parameters(session_updates[0])
    assert parameters["state"] == "active"
    assert parameters["authentication_method"] == "password_totp"
    assert parameters["authenticated_at"] == _DATABASE_NOW
    assert parameters["session_secret_hash"] == "cd" * 32
    assert parameters["csrf_secret_hash"] == "ef" * 32


# --- recovery -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recover_session_consumes_one_code_and_transitions_the_binding() -> None:
    recovery_row = SimpleNamespace(recovery_code_id=uuid4())
    engine = ScriptedEngine(
        [
            ScriptedResult(rows=(_credential_row(),)),
            ScriptedResult(rows=(recovery_row,)),
            ScriptedResult(rowcount=1),  # code consumed
            ScriptedResult(rowcount=1),  # session transitioned
        ]
    )
    store = TotpStore(engine, secret_codec=ScriptedTotpCodec())
    recovered = await store.recover_session(
        RecoverSessionCommand(
            user_id=_USER_ID,
            current_web_session_id=_WEB_SESSION_ID,
            prior_session_secret_hash="ab" * 32,
            new_session_secret_hash="cd" * 32,
            new_csrf_secret_hash="ef" * 32,
            recovery_code_hash="0" * 64,
            database_now=_DATABASE_NOW,
            diagnostic_context=_diagnostic_context(),
        )
    )
    assert recovered.web_session_id == _WEB_SESSION_ID
    connection = engine.connections[0]
    consume_parameters = _statement_parameters(
        _statements_of(connection, sa.sql.dml.Update, "totp_recovery_codes")[0]
    )
    assert consume_parameters["used_at"] == _DATABASE_NOW
    session_parameters = _statement_parameters(
        _statements_of(connection, sa.sql.dml.Update, "web_sessions")[0]
    )
    assert session_parameters["state"] == "recovery_limited"
    assert session_parameters["authentication_method"] == "recovery_code"
    assert session_parameters["authenticated_at"] == _DATABASE_NOW
    assert session_parameters["reauthenticated_at"] is None
    # Entering recovery rotates the binding but never touches expiry bounds.
    assert "idle_expires_at" not in session_parameters
    assert "absolute_expires_at" not in session_parameters


@pytest.mark.asyncio
async def test_recover_session_without_an_unused_code_fails_closed() -> None:
    engine = ScriptedEngine([ScriptedResult(rows=(_credential_row(),)), ScriptedResult(rows=())])
    store = TotpStore(engine, secret_codec=ScriptedTotpCodec())
    with pytest.raises(AuthenticationError) as rejected:
        await store.recover_session(
            RecoverSessionCommand(
                user_id=_USER_ID,
                current_web_session_id=_WEB_SESSION_ID,
                prior_session_secret_hash="ab" * 32,
                new_session_secret_hash="cd" * 32,
                new_csrf_secret_hash="ef" * 32,
                recovery_code_hash="0" * 64,
                database_now=_DATABASE_NOW,
                diagnostic_context=_diagnostic_context(),
            )
        )
    assert rejected.value.error_code is ErrorCode.AUTHENTICATION_FAILED
    assert _statements_of(engine.connections[0], sa.sql.dml.Update, "web_sessions") == []


# --- regeneration and disable -----------------------------------------------------------


@pytest.mark.asyncio
async def test_regenerate_recovery_codes_invalidates_unused_prior_revision() -> None:
    engine = ScriptedEngine(
        [
            ScriptedResult(rows=(_credential_row(revision=3),)),
            ScriptedResult(rowcount=1),  # credential revision bumped
            ScriptedResult(rowcount=4),  # four unused prior-revision codes
            *[ScriptedResult(rowcount=1) for _ in _RECOVERY_HASHES],
        ]
    )
    store = TotpStore(engine, secret_codec=ScriptedTotpCodec())
    regenerated = await store.regenerate_recovery_codes(
        RegenerateRecoveryCodesCommand(
            user_id=_USER_ID,
            workspace_id=_WORKSPACE_ID,
            recovery_code_hashes=_RECOVERY_HASHES,
            database_now=_DATABASE_NOW,
            diagnostic_context=_diagnostic_context(),
        )
    )
    assert regenerated.revision == 4
    assert regenerated.invalidated_code_count == 4
    connection = engine.connections[0]
    recovery_inserts = _statements_of(connection, sa.sql.dml.Insert, "totp_recovery_codes")
    assert len(recovery_inserts) == 10
    assert all(_statement_parameters(insert)["revision"] == 4 for insert in recovery_inserts)


@pytest.mark.asyncio
async def test_disable_totp_closes_every_surface_and_rotates_password_only() -> None:
    engine = ScriptedEngine(
        [
            ScriptedResult(rows=(_user_credential_row(),)),
            ScriptedResult(rows=(_credential_row(),)),
            ScriptedResult(rowcount=1),  # credential replaced
            ScriptedResult(rowcount=10),  # every recovery code revoked
            ScriptedResult(rowcount=1),  # credential revision bumped
            ScriptedResult(rowcount=2),  # other sessions revoked
            ScriptedResult(rowcount=1),  # current session rotated
        ]
    )
    store = TotpStore(engine, secret_codec=ScriptedTotpCodec())
    disabled = await store.disable_totp(
        DisableTotpCommand(
            user_id=_USER_ID,
            workspace_id=_WORKSPACE_ID,
            current_web_session_id=_WEB_SESSION_ID,
            prior_session_secret_hash="ab" * 32,
            new_session_secret_hash="cd" * 32,
            new_csrf_secret_hash="ef" * 32,
            database_now=_DATABASE_NOW,
            diagnostic_context=_diagnostic_context(),
        )
    )
    assert disabled.credential_revision == 2
    assert disabled.revoked_session_count == 2
    connection = engine.connections[0]
    session_updates = _statements_of(connection, sa.sql.dml.Update, "web_sessions")
    assert len(session_updates) == 2
    revoke_parameters = _statement_parameters(session_updates[0])
    assert revoke_parameters["state"] == "revoked"
    assert revoke_parameters["revocation_reason"] == "totp_disabled"
    assert revoke_parameters["authenticated_at"] is None
    rotation_parameters = _statement_parameters(session_updates[1])
    assert rotation_parameters["authentication_method"] == "password"
    assert rotation_parameters["credential_revision"] == 2
    assert rotation_parameters["session_secret_hash"] == "cd" * 32
    assert rotation_parameters["csrf_secret_hash"] == "ef" * 32


@pytest.mark.asyncio
async def test_disable_totp_without_an_active_credential_fails_closed() -> None:
    engine = ScriptedEngine(
        [ScriptedResult(rows=(_user_credential_row(),)), ScriptedResult(rows=())]
    )
    store = TotpStore(engine, secret_codec=ScriptedTotpCodec())
    with pytest.raises(AuthenticationError) as rejected:
        await store.disable_totp(
            DisableTotpCommand(
                user_id=_USER_ID,
                workspace_id=_WORKSPACE_ID,
                current_web_session_id=_WEB_SESSION_ID,
                prior_session_secret_hash="ab" * 32,
                new_session_secret_hash="cd" * 32,
                new_csrf_secret_hash="ef" * 32,
                database_now=_DATABASE_NOW,
                diagnostic_context=_diagnostic_context(),
            )
        )
    assert rejected.value.error_code is ErrorCode.AUTHENTICATION_FAILED
    assert _statements_of(engine.connections[0], sa.sql.dml.Update, "web_sessions") == []


# --- throttle buckets -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_verification_failure_inserts_the_first_bucket_row() -> None:
    engine = ScriptedEngine(
        [
            ScriptedResult(rows=()),
            ScriptedResult(rows=(SimpleNamespace(throttle_bucket_id=uuid4()),)),
        ]
    )
    store = TotpStore(engine, secret_codec=ScriptedTotpCodec())
    transition = await store.record_verification_failure(
        bucket_kind=ThrottleBucketKind.TOTP_VERIFICATION,
        bucket_hash="0" * 64,
        database_now=_DATABASE_NOW,
    )
    assert transition.failed_attempt_count == 1
    assert transition.became_locked is False
    inserts = _statements_of(
        engine.connections[0],
        sa.sql.dml.Insert,
        "authentication_throttle_buckets",
    )
    assert len(inserts) == 1
    assert _statement_parameters(inserts[0])["bucket_kind"] == "totp_verification"
    assert "ON CONFLICT ON CONSTRAINT uq_authentication_throttle_buckets__kind_hash" in str(
        inserts[0].compile(dialect=postgresql.dialect())
    )


@pytest.mark.asyncio
async def test_record_verification_failure_losing_the_cold_insert_relocks_and_updates() -> None:
    # A concurrent first failure won the guarded insert: the loser re-selects
    # the winner's row under the lock and continues through the update path
    # instead of surfacing the unique violation as internal_error.
    engine = ScriptedEngine(
        [
            ScriptedResult(rows=()),
            ScriptedResult(rowcount=0),
            ScriptedResult(
                rows=(
                    SimpleNamespace(
                        throttle_bucket_id=uuid4(),
                        window_started_at=_DATABASE_NOW,
                        failed_attempt_count=1,
                        locked_until=None,
                    ),
                )
            ),
            ScriptedResult(rowcount=1),
        ]
    )
    store = TotpStore(engine, secret_codec=ScriptedTotpCodec())
    transition = await store.record_verification_failure(
        bucket_kind=ThrottleBucketKind.TOTP_VERIFICATION,
        bucket_hash="0" * 64,
        database_now=_DATABASE_NOW,
    )
    assert transition.failed_attempt_count == 2
    assert transition.became_locked is False
    connection = engine.connections[0]
    inserts = _statements_of(connection, sa.sql.dml.Insert, "authentication_throttle_buckets")
    assert len(inserts) == 1
    assert "ON CONFLICT ON CONSTRAINT uq_authentication_throttle_buckets__kind_hash DO NOTHING" in (
        str(inserts[0].compile(dialect=postgresql.dialect()))
    )
    updates = _statements_of(connection, sa.sql.dml.Update, "authentication_throttle_buckets")
    assert len(updates) == 1
    assert _statement_parameters(updates[0])["failed_attempt_count"] == 2
