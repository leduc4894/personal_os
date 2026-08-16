"""Device-token store transaction contracts over a scripted engine.

The store unit tests double the async engine with a scripted connection that
records every executed statement and returns programmed rows: the grant
exchange locks the grant by its polling-secret digest, rechecks the
approving user and workspace, then creates exactly one device, family, access
token and refresh token, anchors their identities on the grant behind the
exchanged state matrix and appends the registration and family-creation audit
rows inside one transaction; a pending or expired poll closes with its
registry code before any write. The refresh rotation follows the binding
rotate-predecessor-then-insert-successor order and never extends the family
absolute expiry; confirmed reuse revokes family, tokens and device and writes
exactly one reuse audit row before the closed reuse rejection; access
authentication verifies the presented hash under the row's derivation key and
updates the device last-seen stamp at most once per five minutes. No database
is touched.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.sql.elements import TextClause

from personal_os.authentication.device_tokens import (
    INITIAL_REFRESH_GENERATION,
    AccessTokenAuthenticationCommand,
    ExchangeGrantCommand,
    RefreshRotationCommand,
)
from personal_os.authentication.errors import AuthenticationError
from personal_os.diagnostics.context import DiagnosticContext, create_diagnostic_context
from personal_os.error_contracts.codes import ErrorCode
from postgresql_source_store.device_authorization_store import (
    DEVICE_REGISTERED_AUDIT_ACTION,
    DEVICE_TOKEN_FAMILY_CREATED_AUDIT_ACTION,
    DeviceAuthorizationStore,
)
from postgresql_source_store.device_token_store import (
    DEVICE_TOKEN_REUSE_DETECTED_AUDIT_ACTION,
    DeviceTokenStore,
)

_DATABASE_NOW = datetime(2026, 8, 16, 12, 0, 0, tzinfo=UTC)
_USER_ID = uuid4()
_WORKSPACE_ID = uuid4()
_WEB_SESSION_ID = uuid4()
_GRANT_ID = uuid4()
_DEVICE_ID = uuid4()
_FAMILY_ID = uuid4()
_ACCESS_TOKEN_ID = uuid4()
_REFRESH_TOKEN_ID = uuid4()
_SUCCESSOR_REFRESH_TOKEN_ID = uuid4()
_SUCCESSOR_ACCESS_TOKEN_ID = uuid4()
_PREDECESSOR_SECRET_HASH = "a" * 64
_SUCCESSOR_SECRET_HASH = "b" * 64
_SUCCESSOR_ACCESS_SECRET_HASH = "c" * 64
_POLLING_SECRET_HASH = "d" * 64
_DERIVATION_KEY_ID = "auth-key-current"
_ROTATION_ID = uuid4()

_ACCESS_EXPIRES_AT = _DATABASE_NOW + timedelta(minutes=15)
_REFRESH_INACTIVITY_EXPIRES_AT = _DATABASE_NOW + timedelta(days=30)
_ABSOLUTE_EXPIRES_AT = _DATABASE_NOW + timedelta(days=90)


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
    raw_parameters: dict[str, object] = dict(statement.compile().params)  # type: ignore[attr-defined]
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


def _first_statement_of(
    connection: ScriptedConnection, kind: type, table_name: str
) -> tuple[int, object] | None:
    for position, statement in enumerate(connection.executed_statements):
        if isinstance(statement, kind) and statement.table.name == table_name:  # type: ignore[attr-defined]
            return position, statement
    return None


def _diagnostic_context() -> DiagnosticContext:
    return create_diagnostic_context().context


def _grant_row(
    *,
    state: str = "approved",
    device_id: UUID | None = None,
    token_family_id: UUID | None = None,
    initial_access_token_id: UUID | None = None,
    initial_refresh_token_id: UUID | None = None,
    derivation_key_id: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        grant_id=_GRANT_ID,
        user_code_hash="e" * 64,
        polling_secret_hash=_POLLING_SECRET_HASH,
        client_instance_id=uuid4(),
        claimed_device_id=None,
        device_name="Personal desktop",
        platform_class="obsidian_desktop",
        platform_name="windows",
        plugin_version="1.4.0",
        requested_scope="obsidian_sync",
        state=state,
        created_at=_DATABASE_NOW - timedelta(minutes=5),
        expires_at=_DATABASE_NOW + timedelta(minutes=5),
        approved_at=_DATABASE_NOW - timedelta(minutes=1) if state != "pending" else None,
        denied_at=_DATABASE_NOW if state == "denied" else None,
        exchanged_at=_DATABASE_NOW - timedelta(seconds=30) if state == "exchanged" else None,
        approved_by_user_id=_USER_ID if state != "pending" else None,
        approved_web_session_id=_WEB_SESSION_ID if state != "pending" else None,
        device_id=device_id,
        token_family_id=token_family_id,
        initial_access_token_id=initial_access_token_id,
        initial_refresh_token_id=initial_refresh_token_id,
        derivation_key_id=derivation_key_id,
    )


def _user_row(status: str = "active") -> SimpleNamespace:
    return SimpleNamespace(user_id=_USER_ID, status=status)


def _workspace_row(status: str = "active") -> SimpleNamespace:
    return SimpleNamespace(workspace_id=_WORKSPACE_ID, owner_user_id=_USER_ID, status=status)


def _refresh_token_row(
    *,
    token_id: UUID,
    state: str = "active",
    generation: int = INITIAL_REFRESH_GENERATION,
    secret_hash: str = _PREDECESSOR_SECRET_HASH,
    family_id: UUID = _FAMILY_ID,
    predecessor_token_id: UUID | None = None,
    successor_token_id: UUID | None = None,
    rotation_id: UUID | None = None,
    expires_at: datetime | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        device_token_id=token_id,
        token_family_id=family_id,
        user_id=_USER_ID,
        workspace_id=_WORKSPACE_ID,
        device_id=_DEVICE_ID,
        token_kind="refresh",
        generation=generation,
        secret_hash=secret_hash,
        state=state,
        predecessor_token_id=predecessor_token_id,
        successor_token_id=successor_token_id,
        rotation_id=rotation_id,
        derivation_key_id=_DERIVATION_KEY_ID,
        issued_at=_DATABASE_NOW - timedelta(days=1),
        expires_at=expires_at or _REFRESH_INACTIVITY_EXPIRES_AT,
        rotated_at=_DATABASE_NOW if state == "rotated" else None,
        revoked_at=_DATABASE_NOW if state == "revoked" else None,
    )


def _access_token_row(
    *,
    state: str = "active",
    expires_at: datetime | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        device_token_id=_ACCESS_TOKEN_ID,
        token_family_id=_FAMILY_ID,
        user_id=_USER_ID,
        workspace_id=_WORKSPACE_ID,
        device_id=_DEVICE_ID,
        token_kind="access",
        generation=INITIAL_REFRESH_GENERATION,
        secret_hash=_PREDECESSOR_SECRET_HASH,
        state=state,
        predecessor_token_id=None,
        successor_token_id=None,
        rotation_id=None,
        derivation_key_id=_DERIVATION_KEY_ID,
        issued_at=_DATABASE_NOW - timedelta(minutes=1),
        expires_at=expires_at or _ACCESS_EXPIRES_AT,
        rotated_at=None,
        revoked_at=_DATABASE_NOW if state == "revoked" else None,
    )


def _family_row(
    *,
    state: str = "active",
    current_refresh_generation: int = INITIAL_REFRESH_GENERATION,
    inactivity_expires_at: datetime | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        token_family_id=_FAMILY_ID,
        user_id=_USER_ID,
        workspace_id=_WORKSPACE_ID,
        device_id=_DEVICE_ID,
        state=state,
        current_refresh_generation=current_refresh_generation,
        created_at=_DATABASE_NOW - timedelta(days=1),
        last_refreshed_at=_DATABASE_NOW - timedelta(days=1),
        inactivity_expires_at=inactivity_expires_at or _REFRESH_INACTIVITY_EXPIRES_AT,
        absolute_expires_at=_ABSOLUTE_EXPIRES_AT,
        revoked_at=_DATABASE_NOW if state == "revoked" else None,
        revocation_reason="token_reuse" if state == "revoked" else None,
    )


def _device_row(*, status: str = "active", last_seen_at: datetime | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        device_id=_DEVICE_ID,
        workspace_id=_WORKSPACE_ID,
        user_id=_USER_ID,
        device_name="Personal desktop",
        device_kind="obsidian",
        status=status,
        registered_at=_DATABASE_NOW - timedelta(days=1),
        last_seen_at=last_seen_at,
        revoked_at=_DATABASE_NOW if status == "revoked" else None,
    )


def _exchange_command() -> ExchangeGrantCommand:
    return ExchangeGrantCommand(
        grant_id=_GRANT_ID,
        polling_secret_hash=_POLLING_SECRET_HASH,
        device_id=_DEVICE_ID,
        token_family_id=_FAMILY_ID,
        access_token_id=_ACCESS_TOKEN_ID,
        refresh_token_id=_REFRESH_TOKEN_ID,
        access_secret_hash=_SUCCESSOR_ACCESS_SECRET_HASH,
        refresh_secret_hash=_SUCCESSOR_SECRET_HASH,
        derivation_key_id=_DERIVATION_KEY_ID,
        access_expires_at=_ACCESS_EXPIRES_AT,
        refresh_expires_at=_REFRESH_INACTIVITY_EXPIRES_AT,
        family_absolute_expires_at=_ABSOLUTE_EXPIRES_AT,
        database_now=_DATABASE_NOW,
        diagnostic_context=_diagnostic_context(),
    )


def _refresh_command() -> RefreshRotationCommand:
    return RefreshRotationCommand(
        access_expires_at=_ACCESS_EXPIRES_AT,
        predecessor_token_id=_REFRESH_TOKEN_ID,
        predecessor_secret_hashes_by_key_id={_DERIVATION_KEY_ID: _PREDECESSOR_SECRET_HASH},
        rotation_id=_ROTATION_ID,
        successor_refresh_token_id=_SUCCESSOR_REFRESH_TOKEN_ID,
        successor_access_token_id=_SUCCESSOR_ACCESS_TOKEN_ID,
        successor_refresh_secret_hash=_SUCCESSOR_SECRET_HASH,
        successor_access_secret_hash=_SUCCESSOR_ACCESS_SECRET_HASH,
        derivation_key_id=_DERIVATION_KEY_ID,
        database_now=_DATABASE_NOW,
        diagnostic_context=_diagnostic_context(),
    )


# --- grant exchange ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exchange_creates_device_family_tokens_and_anchors_the_grant() -> None:
    engine = ScriptedEngine(
        [
            ScriptedResult(rows=(_grant_row(state="approved"),)),
            ScriptedResult(rows=(_user_row(),)),
            ScriptedResult(rows=(_workspace_row(),)),
        ]
    )
    store = DeviceAuthorizationStore(engine)  # type: ignore[arg-type]

    provisioned = await store.poll_exchange(_exchange_command())

    connection = engine.connections[0]
    assert provisioned.grant_id == _GRANT_ID
    assert provisioned.device_id == _DEVICE_ID
    assert provisioned.token_family_id == _FAMILY_ID
    assert provisioned.access_token_id == _ACCESS_TOKEN_ID
    assert provisioned.refresh_token_id == _REFRESH_TOKEN_ID
    assert provisioned.refresh_generation == INITIAL_REFRESH_GENERATION
    assert provisioned.access_expires_at == _ACCESS_EXPIRES_AT
    assert provisioned.refresh_expires_at == _REFRESH_INACTIVITY_EXPIRES_AT

    device_insert = _statements_of(connection, sa.Insert, "devices")
    assert len(device_insert) == 1
    device_values = _statement_parameters(device_insert[0])
    assert device_values["device_id"] == _DEVICE_ID
    assert device_values["device_kind"] == "obsidian"
    assert device_values["status"] == "active"
    assert device_values["registered_at"] == _DATABASE_NOW

    family_inserts = _statements_of(connection, sa.Insert, "device_token_families")
    assert len(family_inserts) == 1
    family_values = _statement_parameters(family_inserts[0])
    assert family_values["state"] == "active"
    assert family_values["current_refresh_generation"] == INITIAL_REFRESH_GENERATION
    assert family_values["inactivity_expires_at"] == _REFRESH_INACTIVITY_EXPIRES_AT
    assert family_values["absolute_expires_at"] == _ABSOLUTE_EXPIRES_AT

    token_inserts = _statements_of(connection, sa.Insert, "device_tokens")
    assert len(token_inserts) == 2
    kinds = {values["token_kind"] for values in map(_statement_parameters, token_inserts)}
    assert kinds == {"access", "refresh"}
    refresh_insert_values = next(
        values
        for values in map(_statement_parameters, token_inserts)
        if values["token_kind"] == "refresh"
    )
    assert refresh_insert_values["state"] == "active"
    assert refresh_insert_values.get("predecessor_token_id") is None
    assert refresh_insert_values["derivation_key_id"] == _DERIVATION_KEY_ID

    grant_updates = _statements_of(connection, sa.Update, "device_authorization_grants")
    assert len(grant_updates) == 1
    grant_values = _statement_parameters(grant_updates[0])
    assert grant_values["state"] == "exchanged"
    assert grant_values["device_id"] == _DEVICE_ID
    assert grant_values["token_family_id"] == _FAMILY_ID
    assert grant_values["initial_access_token_id"] == _ACCESS_TOKEN_ID
    assert grant_values["initial_refresh_token_id"] == _REFRESH_TOKEN_ID
    assert grant_values["derivation_key_id"] == _DERIVATION_KEY_ID
    assert grant_values["exchanged_at"] == _DATABASE_NOW

    audit_inserts = _statements_of(connection, sa.Insert, "audit_events")
    assert [values["action"] for values in map(_statement_parameters, audit_inserts)] == [
        DEVICE_REGISTERED_AUDIT_ACTION,
        DEVICE_TOKEN_FAMILY_CREATED_AUDIT_ACTION,
    ]


@pytest.mark.asyncio
async def test_pending_and_denied_polls_close_before_any_write() -> None:
    for state, expected_code in (
        ("pending", ErrorCode.DEVICE_AUTHORIZATION_PENDING),
        ("denied", ErrorCode.DEVICE_AUTHORIZATION_DENIED),
    ):
        engine = ScriptedEngine([ScriptedResult(rows=(_grant_row(state=state),))])
        store = DeviceAuthorizationStore(engine)  # type: ignore[arg-type]
        with pytest.raises(AuthenticationError) as raised:
            await store.poll_exchange(_exchange_command())
        assert raised.value.error_code is expected_code
        connection = engine.connections[0]
        assert _statements_of(connection, sa.Insert, "devices") == []
        assert _statements_of(connection, sa.Update, "device_authorization_grants") == []


@pytest.mark.asyncio
async def test_expired_pending_poll_closes_with_the_expired_code() -> None:
    expired_row = _grant_row(state="pending")
    expired_row.expires_at = _DATABASE_NOW - timedelta(seconds=1)
    engine = ScriptedEngine([ScriptedResult(rows=(expired_row,))])
    store = DeviceAuthorizationStore(engine)  # type: ignore[arg-type]
    with pytest.raises(AuthenticationError) as raised:
        await store.poll_exchange(_exchange_command())
    assert raised.value.error_code is ErrorCode.DEVICE_AUTHORIZATION_EXPIRED


@pytest.mark.asyncio
async def test_expired_approved_grant_refuses_the_first_exchange() -> None:
    expired_row = _grant_row(state="approved")
    expired_row.expires_at = _DATABASE_NOW - timedelta(seconds=1)
    engine = ScriptedEngine([ScriptedResult(rows=(expired_row,))])
    store = DeviceAuthorizationStore(engine)  # type: ignore[arg-type]
    with pytest.raises(AuthenticationError) as raised:
        await store.poll_exchange(_exchange_command())
    assert raised.value.error_code is ErrorCode.DEVICE_AUTHORIZATION_EXPIRED
    # The closed expiry precedes every write: no recheck, no rows, no audit,
    # and the grant keeps its approved state with unset anchors.
    connection = engine.connections[0]
    assert _statements_of(connection, sa.Insert, "devices") == []
    assert _statements_of(connection, sa.Insert, "device_token_families") == []
    assert _statements_of(connection, sa.Insert, "device_tokens") == []
    assert _statements_of(connection, sa.Insert, "audit_events") == []
    assert _statements_of(connection, sa.Update, "device_authorization_grants") == []


@pytest.mark.asyncio
async def test_unknown_polling_secret_fails_closed() -> None:
    engine = ScriptedEngine([ScriptedResult()])
    store = DeviceAuthorizationStore(engine)  # type: ignore[arg-type]
    with pytest.raises(AuthenticationError) as raised:
        await store.poll_exchange(_exchange_command())
    assert raised.value.error_code is ErrorCode.DEVICE_CREDENTIAL_INVALID


@pytest.mark.asyncio
async def test_exchanged_grant_replays_only_while_generation_one_is_current() -> None:
    engine = ScriptedEngine(
        [
            ScriptedResult(
                rows=(
                    _grant_row(
                        state="exchanged",
                        device_id=_DEVICE_ID,
                        token_family_id=_FAMILY_ID,
                        initial_access_token_id=_ACCESS_TOKEN_ID,
                        initial_refresh_token_id=_REFRESH_TOKEN_ID,
                        derivation_key_id=_DERIVATION_KEY_ID,
                    ),
                )
            ),
            ScriptedResult(rows=(_refresh_token_row(token_id=_REFRESH_TOKEN_ID, generation=1),)),
            ScriptedResult(rows=(_family_row(current_refresh_generation=1),)),
            # The anchored access token row carries the original timestamps.
            ScriptedResult(rows=(_access_token_row(),)),
        ]
    )
    store = DeviceAuthorizationStore(engine)  # type: ignore[arg-type]

    provisioned = await store.poll_exchange(_exchange_command())

    connection = engine.connections[0]
    assert provisioned.access_token_id == _ACCESS_TOKEN_ID
    assert provisioned.refresh_token_id == _REFRESH_TOKEN_ID
    assert provisioned.access_expires_at == _ACCESS_EXPIRES_AT
    assert provisioned.refresh_expires_at == _REFRESH_INACTIVITY_EXPIRES_AT
    assert provisioned.derivation_key_id == _DERIVATION_KEY_ID
    assert _statements_of(connection, sa.Insert, "devices") == []
    assert _statements_of(connection, sa.Insert, "device_tokens") == []
    assert _statements_of(connection, sa.Insert, "audit_events") == []


@pytest.mark.asyncio
async def test_exchanged_grant_after_rotation_is_terminally_consumed() -> None:
    engine = ScriptedEngine(
        [
            ScriptedResult(
                rows=(
                    _grant_row(
                        state="exchanged",
                        device_id=_DEVICE_ID,
                        token_family_id=_FAMILY_ID,
                        initial_access_token_id=_ACCESS_TOKEN_ID,
                        initial_refresh_token_id=_REFRESH_TOKEN_ID,
                        derivation_key_id=_DERIVATION_KEY_ID,
                    ),
                )
            ),
            ScriptedResult(rows=(_refresh_token_row(token_id=_REFRESH_TOKEN_ID, state="rotated"),)),
            ScriptedResult(rows=(_family_row(current_refresh_generation=2),)),
        ]
    )
    store = DeviceAuthorizationStore(engine)  # type: ignore[arg-type]
    with pytest.raises(AuthenticationError) as raised:
        await store.poll_exchange(_exchange_command())
    assert raised.value.error_code is ErrorCode.DEVICE_AUTHORIZATION_STATE_INVALID


# --- refresh rotation -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_rotates_predecessor_before_inserting_the_successor() -> None:
    engine = ScriptedEngine(
        [
            ScriptedResult(rows=(_refresh_token_row(token_id=_REFRESH_TOKEN_ID, generation=1),)),
            ScriptedResult(rows=(_family_row(current_refresh_generation=1),)),
            ScriptedResult(rowcount=1),
            ScriptedResult(),
            ScriptedResult(),
            ScriptedResult(rowcount=1),
            ScriptedResult(rowcount=1),
        ]
    )
    store = DeviceTokenStore(engine)  # type: ignore[arg-type]

    rotation = await store.refresh_rotation(_refresh_command())

    connection = engine.connections[0]
    assert rotation.successor_refresh_token_id == _SUCCESSOR_REFRESH_TOKEN_ID
    assert rotation.successor_access_token_id == _SUCCESSOR_ACCESS_TOKEN_ID
    assert rotation.successor_generation == 2
    assert rotation.access_expires_at == _ACCESS_EXPIRES_AT
    assert rotation.refresh_expires_at == _REFRESH_INACTIVITY_EXPIRES_AT
    assert rotation.family_absolute_expires_at == _ABSOLUTE_EXPIRES_AT

    predecessor_rotation = _first_statement_of(connection, sa.Update, "device_tokens")
    successor_insert = _first_statement_of(connection, sa.Insert, "device_tokens")
    assert predecessor_rotation is not None and successor_insert is not None
    assert predecessor_rotation[0] < successor_insert[0]
    rotation_values = _statement_parameters(predecessor_rotation[1])
    assert rotation_values["state"] == "rotated"
    # The predecessor leaves the active state before the successors exist;
    # its nullable successor link lands only after the inserts.
    assert rotation_values.get("successor_token_id") is None
    token_updates = _statements_of(connection, sa.Update, "device_tokens")
    assert len(token_updates) == 2
    successor_link = _statement_parameters(token_updates[1])
    assert successor_link["successor_token_id"] == _SUCCESSOR_REFRESH_TOKEN_ID
    assert connection.executed_statements.index(token_updates[1]) > successor_insert[0]

    token_inserts = _statements_of(connection, sa.Insert, "device_tokens")
    assert len(token_inserts) == 2
    inserted_values = [
        parameters["device_token_id"] for parameters in map(_statement_parameters, token_inserts)
    ]
    assert set(inserted_values) == {_SUCCESSOR_REFRESH_TOKEN_ID, _SUCCESSOR_ACCESS_TOKEN_ID}
    successor_refresh_values = next(
        values
        for values in map(_statement_parameters, token_inserts)
        if values["device_token_id"] == _SUCCESSOR_REFRESH_TOKEN_ID
    )
    assert successor_refresh_values["generation"] == 2
    assert successor_refresh_values["predecessor_token_id"] == _REFRESH_TOKEN_ID
    assert successor_refresh_values["rotation_id"] == _ROTATION_ID

    family_updates = _statements_of(connection, sa.Update, "device_token_families")
    assert len(family_updates) == 1
    family_values = _statement_parameters(family_updates[0])
    assert family_values["current_refresh_generation"] == 2
    assert family_values["inactivity_expires_at"] == _REFRESH_INACTIVITY_EXPIRES_AT
    # The absolute expiry is never rewritten by a rotation.
    assert "absolute_expires_at" not in family_values
    assert _statements_of(connection, sa.Insert, "audit_events") == []


@pytest.mark.asyncio
async def test_refresh_replay_returns_the_anchored_successor_without_new_rows() -> None:
    anchored_issued_at = _DATABASE_NOW - timedelta(minutes=3)
    successor_row = _refresh_token_row(
        token_id=_SUCCESSOR_REFRESH_TOKEN_ID,
        generation=2,
        predecessor_token_id=_REFRESH_TOKEN_ID,
        rotation_id=_ROTATION_ID,
    )
    successor_row.issued_at = anchored_issued_at
    successor_row.expires_at = anchored_issued_at + timedelta(days=30)
    predecessor_row = _refresh_token_row(
        token_id=_REFRESH_TOKEN_ID,
        state="rotated",
        generation=1,
        successor_token_id=_SUCCESSOR_REFRESH_TOKEN_ID,
    )
    engine = ScriptedEngine(
        [
            ScriptedResult(rows=(predecessor_row,)),
            ScriptedResult(rows=(_family_row(current_refresh_generation=2),)),
            ScriptedResult(rows=(successor_row,)),
            ScriptedResult(
                rows=(
                    SimpleNamespace(
                        device_token_id=_SUCCESSOR_ACCESS_TOKEN_ID,
                        expires_at=_ACCESS_EXPIRES_AT,
                    ),
                )
            ),
        ]
    )
    store = DeviceTokenStore(engine)  # type: ignore[arg-type]

    rotation = await store.refresh_rotation(_refresh_command())

    connection = engine.connections[0]
    assert rotation.successor_generation == 2
    assert rotation.successor_refresh_token_id == _SUCCESSOR_REFRESH_TOKEN_ID
    assert rotation.access_expires_at == _ACCESS_EXPIRES_AT
    assert rotation.refresh_expires_at == successor_row.expires_at
    assert rotation.rotated_at == predecessor_row.rotated_at
    assert _statements_of(connection, sa.Insert, "device_tokens") == []
    assert _statements_of(connection, sa.Update, "device_token_families") == []


@pytest.mark.asyncio
async def test_confirmed_reuse_revokes_family_tokens_device_and_audits() -> None:
    predecessor_row = _refresh_token_row(
        token_id=_REFRESH_TOKEN_ID,
        state="rotated",
        generation=1,
        successor_token_id=_SUCCESSOR_REFRESH_TOKEN_ID,
    )
    successor_row = _refresh_token_row(
        token_id=_SUCCESSOR_REFRESH_TOKEN_ID,
        generation=2,
        predecessor_token_id=_REFRESH_TOKEN_ID,
        rotation_id=uuid4(),
    )
    engine = ScriptedEngine(
        [
            ScriptedResult(rows=(predecessor_row,)),
            ScriptedResult(rows=(_family_row(current_refresh_generation=2),)),
            ScriptedResult(rows=(successor_row,)),
            ScriptedResult(rowcount=1),
            ScriptedResult(rowcount=2),
            ScriptedResult(rowcount=1),
        ]
    )
    store = DeviceTokenStore(engine)  # type: ignore[arg-type]

    with pytest.raises(AuthenticationError) as raised:
        await store.refresh_rotation(_refresh_command())
    assert raised.value.error_code is ErrorCode.DEVICE_TOKEN_REUSE_DETECTED

    connection = engine.connections[0]
    family_updates = _statements_of(connection, sa.Update, "device_token_families")
    assert len(family_updates) == 1
    family_values = _statement_parameters(family_updates[0])
    assert family_values["state"] == "revoked"
    token_updates = _statements_of(connection, sa.Update, "device_tokens")
    assert len(token_updates) == 1
    token_update_values = _statement_parameters(token_updates[0])
    assert token_update_values["state"] == "revoked"
    device_updates = _statements_of(connection, sa.Update, "devices")
    assert len(device_updates) == 1
    device_update_values = _statement_parameters(device_updates[0])
    assert device_update_values["status"] == "revoked"
    audit_inserts = _statements_of(connection, sa.Insert, "audit_events")
    assert [values["action"] for values in map(_statement_parameters, audit_inserts)] == [
        DEVICE_TOKEN_REUSE_DETECTED_AUDIT_ACTION
    ]


@pytest.mark.asyncio
async def test_refresh_rejects_unknown_and_hash_mismatched_credentials() -> None:
    engine = ScriptedEngine([ScriptedResult()])
    store = DeviceTokenStore(engine)  # type: ignore[arg-type]
    with pytest.raises(AuthenticationError) as raised:
        await store.refresh_rotation(_refresh_command())
    assert raised.value.error_code is ErrorCode.DEVICE_CREDENTIAL_INVALID

    mismatched_row = _refresh_token_row(token_id=_REFRESH_TOKEN_ID, secret_hash="f" * 64)
    mismatch_engine = ScriptedEngine(
        [ScriptedResult(rows=(mismatched_row,)), ScriptedResult(rows=(_family_row(),))]
    )
    mismatch_store = DeviceTokenStore(mismatch_engine)  # type: ignore[arg-type]
    with pytest.raises(AuthenticationError) as mismatch:
        await mismatch_store.refresh_rotation(_refresh_command())
    assert mismatch.value.error_code is ErrorCode.DEVICE_CREDENTIAL_INVALID


# --- access authentication -------------------------------------------------------------


def _access_command() -> AccessTokenAuthenticationCommand:
    return AccessTokenAuthenticationCommand(
        token_id=_ACCESS_TOKEN_ID,
        secret_hashes_by_key_id={_DERIVATION_KEY_ID: _PREDECESSOR_SECRET_HASH},
        database_now=_DATABASE_NOW,
    )


@pytest.mark.asyncio
async def test_access_authentication_checks_every_state_and_updates_last_seen() -> None:
    stale_device = _device_row(last_seen_at=_DATABASE_NOW - timedelta(minutes=6))
    engine = ScriptedEngine(
        [
            ScriptedResult(rows=(_access_token_row(),)),
            ScriptedResult(rows=(_family_row(),)),
            ScriptedResult(rows=(stale_device,)),
            ScriptedResult(rows=(_user_row(),)),
            ScriptedResult(rows=(_workspace_row(),)),
            ScriptedResult(rowcount=1),
        ]
    )
    store = DeviceTokenStore(engine)  # type: ignore[arg-type]

    authenticated = await store.authenticate_access_token(_access_command())

    connection = engine.connections[0]
    assert authenticated.context.user_id == _USER_ID
    assert authenticated.context.workspace_id == _WORKSPACE_ID
    assert authenticated.context.device_id == _DEVICE_ID
    assert authenticated.context.scope.value == "obsidian_sync"
    device_updates = _statements_of(connection, sa.Update, "devices")
    assert len(device_updates) == 1


@pytest.mark.asyncio
async def test_access_authentication_skips_last_seen_within_five_minutes() -> None:
    fresh_device = _device_row(last_seen_at=_DATABASE_NOW - timedelta(minutes=2))
    engine = ScriptedEngine(
        [
            ScriptedResult(rows=(_access_token_row(),)),
            ScriptedResult(rows=(_family_row(),)),
            ScriptedResult(rows=(fresh_device,)),
            ScriptedResult(rows=(_user_row(),)),
            ScriptedResult(rows=(_workspace_row(),)),
        ]
    )
    store = DeviceTokenStore(engine)  # type: ignore[arg-type]

    await store.authenticate_access_token(_access_command())

    connection = engine.connections[0]
    assert _statements_of(connection, sa.Update, "devices") == []


@pytest.mark.asyncio
async def test_access_authentication_closes_on_expired_revoked_and_mismatched_states() -> None:
    expired_row = _access_token_row(expires_at=_DATABASE_NOW - timedelta(seconds=1))
    engine = ScriptedEngine([ScriptedResult(rows=(expired_row,))])
    store = DeviceTokenStore(engine)  # type: ignore[arg-type]
    with pytest.raises(AuthenticationError) as raised:
        await store.authenticate_access_token(_access_command())
    assert raised.value.error_code is ErrorCode.DEVICE_CREDENTIAL_INVALID

    revoked_row = _access_token_row(state="revoked")
    revoked_engine = ScriptedEngine([ScriptedResult(rows=(revoked_row,))])
    revoked_store = DeviceTokenStore(revoked_engine)  # type: ignore[arg-type]
    with pytest.raises(AuthenticationError) as revoked:
        await revoked_store.authenticate_access_token(_access_command())
    assert revoked.value.error_code is ErrorCode.DEVICE_REVOKED

    mismatch_row = _access_token_row()
    mismatch_row.secret_hash = "9" * 64
    mismatch_engine = ScriptedEngine([ScriptedResult(rows=(mismatch_row,))])
    mismatch_store = DeviceTokenStore(mismatch_engine)  # type: ignore[arg-type]
    with pytest.raises(AuthenticationError) as mismatch:
        await mismatch_store.authenticate_access_token(_access_command())
    assert mismatch.value.error_code is ErrorCode.DEVICE_CREDENTIAL_INVALID

    revoked_family_engine = ScriptedEngine(
        [
            ScriptedResult(rows=(_access_token_row(),)),
            ScriptedResult(rows=(_family_row(state="revoked"),)),
        ]
    )
    revoked_family_store = DeviceTokenStore(revoked_family_engine)  # type: ignore[arg-type]
    with pytest.raises(AuthenticationError) as family_rejected:
        await revoked_family_store.authenticate_access_token(_access_command())
    assert family_rejected.value.error_code is ErrorCode.DEVICE_REVOKED
