"""Upgrade/downgrade contract for the device-sync scale indexes.

These tests replay the ``20260901_02`` upgrade and downgrade against a
recording stub of ``alembic.op`` (never a database) and read the revision
source. They pin the revision chain over the grant-poll pacing bucket kind
head, the two-index upgrade that creates the workspace-scoped pull
composite and the partial tombstone-restore index with their exact table,
column and predicate shapes, the downgrade that drops both indexes in
exact reverse creation order, and the migration hygiene rules that keep
the revision free of any domain import.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from alembic.config import Config
from alembic.script import ScriptDirectory

REPO_ROOT: Path = Path(__file__).resolve().parents[3]
ALEMBIC_INI_PATH: Path = REPO_ROOT / "alembic.ini"
MIGRATION_PATH = (
    REPO_ROOT
    / "migrations"
    / "versions"
    / "20260901_02_add_device_sync_workspace_scoped_indexes.py"
)

DEVICE_SYNC_SCALE_INDEX_REVISION: str = "20260901_02"
GRANT_POLL_BUCKET_KIND_REVISION: str = "20260901_01"
TERMINAL_LOCATOR_REMEDIATION_REVISION: str = "20260901_03"

SCHEMA_NAME: str = "knowledge"
SYNC_EVENTS_TABLE_NAME: str = "sync_events"
SOURCE_TOMBSTONES_TABLE_NAME: str = "source_tombstones"
WORKSPACE_EVENT_SEQUENCE_INDEX_NAME: str = "ix_sync_events__workspace_event_sequence"
RESTORE_EVENT_ID_INDEX_NAME: str = "ix_source_tombstones__restore_event_id"
RESTORE_EVENT_ID_PARTIAL_PREDICATE_TEXT: str = "restore_event_id IS NOT NULL"


class _IndexRecordingAlembicOp:
    """Stub of ``alembic.op`` recording every operation with its arguments."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []
        self.calls: dict[str, SimpleNamespace] = {}

    def create_index(self, index_name: str, *args: Any, **kwargs: Any) -> None:
        self.events.append(("create_index", index_name))
        self.calls[f"create_index:{index_name}"] = SimpleNamespace(args=args, kwargs=kwargs)

    def drop_index(self, index_name: str, *args: Any, **kwargs: Any) -> None:
        self.events.append(("drop_index", index_name))
        self.calls[f"drop_index:{index_name}"] = SimpleNamespace(args=args, kwargs=kwargs)


def _load_revision_module() -> Any:
    assert MIGRATION_PATH.is_file(), (
        f"missing device sync scale index migration: {MIGRATION_PATH.name}"
    )
    spec = importlib.util.spec_from_file_location(
        "device_sync_scale_index_migration", MIGRATION_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _replay(function_name: str) -> _IndexRecordingAlembicOp:
    module = _load_revision_module()
    recorder = _IndexRecordingAlembicOp()
    module.op = recorder  # type: ignore[attr-defined]
    getattr(module, function_name)()
    return recorder


def _script_directory() -> ScriptDirectory:
    return ScriptDirectory.from_config(Config(str(ALEMBIC_INI_PATH)))


def _migration_source() -> str:
    return MIGRATION_PATH.read_text(encoding="utf-8")


def test_device_sync_scale_index_revision_stacks_on_the_grant_poll_head() -> None:
    module = _load_revision_module()
    assert module.revision == DEVICE_SYNC_SCALE_INDEX_REVISION
    assert module.down_revision == GRANT_POLL_BUCKET_KIND_REVISION


def test_canonical_schema_revision_points_at_the_new_head() -> None:
    from personal_os.database_schema import CANONICAL_POSTGRESQL_SCHEMA_REVISION

    assert CANONICAL_POSTGRESQL_SCHEMA_REVISION == "20260902_01"


def test_device_sync_scale_index_revision_is_chained_below_the_remediation_head() -> None:
    # The terminal locator remediation revision ``20260901_03`` stacks on the
    # device-sync scale index revision, so the single graph head moved past it.
    scripts = _script_directory()
    assert scripts.get_heads() == ["20260902_01"]


def test_upgrade_creates_both_scale_indexes_in_order() -> None:
    recorder = _replay("upgrade")
    assert recorder.events == [
        ("create_index", WORKSPACE_EVENT_SEQUENCE_INDEX_NAME),
        ("create_index", RESTORE_EVENT_ID_INDEX_NAME),
    ]


def test_upgrade_pins_the_workspace_scoped_composite_shape() -> None:
    """The pull index must prefix the workspace before the ordered sequence."""

    recorder = _replay("upgrade")
    call = recorder.calls[f"create_index:{WORKSPACE_EVENT_SEQUENCE_INDEX_NAME}"]
    assert call.args == (SYNC_EVENTS_TABLE_NAME, ["workspace_id", "event_sequence"])
    assert call.kwargs == {"schema": SCHEMA_NAME}


def test_upgrade_pins_the_partial_tombstone_restore_shape() -> None:
    """The restore index must cover only tombstones that own a restore event."""

    recorder = _replay("upgrade")
    call = recorder.calls[f"create_index:{RESTORE_EVENT_ID_INDEX_NAME}"]
    assert call.args == (SOURCE_TOMBSTONES_TABLE_NAME, ["restore_event_id"])
    assert call.kwargs["schema"] == SCHEMA_NAME
    where = call.kwargs["postgresql_where"]
    assert str(where) == RESTORE_EVENT_ID_PARTIAL_PREDICATE_TEXT


def test_downgrade_drops_both_indexes_in_exact_reverse() -> None:
    recorder = _replay("downgrade")
    assert recorder.events == [
        ("drop_index", RESTORE_EVENT_ID_INDEX_NAME),
        ("drop_index", WORKSPACE_EVENT_SEQUENCE_INDEX_NAME),
    ]
    for index_name, table_name in (
        (RESTORE_EVENT_ID_INDEX_NAME, SOURCE_TOMBSTONES_TABLE_NAME),
        (WORKSPACE_EVENT_SEQUENCE_INDEX_NAME, SYNC_EVENTS_TABLE_NAME),
    ):
        call = recorder.calls[f"drop_index:{index_name}"]
        assert call.kwargs == {"table_name": table_name, "schema": SCHEMA_NAME}


def test_migration_hygiene_rules_hold() -> None:
    source = _migration_source()
    lowered = source.lower()
    assert "personal_os" not in source
    assert "gen_random_uuid" not in lowered
    assert "uuid_generate" not in lowered
    assert "jsonb" not in lowered
    assert "create extension" not in lowered


def test_migration_records_nothing_but_index_operations() -> None:
    recorder = _replay("upgrade")
    for operation, _ in recorder.events:
        assert operation == "create_index"
