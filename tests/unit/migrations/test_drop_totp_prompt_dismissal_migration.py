"""Upgrade/downgrade contract for dropping the retired dismissal timestamp.

These tests replay the ``20260902_02`` upgrade and downgrade against a
recording stub of ``alembic.op`` (never a database) and read the revision
source. They pin the revision chain over the source-conflict head, the
upgrade that drops ``ck_user_credentials__timestamps``, drops
``totp_prompt_dismissed_at`` and rebuilds the reduced named CHECK, the
downgrade that reverses that exact order and restores the nullable
``TIMESTAMP WITH TIME ZONE`` column with the original clause permitting a
null restored value, the DML-metadata alignment that no longer declares the
retired column, and the data-free migration hygiene: no statement ever
selects, copies or writes credential rows, so no secret can transit the
migration.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from alembic.config import Config
from alembic.script import ScriptDirectory

from postgresql_source_store.tables import SOURCE_STORE_TABLES

REPO_ROOT = Path(__file__).resolve().parents[3]
ALEMBIC_INI_PATH = REPO_ROOT / "alembic.ini"
MIGRATION_PATH = (
    REPO_ROOT / "migrations" / "versions" / "20260902_02_drop_totp_prompt_dismissal.py"
)

TOTP_PROMPT_DISMISSAL_REVISION: str = "20260902_02"
SOURCE_CONFLICTS_REVISION: str = "20260902_01"

CREDENTIALS_TABLE_NAME: str = "user_credentials"
RETIRED_COLUMN_NAME: str = "totp_prompt_dismissed_at"
TIMESTAMPS_CHECK_NAME: str = "ck_user_credentials__timestamps"
SCHEMA_NAME: str = "knowledge"

#: The reduced timestamp invariant at the new head, exactly as the retirement
#: plan's Produces clause pins it.
REDUCED_TIMESTAMPS_CHECK: str = "updated_at >= created_at AND password_changed_at >= created_at"

#: The clause the ``20260816_01`` authentication revision originally wrote;
#: the downgrade restores it verbatim, so a null restored dismissal timestamp
#: (the only value the downgrade can reintroduce) still satisfies it.
ORIGINAL_TIMESTAMPS_CHECK: str = (
    "updated_at >= created_at "
    "AND password_changed_at >= created_at "
    "AND (totp_prompt_dismissed_at IS NULL OR totp_prompt_dismissed_at >= created_at)"
)


class _EventRecordingAlembicOp:
    """Stub of ``alembic.op`` recording every operation as an ordered event."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, str]] = []

    def drop_constraint(self, constraint_name: str, table_name: str, **kwargs: Any) -> None:
        self.events.append(("drop_constraint", constraint_name, table_name))

    def drop_column(self, table_name: str, column_name: str, **kwargs: Any) -> None:
        self.events.append(("drop_column", column_name, table_name))

    def add_column(self, table_name: str, column: sa.Column, **kwargs: Any) -> None:
        self.events.append(("add_column", column.name, table_name))

    def create_check_constraint(
        self, constraint_name: str, table_name: str, condition: str, **kwargs: Any
    ) -> None:
        self.events.append(("create_check_constraint", constraint_name, condition))

    def execute(self, sql: object, **kwargs: Any) -> None:
        self.events.append(("execute", str(getattr(sql, "text", sql)), ""))


