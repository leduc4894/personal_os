"""DML Core metadata contract against the migration DDL authority.

The Alembic migrations ``20260813_01`` (baseline), ``20260816_01``
(authentication schema), ``20260817_01`` (exclusion policy schema),
``20260818_01`` (small-file sync operations) and ``20260820_01`` (source
lifecycle schema) are the DDL authority. This test
loads the migration modules, replays their ``upgrade()`` against a recording
stub of ``alembic.op`` (including the policy migration's
``add_column``/``alter_column`` evolution of ``projection_intents``) and
compares the thirty schema-qualified tables the migrations create with the
typed DML metadata in ``postgresql_source_store.tables``: identical table
names, schema, column names, column types and nullability, with full coverage
in both directions and no ``create_all()`` path anywhere in the adapter
package.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import MappingProxyType
from typing import Any

import sqlalchemy as sa

from personal_os.recovery.contracts import CANONICAL_COUNT_TABLES, MANIFEST_CONTRACT
from postgresql_source_store.tables import (
    SOURCE_STORE_METADATA,
    SOURCE_STORE_SCHEMA,
    SOURCE_STORE_TABLES,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
MIGRATION_GLOBS: tuple[str, ...] = (
    "20260813_01*.py",
    "20260816_01*.py",
    "20260817_01*.py",
    "20260818_01*.py",
    "20260820_01*.py",
)
MIGRATION_DIRECTORY = REPO_ROOT / "migrations" / "versions"
PACKAGE_SOURCE_ROOT = (
    REPO_ROOT / "packages" / "postgresql-source-store" / "src" / "postgresql_source_store"
)

BASELINE_TABLE_NAMES = frozenset(
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

AUTHENTICATION_TABLE_NAMES = frozenset(
    {
        "user_credentials",
        "web_sessions",
        "totp_credentials",
        "totp_recovery_codes",
        "device_token_families",
        "device_tokens",
        "device_authorization_grants",
        "authentication_throttle_buckets",
    }
)

POLICY_TABLE_NAMES = frozenset(
    {
        "workspace_policy_state",
        "policy_drafts",
        "policy_draft_rules",
        "source_policies",
        "policy_rules",
        "policy_previews",
        "policy_preview_results",
        "policy_evaluations",
        "policy_reconciliation_intents",
        "policy_signing_keys",
        "policy_keysets",
        "policy_keyset_signatures",
    }
)

SMALL_FILE_TABLE_NAMES = frozenset({"small_file_upload_operations"})

LIFECYCLE_TABLE_NAMES = frozenset({"source_locators", "source_tombstones"})

EXPECTED_TABLE_NAMES = (
    BASELINE_TABLE_NAMES
    | AUTHENTICATION_TABLE_NAMES
    | POLICY_TABLE_NAMES
    | SMALL_FILE_TABLE_NAMES
    | LIFECYCLE_TABLE_NAMES
)


class _ScriptedBindResult:
    """Minimal bind result facade for the seeded policy migration replay."""

    def fetchall(self) -> list[Any]:
        return []


class _ScriptedBind:
    """Bind double whose workspace read yields no rows (empty replay database)."""

    def execute(self, statement: object) -> _ScriptedBindResult:
        return _ScriptedBindResult()


class _RecordingAlembicOp:
    """Stub of ``alembic.op`` that records created tables and ignores DDL."""

    def __init__(self) -> None:
        self.created_tables: list[sa.Table] = []
        self._tables_by_name: dict[str, sa.Table] = {}
        self._bind = _ScriptedBind()

    def _table(self, table_name: str) -> sa.Table:
        table = self._tables_by_name.get(table_name)
        assert table is not None, f"operation references unknown table {table_name!r}"
        return table

    def create_table(self, name: str, *args: Any, **kwargs: Any) -> sa.Table:
        schema = kwargs.get("schema")
        table = sa.Table(name, sa.MetaData(), *args, schema=schema)
        self.created_tables.append(table)
        self._tables_by_name[name] = table
        return table

    def add_column(self, table_name: str, column: sa.Column, **kwargs: Any) -> None:
        self._table(table_name).append_column(column)

    def drop_column(self, table_name: str, column_name: str, **kwargs: Any) -> None:
        self._table(table_name).drop_column(column_name)

    def alter_column(self, table_name: str, column_name: str, **kwargs: Any) -> None:
        column = self._table(table_name).columns[column_name]
        nullable = kwargs.get("nullable")
        if isinstance(nullable, bool):
            column.nullable = nullable

    def create_index(self, *args: Any, **kwargs: Any) -> None:
        return None

    def create_foreign_key(self, *args: Any, **kwargs: Any) -> None:
        return None

    def drop_constraint(self, *args: Any, **kwargs: Any) -> None:
        return None

    def create_check_constraint(self, *args: Any, **kwargs: Any) -> None:
        return None

    def execute(self, *args: Any, **kwargs: Any) -> None:
        return None

    def get_context(self) -> None:
        return None

    def get_bind(self) -> _ScriptedBind:
        return self._bind


def _load_migrations() -> list[Any]:
    migrations: list[Any] = []
    matched_paths: list[Path] = []
    for migration_glob in MIGRATION_GLOBS:
        matches = sorted(MIGRATION_DIRECTORY.glob(migration_glob))
        assert len(matches) == 1, (
            f"expected exactly one migration matching {migration_glob}, found {matches}"
        )
        matched_paths.append(matches[0])
    for index, migration_path in enumerate(matched_paths):
        spec = importlib.util.spec_from_file_location(
            f"canonical_migration_{index}", migration_path
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        migrations.append(module)
    return migrations


def _collect_migration_tables() -> dict[str, sa.Table]:
    recorder = _RecordingAlembicOp()
    # Each migration resolves ``op`` from its module globals at call time, so
    # the recorder replays ``upgrade()`` without any database or Alembic
    # context, in revision-chain order.
    for migration in _load_migrations():
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

        actual_field_map = {column.name: _column_signature(column) for column in dml_table.columns}
        expected_field_map = {
            column.name: _column_signature(column) for column in migration_table.columns
        }
        assert expected_field_map.items() == actual_field_map.items(), (
            f"{table_name}: DML columns must exactly cover the migrated columns; "
            f"missing={set(expected_field_map) - set(actual_field_map)} "
            f"extra={set(actual_field_map) - set(expected_field_map)} "
            f"changed={
                {
                    name
                    for name in actual_field_map.keys() & expected_field_map.keys()
                    if actual_field_map[name] != expected_field_map[name]
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


def test_lifecycle_tables_are_counted_by_the_canonical_v3_backup_manifest() -> None:
    assert MANIFEST_CONTRACT == "canonical_core_backup/v3"
    assert {"source_locators", "source_tombstones"} <= set(CANONICAL_COUNT_TABLES)
