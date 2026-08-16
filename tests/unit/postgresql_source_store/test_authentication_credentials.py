"""Credential and throttle transaction contracts over a scripted engine double.

The credential store unit tests double the async engine with a scripted
connection that records every executed statement and returns programmed rows:
login material resolution binds the username and both HMACed bucket hashes;
failure recording applies the pure domain transition under row locks and audits
only trusted accounts; the login-success transaction rechecks the active
user/workspace and credential revision before inserting the session, upgrading
an obsolete hash and resetting the streak; password change bumps the revision,
revokes every other session with cleared authenticated timestamps and rotates
the current binding; and ``required_key_ids`` unions the TOTP ciphertext key
references with the replay-eligible grant/token state. No database is touched.
"""

from __future__ import annotations

import dataclasses
import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.sql.elements import TextClause

from personal_os.authentication.errors import AuthenticationError
from personal_os.authentication.sessions import (
    ChangePasswordCommand,
    CommitLoginSuccessCommand,
    RecordLoginFailureCommand,
    ThrottleBucketState,
    ThrottleWindowPolicy,
)
from personal_os.diagnostics.context import create_diagnostic_context
from personal_os.error_contracts.codes import ErrorCode
from postgresql_source_store.authentication_credentials import (
    LOGIN_REJECTED_AUDIT_ACTION,
    LOGIN_SUCCEEDED_AUDIT_ACTION,
    PASSWORD_CHANGED_AUDIT_ACTION,
    REVOCATION_REASON_PASSWORD_CHANGED,
    CredentialStore,
)

_DATABASE_NOW = datetime(2026, 8, 16, 12, 0, 0, tzinfo=UTC)
_USER_ID = uuid4()
_WORKSPACE_ID = uuid4()
_USERNAME_BUCKET_HASH = "ab" * 32
_SOURCE_BUCKET_HASH = "cd" * 32
_SESSION_SECRET_HASH = "ef" * 32
_CSRF_SECRET_HASH = "0f" * 32
_PHC_HASH = "$argon2id$v=19$m=65536,t=3,p=1$c2FsdHNhbHQ$c2VjcmV0aGFzaDEyMzQ1Njc4OTA"


class ScriptedResult:
    """Programmed result double exposing the executed-statement return shape."""

    def __init__(self, rows: tuple[Any, ...] = (), rowcount: int = -1) -> None:
        self._rows = list(rows)
        self.rowcount = rowcount

    def one_or_none(self) -> Any:
        return self._rows[0] if self._rows else None

    def one(self) -> Any:
        assert len(self._rows) == 1, "scripted result must program exactly one row"
        return self._rows[0]

    def all(self) -> list[Any]:
        return list(self._rows)


class ScriptedConnection:
    """Connection double recording statements and popping programmed results."""

    def __init__(self, results: list[ScriptedResult]) -> None:
        self._results = results
        self.executed_statements: list[Any] = []

    async def execute(self, statement: Any) -> ScriptedResult:
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


def _statement_parameters(statement: Any) -> dict[str, Any]:
    """Compiled bind parameters with WHERE-suffix aliases added.

    ``VALUES`` binds keep their plain names; WHERE binds carry a numeric
    suffix, so each suffixed name additionally exposes its stripped alias
    unless a plain-named ``VALUES`` bind already owns it.
    """
    raw_parameters = dict(statement.compile().params)
    resolved = dict(raw_parameters)
    for name, value in raw_parameters.items():
        stripped_name = _BINDNAME_SUFFIX_PATTERN.sub("", name)
        if stripped_name != name and stripped_name not in resolved:
            resolved[stripped_name] = value
    return resolved


def _record_login_failure_command(
    *, user_id: UUID | None = _USER_ID, workspace_id: UUID | None = _WORKSPACE_ID
) -> RecordLoginFailureCommand:
    return RecordLoginFailureCommand(
        username_bucket_hash=_USERNAME_BUCKET_HASH,
        source_bucket_hash=_SOURCE_BUCKET_HASH,
        user_id=user_id,
        workspace_id=workspace_id,
        database_now=_DATABASE_NOW,
        diagnostic_context=create_diagnostic_context().context,
    )


