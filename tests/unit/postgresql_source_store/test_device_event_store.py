"""Device event store hydration, statement scope and cursor-fencing seams.

These tests pin the pure pieces of the PostgreSQL device event adapter
without a database: every statement is parameter-bound and credential-scoped
(no literal workspace, device, run or watermark value ever appears in
compiled SQL), the pull page reads one bounded window between the delivered
cursor and a frozen statement checkpoint, the six canonical event types
hydrate their exact operation-shaped operands from the joined lifecycle
rows, a missing retained predecessor operand is the closed cursor gap, an
impossible hydrated shape is the closed integrity failure (never a skip),
and the domain retry policy passes typed errors through while mapping every
driver failure onto the closed device sync boundary without leaking driver
text. Durable transaction behavior is integration territory (disposable
stack).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
import sqlalchemy.exc as sa_exc
from sqlalchemy.dialects import postgresql

from personal_os.device_sync.contracts import (
    _EVENT_TYPES_WITH_PRIOR_LOCATOR,
    _EVENT_TYPES_WITH_RESULTING_LOCATOR,
    _EVENT_TYPES_WITH_TOMBSTONE,
    MAX_PULL_EVENTS,
    DeviceEventType,
)
from personal_os.device_sync.errors import DeviceSyncError, DeviceSyncErrorCode
from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import InternalApplicationError
from postgresql_source_store.device_event_store import (
    DEVICE_STATUS_ACTIVE,
    DeviceSyncDatabaseRetryPolicy,
    classify_cursor_gap,
    device_acknowledged_advance_statement,
    device_cursor_bootstrap_insert_statement,
    device_cursor_select_statement,
    device_delivered_watermark_advance_statement,
    device_event_checkpoint_statement,
    device_pull_page_statement,
    hydrate_device_event,
    manifest_action_page_statement,
    map_device_sync_database_failure,
    validate_pull_limit,
    workspace_minimum_acknowledged_statement,
)

_WORKSPACE_ID = UUID("018f47a0-7b00-7000-8000-000000000001")
_DEVICE_ID = UUID("018f47a0-7b00-7000-8000-000000000002")
_SOURCE_ID = UUID("018f47a0-7b00-7000-8000-000000000004")
_EVENT_ID = UUID("018f47a0-7b00-7000-8000-000000000005")
_BASE_VERSION_ID = UUID("018f47a0-7b00-7000-8000-000000000006")
_CURRENT_VERSION_ID = UUID("018f47a0-7b00-7000-8000-000000000007")
_TOMBSTONE_ID = UUID("018f47a0-7b00-7000-8000-000000000008")
_MANIFEST_RUN_ID = UUID("018f47a0-7b00-7000-8000-000000000009")

_NOW = datetime(2026, 8, 26, 4, 5, 6, tzinfo=UTC)
_BASE_SHA256 = "a" * 64
_CURRENT_SHA256 = "b" * 64

_SENTINEL_DRIVER_TEXT = "driver-sentinel-text"
_SENTINEL_STATEMENT = "SELECT sentinel"


# --- hydration fixtures ------------------------------------------------------


def _fingerprint_columns(prefix: str) -> dict[str, Any]:
    sha256 = _BASE_SHA256 if prefix == "base" else _CURRENT_SHA256
    return {
        f"{prefix}_sha256": sha256,
        f"{prefix}_size_bytes": 128,
        f"{prefix}_media_type": "text/markdown",
    }


def _base_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "event_id": _EVENT_ID,
        "event_sequence": 9,
        "event_type": "created",
        "source_id": _SOURCE_ID,
        "origin_device_id": _DEVICE_ID,
        "base_version_id": None,
        "current_version_id": _CURRENT_VERSION_ID,
        "committed_at": _NOW,
        "resulting_locator": None,
        "prior_locator": None,
        "delete_tombstone_id": None,
        "restore_tombstone_id": None,
        **_fingerprint_columns("current"),
        "base_sha256": None,
        "base_size_bytes": None,
        "base_media_type": None,
    }
    row.update(overrides)
    return row


_DATABASE_TOKEN_BY_EVENT_TYPE: dict[DeviceEventType, str] = {
    DeviceEventType.CREATED: "create",
    DeviceEventType.UPDATED: "update",
    DeviceEventType.RENAMED: "rename",
    DeviceEventType.MOVED: "move",
    DeviceEventType.DELETED: "delete",
    DeviceEventType.RESTORED: "restore",
}


def row_for(event_type: DeviceEventType) -> dict[str, Any]:
    """Build one fully hydrated lifecycle row for one canonical event type.

    The row carries the canonical ``sync_events.event_type`` database token
    (``create``/``update``/``rename``/``move``/``delete``/``restore``), not
    the domain enum value, exactly as the joined pull page returns it.
    """

    token = _DATABASE_TOKEN_BY_EVENT_TYPE[event_type]
    if event_type is DeviceEventType.CREATED:
        return _base_row(
            event_type=token,
            resulting_locator="notes/created.md",
        )
    if event_type is DeviceEventType.UPDATED:
        return _base_row(
            event_type=token,
            base_version_id=_BASE_VERSION_ID,
            **_fingerprint_columns("base"),
            resulting_locator="notes/active.md",
        )
    if event_type in {DeviceEventType.RENAMED, DeviceEventType.MOVED}:
        return _base_row(
            event_type=token,
            base_version_id=_BASE_VERSION_ID,
            **_fingerprint_columns("base"),
            prior_locator="notes/old.md",
            resulting_locator="notes/new.md",
        )
    if event_type is DeviceEventType.DELETED:
        return _base_row(
            event_type=token,
            base_version_id=_BASE_VERSION_ID,
            **_fingerprint_columns("base"),
            prior_locator="notes/gone.md",
            delete_tombstone_id=_TOMBSTONE_ID,
        )
    if event_type is DeviceEventType.RESTORED:
        return _base_row(
            event_type=token,
            base_version_id=_BASE_VERSION_ID,
            **_fingerprint_columns("base"),
            resulting_locator="notes/back.md",
            restore_tombstone_id=_TOMBSTONE_ID,
        )
    raise AssertionError(f"unknown canonical event type under test: {event_type}")


def assert_event_shape(hydrated: Any) -> None:
    """Assert the operation-shaped operand contract of one hydrated event."""

    if hydrated.event_type is DeviceEventType.UPDATED:
        # The pull wire always carries the update's content target: the
        # locator active at the event's own sequence (never the prior
        # locator — an update changes no locator).
        assert hydrated.resulting_locator is not None
        assert hydrated.prior_locator is None
    elif hydrated.event_type in _EVENT_TYPES_WITH_RESULTING_LOCATOR:
        assert hydrated.resulting_locator is not None
    else:
        assert hydrated.resulting_locator is None
    if hydrated.event_type in _EVENT_TYPES_WITH_PRIOR_LOCATOR:
        assert hydrated.prior_locator is not None
    else:
        assert hydrated.prior_locator is None
    if hydrated.event_type in _EVENT_TYPES_WITH_TOMBSTONE:
        assert hydrated.tombstone_id is not None
    else:
        assert hydrated.tombstone_id is None


@pytest.mark.parametrize("event_type", tuple(DeviceEventType))
def test_hydrates_operation_shaped_event(event_type: DeviceEventType) -> None:
    hydrated = hydrate_device_event(row_for(event_type))
    assert hydrated.event_type is event_type
    assert_event_shape(hydrated)
    assert hydrated.event_id == _EVENT_ID
    assert hydrated.event_sequence == 9
    assert hydrated.source_id == _SOURCE_ID
    assert hydrated.origin_device_id == _DEVICE_ID
    assert hydrated.committed_at == _NOW
    if event_type in {DeviceEventType.DELETED, DeviceEventType.RESTORED}:
        assert hydrated.tombstone_id == _TOMBSTONE_ID
    if event_type is DeviceEventType.UPDATED:
        assert hydrated.base_version_id == _BASE_VERSION_ID
        assert hydrated.current_version_id == _CURRENT_VERSION_ID
        assert hydrated.base_fingerprint is not None
        assert hydrated.current_fingerprint is not None
        assert hydrated.resulting_locator is not None
        assert hydrated.resulting_locator.value == "notes/active.md"


def test_update_hydrates_the_active_locator_not_a_prior_locator() -> None:
    """The update's resulting locator is its content target (spec 7.1).

    An update changes no locator, so the prior operand stays null and the
    resulting operand carries the source's locator active at the event's
    own sequence — the operand the Task 10 applier stages the replacement
    at.
    """

    hydrated = hydrate_device_event(row_for(DeviceEventType.UPDATED))
    assert hydrated.resulting_locator is not None
    assert hydrated.resulting_locator.value == "notes/active.md"
    assert hydrated.prior_locator is None


def test_update_without_a_resolvable_active_locator_fails_integrity() -> None:
    """An update whose active locator cannot be resolved is never null-passed.

    The lifecycle invariant guarantees every live source holds a locator
    open, so a locator-less update row is an impossible hydrated shape:
    the closed integrity failure, never a silent skip or a null operand
    that would reject every remote content edit downstream.
    """

    with pytest.raises(DeviceSyncError) as raised:
        hydrate_device_event(row_for(DeviceEventType.UPDATED) | {"resulting_locator": None})
    assert raised.value.code is DeviceSyncErrorCode.EVENT_INTEGRITY_FAILED


def test_hydrated_events_never_leak_private_operands_in_repr() -> None:
    hydrated = hydrate_device_event(row_for(DeviceEventType.RENAMED))
    rendered = repr(hydrated)
    assert "notes/old.md" not in rendered
    assert "notes/new.md" not in rendered
    assert _BASE_SHA256 not in rendered
    assert "<redacted>" in rendered


def test_hydration_carries_the_origin_device_from_the_canonical_row() -> None:
    hydrated = hydrate_device_event(row_for(DeviceEventType.CREATED) | {"origin_device_id": None})
    assert hydrated.origin_device_id is None


def test_unknown_event_type_token_fails_integrity() -> None:
    with pytest.raises(DeviceSyncError) as raised:
        hydrate_device_event(row_for(DeviceEventType.CREATED) | {"event_type": "purge"})
    assert raised.value.code is DeviceSyncErrorCode.EVENT_INTEGRITY_FAILED


def test_database_event_type_mapping_is_closed_over_the_migration_vocabulary() -> None:
    from postgresql_source_store.device_event_store import EVENT_TYPE_BY_DATABASE_TOKEN

    assert set(EVENT_TYPE_BY_DATABASE_TOKEN) == {
        "create",
        "update",
        "rename",
        "move",
        "delete",
        "restore",
    }
    assert set(EVENT_TYPE_BY_DATABASE_TOKEN.values()) == set(DeviceEventType)


def test_naive_committed_at_fails_integrity() -> None:
    with pytest.raises(DeviceSyncError) as raised:
        hydrate_device_event(
            row_for(DeviceEventType.CREATED) | {"committed_at": _NOW.replace(tzinfo=None)}
        )
    assert raised.value.code is DeviceSyncErrorCode.EVENT_INTEGRITY_FAILED


def test_missing_base_version_predecessor_raises_cursor_gap() -> None:
    row = row_for(DeviceEventType.UPDATED)
    row["base_sha256"] = None
    row["base_size_bytes"] = None
    row["base_media_type"] = None
    with pytest.raises(DeviceSyncError) as raised:
        hydrate_device_event(row)
    assert raised.value.code is DeviceSyncErrorCode.CURSOR_GAP


def test_missing_current_version_operand_raises_integrity() -> None:
    row = row_for(DeviceEventType.CREATED)
    row["current_sha256"] = None
    row["current_size_bytes"] = None
    row["current_media_type"] = None
    with pytest.raises(DeviceSyncError) as raised:
        hydrate_device_event(row)
    assert raised.value.code is DeviceSyncErrorCode.EVENT_INTEGRITY_FAILED


def test_missing_required_resulting_locator_operand_raises_integrity() -> None:
    with pytest.raises(DeviceSyncError) as raised:
        hydrate_device_event(row_for(DeviceEventType.CREATED) | {"resulting_locator": None})
    assert raised.value.code is DeviceSyncErrorCode.EVENT_INTEGRITY_FAILED


def test_missing_required_prior_locator_operand_raises_integrity() -> None:
    with pytest.raises(DeviceSyncError) as raised:
        hydrate_device_event(row_for(DeviceEventType.RENAMED) | {"prior_locator": None})
    assert raised.value.code is DeviceSyncErrorCode.EVENT_INTEGRITY_FAILED


def test_missing_tombstone_operand_raises_integrity() -> None:
    with pytest.raises(DeviceSyncError) as raised:
        hydrate_device_event(
            row_for(DeviceEventType.DELETED)
            | {"delete_tombstone_id": None, "restore_tombstone_id": None}
        )
    assert raised.value.code is DeviceSyncErrorCode.EVENT_INTEGRITY_FAILED


def test_fingerprint_operand_with_invalid_digest_fails_integrity() -> None:
    with pytest.raises(DeviceSyncError) as raised:
        hydrate_device_event(row_for(DeviceEventType.CREATED) | {"current_sha256": "not-a-digest"})
    assert raised.value.code is DeviceSyncErrorCode.EVENT_INTEGRITY_FAILED


# --- cursor gap classification -----------------------------------------------


def test_cursor_gap_requires_history_below_the_cursor_above_the_floor() -> None:
    assert classify_cursor_gap(
        delivered_through_sequence=9, checkpoint_sequence=5, floor_sequence=0
    )
    assert classify_cursor_gap(
        delivered_through_sequence=9, checkpoint_sequence=None, floor_sequence=0
    )


def test_cursor_gap_never_fires_for_the_floor_owning_device() -> None:
    assert not classify_cursor_gap(
        delivered_through_sequence=9, checkpoint_sequence=5, floor_sequence=9
    )
    assert not classify_cursor_gap(
        delivered_through_sequence=9, checkpoint_sequence=None, floor_sequence=11
    )


def test_cursor_gap_never_fires_when_history_reaches_the_watermark() -> None:
    assert not classify_cursor_gap(
        delivered_through_sequence=9, checkpoint_sequence=9, floor_sequence=0
    )
    assert not classify_cursor_gap(
        delivered_through_sequence=9, checkpoint_sequence=12, floor_sequence=3
    )
    assert not classify_cursor_gap(
        delivered_through_sequence=0, checkpoint_sequence=None, floor_sequence=0
    )


# --- pull limit bound ---------------------------------------------------------


def test_pull_limit_accepts_exactly_the_bounded_window() -> None:
    validate_pull_limit(1)
    validate_pull_limit(MAX_PULL_EVENTS)


@pytest.mark.parametrize("limit", (0, -1, MAX_PULL_EVENTS + 1))
def test_pull_limit_rejects_out_of_bounds_windows(limit: int) -> None:
    with pytest.raises(ValueError, match="limit"):
        validate_pull_limit(limit)


# --- statement parameterization, scope and bounds -----------------------------


def _bind_marker(text: str, parameter: str) -> bool:
    """Check whether a parameter-bound marker is in the SQL text.

    SQLAlchemy names duplicate columns with a numeric suffix
    (``%(workspace_id_1)s``), so we accept either the bare or suffixed form.
    """

    if f"%({parameter})s" in text:
        return True
    return any(
        marker in text
        for marker in (
            f"%({parameter}_1)s",
            f"%({parameter}_2)s",
            f"%({parameter}_3)s",
        )
    )


def test_page_statement_is_credential_scoped_bounded_and_ordered() -> None:
    statement = device_pull_page_statement(
        _WORKSPACE_ID,
        after_sequence=3,
        through_sequence=9,
        limit=MAX_PULL_EVENTS + 1,
    )
    compiled = statement.compile(dialect=postgresql.dialect())
    text = str(compiled)
    assert _bind_marker(text, "workspace_id")
    assert _bind_marker(text, "after_sequence")
    assert _bind_marker(text, "through_sequence")
    assert _bind_marker(text, "pull_limit")
    assert "LIMIT %(pull_limit)s" in text
    assert "knowledge.sync_events.event_sequence ASC" in text
    assert "knowledge.sync_events.event_sequence >" in text
    assert "knowledge.sync_events.event_sequence <=" in text
    # Hydration operands come from the canonical lifecycle rows.
    for alias in (
        "opened_locator",
        "closed_locator",
        "delete_tombstone",
        "restore_tombstone",
        "current_version",
        "base_version",
        "current_object",
        "base_object",
        "active_locator",
    ):
        assert f"AS {alias}" in text, f"missing hydration join alias {alias}"
    # An update's resulting locator hydrates from the locator open at the
    # event's own sequence, gated on the update token so every other event
    # type keeps its opened-locator operand untouched.
    assert "CASE WHEN (knowledge.sync_events.event_type = 'update')" in text
    assert "LATERAL" in text
    # No literal credential-scope value appears in the compiled SQL.
    assert str(_WORKSPACE_ID) not in text


def test_checkpoint_statement_reads_one_descending_head_row() -> None:
    statement = device_event_checkpoint_statement(_WORKSPACE_ID)
    compiled = statement.compile(dialect=postgresql.dialect())
    text = str(compiled)
    assert _bind_marker(text, "workspace_id")
    assert "ORDER BY knowledge.sync_events.event_sequence DESC" in text
    assert "LIMIT" in text
    assert 1 in compiled.params.values()
    assert str(_WORKSPACE_ID) not in text


def test_cursor_select_statement_locks_the_exact_workspace_device_row() -> None:
    locked_text = str(
        device_cursor_select_statement(_WORKSPACE_ID, _DEVICE_ID, for_update=True).compile(
            dialect=postgresql.dialect()
        )
    )
    assert _bind_marker(locked_text, "workspace_id")
    assert _bind_marker(locked_text, "device_id")
    assert "FOR UPDATE" in locked_text
    assert str(_WORKSPACE_ID) not in locked_text
    assert str(_DEVICE_ID) not in locked_text
    unlocked_text = str(
        device_cursor_select_statement(_WORKSPACE_ID, _DEVICE_ID).compile(
            dialect=postgresql.dialect()
        )
    )
    assert "FOR UPDATE" not in unlocked_text


def test_delivered_watermark_advance_touches_only_the_delivered_column() -> None:
    text = str(
        device_delivered_watermark_advance_statement(
            _WORKSPACE_ID, _DEVICE_ID, delivered_through_sequence=9
        ).compile(dialect=postgresql.dialect())
    )
    assert "UPDATE knowledge.device_cursors" in text
    assert "acknowledged_sequence" not in text
    assert "delivered_through_sequence" in text
    assert "updated_at" in text
    assert "delivered_through_sequence <" in text
    assert _bind_marker(text, "workspace_id")
    assert _bind_marker(text, "device_id")
    assert _bind_marker(text, "delivered_through_sequence")


def test_bootstrap_insert_defaults_acknowledged_zero_and_ignores_conflicts() -> None:
    text = str(
        device_cursor_bootstrap_insert_statement(
            device_cursor_id=_EVENT_ID,
            workspace_id=_WORKSPACE_ID,
            device_id=_DEVICE_ID,
            delivered_through_sequence=9,
        ).compile(dialect=postgresql.dialect())
    )
    assert "INSERT INTO knowledge.device_cursors" in text
    assert "ON CONFLICT (workspace_id, device_id) DO NOTHING" in text
    assert _bind_marker(text, "acknowledged_sequence")
    assert _bind_marker(text, "delivered_through_sequence")


def test_acknowledged_advance_statement_binds_the_applied_sequence() -> None:
    text = str(
        device_acknowledged_advance_statement(
            _WORKSPACE_ID, _DEVICE_ID, applied_through_sequence=9
        ).compile(dialect=postgresql.dialect())
    )
    assert "UPDATE knowledge.device_cursors" in text
    assert "acknowledged_sequence" in text
    assert "delivered_through_sequence" not in text.split("WHERE", 1)[1]
    assert _bind_marker(text, "applied_through_sequence")
    assert _bind_marker(text, "workspace_id")
    assert _bind_marker(text, "device_id")


def test_floor_statement_joins_active_devices_only() -> None:
    statement = workspace_minimum_acknowledged_statement(_WORKSPACE_ID)
    text = str(statement.compile(dialect=postgresql.dialect()))
    assert "min(knowledge.device_cursors.acknowledged_sequence)" in text
    assert "JOIN knowledge.devices ON" in text
    assert "knowledge.devices.status =" in text
    assert _bind_marker(text, "status")
    assert _bind_marker(text, "workspace_id")
    assert DEVICE_STATUS_ACTIVE == "active"


def test_action_page_statement_is_run_scoped_and_index_ordered() -> None:
    text = str(
        manifest_action_page_statement(
            _MANIFEST_RUN_ID,
            workspace_id=_WORKSPACE_ID,
            after_action_index=500,
            limit=MAX_PULL_EVENTS + 1,
        ).compile(dialect=postgresql.dialect())
    )
    assert _bind_marker(text, "manifest_run_id")
    assert _bind_marker(text, "after_action_index")
    assert "LIMIT %(pull_limit)s" in text
    assert "ORDER BY knowledge.manifest_actions.action_index ASC" in text
    # The checkpoint locator hydrates at read time only for download actions,
    # through a workspace-scoped outer join on the canonical locator row so a
    # foreign locator id never crosses the credential boundary (task 11b).
    assert "LEFT OUTER JOIN knowledge.source_locators ON" in text
    assert _bind_marker(text, "workspace_id_1")
    assert "CASE WHEN (knowledge.manifest_actions.action_kind = 'download')" in text
    assert str(_WORKSPACE_ID) not in text


# --- domain database retry policy ---------------------------------------------


class _DriverFailure(Exception):
    """Fake driver exception carrying a SQLSTATE and sentinel driver text."""

    def __init__(self, sqlstate: str | None) -> None:
        super().__init__(_SENTINEL_DRIVER_TEXT)
        self.sqlstate = sqlstate


class _SleepRecorder:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


def _contention_failure() -> sa_exc.DBAPIError:
    return sa_exc.DBAPIError(_SENTINEL_STATEMENT, {}, _DriverFailure("40P01"))


def _unavailable_failure() -> sa_exc.DBAPIError:
    return sa_exc.DBAPIError(_SENTINEL_STATEMENT, {}, _DriverFailure("08006"))


def _integrity_failure() -> sa_exc.DBAPIError:
    return sa_exc.DBAPIError(_SENTINEL_STATEMENT, {}, _DriverFailure("23505"))


@pytest.mark.asyncio
async def test_retry_policy_retries_contention_then_succeeds() -> None:
    sleep = _SleepRecorder()
    attempts: list[int] = []

    async def operation(attempt: int) -> str:
        attempts.append(attempt)
        if len(attempts) < 3:
            raise _contention_failure()
        return "delivered"

    result = await DeviceSyncDatabaseRetryPolicy().run(
        operation, sleep=sleep, jitter=lambda minimum, maximum: minimum
    )
    assert result == "delivered"
    assert attempts == [1, 2, 3]
    assert len(sleep.delays) == 2


@pytest.mark.asyncio
async def test_retry_policy_passes_typed_device_errors_through() -> None:
    async def operation(attempt: int) -> None:
        raise DeviceSyncError(DeviceSyncErrorCode.CURSOR_GAP)

    with pytest.raises(DeviceSyncError) as raised:
        await DeviceSyncDatabaseRetryPolicy().run(
            operation, sleep=_SleepRecorder(), jitter=lambda minimum, maximum: minimum
        )
    assert raised.value.code is DeviceSyncErrorCode.CURSOR_GAP


def test_database_failure_mapping_never_leaks_driver_text() -> None:
    for cause, expected_dependency in (
        (_contention_failure(), True),
        (_unavailable_failure(), True),
        (_integrity_failure(), False),
    ):
        error = map_device_sync_database_failure(cause)
        rendered = f"{error!r} {error} {error.to_safe_dict()}"
        assert _SENTINEL_DRIVER_TEXT not in rendered
        assert _SENTINEL_STATEMENT not in rendered
        if expected_dependency:
            assert isinstance(error, DeviceSyncError)
            assert error.code is DeviceSyncErrorCode.DEPENDENCY_UNAVAILABLE
        else:
            assert isinstance(error, InternalApplicationError)


def test_database_failure_mapping_routes_non_database_bugs_to_internal() -> None:
    error = map_device_sync_database_failure(RuntimeError(_SENTINEL_DRIVER_TEXT))
    assert isinstance(error, InternalApplicationError)
    assert error.error_code is ErrorCode.INTERNAL_ERROR


def test_hydration_accepts_readonly_row_mappings() -> None:
    class _ReadOnlyRow(Mapping[str, Any]):
        def __init__(self, values: dict[str, Any]) -> None:
            self._values = values

        def __getitem__(self, key: str) -> Any:
            return self._values[key]

        def __iter__(self) -> Any:
            return iter(self._values)

        def __len__(self) -> int:
            return len(self._values)

    hydrated = hydrate_device_event(_ReadOnlyRow(row_for(DeviceEventType.MOVED)))
    assert_event_shape(hydrated)
