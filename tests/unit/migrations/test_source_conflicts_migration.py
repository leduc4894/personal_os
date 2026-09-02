"""Static DDL contract for the canonical source-conflict revision."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa
from alembic.config import Config
from alembic.script import ScriptDirectory

REPO_ROOT = Path(__file__).resolve().parents[3]
ALEMBIC_INI_PATH = REPO_ROOT / "alembic.ini"
MIGRATION_PATH = REPO_ROOT / "migrations" / "versions" / "20260902_01_add_source_conflicts.py"

SOURCE_CONFLICTS_REVISION = "20260902_01"
TERMINAL_LOCATOR_REMEDIATION_REVISION = "20260901_03"

CONFLICT_COLUMNS = frozenset(
    {
        "conflict_id",
        "workspace_id",
        "source_id",
        "conflict_kind",
        "status",
        "originating_event_id",
        "originating_device_id",
        "capture_idempotency_key",
        "base_version_id",
        "observed_remote_version_id",
        "candidate_kind",
        "verified_candidate_object_id",
        "normalized_locator",
        "resolution_kind",
        "resolution_event_id",
        "resolution_idempotency_key",
        "resulting_version_id",
        "successor_conflict_id",
        "captured_at",
        "closed_at",
    }
)

_UUID_TEXT_GRAMMAR_CHECK = (
    "{column} ~ '^[0-9a-f]{{8}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{12}}$' "
    "AND {column} <> '00000000-0000-0000-0000-000000000000'"
)


class _Result:
    def __init__(self, count: int = 0) -> None:
        self._count = count

    def scalar_one(self) -> int:
        return self._count


class _Bind:
    """Bind double answering the conflict-evidence downgrade gate count."""

    def __init__(self, conflict_rows: int = 0) -> None:
        self.conflict_rows = conflict_rows
        self.executed: list[str] = []

    def execute(self, statement: object, *args: Any) -> _Result:
        statement_text = str(statement)
        self.executed.append(statement_text)
        return _Result(self.conflict_rows)


class _Op:
    def __init__(
        self,
        *,
        conflict_rows: int = 0,
        x_arguments: list[str] | None = None,
    ) -> None:
        self.events: list[tuple[str, str]] = []
        self.tables: dict[str, Any] = {}
        self.check_constraints: dict[str, str] = {}
        self.indexes: dict[str, tuple[tuple[Any, ...], dict[str, Any]]] = {}
        self.bind = _Bind(conflict_rows)
        self.context = SimpleNamespace(
            config=SimpleNamespace(cmd_opts=SimpleNamespace(x=x_arguments or []))
        )

    def create_table(self, name: str, *args: Any, **kwargs: Any) -> Any:
        self.events.append(("create_table", name))
        table = sa.Table(name, sa.MetaData(), *args, schema=kwargs.get("schema"))
        self.tables[name] = table
        return table

    def create_index(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.events.append(("create_index", name))
        self.indexes[name] = (args, kwargs)

    def create_check_constraint(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.events.append(("create_check_constraint", name))
        self.check_constraints[name] = str(args[1])

    def drop_constraint(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.events.append(("drop_constraint", name))

    def drop_index(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.events.append(("drop_index", name))

    def drop_table(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.events.append(("drop_table", name))

    def execute(self, statement: Any, **kwargs: Any) -> None:
        self.events.append(("execute", str(statement)))

    def get_bind(self) -> _Bind:
        return self.bind

    def get_context(self) -> Any:
        return self.context


def _load_revision() -> Any:
    assert MIGRATION_PATH.is_file(), f"missing conflict migration: {MIGRATION_PATH.name}"
    spec = importlib.util.spec_from_file_location("source_conflicts_migration", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _replay(function_name: str, **kwargs: Any) -> _Op:
    revision = _load_revision()
    recorder = _Op(**kwargs)
    revision.op = recorder
    getattr(revision, function_name)()
    return recorder


def _named_constraints(table: Any) -> dict[str, Any]:
    named: dict[str, Any] = {}
    for constraint in table.constraints:
        if constraint.name is not None:
            named[constraint.name] = constraint
    return named


def test_revision_stacks_directly_on_the_terminal_locator_head() -> None:
    scripts = ScriptDirectory.from_config(Config(str(ALEMBIC_INI_PATH)))
    assert scripts.get_heads() == [SOURCE_CONFLICTS_REVISION]
    revision = scripts.get_revision(SOURCE_CONFLICTS_REVISION)
    assert revision is not None
    assert revision.down_revision == TERMINAL_LOCATOR_REMEDIATION_REVISION


def test_upgrade_creates_the_source_conflicts_record_with_the_exact_columns() -> None:
    recorder = _replay("upgrade")
    assert [detail for operation, detail in recorder.events if operation == "create_table"] == [
        "source_conflicts"
    ]
    assert {column.name for column in recorder.tables["source_conflicts"].columns} == (
        CONFLICT_COLUMNS
    )


def test_upgrade_binds_every_evidence_reference_with_on_delete_restrict() -> None:
    recorder = _replay("upgrade")
    constraints = _named_constraints(recorder.tables["source_conflicts"])
    assert {
        name: (
            tuple(constraint.column_keys),
            tuple(element.target_fullname for element in constraint.elements),
            constraint.ondelete,
        )
        for name, constraint in constraints.items()
        if name.startswith("fk_source_conflicts")
    } == {
        "fk_source_conflicts__workspace": (
            ("workspace_id",),
            ("knowledge.workspaces.workspace_id",),
            "RESTRICT",
        ),
        "fk_source_conflicts__source": (
            ("workspace_id", "source_id"),
            ("knowledge.sources.workspace_id", "knowledge.sources.source_id"),
            "RESTRICT",
        ),
        "fk_source_conflicts__originating_event": (
            ("workspace_id", "source_id", "originating_event_id"),
            (
                "knowledge.sync_events.workspace_id",
                "knowledge.sync_events.source_id",
                "knowledge.sync_events.event_id",
            ),
            "RESTRICT",
        ),
        "fk_source_conflicts__resolution_event": (
            ("workspace_id", "source_id", "resolution_event_id"),
            (
                "knowledge.sync_events.workspace_id",
                "knowledge.sync_events.source_id",
                "knowledge.sync_events.event_id",
            ),
            "RESTRICT",
        ),
        "fk_source_conflicts__device": (
            ("workspace_id", "originating_device_id"),
            (
                "knowledge.devices.workspace_id",
                "knowledge.devices.device_id",
            ),
            "RESTRICT",
        ),
        "fk_source_conflicts__base_version": (
            ("workspace_id", "source_id", "base_version_id"),
            (
                "knowledge.source_versions.workspace_id",
                "knowledge.source_versions.source_id",
                "knowledge.source_versions.source_version_id",
            ),
            "RESTRICT",
        ),
        "fk_source_conflicts__observed_remote_version": (
            ("workspace_id", "source_id", "observed_remote_version_id"),
            (
                "knowledge.source_versions.workspace_id",
                "knowledge.source_versions.source_id",
                "knowledge.source_versions.source_version_id",
            ),
            "RESTRICT",
        ),
        "fk_source_conflicts__resulting_version": (
            ("workspace_id", "source_id", "resulting_version_id"),
            (
                "knowledge.source_versions.workspace_id",
                "knowledge.source_versions.source_id",
                "knowledge.source_versions.source_version_id",
            ),
            "RESTRICT",
        ),
        "fk_source_conflicts__candidate_object": (
            ("verified_candidate_object_id",),
            ("knowledge.content_objects.content_object_id",),
            "RESTRICT",
        ),
        "fk_source_conflicts__successor": (
            ("successor_conflict_id",),
            ("knowledge.source_conflicts.conflict_id",),
            "RESTRICT",
        ),
    }


def test_upgrade_enforces_the_closed_conflict_vocabulary_and_shapes() -> None:
    recorder = _replay("upgrade")
    constraints = _named_constraints(recorder.tables["source_conflicts"])
    assert str(constraints["ck_source_conflicts__conflict_kind"].sqltext) == (
        "conflict_kind IN ('stale_content', 'edit_remote_delete', "
        "'delete_remote_edit', 'locator_collision')"
    )
    assert str(constraints["ck_source_conflicts__status"].sqltext) == (
        "status IN ('open', 'resolving', 'resolved', 'superseded')"
    )
    assert str(constraints["ck_source_conflicts__resolution_kind"].sqltext) == (
        "resolution_kind IS NULL OR resolution_kind IN ('keep_remote', 'keep_local', 'save_merged')"
    )
    assert str(constraints["ck_source_conflicts__candidate_kind"].sqltext) == (
        "candidate_kind IN ('content', 'delete')"
    )
    assert str(constraints["ck_source_conflicts__candidate_shape"].sqltext) == (
        "(candidate_kind = 'content') = (verified_candidate_object_id IS NOT NULL)"
    )
    assert str(constraints["ck_source_conflicts__kind_candidate"].sqltext) == (
        "(conflict_kind NOT IN ('stale_content', 'edit_remote_delete') "
        "OR candidate_kind = 'content') "
        "AND (conflict_kind <> 'delete_remote_edit' OR candidate_kind = 'delete')"
    )
    assert str(constraints["ck_source_conflicts__source_binding"].sqltext) == (
        "source_id IS NOT NULL OR conflict_kind = 'locator_collision'"
    )
    assert str(constraints["ck_source_conflicts__locator_snapshot"].sqltext) == (
        "conflict_kind <> 'locator_collision' OR normalized_locator IS NOT NULL"
    )
    assert str(constraints["ck_source_conflicts__capture_key_grammar"].sqltext) == (
        _UUID_TEXT_GRAMMAR_CHECK.format(column="capture_idempotency_key")
    )
    assert str(constraints["ck_source_conflicts__resolution_key_grammar"].sqltext) == (
        f"resolution_idempotency_key IS NULL OR "
        f"({_UUID_TEXT_GRAMMAR_CHECK.format(column='resolution_idempotency_key')})"
    )


def test_upgrade_enforces_the_closed_status_shapes_and_closure_time() -> None:
    recorder = _replay("upgrade")
    constraints = _named_constraints(recorder.tables["source_conflicts"])
    assert str(constraints["ck_source_conflicts__open_shape"].sqltext) == (
        "status <> 'open' OR (resolution_kind IS NULL AND resolution_event_id IS NULL "
        "AND resolution_idempotency_key IS NULL AND resulting_version_id IS NULL "
        "AND successor_conflict_id IS NULL AND closed_at IS NULL)"
    )
    assert str(constraints["ck_source_conflicts__resolving_shape"].sqltext) == (
        "status <> 'resolving' OR (resolution_kind IS NOT NULL "
        "AND resolution_event_id IS NOT NULL AND resolution_idempotency_key IS NOT NULL "
        "AND resulting_version_id IS NULL AND successor_conflict_id IS NULL "
        "AND closed_at IS NULL)"
    )
    assert str(constraints["ck_source_conflicts__resolved_shape"].sqltext) == (
        "status <> 'resolved' OR (resolution_kind IS NOT NULL "
        "AND resolution_event_id IS NOT NULL AND resolution_idempotency_key IS NOT NULL "
        "AND closed_at IS NOT NULL AND successor_conflict_id IS NULL "
        "AND ((resolution_kind = 'keep_remote' AND resulting_version_id IS NULL) "
        "OR (resolution_kind IN ('keep_local', 'save_merged') "
        "AND resulting_version_id IS NOT NULL)))"
    )
    assert str(constraints["ck_source_conflicts__superseded_shape"].sqltext) == (
        "status <> 'superseded' OR (resolution_event_id IS NOT NULL "
        "AND resolution_idempotency_key IS NOT NULL AND successor_conflict_id IS NOT NULL "
        "AND closed_at IS NOT NULL AND resulting_version_id IS NULL)"
    )
    assert str(constraints["ck_source_conflicts__closure_time"].sqltext) == (
        "closed_at IS NULL OR closed_at >= captured_at"
    )
    assert str(constraints["ck_source_conflicts__successor_distinct"].sqltext) == (
        "successor_conflict_id IS NULL OR successor_conflict_id <> conflict_id"
    )


def test_upgrade_pins_the_capture_idempotency_and_event_identities() -> None:
    recorder = _replay("upgrade")
    constraints = _named_constraints(recorder.tables["source_conflicts"])
    assert {
        name: tuple(column.name for column in constraint.columns)
        for name, constraint in constraints.items()
        if isinstance(constraint, sa.UniqueConstraint)
    } == {
        "uq_source_conflicts__capture_idempotency": ("workspace_id", "capture_idempotency_key"),
        "uq_source_conflicts__originating_event": ("workspace_id", "originating_event_id"),
    }


def test_upgrade_creates_the_listing_history_and_replay_indexes() -> None:
    recorder = _replay("upgrade")
    for index_name, expected_columns, expected_where in (
        (
            "ix_source_conflicts__workspace_open_listing",
            ["workspace_id", "conflict_id"],
            "status = 'open'",
        ),
        (
            "ix_source_conflicts__source_history",
            ["workspace_id", "source_id", "captured_at", "conflict_id"],
            None,
        ),
        (
            "uq_source_conflicts__resolution_event",
            ["workspace_id", "resolution_event_id"],
            "resolution_event_id IS NOT NULL",
        ),
    ):
        arguments, keyword_arguments = recorder.indexes[index_name]
        assert arguments[0] == "source_conflicts"
        assert list(arguments[1]) == expected_columns
        assert keyword_arguments["schema"] == "knowledge"
        if expected_where is None:
            assert not keyword_arguments.get("unique", False)
            assert "postgresql_where" not in keyword_arguments
        else:
            assert str(keyword_arguments["postgresql_where"]) == expected_where


def test_upgrade_extends_the_sync_event_vocabulary_with_the_conflict_tokens() -> None:
    recorder = _replay("upgrade")
    assert recorder.check_constraints["ck_sync_events__event_type"] == (
        "event_type IN ('create', 'update', 'rename', 'move', 'delete', 'restore', "
        "'conflict_capture', 'conflict_resolve')"
    )
    assert (
        sum(
            operation == "drop_constraint" and detail == "ck_sync_events__event_type"
            for operation, detail in recorder.events
        )
        == 1
    )
    assert (
        sum(
            operation == "create_check_constraint" and detail == "ck_sync_events__event_type"
            for operation, detail in recorder.events
        )
        == 1
    )


def test_downgrade_refuses_to_destroy_conflict_evidence_without_explicit_gate() -> None:
    with pytest.raises(RuntimeError, match="source_conflict_downgrade_requires_explicit_gate"):
        _replay("downgrade", conflict_rows=1)


def test_downgrade_gate_runs_before_any_drop() -> None:
    recorder = _replay("downgrade", conflict_rows=0)
    gate_sql = recorder.bind.executed[0]
    assert gate_sql.lstrip().upper().startswith("SELECT")
    assert "knowledge.source_conflicts" in gate_sql
    first_drop = next(
        (
            index
            for index, (operation, _detail) in enumerate(recorder.events)
            if operation in {"drop_index", "drop_table", "drop_constraint"}
        ),
        None,
    )
    assert first_drop is not None


def test_downgrade_drops_conflict_schema_and_restores_the_predecessor_vocabulary() -> None:
    recorder = _replay("downgrade", conflict_rows=1, x_arguments=["allow_destructive=true"])
    assert [detail for operation, detail in recorder.events if operation == "drop_index"] == [
        "uq_source_conflicts__resolution_event",
        "ix_source_conflicts__source_history",
        "ix_source_conflicts__workspace_open_listing",
    ]
    assert [detail for operation, detail in recorder.events if operation == "drop_table"] == [
        "source_conflicts"
    ]
    assert recorder.check_constraints["ck_sync_events__event_type"] == (
        "event_type IN ('create', 'update', 'rename', 'move', 'delete', 'restore')"
    )
    conflict_event_cleanup = next(
        detail
        for operation, detail in recorder.events
        if operation == "execute" and "DELETE FROM knowledge.sync_events" in detail
    )
    assert "conflict_capture" in conflict_event_cleanup
    assert "conflict_resolve" in conflict_event_cleanup


def test_downgrade_removes_conflict_projection_intents_before_conflict_events() -> None:
    """A published resolution leaves rebuildable intents RESTRICTing the events.

    Every ``keep_local``/``save_merged`` resolution inserts two upsert
    projection intents whose ``fk_projection_intents__event_source`` uses
    ``ON DELETE RESTRICT``; the gated cleanup must delete those intents
    BEFORE the conflict sync events or the walk aborts mid-flight on the raw
    foreign-key violation instead of completing under the explicit gate.
    """
    recorder = _replay("downgrade", conflict_rows=1, x_arguments=["allow_destructive=true"])
    cleanup = next(
        detail
        for operation, detail in recorder.events
        if operation == "execute" and "DELETE FROM knowledge.projection_intents" in detail
    )
    intents_delete_index = cleanup.index("DELETE FROM knowledge.projection_intents")
    events_delete_index = cleanup.index("DELETE FROM knowledge.sync_events")
    assert intents_delete_index < events_delete_index
    # The intent delete reaches only the conflict events, through the
    # closed-token subselect over the conflict event vocabulary.
    assert "event_type IN ('conflict_capture', 'conflict_resolve')" in cleanup