def _commit_login_success_command() -> CommitLoginSuccessCommand:
    return CommitLoginSuccessCommand(
        user_id=_USER_ID,
        workspace_id=_WORKSPACE_ID,
        expected_credential_revision=1,
        username_bucket_hash=_USERNAME_BUCKET_HASH,
        web_session_id=uuid4(),
        session_secret_hash=_SESSION_SECRET_HASH,
        csrf_secret_hash=_CSRF_SECRET_HASH,
        authentication_method="password",
        database_now=_DATABASE_NOW,
        active_idle_expires_at=_DATABASE_NOW + timedelta(hours=12),
        pending_totp_idle_expires_at=_DATABASE_NOW + timedelta(minutes=5),
        absolute_expires_at=_DATABASE_NOW + timedelta(days=7),
        upgraded_password_hash=None,
        diagnostic_context=create_diagnostic_context().context,
    )


def _change_password_command() -> ChangePasswordCommand:
    return ChangePasswordCommand(
        user_id=_USER_ID,
        workspace_id=_WORKSPACE_ID,
        current_web_session_id=uuid4(),
        prior_session_secret_hash=_SESSION_SECRET_HASH,
        expected_credential_revision=1,
        new_password_hash=_PHC_HASH,
        new_session_secret_hash="11" * 32,
        new_csrf_secret_hash="22" * 32,
        database_now=_DATABASE_NOW,
        diagnostic_context=create_diagnostic_context().context,
    )


def _credential_row(
    *,
    credential_revision: int = 1,
    user_status: str = "active",
    workspace_status: str = "active",
    has_active_totp: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        credential_revision=credential_revision,
        user_status=user_status,
        workspace_status=workspace_status,
        has_active_totp=has_active_totp,
    )


def _bucket_row(
    *,
    throttle_bucket_id: UUID | None = None,
    failed_attempt_count: int = 0,
    locked_until: datetime | None = None,
    window_started_at: datetime | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        throttle_bucket_id=throttle_bucket_id if throttle_bucket_id is not None else uuid4(),
        window_started_at=(window_started_at if window_started_at is not None else _DATABASE_NOW),
        failed_attempt_count=failed_attempt_count,
        locked_until=locked_until,
    )


# --- login material resolution --------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_login_material_binds_username_and_bucket_hashes() -> None:
    engine = ScriptedEngine(
        [
            ScriptedResult(
                rows=(
                    SimpleNamespace(
                        user_id=_USER_ID,
                        user_status="active",
                        workspace_id=_WORKSPACE_ID,
                        workspace_status="active",
                        password_hash=_PHC_HASH,
                        credential_revision=1,
                    ),
                )
            ),
            ScriptedResult(rows=(_bucket_row(failed_attempt_count=2),)),
            ScriptedResult(rows=(_bucket_row(failed_attempt_count=0),)),
        ]
    )
    store = CredentialStore(engine)
    material = await store.resolve_login_material(
        username="owner",
        username_bucket_hash=_USERNAME_BUCKET_HASH,
        source_bucket_hash=_SOURCE_BUCKET_HASH,
    )
    assert material.user_id == _USER_ID
    assert material.workspace_id == _WORKSPACE_ID
    assert material.is_trusted_account is True
    assert material.password_hash == _PHC_HASH
    assert material.credential_revision == 1
    assert material.username_bucket == ThrottleBucketState(
        window_started_at=_DATABASE_NOW, failed_attempt_count=2, locked_until=None
    )
    assert material.source_bucket is not None
    assert material.source_bucket.failed_attempt_count == 0
    connection = engine.connections[0]
    material_statement, username_statement, source_statement = connection.executed_statements[3:]
    assert "users" in str(material_statement)
    assert _statement_parameters(material_statement)["username"] == "owner"
    username_parameters = _statement_parameters(username_statement)
    source_parameters = _statement_parameters(source_statement)
    assert username_parameters["bucket_kind"] == "login_username"
    assert username_parameters["bucket_hash"] == _USERNAME_BUCKET_HASH
    assert source_parameters["bucket_kind"] == "login_source"
    assert source_parameters["bucket_hash"] == _SOURCE_BUCKET_HASH


