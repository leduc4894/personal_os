"""Static DDL contract for the canonical source-locator lifecycle revision."""

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
MIGRATION_PATH = (
    REPO_ROOT / "migrations" / "versions" / "20260820_01_add_source_locator_lifecycle.py"
)

SOURCE_LIFECYCLE_REVISION = "20260820_01"
SMALL_FILE_REVISION = "20260818_01"

LOCATOR_COLUMNS = frozenset(
    {
        "source_locator_id",
        "workspace_id",
        "source_id",
        "normalized_locator",
        "display_locator",
        "opened_event_id",
        "opened_sequence",
        "closed_event_id",
        "closed_sequence",
        "opened_at",
        "closed_at",
    }
)

TOMBSTONE_COLUMNS = frozenset(
    {
        "source_tombstone_id",
        "workspace_id",
        "source_id",
        "delete_event_id",
        "retained_version_id",
        "retained_locator",
        "actor_kind",
        "actor_id",
        "deleted_at",
        "restore_event_id",
        "restored_at",
    }
)


class _Result:
    def __init__(self, count: int = 0) -> None:
        self._count = count

    def scalar_one(self) -> int:
        return self._count


class _Bind:
    """Bind double answering gate counts per evidence table.

    ``small_file_rows`` defaults to ``count`` so existing replays keep the
    historical single-number behavior; the preflight order pin and the split
    refusal cases set it explicitly (the real SQL counts differ per table).
    """

    def __init__(self, count: int = 0, small_file_rows: int | None = None) -> None:
        self.count = count
        self.small_file_rows = count if small_file_rows is None else small_file_rows
        self.executed: list[str] = []

    def execute(self, statement: object, *args: Any) -> _Result:
        statement_text = str(statement)
        self.executed.append(statement_text)
        if "small_file_upload_operations" in statement_text:
            return _Result(self.small_file_rows)
        return _Result(self.count)


class _Op:
    def __init__(
        self,
        *,
        protected_rows: int = 0,
        small_file_rows: int | None = None,
        x_arguments: list[str] | None = None,
    ) -> None:
        self.events: list[tuple[str, str]] = []
        self.tables: dict[str, Any] = {}
        self.check_constraints: dict[str, str] = {}
        self.indexes: dict[str, tuple[tuple[Any, ...], dict[str, Any]]] = {}
        self.bind = _Bind(protected_rows, small_file_rows)
        self.context = SimpleNamespace(
            config=SimpleNamespace(cmd_opts=SimpleNamespace(x=x_arguments or []))
        )

    def create_table(self, name: str, *args: Any, **kwargs: Any) -> Any:
        self.events.append(("create_table", name))
        table = SimpleNamespace(args=args)
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

    def add_column(self, table_name: str, column: Any, **kwargs: Any) -> None:
        self.events.append(("add_column", f"{table_name}.{column.name}"))

    def drop_column(self, table_name: str, column_name: str, **kwargs: Any) -> None:
        self.events.append(("drop_column", f"{table_name}.{column_name}"))

    def execute(self, statement: Any, **kwargs: Any) -> None:
        self.events.append(("execute", str(statement)))

    def get_bind(self) -> _Bind:
        return self.bind

    def get_context(self) -> Any:
        return self.context


def _load_revision() -> Any:
    assert MIGRATION_PATH.is_file(), f"missing lifecycle migration: {MIGRATION_PATH.name}"
    spec = importlib.util.spec_from_file_location("source_lifecycle_migration", MIGRATION_PATH)
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


def test_revision_stacks_directly_on_the_small_file_head() -> None:
    scripts = ScriptDirectory.from_config(Config(str(ALEMBIC_INI_PATH)))
    # The device sync, multipart, operation-size-bound, deferred-identity,
    # sealed-token, submitted policy verdict, grant-poll bucket kind,
    # device-sync scale index, terminal locator remediation and source-conflict
    # revisions stack on the lifecycle revision, so the single graph head moved
    # past it.
    assert scripts.get_heads() == ["20260902_01"]
    revision = scripts.get_revision(SOURCE_LIFECYCLE_REVISION)
    assert revision is not None
    assert revision.down_revision == SMALL_FILE_REVISION


def test_upgrade_creates_the_closed_locator_and_tombstone_records() -> None:
    recorder = _replay("upgrade")
    assert [detail for operation, detail in recorder.events if operation == "create_table"] == [
        "source_locators",
        "source_tombstones",
    ]
    assert {
        column.name
        for column in recorder.tables["source_locators"].args
        if isinstance(column, sa.Column)
    } == LOCATOR_COLUMNS
    assert {
        column.name
        for column in recorder.tables["source_tombstones"].args
        if isinstance(column, sa.Column)
    } == TOMBSTONE_COLUMNS


def _named_constraints(table: Any) -> dict[str, Any]:
    return {constraint.name: constraint for constraint in table.args if constraint.name is not None}


