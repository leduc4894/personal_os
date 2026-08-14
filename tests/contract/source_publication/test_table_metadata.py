"""DML Core metadata contract against the baseline migration DDL authority.

The Alembic migration ``20260813_01`` is the single DDL authority. This test
loads the migration module, replays its ``upgrade()`` against a recording stub
of ``alembic.op`` and compares the nine schema-qualified tables the migration
creates with the typed DML metadata in ``postgresql_source_store.tables``:
identical table names, schema, column names, column types and nullability,
with full coverage in both directions and no ``create_all()`` path anywhere in
the adapter package.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import MappingProxyType
from typing import Any

import sqlalchemy as sa

from postgresql_source_store.tables import (
    SOURCE_STORE_METADATA,
    SOURCE_STORE_SCHEMA,
    SOURCE_STORE_TABLES,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
MIGRATION_GLOB = "20260813_01*.py"
MIGRATION_DIRECTORY = REPO_ROOT / "migrations" / "versions"
PACKAGE_SOURCE_ROOT = (
    REPO_ROOT / "packages" / "postgresql-source-store" / "src" / "postgresql_source_store"
)

EXPECTED_TABLE_NAMES = frozenset(
    {
        "users",
        "workspaces",
        "devices",
        "content_objects",
        "sources",
        "source_versions",
        "sync_events",
        "projection_intents",
        "audit_events",
    }
)


class _RecordingAlembicOp:
    """Stub of ``alembic.op`` that records created tables and ignores DDL."""

    def __init__(self) -> None:
        self.created_tables: list[sa.Table] = []

    def create_table(self, name: str, *args: Any, **kwargs: Any) -> sa.Table:
        schema = kwargs.get("schema")
        table = sa.Table(name, sa.MetaData(), *args, schema=schema)
        self.created_tables.append(table)
        return table

    def create_index(self, *args: Any, **kwargs: Any) -> None:
        return None

    def create_foreign_key(self, *args: Any, **kwargs: Any) -> None:
        return None

    def execute(self, *args: Any, **kwargs: Any) -> None:
        return None

    def get_context(self) -> None:
        return None


def _load_baseline_migration() -> Any:
    matches = sorted(MIGRATION_DIRECTORY.glob(MIGRATION_GLOB))
    assert len(matches) == 1, f"expected exactly one baseline migration, found {matches}"
    spec = importlib.util.spec_from_file_location("canonical_baseline_migration", matches[0])
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _collect_migration_tables() -> dict[str, sa.Table]:
    migration = _load_baseline_migration()
    recorder = _RecordingAlembicOp()
    # The migration resolves ``op`` from its module globals at call time, so the
    # recorder replays ``upgrade()`` without any database or Alembic context.
    migration.op = recorder  # type: ignore[attr-defined]
    migration.upgrade()
    return {table.name: table for table in recorder.created_tables}


def _column_signature(column: sa.Column) -> tuple[Any, ...]:
    return (
        column.name,
        type(column.type),
        getattr(column.type, "length", None),
        getattr(column.type, "timezone", None),
        column.nullable,
    )


def test_dml_metadata_covers_every_migrated_column_and_type() -> None:
    migration_tables = _collect_migration_tables()

    assert set(SOURCE_STORE_TABLES) == EXPECTED_TABLE_NAMES
    assert set(migration_tables) == EXPECTED_TABLE_NAMES

    for table_name in sorted(EXPECTED_TABLE_NAMES):
        dml_table = SOURCE_STORE_TABLES[table_name]
        migration_table = migration_tables[table_name]

        assert dml_table.schema == SOURCE_STORE_SCHEMA, table_name
        assert migration_table.schema == SOURCE_STORE_SCHEMA, table_name
        assert dml_table.metadata is SOURCE_STORE_METADATA

        dml_columns = {column.name: _column_signature(column) for column in dml_table.columns}
        migration_columns = {
            column.name: _column_signature(column) for column in migration_table.columns
        }
        assert dml_columns == migration_columns, (
            f"{table_name}: DML columns must exactly cover the migrated columns; "
            f"missing={set(migration_columns) - set(dml_columns)} "
            f"extra={set(dml_columns) - set(migration_columns)} "
            f"changed={
                {
                    name
                    for name in dml_columns.keys() & migration_columns.keys()
                    if dml_columns[name] != migration_columns[name]
                }
            }"
        )

        dml_primary_keys = {column.name for column in dml_table.primary_key.columns}
        migration_primary_keys = {column.name for column in migration_table.primary_key.columns}
        assert dml_primary_keys == migration_primary_keys, table_name


def test_dml_metadata_declares_no_create_all_path() -> None:
    for source_path in sorted(PACKAGE_SOURCE_ROOT.glob("*.py")):
        source_text = source_path.read_text(encoding="utf-8")
        assert ".create_all(" not in source_text, (
            f"{source_path.name}: the migration is the DDL authority; the adapter "
            "must not create schema at runtime"
        )


def test_dml_table_map_is_an_immutable_view_of_the_metadata() -> None:
    assert isinstance(SOURCE_STORE_TABLES, MappingProxyType)
    assert set(SOURCE_STORE_TABLES.values()) == set(SOURCE_STORE_METADATA.tables.values())