@pytest.mark.asyncio
async def test_resolve_login_material_without_credential_has_no_secrets() -> None:
    engine = ScriptedEngine(
        [
            ScriptedResult(
                rows=(
                    SimpleNamespace(
                        user_id=_USER_ID,
                        user_status="active",
                        workspace_id=None,
                        workspace_status=None,
                        password_hash=None,
                        credential_revision=None,
                    ),
                )
            ),
            ScriptedResult(),
            ScriptedResult(),
        ]
    )
    store = CredentialStore(engine)
    material = await store.resolve_login_material(
        username="owner",
        username_bucket_hash=_USERNAME_BUCKET_HASH,
        source_bucket_hash=_SOURCE_BUCKET_HASH,
    )
    assert material.user_id is None
    assert material.workspace_id is None
    assert material.is_trusted_account is False
    assert material.password_hash is None
    assert material.credential_revision is None
    assert material.username_bucket is None
    assert material.source_bucket is None


# --- login failure recording ----------------------------------------------------


@pytest.mark.asyncio
async def test_fifth_failure_persists_locked_bucket_and_audits_trusted_account() -> None:
    engine = ScriptedEngine(
        [
            ScriptedResult(rows=(_credential_row(),)),
            ScriptedResult(rows=(_bucket_row(failed_attempt_count=4),)),
            ScriptedResult(rowcount=1),
            ScriptedResult(rows=(_bucket_row(failed_attempt_count=4),)),
            ScriptedResult(rowcount=1),
        ]
    )
    store = CredentialStore(engine, throttle_policy=ThrottleWindowPolicy())
    recorded = await store.record_login_failure(_record_login_failure_command())
    assert recorded.username_bucket.failed_attempt_count == 5
    assert recorded.username_bucket.locked_until == _DATABASE_NOW + timedelta(minutes=15)
    assert recorded.source_bucket.locked_until == _DATABASE_NOW + timedelta(minutes=15)
    assert recorded.was_audited is True
    connection = engine.connections[0]
    updates = [
        statement
        for statement in connection.executed_statements
        if isinstance(statement, sa.sql.dml.Update)
        and statement.table.name == "authentication_throttle_buckets"
    ]
    assert len(updates) == 2
    username_update_parameters = _statement_parameters(updates[0])
    assert username_update_parameters["failed_attempt_count"] == 5
    assert username_update_parameters["locked_until"] == _DATABASE_NOW + timedelta(minutes=15)
    assert username_update_parameters["window_started_at"] == _DATABASE_NOW
    audit_inserts = [
        statement
        for statement in connection.executed_statements
        if isinstance(statement, sa.sql.dml.Insert) and statement.table.name == "audit_events"
    ]
    assert len(audit_inserts) == 1
    audit_parameters = _statement_parameters(audit_inserts[0])
    assert audit_parameters["action"] == LOGIN_REJECTED_AUDIT_ACTION
    assert audit_parameters["result"] == "rejected"
    assert audit_parameters["actor_id"] == _USER_ID


@pytest.mark.asyncio
async def test_untrusted_account_failure_skips_the_audit_row() -> None:
    engine = ScriptedEngine(
        [
            ScriptedResult(rows=(_bucket_row(),)),
            ScriptedResult(rowcount=1),
            ScriptedResult(rows=(_bucket_row(),)),
            ScriptedResult(rowcount=1),
        ]
    )
    store = CredentialStore(engine)
    recorded = await store.record_login_failure(
        _record_login_failure_command(user_id=None, workspace_id=None)
    )
    assert recorded.was_audited is False
    assert recorded.username_bucket.failed_attempt_count == 1
    connection = engine.connections[0]
    assert not [
        statement
        for statement in connection.executed_statements
        if isinstance(statement, sa.sql.dml.Insert) and statement.table.name == "audit_events"
    ]