def _load_revision_module() -> Any:
    assert MIGRATION_PATH.is_file(), f"missing retirement migration: {MIGRATION_PATH.name}"
    spec = importlib.util.spec_from_file_location(
        "totp_prompt_dismissal_retirement", MIGRATION_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _replay(function_name: str) -> _EventRecordingAlembicOp:
    module = _load_revision_module()
    recorder = _EventRecordingAlembicOp()
    module.op = recorder  # type: ignore[attr-defined]
    getattr(module, function_name)()
    return recorder


def _script_directory() -> ScriptDirectory:
    return ScriptDirectory.from_config(Config(str(ALEMBIC_INI_PATH)))


def _migration_source() -> str:
    return MIGRATION_PATH.read_text(encoding="utf-8")


def test_retirement_revision_stacks_on_the_source_conflict_head() -> None:
    scripts = _script_directory()
    assert scripts.get_heads() == [TOTP_PROMPT_DISMISSAL_REVISION]
    revision = scripts.get_revision(TOTP_PROMPT_DISMISSAL_REVISION)
    assert revision is not None
    assert revision.down_revision == SOURCE_CONFLICTS_REVISION


def test_canonical_schema_revision_points_at_the_new_head() -> None:
    # The dismissal-retirement revision ``20260902_02`` is the head now.
    from personal_os.database_schema import CANONICAL_POSTGRESQL_SCHEMA_REVISION

    assert CANONICAL_POSTGRESQL_SCHEMA_REVISION == TOTP_PROMPT_DISMISSAL_REVISION


def test_upgrade_drops_the_check_then_the_column_then_rebuilds_the_reduced_check() -> None:
    recorder = _replay("upgrade")
    assert recorder.events == [
        ("drop_constraint", TIMESTAMPS_CHECK_NAME, CREDENTIALS_TABLE_NAME),
        ("drop_column", RETIRED_COLUMN_NAME, CREDENTIALS_TABLE_NAME),
        ("create_check_constraint", TIMESTAMPS_CHECK_NAME, REDUCED_TIMESTAMPS_CHECK),
    ]


def test_upgrade_targets_the_knowledge_schema_with_the_check_type() -> None:
    """The constraint drop must target the CHECK inside ``knowledge``."""

    module = _load_revision_module()
    captured: list[tuple[str, dict[str, Any]]] = []

    class _SchemaRecordingAlembicOp(_EventRecordingAlembicOp):
        def drop_constraint(self, constraint_name: str, table_name: str, **kwargs: Any) -> None:
            captured.append((constraint_name, kwargs))
            super().drop_constraint(constraint_name, table_name, **kwargs)

        def drop_column(self, table_name: str, column_name: str, **kwargs: Any) -> None:
            captured.append((column_name, kwargs))
            super().drop_column(table_name, column_name, **kwargs)

        def create_check_constraint(
            self, constraint_name: str, table_name: str, condition: str, **kwargs: Any
        ) -> None:
            captured.append((constraint_name, kwargs))
            super().create_check_constraint(constraint_name, table_name, condition, **kwargs)

    module.op = _SchemaRecordingAlembicOp()  # type: ignore[attr-defined]
    module.upgrade()
    assert captured, "the upgrade must call alembic.op"
    for name, kwargs in captured:
        assert kwargs.get("schema") == SCHEMA_NAME, name
    timestamp_constraint_drops = [
        kwargs for name, kwargs in captured if name == TIMESTAMPS_CHECK_NAME and "type_" in kwargs
    ]
    assert len(timestamp_constraint_drops) == 1
    assert timestamp_constraint_drops[0].get("type_") == "check"


def test_downgrade_reverses_the_upgrade_in_the_exact_order() -> None:
    recorder = _replay("downgrade")
    assert recorder.events == [
        ("drop_constraint", TIMESTAMPS_CHECK_NAME, CREDENTIALS_TABLE_NAME),
        ("add_column", RETIRED_COLUMN_NAME, CREDENTIALS_TABLE_NAME),
        ("create_check_constraint", TIMESTAMPS_CHECK_NAME, ORIGINAL_TIMESTAMPS_CHECK),
    ]


def test_downgrade_restores_a_nullable_timestamptz_column() -> None:
    module = _load_revision_module()
    added_columns: list[tuple[str, sa.Column, dict[str, Any]]] = []

    class _ColumnRecordingAlembicOp(_EventRecordingAlembicOp):
        def add_column(self, table_name: str, column: sa.Column, **kwargs: Any) -> None:
            added_columns.append((table_name, column, kwargs))
            super().add_column(table_name, column, **kwargs)

    module.op = _ColumnRecordingAlembicOp()  # type: ignore[attr-defined]
    module.downgrade()
    assert [(table, column.name) for table, column, _ in added_columns] == [
        (CREDENTIALS_TABLE_NAME, RETIRED_COLUMN_NAME)
    ]
    _, column, kwargs = added_columns[0]
    assert isinstance(column.type, sa.TIMESTAMP)
    assert column.type.timezone is True
    assert column.nullable is True
    assert kwargs.get("schema") == SCHEMA_NAME


def test_downgrade_clause_permits_a_null_restored_value() -> None:
    """The restored CHECK must accept the only value the downgrade can
    reintroduce: SQL NULL on every pre-existing credential row."""

    recorder = _replay("downgrade")
    restored_check = next(
        condition
        for operation, name, condition in recorder.events
        if operation == "create_check_constraint" and name == TIMESTAMPS_CHECK_NAME
    )
    assert "totp_prompt_dismissed_at IS NULL OR totp_prompt_dismissed_at >= created_at" in (
        restored_check
    )


def test_dml_metadata_no_longer_declares_the_retired_column() -> None:
    credentials = SOURCE_STORE_TABLES[CREDENTIALS_TABLE_NAME]
    assert RETIRED_COLUMN_NAME not in {column.name for column in credentials.columns}


def test_migration_hygiene_rules_hold() -> None:
    source = _migration_source()
    lowered = source.lower()
    assert "personal_os" not in source
    assert "gen_random_uuid" not in lowered
    assert "uuid_generate" not in lowered
    assert "jsonb" not in lowered
    assert "create extension" not in lowered


def test_migration_touches_no_credential_data() -> None:
    """Dropping a timestamp column never reads or writes credential rows.

    The revision may not select, copy or log credential data — least of all a
    TOTP secret — so both directions must issue constraint and column DDL
    only, never a raw ``execute`` statement.
    """
    for function_name in ("upgrade", "downgrade"):
        recorder = _replay(function_name)
        assert all(operation != "execute" for operation, _, _ in recorder.events), function_name
