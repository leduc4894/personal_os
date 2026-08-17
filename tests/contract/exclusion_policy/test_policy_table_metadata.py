"""Policy DML Core metadata contract against the policy migration DDL authority.

The Alembic migration ``20260817_01`` is the DDL authority for the exclusion
policy schema. This test replays its ``upgrade()`` against a recording stub of
``alembic.op`` (created tables, added columns, altered nullability, created
check constraints, foreign keys, partial indexes and executed SQL) and compares
the twelve policy tables plus the evolved ``projection_intents`` shape with the
typed DML metadata in ``postgresql_source_store.tables``: identical table
names, schema, column names, column types, nullability and primary keys, with
full coverage in both directions. Structural pins from spec section 8 —
workspace-unique revision numbers, the evaluation identity triple, the
preview-result identity including the exact preview, append-only triggers on
the immutable history tables and the projection-intent origin discriminator —
are asserted against the replayed DDL itself.
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
)

REPO_ROOT = Path(__file__).resolve().parents[3]
MIGRATION_PATH = (
    REPO_ROOT / "migrations" / "versions" / "20260817_01_add_exclusion_policy_publication.py"
)

POLICY_TABLE_NAMES: frozenset[str] = frozenset(
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

#: Foreign-key dependency order of the twelve ``create_table`` calls.
POLICY_TABLE_CREATION_ORDER: tuple[str, ...] = (
    "workspace_policy_state",
    "policy_signing_keys",
    "policy_keysets",
    "policy_keyset_signatures",
    "policy_drafts",
    "policy_draft_rules",
    "policy_previews",
    "source_policies",
    "policy_rules",
    "policy_preview_results",
    "policy_evaluations",
    "policy_reconciliation_intents",
)

#: Published policy artifacts and insert-once evidence: no update path at all.
APPEND_ONLY_POLICY_TABLES: tuple[str, ...] = (
    "source_policies",
    "policy_rules",
    "policy_evaluations",
    "policy_signing_keys",
    "policy_keysets",
    "policy_keyset_signatures",
)


class _ScriptedBindResult:
    """Minimal bind result facade for the seeded-migration replay (no rows)."""

    def fetchall(self) -> list[Any]:
        return []


class _ScriptedBind:
    """Bind double whose workspace read yields no rows (empty replay database)."""

    def execute(self, statement: object) -> _ScriptedBindResult:
        return _ScriptedBindResult()


class _RecordingAlembicOp:
    """Stub of ``alembic.op`` recording every structural upgrade operation."""

    def __init__(self) -> None:
        self.tables: dict[str, sa.Table] = {}
        self.executed_sql: list[str] = []
        self._bind = _ScriptedBind()

    def _table(self, table_name: str) -> sa.Table:
        table = self.tables.get(table_name)
        assert table is not None, f"operation references unknown table {table_name!r}"
        return table

    def create_table(self, name: str, *args: Any, **kwargs: Any) -> sa.Table:
        schema = kwargs.get("schema")
        table = sa.Table(name, sa.MetaData(), *args, schema=schema)
        self.tables[name] = table
        return table

    def create_index(self, index_name: str, table_name: str, columns: Any, **kwargs: Any) -> None:
        table = self._table(table_name)
        column_objects = [
            table.columns[str(column)] if isinstance(column, str) else sa.text(str(column))
            for column in columns
        ]
        sa.Index(
            index_name,
            *column_objects,
            unique=bool(kwargs.get("unique", False)),
            postgresql_where=kwargs.get("postgresql_where"),
        )

    def create_foreign_key(
        self,
        constraint_name: str,
        source_table: str,
        referent_table: str,
        local_columns: Any,
        remote_columns: Any,
        **kwargs: Any,
    ) -> None:
        table = self._table(source_table)
        table.append_constraint(
            sa.ForeignKeyConstraint(
                [str(column) for column in local_columns],
                [f"{referent_table}.{column}" for column in remote_columns],
                name=constraint_name,
                ondelete=kwargs.get("ondelete"),
            )
        )

    def create_check_constraint(
        self, constraint_name: str, table_name: str, condition: str, **kwargs: Any
    ) -> None:
        self._table(table_name).append_constraint(
            sa.CheckConstraint(condition, name=constraint_name)
        )

    def add_column(self, table_name: str, column: sa.Column, **kwargs: Any) -> None:
        self._table(table_name).append_column(column)

    def alter_column(self, table_name: str, column_name: str, **kwargs: Any) -> None:
        column = self._table(table_name).columns[column_name]
        nullable = kwargs.get("nullable")
        if isinstance(nullable, bool):
            column.nullable = nullable

    def drop_table(self, table_name: str, **kwargs: Any) -> None:
        self.tables.pop(table_name, None)

    def drop_column(self, table_name: str, column_name: str, **kwargs: Any) -> None:
        self._table(table_name).columns.remove(self._table(table_name).columns[column_name])

    def drop_index(self, index_name: str, table_name: str, **kwargs: Any) -> None:
        table = self.tables.get(table_name)
        if table is not None:
            for index in list(table.indexes):
                if index.name == index_name:
                    table.indexes.remove(index)

    def drop_constraint(self, constraint_name: str, table_name: str, **kwargs: Any) -> None:
        table = self.tables.get(table_name)
        if table is not None:
            for constraint in list(table.constraints):
                if constraint.name == constraint_name:
                    table.constraints.remove(constraint)

    def execute(self, sql: Any, **kwargs: Any) -> None:
        self.executed_sql.append(str(sql))

    def get_context(self) -> None:
        return None

    def get_bind(self) -> _ScriptedBind:
        return self._bind


def _replay_upgrade() -> _RecordingAlembicOp:
    """Replay the full migration chain so ``projection_intents`` pre-exists."""
    recorder = _RecordingAlembicOp()
    for migration_filename in (
        "20260813_01_create_canonical_postgresql_baseline.py",
        "20260816_01_add_web_authentication_and_device_tokens.py",
        "20260817_01_add_exclusion_policy_publication.py",
    ):
        migration_path = REPO_ROOT / "migrations" / "versions" / migration_filename
        spec = importlib.util.spec_from_file_location(
            f"policy_metadata_replay_{migration_filename[:11]}", migration_path
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.op = recorder  # type: ignore[attr-defined]
        module.upgrade()
    return recorder


def _column_signature(column: sa.Column) -> tuple[Any, ...]:
    return (
        column.name,
        type(column.type),
        getattr(column.type, "length", None),
        getattr(column.type, "timezone", None),
        column.nullable,
    )


def _unique_column_sets(table: sa.Table) -> set[tuple[str, ...]]:
    return {
        tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, sa.UniqueConstraint)
    }


def _check_expressions(table: sa.Table) -> dict[str, str]:
    return {
        constraint.name: str(constraint.sqltext)
        for constraint in table.constraints
        if isinstance(constraint, sa.CheckConstraint) and constraint.name is not None
    }


def _indexes(table: sa.Table) -> dict[str, sa.Index]:
    return {index.name: index for index in table.indexes if index.name is not None}


# --- DML metadata coverage ------------------------------------------------------


def test_policy_migration_creates_the_twelve_tables_in_dependency_order() -> None:
    recorder = _replay_upgrade()
    policy_created = [name for name in recorder.tables if name in POLICY_TABLE_NAMES]
    assert policy_created == list(POLICY_TABLE_CREATION_ORDER)


def test_dml_metadata_covers_every_policy_table_column_and_type() -> None:
    recorder = _replay_upgrade()

    assert set(SOURCE_STORE_TABLES) >= POLICY_TABLE_NAMES
    for table_name in sorted(POLICY_TABLE_NAMES):
        dml_table = SOURCE_STORE_TABLES[table_name]
        migration_table = recorder.tables[table_name]

        assert dml_table.schema == SOURCE_STORE_SCHEMA, table_name
        assert migration_table.schema == SOURCE_STORE_SCHEMA, table_name
        assert dml_table.metadata is SOURCE_STORE_METADATA, table_name

        dml_columns = {column.name: _column_signature(column) for column in dml_table.columns}
        migration_columns = {
            column.name: _column_signature(column) for column in migration_table.columns
        }
        assert dml_columns == migration_columns, table_name

        dml_primary_keys = {column.name for column in dml_table.primary_key.columns}
        migration_primary_keys = {column.name for column in migration_table.primary_key.columns}
        assert dml_primary_keys == migration_primary_keys, table_name


def test_dml_metadata_projection_intents_carries_the_origin_discriminator() -> None:
    recorder = _replay_upgrade()
    dml_table = SOURCE_STORE_TABLES["projection_intents"]
    migration_table = recorder.tables["projection_intents"]

    dml_columns = {column.name: _column_signature(column) for column in dml_table.columns}
    migration_columns = {
        column.name: _column_signature(column) for column in migration_table.columns
    }
    assert dml_columns == migration_columns
    assert dml_columns["origin_kind"][4] is False, "origin_kind must be NOT NULL"
    assert dml_columns["origin_kind"][0] == "origin_kind"
    assert dml_columns["event_id"][4] is True, "event_id must become nullable"
    assert dml_columns["policy_revision_id"][4] is True, "policy_revision_id must be nullable"


# --- structural pins from spec section 8 ----------------------------------------


def test_revision_numbers_are_unique_per_workspace() -> None:
    recorder = _replay_upgrade()
    unique_sets = _unique_column_sets(recorder.tables["source_policies"])
    assert ("workspace_id", "revision_number") in unique_sets
    assert ("workspace_id", "policy_revision_id") in unique_sets
    assert ("workspace_id", "publication_idempotency_key") in unique_sets


def test_one_working_draft_per_workspace_is_unique() -> None:
    recorder = _replay_upgrade()
    unique_sets = _unique_column_sets(recorder.tables["policy_drafts"])
    # Spec 8.2 pins ``workspace_id unique``: exactly one working draft per
    # workspace. The composite (workspace_id, policy_draft_id) unique only
    # serves as the preview foreign-key target and constrains nothing beyond
    # the primary key, so the single-column unique must exist on its own.
    assert ("workspace_id",) in unique_sets


def test_evaluation_identity_is_revision_source_and_sequence() -> None:
    recorder = _replay_upgrade()
    unique_sets = _unique_column_sets(recorder.tables["policy_evaluations"])
    assert ("policy_revision_id", "source_id", "subject_event_sequence") in unique_sets


def test_preview_result_identity_includes_the_exact_preview() -> None:
    recorder = _replay_upgrade()
    primary_keys = {
        column.name for column in recorder.tables["policy_preview_results"].primary_key.columns
    }
    assert primary_keys == {"policy_preview_id", "source_id"}


def test_immutable_policy_history_tables_have_no_update_or_delete_path() -> None:
    recorder = _replay_upgrade()
    upgrade_sql = "\n".join(recorder.executed_sql)
    for table_name in APPEND_ONLY_POLICY_TABLES:
        assert f"BEFORE UPDATE OR DELETE ON knowledge.{table_name}" in upgrade_sql, table_name


def test_projection_intent_origin_check_binds_exactly_one_reference() -> None:
    recorder = _replay_upgrade()
    checks = _check_expressions(recorder.tables["projection_intents"])
    origin_check = checks.get("ck_projection_intents__origin")
    assert origin_check is not None
    assert "(origin_kind = 'source_event') = (event_id IS NOT NULL)" in origin_check
    assert "(origin_kind = 'policy_transition') = (policy_revision_id IS NOT NULL)" in origin_check


def test_typed_operand_columns_back_both_rule_tables() -> None:
    recorder = _replay_upgrade()
    expected_operands = {
        "source_id_operand",
        "text_operand",
        "size_bytes_operand",
        "semantic_fingerprint",
    }
    closed_kinds = {
        "exact_source_id",
        "folder_prefix",
        "path_glob",
        "extension",
        "media_type",
        "maximum_size",
        "source_type",
    }
    for table_name in ("policy_draft_rules", "policy_rules"):
        table = recorder.tables[table_name]
        assert expected_operands <= {column.name for column in table.columns}, table_name
        kind_check = _check_expressions(table).get(f"ck_{table_name}__rule_kind")
        assert kind_check is not None, table_name
        assert {member for member in closed_kinds if f"'{member}'" in kind_check} == closed_kinds


def test_partial_indexes_serve_pending_and_policy_transition_lookups() -> None:
    recorder = _replay_upgrade()
    preview_pending = _indexes(recorder.tables["policy_previews"]).get(
        "ix_policy_previews__pending_dispatch"
    )
    assert preview_pending is not None
    assert not preview_pending.unique
    assert "state = 'pending'" in str(preview_pending.dialect_kwargs["postgresql_where"])

    reconciliation_pending = _indexes(recorder.tables["policy_reconciliation_intents"]).get(
        "ix_policy_reconciliation_intents__pending_dispatch"
    )
    assert reconciliation_pending is not None
    assert not reconciliation_pending.unique
    assert "state = 'pending'" in str(reconciliation_pending.dialect_kwargs["postgresql_where"])

    policy_transition = _indexes(recorder.tables["projection_intents"]).get(
        "uq_projection_intents__policy_transition"
    )
    assert policy_transition is not None
    assert policy_transition.unique
    assert "origin_kind = 'policy_transition'" in str(
        policy_transition.dialect_kwargs["postgresql_where"]
    )
    assert [column.name for column in policy_transition.columns] == [
        "policy_revision_id",
        "source_id",
        "projection_kind",
    ]


def test_every_policy_foreign_key_restricts_deletes() -> None:
    recorder = _replay_upgrade()
    for table in recorder.tables.values():
        if table.name not in POLICY_TABLE_NAMES and table.name != "projection_intents":
            continue
        for constraint in table.constraints:
            if not isinstance(constraint, sa.ForeignKeyConstraint):
                continue
            assert constraint.ondelete == "RESTRICT", (
                f"{table.name}.{constraint.name}: policy foreign keys must restrict deletes"
            )