def test_upgrade_uses_text_columns_and_exact_lifecycle_constraint_arguments() -> None:
    recorder = _replay("upgrade")
    locator_table = recorder.tables["source_locators"]
    tombstone_table = recorder.tables["source_tombstones"]
    locator_columns = {
        column.name: column for column in locator_table.args if isinstance(column, sa.Column)
    }
    tombstone_columns = {
        column.name: column for column in tombstone_table.args if isinstance(column, sa.Column)
    }
    assert isinstance(locator_columns["normalized_locator"].type, sa.Text)
    assert isinstance(locator_columns["display_locator"].type, sa.Text)
    assert isinstance(tombstone_columns["retained_locator"].type, sa.Text)

    locator_constraints = _named_constraints(locator_table)
    tombstone_constraints = _named_constraints(tombstone_table)
    assert str(locator_constraints["ck_source_locators__closure"].sqltext) == (
        "((closed_event_id IS NULL AND closed_sequence IS NULL AND closed_at IS NULL) "
        "OR (closed_event_id IS NOT NULL AND closed_sequence IS NOT NULL "
        "AND closed_at IS NOT NULL)) "
        "AND (closed_sequence IS NULL OR closed_sequence > opened_sequence) "
        "AND (closed_at IS NULL OR closed_at >= opened_at)"
    )
    assert str(tombstone_constraints["ck_source_tombstones__restore"].sqltext) == (
        "(restore_event_id IS NULL) = (restored_at IS NULL) "
        "AND (restored_at IS NULL OR restored_at >= deleted_at)"
    )
    assert {
        name: (
            tuple(constraint.column_keys),
            tuple(element.target_fullname for element in constraint.elements),
            constraint.ondelete,
        )
        for name, constraint in locator_constraints.items()
        if name.startswith("fk_source_locators")
    } == {
        "fk_source_locators__workspace": (
            ("workspace_id",),
            ("knowledge.workspaces.workspace_id",),
            "RESTRICT",
        ),
        "fk_source_locators__source": (
            ("workspace_id", "source_id"),
            ("knowledge.sources.workspace_id", "knowledge.sources.source_id"),
            "RESTRICT",
        ),
        "fk_source_locators__opened_event": (
            ("workspace_id", "source_id", "opened_event_id"),
            (
                "knowledge.sync_events.workspace_id",
                "knowledge.sync_events.source_id",
                "knowledge.sync_events.event_id",
            ),
            "RESTRICT",
        ),
        "fk_source_locators__closed_event": (
            ("workspace_id", "source_id", "closed_event_id"),
            (
                "knowledge.sync_events.workspace_id",
                "knowledge.sync_events.source_id",
                "knowledge.sync_events.event_id",
            ),
            "RESTRICT",
        ),
    }
    assert {
        name: (
            tuple(constraint.column_keys),
            tuple(element.target_fullname for element in constraint.elements),
            constraint.ondelete,
        )
        for name, constraint in tombstone_constraints.items()
        if name.startswith("fk_source_tombstones")
    } == {
        "fk_source_tombstones__workspace": (
            ("workspace_id",),
            ("knowledge.workspaces.workspace_id",),
            "RESTRICT",
        ),
        "fk_source_tombstones__source": (
            ("workspace_id", "source_id"),
            ("knowledge.sources.workspace_id", "knowledge.sources.source_id"),
            "RESTRICT",
        ),
        "fk_source_tombstones__delete_event": (
            ("workspace_id", "source_id", "delete_event_id"),
            (
                "knowledge.sync_events.workspace_id",
                "knowledge.sync_events.source_id",
                "knowledge.sync_events.event_id",
            ),
            "RESTRICT",
        ),
        "fk_source_tombstones__retained_version": (
            ("workspace_id", "source_id", "retained_version_id"),
            (
                "knowledge.source_versions.workspace_id",
                "knowledge.source_versions.source_id",
                "knowledge.source_versions.source_version_id",
            ),
            "RESTRICT",
        ),
        "fk_source_tombstones__restore_event": (
            ("workspace_id", "source_id", "restore_event_id"),
            (
                "knowledge.sync_events.workspace_id",
                "knowledge.sync_events.source_id",
                "knowledge.sync_events.event_id",
            ),
            "RESTRICT",
        ),
    }

    for index_name, expected_table, expected_columns, expected_where in (
        (
            "uq_source_locators_active_workspace_path",
            "source_locators",
            ["workspace_id", "normalized_locator"],
            "closed_event_id IS NULL",
        ),
        (
            "uq_source_locators_active_source",
            "source_locators",
            ["source_id"],
            "closed_event_id IS NULL",
        ),
        (
            "uq_source_tombstones_open_source",
            "source_tombstones",
            ["source_id"],
            "restore_event_id IS NULL",
        ),
    ):
        arguments, keyword_arguments = recorder.indexes[index_name]
        assert arguments == (expected_table, expected_columns)
        assert keyword_arguments["unique"] is True
        assert keyword_arguments["schema"] == "knowledge"
        assert str(keyword_arguments["postgresql_where"]) == expected_where
    assert recorder.check_constraints["ck_sync_events__event_type"] == (
        "event_type IN ('create', 'update', 'rename', 'move', 'delete', 'restore')"
    )
    assert recorder.check_constraints["ck_sources__sync_state"] == (
        "sync_state IN ('pending', 'active', 'stored_not_indexed', 'deleted')"
    )
    assert recorder.check_constraints["ck_sources__deletion"] == (
        "(sync_state = 'deleted') = (deleted_at IS NOT NULL) "
        "AND (deleted_at IS NULL OR deleted_at >= created_at)"
    )
    for constraint_name in (
        "ck_sync_events__event_type",
        "ck_sources__sync_state",
        "ck_sources__deletion",
    ):
        assert (
            sum(
                operation == "drop_constraint" and detail == constraint_name
                for operation, detail in recorder.events
            )
            == 1
        )
        assert (
            sum(
                operation == "create_check_constraint" and detail == constraint_name
                for operation, detail in recorder.events
            )
            == 1
        )