# --- login success commit --------------------------------------------------------


@pytest.mark.asyncio
async def test_commit_login_success_rechecks_and_inserts_active_session() -> None:
    engine = ScriptedEngine(
        [
            ScriptedResult(rows=(_credential_row(),)),
            ScriptedResult(rowcount=1),
            ScriptedResult(rowcount=1),
        ]
    )
    store = CredentialStore(engine)
    command = _commit_login_success_command()
    committed = await store.commit_login_success(command)
    assert committed.state.value == "active"
    assert committed.authenticated_at == _DATABASE_NOW
    connection = engine.connections[0]
    session_inserts = [
        statement
        for statement in connection.executed_statements
        if isinstance(statement, sa.sql.dml.Insert) and statement.table.name == "web_sessions"
    ]
    assert len(session_inserts) == 1
    parameters = _statement_parameters(session_inserts[0])
    assert parameters["state"] == "active"
    assert parameters["authenticated_at"] == _DATABASE_NOW
    assert parameters["session_secret_hash"] == _SESSION_SECRET_HASH
    assert parameters["csrf_secret_hash"] == _CSRF_SECRET_HASH
    assert parameters["idle_expires_at"] == command.active_idle_expires_at
    assert parameters["absolute_expires_at"] == command.absolute_expires_at
    audit_parameters = _statement_parameters(
        next(
            statement
            for statement in connection.executed_statements
            if isinstance(statement, sa.sql.dml.Insert) and statement.table.name == "audit_events"
        )
    )
    assert audit_parameters["action"] == LOGIN_SUCCEEDED_AUDIT_ACTION
    assert audit_parameters["result"] == "succeeded"


@pytest.mark.asyncio
async def test_commit_login_success_with_active_totp_inserts_pending_session() -> None:
    engine = ScriptedEngine(
        [
            ScriptedResult(rows=(_credential_row(has_active_totp=True),)),
            ScriptedResult(rowcount=1),
        ]
    )
    store = CredentialStore(engine)
    committed = await store.commit_login_success(_commit_login_success_command())
    assert committed.state.value == "pending_totp"
    assert committed.authenticated_at is None
    parameters = _statement_parameters(
        next(
            statement
            for statement in engine.connections[0].executed_statements
            if isinstance(statement, sa.sql.dml.Insert) and statement.table.name == "web_sessions"
        )
    )
    assert parameters["state"] == "pending_totp"
    assert parameters["authenticated_at"] is None


@pytest.mark.asyncio
async def test_commit_login_success_rejects_stale_revision_without_writes() -> None:
    engine = ScriptedEngine([ScriptedResult(rows=(_credential_row(credential_revision=2),))])
    store = CredentialStore(engine)
    with pytest.raises(AuthenticationError) as rejected:
        await store.commit_login_success(_commit_login_success_command())
    assert rejected.value.error_code is ErrorCode.AUTHENTICATION_FAILED
    mutating = [
        statement
        for statement in engine.connections[0].executed_statements
        if isinstance(statement, (sa.sql.dml.Insert, sa.sql.dml.Update))
    ]
    assert mutating == []


