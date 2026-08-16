"""Web-session transaction contracts over a scripted engine double.

The session store unit tests double the async engine with a scripted connection
that records every executed statement and returns programmed rows: session
resolution selects the row behind its unique secret hash, applies the domain
authentication decision and conditionally advances ``last_seen_at`` with the
idle expiry clamped to the absolute boundary; secret rotation per closed cause
rewrites exactly the cause-owned timestamps and both secret hashes under the
prior-hash guard; and revocation clears ``authenticated_at`` and
``reauthenticated_at`` together, because the schema's state/timestamp matrix
rejects a revoked row that still carries an authenticated moment. No database
is touched.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.sql.elements import TextClause

from personal_os.authentication.contracts import WebSessionState
from personal_os.authentication.errors import AuthenticationError
from personal_os.authentication.sessions import (
    RevokeWebSessionCommand,
    RotateWebSessionSecretsCommand,
    SessionRotationCause,
)
from personal_os.error_contracts.codes import ErrorCode
from postgresql_source_store.web_session_store import (
    WebSessionStore,
)

_DATABASE_NOW = datetime(2026, 8, 16, 12, 0, 0, tzinfo=UTC)
_SESSION_ID = uuid4()
_USER_ID = uuid4()
_WORKSPACE_ID = uuid4()
_PRIOR_SECRET_HASH = "ab" * 32
_NEW_SECRET_HASH = "cd" * 32
_NEW_CSRF_HASH = "ef" * 32
_PHC_HASH = "$argon2id$v=19$m=65536,t=3,p=1$c2FsdHNhbHQ$c2VjcmV0aGFzaDEyMzQ1Njc4OTA"


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
    """Compiled bind parameters with WHERE-suffix aliases added.

    ``VALUES`` binds keep their plain names; WHERE binds carry a numeric
    suffix, so each suffixed name additionally exposes its stripped alias
    unless a plain-named ``VALUES`` bind already owns it.
    """
    raw_parameters: dict[str, object] = dict(
        statement.compile().params  # type: ignore[attr-defined]
    )
    resolved = dict(raw_parameters)
    for name, value in raw_parameters.items():
        stripped_name = _BINDNAME_SUFFIX_PATTERN.sub("", name)
        if stripped_name != name and stripped_name not in resolved:
            resolved[stripped_name] = value
    return resolved


def _session_row(
    *,
    state: str = "active",
    credential_revision: int = 1,
    authentication_method: str = "password",
    authenticated_at: datetime | None = _DATABASE_NOW,
    reauthenticated_at: datetime | None = None,
    last_seen_at: datetime | None = None,
    idle_expires_at: datetime | None = None,
    absolute_expires_at: datetime | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        web_session_id=_SESSION_ID,
        user_id=_USER_ID,
        workspace_id=_WORKSPACE_ID,
        session_secret_hash=_PRIOR_SECRET_HASH,
        csrf_secret_hash="01" * 32,
        state=state,
        credential_revision=credential_revision,
        authentication_method=authentication_method,
        created_at=_DATABASE_NOW - timedelta(minutes=1),
        authenticated_at=authenticated_at,
        reauthenticated_at=reauthenticated_at,
        last_seen_at=last_seen_at,
        idle_expires_at=(
            idle_expires_at if idle_expires_at is not None else _DATABASE_NOW + timedelta(hours=12)
        ),
        absolute_expires_at=(
            absolute_expires_at
            if absolute_expires_at is not None
            else _DATABASE_NOW + timedelta(days=7)
        ),
        revoked_at=None,
        revocation_reason=None,
    )


def _resolved_session_row(**overrides: object) -> SimpleNamespace:
    """One joined ``web_sessions``/``user_credentials`` resolution row."""
    fields: dict[str, object] = {
        **vars(_session_row()),
        "current_credential_revision": 1,
        "password_hash": _PHC_HASH,
    }
    return SimpleNamespace(**(fields | overrides))


# --- session resolution -----------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_session_advances_idle_without_passing_absolute_expiry() -> None:
    absolute_expiry = _DATABASE_NOW + timedelta(hours=8)
    engine = ScriptedEngine(
        [
            ScriptedResult(rows=(_resolved_session_row(absolute_expires_at=absolute_expiry),)),
            ScriptedResult(rowcount=1),
        ]
    )
    store = WebSessionStore(engine)
    resolved = await store.resolve_session(
        session_secret_hash=_PRIOR_SECRET_HASH, database_now=_DATABASE_NOW
    )
    assert resolved.session.web_session_id == _SESSION_ID
    assert resolved.current_credential_revision == 1
    assert resolved.password_hash == _PHC_HASH
    assert resolved.session.state is WebSessionState.ACTIVE
    connection = engine.connections[0]
    activity_updates = [
        statement
        for statement in connection.executed_statements
        if isinstance(statement, sa.sql.dml.Update) and statement.table.name == "web_sessions"
    ]
    assert len(activity_updates) == 1
    parameters = _statement_parameters(activity_updates[0])
    # database_now + 12h idle would pass the 8h absolute boundary: clamped.
    assert parameters["idle_expires_at"] == absolute_expiry
    assert parameters["last_seen_at"] == _DATABASE_NOW


@pytest.mark.asyncio
async def test_resolve_session_rejects_missing_row_without_activity_write() -> None:
    engine = ScriptedEngine([ScriptedResult(rows=())])
    store = WebSessionStore(engine)
    with pytest.raises(AuthenticationError) as rejected:
        await store.resolve_session(
            session_secret_hash=_PRIOR_SECRET_HASH, database_now=_DATABASE_NOW
        )
    assert rejected.value.error_code is ErrorCode.AUTHENTICATION_REQUIRED
    assert not [
        statement
        for statement in engine.connections[0].executed_statements
        if isinstance(statement, sa.sql.dml.Update)
    ]


@pytest.mark.asyncio
async def test_resolve_session_rejects_stale_revision_without_activity_write() -> None:
    engine = ScriptedEngine(
        [
            ScriptedResult(
                rows=(
                    SimpleNamespace(
                        **{
                            **vars(_session_row()),
                            "current_credential_revision": 2,
                            "password_hash": _PHC_HASH,
                        }
                    ),
                )
            )
        ]
    )
    store = WebSessionStore(engine)
    with pytest.raises(AuthenticationError) as rejected:
        await store.resolve_session(
            session_secret_hash=_PRIOR_SECRET_HASH, database_now=_DATABASE_NOW
        )
    assert rejected.value.error_code is ErrorCode.AUTHENTICATION_REQUIRED
    assert not [
        statement
        for statement in engine.connections[0].executed_statements
        if isinstance(statement, sa.sql.dml.Update)
    ]


# --- secret rotation --------------------------------------------------------------


@pytest.mark.asyncio
async def test_rotate_for_reauthentication_records_the_moment_and_new_hashes() -> None:
    engine = ScriptedEngine([ScriptedResult(rows=(_session_row(),)), ScriptedResult(rowcount=1)])
    store = WebSessionStore(engine)
    rotated = await store.rotate_session_secrets(
        RotateWebSessionSecretsCommand(
            web_session_id=_SESSION_ID,
            prior_session_secret_hash=_PRIOR_SECRET_HASH,
            new_session_secret_hash=_NEW_SECRET_HASH,
            new_csrf_secret_hash=_NEW_CSRF_HASH,
            cause=SessionRotationCause.RECENT_REAUTHENTICATION,
            target_authentication_method="password",
            database_now=_DATABASE_NOW,
        )
    )
    assert rotated.state is WebSessionState.ACTIVE
    parameters = _statement_parameters(
        next(
            statement
            for statement in engine.connections[0].executed_statements
            if isinstance(statement, sa.sql.dml.Update)
        )
    )
    assert parameters["reauthenticated_at"] == _DATABASE_NOW
    assert parameters["session_secret_hash"] == _NEW_SECRET_HASH
    assert parameters["csrf_secret_hash"] == _NEW_CSRF_HASH
    assert "authenticated_at" not in parameters
    assert "state" not in parameters


@pytest.mark.asyncio
async def test_rotate_for_activation_activates_and_rebinds_the_method() -> None:
    engine = ScriptedEngine(
        [
            ScriptedResult(
                rows=(
                    _session_row(
                        state="pending_totp",
                        authentication_method="password",
                        authenticated_at=None,
                        idle_expires_at=_DATABASE_NOW + timedelta(minutes=4),
                    ),
                )
            ),
            ScriptedResult(rowcount=1),
        ]
    )
    store = WebSessionStore(engine)
    rotated = await store.rotate_session_secrets(
        RotateWebSessionSecretsCommand(
            web_session_id=_SESSION_ID,
            prior_session_secret_hash=_PRIOR_SECRET_HASH,
            new_session_secret_hash=_NEW_SECRET_HASH,
            new_csrf_secret_hash=_NEW_CSRF_HASH,
            cause=SessionRotationCause.SESSION_ACTIVATION,
            target_authentication_method="password_totp",
            database_now=_DATABASE_NOW,
        )
    )
    assert rotated.state is WebSessionState.ACTIVE
    parameters = _statement_parameters(
        next(
            statement
            for statement in engine.connections[0].executed_statements
            if isinstance(statement, sa.sql.dml.Update)
        )
    )
    assert parameters["state"] == "active"
    assert parameters["authenticated_at"] == _DATABASE_NOW
    assert parameters["authentication_method"] == "password_totp"
    assert parameters["idle_expires_at"] == _DATABASE_NOW + timedelta(hours=12)
    assert parameters["reauthenticated_at"] is None


@pytest.mark.asyncio
async def test_rotate_rejects_a_guard_mismatch() -> None:
    engine = ScriptedEngine([ScriptedResult(rows=(_session_row(),)), ScriptedResult(rowcount=0)])
    store = WebSessionStore(engine)
    with pytest.raises(AuthenticationError) as rejected:
        await store.rotate_session_secrets(
            RotateWebSessionSecretsCommand(
                web_session_id=_SESSION_ID,
                prior_session_secret_hash=_PRIOR_SECRET_HASH,
                new_session_secret_hash=_NEW_SECRET_HASH,
                new_csrf_secret_hash=_NEW_CSRF_HASH,
                cause=SessionRotationCause.RECENT_REAUTHENTICATION,
                target_authentication_method="password",
                database_now=_DATABASE_NOW,
            )
        )
    assert rejected.value.error_code is ErrorCode.AUTHENTICATION_REQUIRED


# --- revocation -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_revoke_clears_both_authenticated_timestamps() -> None:
    engine = ScriptedEngine([ScriptedResult(rows=(_session_row(),)), ScriptedResult(rowcount=1)])
    store = WebSessionStore(engine)
    revoked = await store.revoke_session(
        RevokeWebSessionCommand(
            session_secret_hash=_PRIOR_SECRET_HASH,
            revocation_reason="logout",
            database_now=_DATABASE_NOW,
        )
    )
    assert revoked.revoked_at == _DATABASE_NOW
    assert revoked.web_session_id == _SESSION_ID
    parameters = _statement_parameters(
        next(
            statement
            for statement in engine.connections[0].executed_statements
            if isinstance(statement, sa.sql.dml.Update)
        )
    )
    assert parameters["state"] == "revoked"
    assert parameters["revoked_at"] == _DATABASE_NOW
    assert parameters["revocation_reason"] == "logout"
    assert parameters["authenticated_at"] is None
    assert parameters["reauthenticated_at"] is None


@pytest.mark.asyncio
async def test_revoke_rejects_an_already_revoked_row() -> None:
    engine = ScriptedEngine([ScriptedResult(rows=(_session_row(state="revoked"),))])
    store = WebSessionStore(engine)
    with pytest.raises(AuthenticationError) as rejected:
        await store.revoke_session(
            RevokeWebSessionCommand(
                session_secret_hash=_PRIOR_SECRET_HASH,
                revocation_reason="logout",
                database_now=_DATABASE_NOW,
            )
        )
    assert rejected.value.error_code is ErrorCode.AUTHENTICATION_REQUIRED
    assert not [
        statement
        for statement in engine.connections[0].executed_statements
        if isinstance(statement, sa.sql.dml.Update)
    ]