def test_upgrade_composes_lifecycle_intent_rule_with_predecessor_upsert_rule() -> None:
    recorder = _replay("upgrade")
    assert recorder.check_constraints["ck_projection_intents__operation_version"] == (
        "(operation <> 'upsert' OR source_version_id IS NOT NULL) "
        "AND (origin_kind <> 'source_event' OR source_version_id IS NOT NULL)"
    )
    requires_source_version = {
        (operation, origin_kind): operation == "upsert" or origin_kind == "source_event"
        for operation in ("upsert", "delete")
        for origin_kind in ("source_event", "policy_transition")
    }
    assert requires_source_version == {
        ("upsert", "source_event"): True,
        ("upsert", "policy_transition"): True,
        ("delete", "source_event"): True,
        ("delete", "policy_transition"): False,
    }


def test_downgrade_restores_predecessor_checks_after_destructive_lifecycle_rows() -> None:
    recorder = _replay("downgrade", protected_rows=3, x_arguments=["allow_destructive=true"])
    assert recorder.check_constraints["ck_projection_intents__operation_version"] == (
        "operation <> 'upsert' OR source_version_id IS NOT NULL"
    )
    assert recorder.check_constraints["ck_sync_events__event_type"] == (
        "event_type IN ('create', 'update')"
    )
    assert recorder.check_constraints["ck_sources__sync_state"] == (
        "sync_state IN ('pending', 'active', 'stored_not_indexed', 'deleted')"
    )
    assert recorder.check_constraints["ck_sources__deletion"] == (
        "(sync_state = 'deleted') = (deleted_at IS NOT NULL) "
        "AND (deleted_at IS NULL OR deleted_at >= created_at)"
    )
    events = recorder.events
    lifecycle_event_delete = next(
        index
        for index, (operation, detail) in enumerate(events)
        if operation == "execute" and "DELETE FROM knowledge.sync_events" in detail
    )
    restored_event_check = next(
        index
        for index, (operation, detail) in enumerate(events)
        if operation == "create_check_constraint" and detail == "ck_sync_events__event_type"
    )
    assert lifecycle_event_delete < restored_event_check


def test_downgrade_counts_small_file_evidence_before_the_lifecycle_gate_and_any_drop() -> None:
    # ``transaction_per_migration`` commits each revision independently, so by
    # the time this downgrade runs every later revision already committed its
    # own downgrade. The small-file evidence count must therefore run FIRST —
    # before this revision's own lifecycle gate and before any drop — so a
    # refusal stops the walk with the locator columns still in place.
    recorder = _replay(
        "downgrade", protected_rows=1, small_file_rows=1, x_arguments=["allow_destructive=true"]
    )
    gate_sql_statements = recorder.bind.executed
    assert len(gate_sql_statements) >= 2, gate_sql_statements
    first_gate_sql = gate_sql_statements[0]
    assert first_gate_sql.lstrip().upper().startswith("SELECT")
    assert "knowledge.small_file_upload_operations" in first_gate_sql
    second_gate_sql = gate_sql_statements[1]
    assert "knowledge.source_locators" in second_gate_sql
    assert "knowledge.source_tombstones" in second_gate_sql


def test_downgrade_preflights_the_small_file_evidence_gate_before_any_drop() -> None:
    with pytest.raises(RuntimeError, match="small_file_sync_downgrade_requires_explicit_gate"):
        _replay("downgrade", protected_rows=0, small_file_rows=1)


def test_downgrade_refuses_to_destroy_lifecycle_history_without_explicit_gate() -> None:
    with pytest.raises(RuntimeError, match="source_lifecycle_downgrade_requires_explicit_gate"):
        _replay("downgrade", protected_rows=1, small_file_rows=0)


def test_downgrade_drops_lifecycle_schema_after_explicit_gate() -> None:
    recorder = _replay("downgrade", protected_rows=1, x_arguments=["allow_destructive=true"])
    assert [detail for operation, detail in recorder.events if operation == "drop_index"] == [
        "uq_source_tombstones_open_source",
        "uq_source_locators_active_source",
        "uq_source_locators_active_workspace_path",
        "ix_source_locators__workspace_source_history",
    ]
    assert [detail for operation, detail in recorder.events if operation == "drop_table"] == [
        "source_tombstones",
        "source_locators",
    ]
