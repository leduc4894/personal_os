"""Device-authorization store transaction contracts over a scripted engine.

The store unit tests double the async engine with a scripted connection that
records every executed statement and returns programmed rows: grant insertion
writes only the two HMAC digests with the pending state and the expiry matrix;
approval and denial lock the grant row ``FOR UPDATE``, apply the pure terminal
decision, update behind the pending-state guard and append exactly one audit
event; a lost race (rowcount zero) surfaces the closed state-invalid rejection
without an audit write; user-code lookup resolves by digest and resets the
lookup throttle bucket only for a resolvable grant; the live-grant window and
the throttle bucket helpers follow the shared bucket conventions. No database
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

from personal_os.authentication.device_authorization import (
    ApproveGrantCommand,
    DenyGrantCommand,
    InsertPendingGrantCommand,
    LiveGrantWindow,
    resolve_terminal_rejection_code,
)
from personal_os.authentication.errors import AuthenticationError
from personal_os.authentication.sessions import ThrottleBucketKind
from personal_os.diagnostics.context import DiagnosticContext, create_diagnostic_context
from personal_os.error_contracts.codes import ErrorCode
from postgresql_source_store.device_authorization_store import (
    DEVICE_AUTHORIZATION_APPROVED_AUDIT_ACTION,
    DEVICE_AUTHORIZATION_DENIED_AUDIT_ACTION,
    DeviceAuthorizationStore,
)

_DATABASE_NOW = datetime(2026, 8, 16, 12, 0, 0, tzinfo=UTC)
_USER_ID = uuid4()
_WORKSPACE_ID = uuid4()
_WEB_SESSION_ID = uuid4()
_GRANT_ID = uuid4()
_CLIENT_INSTANCE_ID = uuid4()
_USER_CODE = "ABCDEFG-W"
_USER_CODE_HASH = "a" * 64
_POLLING_CREDENTIAL_HASH = "b" * 64
_BUCKET_HASH = "c" * 64
_GRANT_EXPIRES_AT = _DATABASE_NOW + timedelta(seconds=600)


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


def _diagnostic_context() -> DiagnosticContext:
    return create_diagnostic_context().context


def _stored_grant_row(*, state: str = "pending") -> SimpleNamespace:
    return SimpleNamespace(
        grant_id=_GRANT_ID,
        user_code_hash=_USER_CODE_HASH,
        polling_secret_hash=_POLLING_CREDENTIAL_HASH,
        client_instance_id=_CLIENT_INSTANCE_ID,
        claimed_device_id=None,
        device_name="Personal desktop",
        platform_class="obsidian_desktop",
        platform_name="windows",
        plugin_version="1.4.0",
        requested_scope="obsidian_sync",
        state=state,
        created_at=_DATABASE_NOW - timedelta(seconds=1),
        expires_at=_GRANT_EXPIRES_AT,
        approved_at=None,
        denied_at=None,
        exchanged_at=None,
        approved_by_user_id=None,
        approved_web_session_id=None,
        device_id=None,
        token_family_id=None,
        initial_access_token_id=None,
        initial_refresh_token_id=None,
        derivation_key_id=None,
    )


def _insert_command(*, creation_bucket_hash: str | None = None) -> InsertPendingGrantCommand:
    return InsertPendingGrantCommand(
        grant_id=_GRANT_ID,
        user_code_hash=_USER_CODE_HASH,
        polling_secret_hash=_POLLING_CREDENTIAL_HASH,
        client_instance_id=_CLIENT_INSTANCE_ID,
        claimed_device_id=None,
        device_name="Personal desktop",
        platform_class="obsidian_desktop",
        platform_name="windows",
        plugin_version="1.4.0",
        requested_scope="obsidian_sync",
        expires_at=_GRANT_EXPIRES_AT,
        database_now=_DATABASE_NOW,
        creation_bucket_hash=creation_bucket_hash,
    )


# --- grant insertion ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_insert_pending_grant_stores_only_hashes_with_pending_state() -> None:
    engine = ScriptedEngine([])
    store = DeviceAuthorizationStore(engine)

    inserted = await store.insert_pending_grant(_insert_command())

    assert inserted.grant_id == _GRANT_ID
    assert inserted.expires_at == _GRANT_EXPIRES_AT
    connection = engine.connections[0]
    inserts = _statements_of(connection, sa.Insert, "device_authorization_grants")
    assert len(inserts) == 1
    parameters = _statement_parameters(inserts[0])
    assert parameters["user_code_hash"] == _USER_CODE_HASH
    assert parameters["polling_secret_hash"] == _POLLING_CREDENTIAL_HASH
    assert parameters["state"] == "pending"
    assert parameters["grant_id"] == _GRANT_ID
    assert _USER_CODE not in parameters.values()
    # Grant creation is an unauthenticated plugin request with no trusted
    # workspace: it writes no audit row (spec 21).
    assert _statements_of(connection, sa.Insert, "audit_events") == []


@pytest.mark.asyncio
async def test_insert_pending_grant_records_the_creation_attempt_in_commit() -> None:
    engine = ScriptedEngine(
        [
            ScriptedResult(),  # FOR UPDATE bucket select: no existing row
            ScriptedResult(  # guarded bucket insert wins: RETURNING row present
                rows=(SimpleNamespace(throttle_bucket_id=uuid4()),)
            ),
        ]
    )
    store = DeviceAuthorizationStore(engine)

    await store.insert_pending_grant(_insert_command(creation_bucket_hash=_BUCKET_HASH))

    connection = engine.connections[0]
    bucket_inserts = _statements_of(connection, sa.Insert, "authentication_throttle_buckets")
    assert len(bucket_inserts) == 1
    parameters = _statement_parameters(bucket_inserts[0])
    assert parameters["bucket_kind"] == ThrottleBucketKind.GRANT_CREATION.value
    assert parameters["bucket_hash"] == _BUCKET_HASH
    assert parameters["failed_attempt_count"] == 1


# --- approval and denial --------------------------------------------------------------


@pytest.mark.asyncio
async def test_approve_grant_locks_updates_and_writes_one_audit_event() -> None:
    engine = ScriptedEngine(
        [
            ScriptedResult(rows=(_stored_grant_row(),)),
            ScriptedResult(rowcount=1),
        ]
    )
    store = DeviceAuthorizationStore(engine)
    command = ApproveGrantCommand(
        grant_id=_GRANT_ID,
        user_id=_USER_ID,
        workspace_id=_WORKSPACE_ID,
        web_session_id=_WEB_SESSION_ID,
        database_now=_DATABASE_NOW,
        diagnostic_context=_diagnostic_context(),
    )

    approved = await store.approve_grant(command)

    assert approved.grant_id == _GRANT_ID
    assert approved.state == "approved"
    assert approved.approved_at == _DATABASE_NOW
    connection = engine.connections[0]
    updates = _statements_of(connection, sa.Update, "device_authorization_grants")
    assert len(updates) == 1
    parameters = _statement_parameters(updates[0])
    assert parameters["state"] == "approved"
    assert parameters["approved_at"] == _DATABASE_NOW
    assert parameters["approved_by_user_id"] == _USER_ID
    assert parameters["approved_web_session_id"] == _WEB_SESSION_ID
    audit_inserts = _statements_of(connection, sa.Insert, "audit_events")
    assert len(audit_inserts) == 1
    audit_parameters = _statement_parameters(audit_inserts[0])
    assert audit_parameters["action"] == DEVICE_AUTHORIZATION_APPROVED_AUDIT_ACTION
    assert audit_parameters["actor_id"] == _USER_ID
    assert audit_parameters["target_id"] == _GRANT_ID


@pytest.mark.asyncio
async def test_approve_grant_lost_race_rejects_without_audit() -> None:
    engine = ScriptedEngine(
        [
            ScriptedResult(rows=(_stored_grant_row(),)),
            ScriptedResult(rowcount=0),
        ]
    )
    store = DeviceAuthorizationStore(engine)
    command = ApproveGrantCommand(
        grant_id=_GRANT_ID,
        user_id=_USER_ID,
        workspace_id=_WORKSPACE_ID,
        web_session_id=_WEB_SESSION_ID,
        database_now=_DATABASE_NOW,
        diagnostic_context=_diagnostic_context(),
    )

    with pytest.raises(AuthenticationError) as raised:
        await store.approve_grant(command)
    assert raised.value.error_code is ErrorCode.DEVICE_AUTHORIZATION_STATE_INVALID
    assert _statements_of(engine.connections[0], sa.Insert, "audit_events") == []


@pytest.mark.asyncio
async def test_deny_grant_writes_the_denial_with_one_audit_event() -> None:
    engine = ScriptedEngine(
        [
            ScriptedResult(rows=(_stored_grant_row(),)),
            ScriptedResult(rowcount=1),
        ]
    )
    store = DeviceAuthorizationStore(engine)
    command = DenyGrantCommand(
        grant_id=_GRANT_ID,
        user_id=_USER_ID,
        workspace_id=_WORKSPACE_ID,
        web_session_id=_WEB_SESSION_ID,
        database_now=_DATABASE_NOW,
        diagnostic_context=_diagnostic_context(),
    )

    denied = await store.deny_grant(command)

    assert denied.grant_id == _GRANT_ID
    assert denied.state == "denied"
    assert denied.denied_at == _DATABASE_NOW
    connection = engine.connections[0]
    updates = _statements_of(connection, sa.Update, "device_authorization_grants")
    parameters = _statement_parameters(updates[0])
    assert parameters["state"] == "denied"
    assert parameters["denied_at"] == _DATABASE_NOW
    audit_inserts = _statements_of(connection, sa.Insert, "audit_events")
    assert len(audit_inserts) == 1
    assert (
        _statement_parameters(audit_inserts[0])["action"]
        == DEVICE_AUTHORIZATION_DENIED_AUDIT_ACTION
    )


@pytest.mark.asyncio
async def test_terminal_transitions_apply_the_pure_rejection_decisions() -> None:
    engine = ScriptedEngine([ScriptedResult(rows=(_stored_grant_row(state="approved"),))])
    store = DeviceAuthorizationStore(engine)
    command = ApproveGrantCommand(
        grant_id=_GRANT_ID,
        user_id=_USER_ID,
        workspace_id=_WORKSPACE_ID,
        web_session_id=_WEB_SESSION_ID,
        database_now=_DATABASE_NOW,
        diagnostic_context=_diagnostic_context(),
    )
    with pytest.raises(AuthenticationError) as raised:
        await store.approve_grant(command)
    assert raised.value.error_code is ErrorCode.DEVICE_AUTHORIZATION_STATE_INVALID
    assert resolve_terminal_rejection_code(None, database_now=_DATABASE_NOW) is (
        ErrorCode.DEVICE_CREDENTIAL_INVALID
    )


# --- lookup and windows ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_lookup_grant_resolves_by_digest_and_resets_the_bucket() -> None:
    engine = ScriptedEngine([ScriptedResult(rows=(_stored_grant_row(),))])
    store = DeviceAuthorizationStore(engine)

    stored = await store.lookup_grant_by_user_code(
        user_code_hash=_USER_CODE_HASH,
        database_now=_DATABASE_NOW,
        reset_bucket_hash=_BUCKET_HASH,
    )

    assert stored is not None
    assert stored.grant_id == _GRANT_ID
    assert stored.device_name == "Personal desktop"
    assert stored.state == "pending"
    connection = engine.connections[0]
    bucket_updates = _statements_of(connection, sa.Update, "authentication_throttle_buckets")
    assert len(bucket_updates) == 1
    parameters = _statement_parameters(bucket_updates[0])
    assert parameters["bucket_kind"] == ThrottleBucketKind.USER_CODE_LOOKUP.value
    assert parameters["failed_attempt_count"] == 0


@pytest.mark.asyncio
async def test_lookup_grant_returns_none_without_a_row() -> None:
    engine = ScriptedEngine([ScriptedResult()])
    store = DeviceAuthorizationStore(engine)

    stored = await store.lookup_grant_by_user_code(
        user_code_hash=_USER_CODE_HASH, database_now=_DATABASE_NOW, reset_bucket_hash=None
    )

    assert stored is None
    assert _statements_of(engine.connections[0], sa.Update, "authentication_throttle_buckets") == []


@pytest.mark.asyncio
async def test_live_grant_window_counts_pending_unexpired_grants() -> None:
    engine = ScriptedEngine(
        [
            ScriptedResult(
                rows=(
                    SimpleNamespace(
                        live_grant_count=3,
                        earliest_expires_at=_DATABASE_NOW + timedelta(seconds=42),
                    ),
                )
            )
        ]
    )
    store = DeviceAuthorizationStore(engine)

    window = await store.live_grant_window(
        client_instance_id=_CLIENT_INSTANCE_ID, database_now=_DATABASE_NOW
    )

    assert window == LiveGrantWindow(
        live_grant_count=3, earliest_expires_at=_DATABASE_NOW + timedelta(seconds=42)
    )


@pytest.mark.asyncio
async def test_throttle_bucket_helpers_follow_the_shared_conventions() -> None:
    bucket_row_id = uuid4()
    engine = ScriptedEngine(
        [
            ScriptedResult(
                rows=(
                    SimpleNamespace(
                        window_started_at=_DATABASE_NOW,
                        failed_attempt_count=2,
                        locked_until=None,
                    ),
                )
            ),
            ScriptedResult(
                rows=(
                    SimpleNamespace(
                        throttle_bucket_id=bucket_row_id,
                        window_started_at=_DATABASE_NOW,
                        failed_attempt_count=2,
                        locked_until=None,
                    ),
                )
            ),
        ]
    )
    store = DeviceAuthorizationStore(engine)

    bucket = await store.resolve_throttle_bucket(
        bucket_kind=ThrottleBucketKind.GRANT_CREATION, bucket_hash=_BUCKET_HASH
    )
    assert bucket is not None
    assert bucket.failed_attempt_count == 2

    transition = await store.record_throttle_attempt(
        bucket_kind=ThrottleBucketKind.GRANT_CREATION,
        bucket_hash=_BUCKET_HASH,
        database_now=_DATABASE_NOW,
    )
    assert transition.failed_attempt_count == 3
    bucket_updates = _statements_of(
        engine.connections[1], sa.Update, "authentication_throttle_buckets"
    )
    assert len(bucket_updates) == 1
    assert _statement_parameters(bucket_updates[0])["failed_attempt_count"] == 3


@pytest.mark.asyncio
async def test_throttle_cold_insert_carries_the_unique_constraint_guard() -> None:
    engine = ScriptedEngine(
        [
            ScriptedResult(rows=()),
            ScriptedResult(rows=(SimpleNamespace(throttle_bucket_id=uuid4()),)),
        ]
    )
    store = DeviceAuthorizationStore(engine)
    transition = await store.record_throttle_attempt(
        bucket_kind=ThrottleBucketKind.GRANT_CREATION,
        bucket_hash=_BUCKET_HASH,
        database_now=_DATABASE_NOW,
    )
    assert transition.failed_attempt_count == 1
    bucket_inserts = _statements_of(
        engine.connections[0], sa.Insert, "authentication_throttle_buckets"
    )
    assert len(bucket_inserts) == 1
    assert "ON CONFLICT ON CONSTRAINT uq_authentication_throttle_buckets__kind_hash DO NOTHING" in (
        str(bucket_inserts[0].compile(dialect=postgresql.dialect()))
    )
    # Winning the guarded insert settles without any update statement.
    assert _statements_of(engine.connections[0], sa.Update, "authentication_throttle_buckets") == []


@pytest.mark.asyncio
async def test_throttle_cold_insert_loser_relocks_the_winner_row_and_updates() -> None:
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
    store = DeviceAuthorizationStore(engine)
    transition = await store.record_throttle_attempt(
        bucket_kind=ThrottleBucketKind.GRANT_CREATION,
        bucket_hash=_BUCKET_HASH,
        database_now=_DATABASE_NOW,
    )
    assert transition.failed_attempt_count == 2
    assert transition.became_locked is False
    connection = engine.connections[0]
    bucket_inserts = _statements_of(connection, sa.Insert, "authentication_throttle_buckets")
    assert len(bucket_inserts) == 1
    assert "ON CONFLICT ON CONSTRAINT uq_authentication_throttle_buckets__kind_hash DO NOTHING" in (
        str(bucket_inserts[0].compile(dialect=postgresql.dialect()))
    )
    bucket_updates = _statements_of(connection, sa.Update, "authentication_throttle_buckets")
    assert len(bucket_updates) == 1
    assert _statement_parameters(bucket_updates[0])["failed_attempt_count"] == 2
