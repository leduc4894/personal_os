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
    def __init__(self, count: int = 0) -> None:
        self.count = count
        self.executed: list[str] = []

    def execute(self, statement: object, *args: Any) -> _Result:
        self.executed.append(str(statement))
        return _Result(self.count)


class _Op:
    def __init__(self, *, protected_rows: int = 0, x_arguments: list[str] | None = None) -> None:
        self.events: list[tuple[str, str]] = []
        self.tables: dict[str, Any] = {}
        self.bind = _Bind(protected_rows)
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

    def create_check_constraint(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.events.append(("create_check_constraint", name))

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
    assert scripts.get_heads() == [SOURCE_LIFECYCLE_REVISION]
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


def test_upgrade_pins_lifecycle_foreign_keys_checks_and_partial_uniqueness() -> None:
    source = MIGRATION_PATH.read_text(encoding="utf-8")
    for identifier in (
        "fk_source_locators__workspace",
        "fk_source_locators__source",
        "fk_source_locators__opened_event",
        "fk_source_locators__closed_event",
        "ck_source_locators__normalized_locator",
        "ck_source_locators__display_locator",
        "ck_source_locators__closure",
        "fk_source_tombstones__workspace",
        "fk_source_tombstones__source",
        "fk_source_tombstones__delete_event",
        "fk_source_tombstones__retained_version",
        "fk_source_tombstones__restore_event",
        "uq_source_tombstones__delete_event",
        "ck_source_tombstones__retained_locator",
        "ck_source_tombstones__actor",
        "ck_source_tombstones__restore",
        "uq_source_locators_active_workspace_path",
        "uq_source_locators_active_source",
        "uq_source_tombstones_open_source",
    ):
        assert identifier in source
    assert "closed_event_id IS NULL" in source
    assert "restore_event_id IS NULL" in source
    assert "event_type IN ('create', 'update', 'rename', 'move', 'delete', 'restore')" in source
    assert "origin_kind <> 'source_event' OR source_version_id IS NOT NULL" in source
    assert "sync_state IN ('pending', 'active', 'stored_not_indexed', 'deleted')" in source


def test_downgrade_refuses_to_destroy_lifecycle_history_without_explicit_gate() -> None:
    with pytest.raises(RuntimeError, match="source_lifecycle_downgrade_requires_explicit_gate"):
        _replay("downgrade", protected_rows=1)


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
