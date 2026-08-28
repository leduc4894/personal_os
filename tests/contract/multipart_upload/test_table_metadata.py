"""Multipart upload DML metadata contract against the migration DDL authority.

The Alembic migrations ``20260828_01`` (table creation) and ``20260828_03``
(deferred provider identity) are the DDL authority for the multipart upload
schema. This test loads both migration modules, replays their ``upgrade()``
against a recording stub of ``alembic.op`` and compares the resulting two
schema-qualified tables with the typed DML metadata exported by
``postgresql_source_store.tables``: identical table names, schema, column
names, column types and nullability, identical primary keys, constraint
ownership staying with the migration (the DML tables duplicate no unique or
foreign key constraint), and the provider identity columns remaining private
text columns — no presigned URL, URL fragment or signature column exists in
either representation.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import sqlalchemy as sa

from postgresql_source_store.tables import (
    SOURCE_STORE_METADATA,
    SOURCE_STORE_SCHEMA,
    SOURCE_STORE_TABLES,
    multipart_parts,
    multipart_uploads,
)

REPO_ROOT: Path = Path(__file__).resolve().parents[3]
MIGRATION_DIRECTORY = REPO_ROOT / "migrations" / "versions"
MIGRATION_GLOB = "20260828_01*.py"
DEFERRED_IDENTITY_MIGRATION_GLOB = "20260828_03*.py"

MULTIPART_TABLE_NAMES = frozenset({"multipart_uploads", "multipart_parts"})

#: The private provider identity columns of the two tables: exact text values
#: that never render in a repr, log, metric or API schema.
PRIVATE_PROVIDER_COLUMNS = frozenset(
    {
        ("multipart_uploads", "staging_key"),
        ("multipart_uploads", "provider_upload_id"),
        ("multipart_parts", "provider_etag"),
    }
)


class _ScriptedBindResult:
    """Minimal bind result facade: a zero scalar and no seeded rows."""

    def fetchall(self) -> list[Any]:
        return []

    def scalar_one(self) -> int:
        return 0


class _ScriptedBind:
    """Bind double whose downgrade gate counts read zero rows."""

    def execute(self, statement: object) -> _ScriptedBindResult:
        return _ScriptedBindResult()


class _RecordingAlembicOp:
    """Stub of ``alembic.op`` that records created tables and ignores DDL."""

    def __init__(self) -> None:
        self.created_tables: list[sa.Table] = []

    def create_table(self, name: str, *args: Any, **kwargs: Any) -> sa.Table:
        table = sa.Table(name, sa.MetaData(), *args, schema=kwargs.get("schema"))
        self.created_tables.append(table)
        return table

    def create_index(self, *args: Any, **kwargs: Any) -> None:
        return None

    def alter_column(
        self, table_name: str, column_name: str, *, nullable: bool | None = None, **kwargs: Any
    ) -> None:
        if nullable is None:
            return None
        for table in self.created_tables:
            if table.name == table_name and column_name in table.columns:
                table.columns[column_name].nullable = nullable
                return None
        raise AssertionError(
            f"alter_column addressed no recorded column: {table_name}.{column_name}"
        )

    def execute(self, *args: Any, **kwargs: Any) -> None:
        return None

    def get_context(self) -> None:
        return None

    def get_bind(self) -> _ScriptedBind:
        return _ScriptedBind()


def _load_migration_module(label: str, pattern: str) -> Any:
    matches = sorted(MIGRATION_DIRECTORY.glob(pattern))
    assert len(matches) == 1, (
        f"expected exactly one migration matching {pattern}, found {matches}"
    )
    spec = importlib.util.spec_from_file_location(label, matches[0])
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def _collect_migration_tables() -> dict[str, sa.Table]:
    recorder = _RecordingAlembicOp()
    creation = _load_migration_module("multipart_migration", MIGRATION_GLOB)
    creation.op = recorder  # type: ignore[attr-defined]
    creation.upgrade()
    deferred_identity = _load_migration_module(
        "multipart_deferred_identity_migration", DEFERRED_IDENTITY_MIGRATION_GLOB
    )
    deferred_identity.op = recorder  # type: ignore[attr-defined]
    deferred_identity.upgrade()
    return {table.name: table for table in recorder.created_tables}


def _column_signature(column: sa.Column) -> tuple[Any, ...]:
    return (
        column.name,
        type(column.type),
        getattr(column.type, "length", None),
        getattr(column.type, "timezone", None),
        column.nullable,
    )


def test_dml_metadata_covers_every_multipart_column_and_type() -> None:
    migration_tables = _collect_migration_tables()

    assert set(migration_tables) == MULTIPART_TABLE_NAMES
    assert set(SOURCE_STORE_TABLES) >= MULTIPART_TABLE_NAMES

    for table_name in sorted(MULTIPART_TABLE_NAMES):
        dml_table = SOURCE_STORE_TABLES[table_name]
        migration_table = migration_tables[table_name]

        assert dml_table.schema == SOURCE_STORE_SCHEMA, table_name
        assert migration_table.schema == SOURCE_STORE_SCHEMA, table_name
        assert dml_table.metadata is SOURCE_STORE_METADATA, table_name

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


def test_multipart_tables_are_exported_module_constants() -> None:
    assert SOURCE_STORE_TABLES["multipart_uploads"] is multipart_uploads
    assert SOURCE_STORE_TABLES["multipart_parts"] is multipart_parts
    assert multipart_uploads.schema == SOURCE_STORE_SCHEMA
    assert multipart_parts.schema == SOURCE_STORE_SCHEMA


def test_provider_identity_columns_are_private_text() -> None:
    """The provider identity is private text; no URL shape is persisted.

    The exact staging key, provider upload ID and provider ETag are
    database-sensitive fields the store keeps as private text columns. The
    two session identity columns are nullable (migration ``20260828_03``):
    spec 6.1 persists the session before the provider create, so the
    identity lands only through the fenced post-create write; the provider
    ETag is only ever written for a confirmed part and stays mandatory. A
    presigned URL (or any URL, query or signature fragment) is short-lived
    authorization, never durable state, so no such column may exist in
    either the DDL or the DML representation.
    """

    for table_name, column_name in sorted(PRIVATE_PROVIDER_COLUMNS):
        column = SOURCE_STORE_TABLES[table_name].columns[column_name]
        assert isinstance(column.type, sa.Text), (table_name, column_name)
        is_session_identity_column = (table_name, column_name) in frozenset(
            {("multipart_uploads", "staging_key"), ("multipart_uploads", "provider_upload_id")}
        )
        assert column.nullable is is_session_identity_column, (table_name, column_name)

    for table_name in sorted(MULTIPART_TABLE_NAMES):
        for column in SOURCE_STORE_TABLES[table_name].columns:
            for forbidden_fragment in ("url", "presigned", "signature", "query"):
                assert forbidden_fragment not in column.name, (
                    table_name,
                    column.name,
                )


def test_dml_tables_duplicate_no_migration_owned_constraint() -> None:
    """The migration owns uniqueness and containment; DML tables carry PKs only."""

    for table_name in sorted(MULTIPART_TABLE_NAMES):
        for constraint in SOURCE_STORE_TABLES[table_name].constraints:
            assert isinstance(constraint, sa.PrimaryKeyConstraint), (table_name, constraint)