@pytest.mark.asyncio
async def test_commit_login_success_upgrades_obsolete_hash() -> None:
    engine = ScriptedEngine(
        [
            ScriptedResult(rows=(_credential_row(),)),
            ScriptedResult(rowcount=1),
            ScriptedResult(rowcount=1),
            ScriptedResult(rowcount=1),
        ]
    )
    store = CredentialStore(engine)
    command = dataclasses.replace(_commit_login_success_command(), upgraded_password_hash=_PHC_HASH)
    await store.commit_login_success(command)
    upgrades = [
        statement
        for statement in engine.connections[0].executed_statements
        if isinstance(statement, sa.sql.dml.Update) and statement.table.name == "user_credentials"
    ]
    assert len(upgrades) == 1
    upgrade_parameters = _statement_parameters(upgrades[0])
    assert upgrade_parameters["password_hash"] == _PHC_HASH
    assert upgrade_parameters["updated_at"] == _DATABASE_NOW
    assert "credential_revision" not in upgrade_parameters


# --- password change -------------------------------------------------------------


@pytest.mark.asyncio
async def test_change_password_bumps_revision_revokes_and_rotates() -> None:
    engine = ScriptedEngine(
        [
            ScriptedResult(rows=(_credential_row(),)),
            ScriptedResult(rowcount=1),
            ScriptedResult(rowcount=2),
            ScriptedResult(rowcount=1),
            ScriptedResult(rowcount=1),
        ]
    )
    store = CredentialStore(engine)
    command = _change_password_command()
    changed = await store.change_password(command)
    assert changed.credential_revision == 2
    assert changed.revoked_session_count == 2
    connection = engine.connections[0]
    credential_updates = [
        statement
        for statement in connection.executed_statements
        if isinstance(statement, sa.sql.dml.Update) and statement.table.name == "user_credentials"
    ]
    assert len(credential_updates) == 1
    credential_parameters = _statement_parameters(credential_updates[0])
    assert credential_parameters["credential_revision"] == 2
    assert credential_parameters["password_hash"] == _PHC_HASH
    assert credential_parameters["password_changed_at"] == _DATABASE_NOW
    session_updates = [
        statement
        for statement in connection.executed_statements
        if isinstance(statement, sa.sql.dml.Update) and statement.table.name == "web_sessions"
    ]
    assert len(session_updates) == 2
    revoke_parameters = _statement_parameters(session_updates[0])
    assert revoke_parameters["state"] == "revoked"
    assert revoke_parameters["revoked_at"] == _DATABASE_NOW
    assert revoke_parameters["revocation_reason"] == REVOCATION_REASON_PASSWORD_CHANGED
    assert revoke_parameters["authenticated_at"] is None
    assert revoke_parameters["reauthenticated_at"] is None
    rotate_parameters = _statement_parameters(session_updates[1])
    assert rotate_parameters["credential_revision"] == 2
    assert rotate_parameters["session_secret_hash"] == command.new_session_secret_hash
    assert rotate_parameters["csrf_secret_hash"] == command.new_csrf_secret_hash
    audit_parameters = _statement_parameters(
        next(
            statement
            for statement in connection.executed_statements
            if isinstance(statement, sa.sql.dml.Insert) and statement.table.name == "audit_events"
        )
    )
    assert audit_parameters["action"] == PASSWORD_CHANGED_AUDIT_ACTION


# --- keyring reference resolution -------------------------------------------------


@pytest.mark.asyncio
async def test_required_key_ids_unions_referenced_key_state() -> None:
    engine = ScriptedEngine(
        [
            ScriptedResult(
                rows=(
                    SimpleNamespace(referenced_key_id="authkey-totp-current"),
                    SimpleNamespace(referenced_key_id="authkey-token-active"),
                )
            )
        ]
    )
    store = CredentialStore(engine)
    required = await store.required_key_ids(database_now=_DATABASE_NOW)
    assert required == frozenset({"authkey-totp-current", "authkey-token-active"})
    statement_text = str(engine.connections[0].executed_statements[3])
    assert "totp_credentials" in statement_text
    assert "device_tokens" in statement_text
    assert "device_authorization_grants" in statement_text


@pytest.mark.asyncio
async def test_required_key_ids_empty_without_references() -> None:
    engine = ScriptedEngine([ScriptedResult(rows=())])
    store = CredentialStore(engine)
    assert await store.required_key_ids(database_now=_DATABASE_NOW) == frozenset()
