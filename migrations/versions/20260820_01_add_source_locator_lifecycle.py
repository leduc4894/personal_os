"""Add canonical source-locator history and tombstones.

Revision ID: 20260820_01
Revises: 20260818_01
Create Date: 2026-08-20

The migration makes locator history and logical deletion canonical relational
state.  It deliberately creates no locator for sources committed before this
revision: legacy sources remain locator-unknown until a later manifest-based
reconciliation proves their locator.  The lifecycle command implementation
and the first-locator publication write are owned by later tasks.

Downgrade discards lifecycle evidence only under the standard explicit
destructive gate.  It removes lifecycle-only events and their dependent
projection intents before restoring the predecessor event vocabulary; source
rows and versions remain intact.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

import sqlalchemy as sa
from alembic import op

revision: str = "20260820_01"
down_revision: str | None = "20260818_01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA_NAME: Final[str] = "knowledge"
_TIMESTAMP_TYPE: Final[sa.TIMESTAMP] = sa.TIMESTAMP(timezone=True)
_DESTRUCTIVE_X_ARGUMENT: Final[str] = "allow_destructive"
_DOWNGRADE_REFUSAL_MESSAGE: Final[str] = "source_lifecycle_downgrade_requires_explicit_gate"
_LOCATOR_MAXIMUM_BYTES: Final[int] = 4096

_LOCATOR_CHECK: Final[str] = (
    "octet_length({column}) BETWEEN 1 AND 4096 "
    "AND left({column}, 1) <> '/' AND right({column}, 1) <> '/' "
    "AND position('//' in {column}) = 0 AND position(chr(92) in {column}) = 0"
)

_LOCATOR_CLOSURE_CHECK: Final[str] = (
    "((closed_event_id IS NULL AND closed_sequence IS NULL AND closed_at IS NULL) "
    "OR (closed_event_id IS NOT NULL AND closed_sequence IS NOT NULL AND closed_at IS NOT NULL)) "
    "AND (closed_sequence IS NULL OR closed_sequence > opened_sequence) "
    "AND (closed_at IS NULL OR closed_at >= opened_at)"
)

_TOMBSTONE_RESTORE_CHECK: Final[str] = (
    "(restore_event_id IS NULL) = (restored_at IS NULL) "
    "AND (restored_at IS NULL OR restored_at >= deleted_at)"
)

_SYNC_EVENT_TYPE_CHECK: Final[str] = (
    "event_type IN ('create', 'update', 'rename', 'move', 'delete', 'restore')"
)
_PREDECESSOR_SYNC_EVENT_TYPE_CHECK: Final[str] = "event_type IN ('create', 'update')"
_SOURCE_SYNC_STATE_CHECK: Final[str] = (
    "sync_state IN ('pending', 'active', 'stored_not_indexed', 'deleted')"
)
_SOURCE_DELETION_CHECK: Final[str] = (
    "(sync_state = 'deleted') = (deleted_at IS NOT NULL) "
    "AND (deleted_at IS NULL OR deleted_at >= created_at)"
)
_PREDECESSOR_SOURCE_SYNC_STATE_CHECK: Final[str] = (
    "sync_state IN ('pending', 'active', 'stored_not_indexed', 'deleted')"
)
_PREDECESSOR_SOURCE_DELETION_CHECK: Final[str] = (
    "(sync_state = 'deleted') = (deleted_at IS NOT NULL) "
    "AND (deleted_at IS NULL OR deleted_at >= created_at)"
)

_DOWNGRADE_GATE_COUNT_SQL: Final[str] = """
SELECT
    (SELECT count(*) FROM knowledge.source_locators)
    + (SELECT count(*) FROM knowledge.source_tombstones)
    + (
        SELECT count(*) FROM knowledge.sync_events
        WHERE event_type IN ('rename', 'move', 'delete', 'restore')
    )
"""

_LIFECYCLE_EVENT_CLEANUP_SQL: Final[str] = """
DELETE FROM knowledge.projection_intents
WHERE event_id IN (
    SELECT event_id
    FROM knowledge.sync_events
    WHERE event_type IN ('rename', 'move', 'delete', 'restore')
);

DELETE FROM knowledge.sync_events
WHERE event_type IN ('rename', 'move', 'delete', 'restore');
"""

_FINAL_CATALOG_ASSERTION_SQL: Final[str] = """
DO $$
DECLARE
    application_table_count integer;
BEGIN
    SELECT count(*) INTO application_table_count
    FROM pg_catalog.pg_tables
    WHERE schemaname = 'knowledge';
    IF application_table_count <> 32 THEN
        RAISE EXCEPTION 'source_lifecycle_schema_table_count_invalid';
    END IF;
END;
$$
"""

_FINAL_DOWNGRADE_ASSERTION_SQL: Final[str] = """
DO $$
DECLARE
    application_table_count integer;
BEGIN
    SELECT count(*) INTO application_table_count
    FROM pg_catalog.pg_tables
    WHERE schemaname = 'knowledge';
    IF application_table_count <> 30 THEN
        RAISE EXCEPTION 'source_lifecycle_downgrade_table_count_invalid';
    END IF;
END;
$$
"""


def _downgrade_gate_open() -> bool:
    """Return whether the explicit destructive Alembic x-argument is present."""

    migration_context = op.get_context()
    config = getattr(migration_context, "config", None)
    command_options = getattr(config, "cmd_opts", None)
    for argument in getattr(command_options, "x", None) or []:
        key, _, value = str(argument).partition("=")
        if key == _DESTRUCTIVE_X_ARGUMENT and value == "true":
            return True
    return False


def upgrade() -> None:
    """Create lifecycle records and strengthen source-event intent lineage."""

    op.create_table(
        "source_locators",
        sa.Column("source_locator_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("normalized_locator", sa.Text(), nullable=False),
        sa.Column("display_locator", sa.Text(), nullable=False),
        sa.Column("opened_event_id", sa.Uuid(), nullable=False),
        sa.Column("opened_sequence", sa.BigInteger(), nullable=False),
        sa.Column("closed_event_id", sa.Uuid(), nullable=True),
        sa.Column("closed_sequence", sa.BigInteger(), nullable=True),
        sa.Column(
            "opened_at",
            _TIMESTAMP_TYPE,
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("closed_at", _TIMESTAMP_TYPE, nullable=True),
        sa.PrimaryKeyConstraint("source_locator_id", name="pk_source_locators"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["knowledge.workspaces.workspace_id"],
            name="fk_source_locators__workspace",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "source_id"],
            ["knowledge.sources.workspace_id", "knowledge.sources.source_id"],
            name="fk_source_locators__source",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "source_id", "opened_event_id"],
            [
                "knowledge.sync_events.workspace_id",
                "knowledge.sync_events.source_id",
                "knowledge.sync_events.event_id",
            ],
            name="fk_source_locators__opened_event",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "source_id", "closed_event_id"],
            [
                "knowledge.sync_events.workspace_id",
                "knowledge.sync_events.source_id",
                "knowledge.sync_events.event_id",
            ],
            name="fk_source_locators__closed_event",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            _LOCATOR_CHECK.format(column="normalized_locator"),
            name="ck_source_locators__normalized_locator",
        ),
        sa.CheckConstraint(
            _LOCATOR_CHECK.format(column="display_locator"),
            name="ck_source_locators__display_locator",
        ),
        sa.CheckConstraint("opened_sequence >= 1", name="ck_source_locators__opened_sequence"),
        sa.CheckConstraint(_LOCATOR_CLOSURE_CHECK, name="ck_source_locators__closure"),
        schema=SCHEMA_NAME,
    )
    op.create_table(
        "source_tombstones",
        sa.Column("source_tombstone_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("delete_event_id", sa.Uuid(), nullable=False),
        sa.Column("retained_version_id", sa.Uuid(), nullable=False),
        sa.Column("retained_locator", sa.Text(), nullable=False),
        sa.Column("actor_kind", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=False),
        sa.Column(
            "deleted_at",
            _TIMESTAMP_TYPE,
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("restore_event_id", sa.Uuid(), nullable=True),
        sa.Column("restored_at", _TIMESTAMP_TYPE, nullable=True),
        sa.PrimaryKeyConstraint("source_tombstone_id", name="pk_source_tombstones"),
        sa.UniqueConstraint("delete_event_id", name="uq_source_tombstones__delete_event"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["knowledge.workspaces.workspace_id"],
            name="fk_source_tombstones__workspace",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "source_id"],
            ["knowledge.sources.workspace_id", "knowledge.sources.source_id"],
            name="fk_source_tombstones__source",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "source_id", "delete_event_id"],
            [
                "knowledge.sync_events.workspace_id",
                "knowledge.sync_events.source_id",
                "knowledge.sync_events.event_id",
            ],
            name="fk_source_tombstones__delete_event",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "source_id", "retained_version_id"],
            [
                "knowledge.source_versions.workspace_id",
                "knowledge.source_versions.source_id",
                "knowledge.source_versions.source_version_id",
            ],
            name="fk_source_tombstones__retained_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "source_id", "restore_event_id"],
            [
                "knowledge.sync_events.workspace_id",
                "knowledge.sync_events.source_id",
                "knowledge.sync_events.event_id",
            ],
            name="fk_source_tombstones__restore_event",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            _LOCATOR_CHECK.format(column="retained_locator"),
            name="ck_source_tombstones__retained_locator",
        ),
        sa.CheckConstraint(
            "actor_kind IN ('user', 'device', 'approved_action')",
            name="ck_source_tombstones__actor",
        ),
        sa.CheckConstraint(_TOMBSTONE_RESTORE_CHECK, name="ck_source_tombstones__restore"),
        schema=SCHEMA_NAME,
    )
    op.create_index(
        "ix_source_locators__workspace_source_history",
        "source_locators",
        ["workspace_id", "source_id", "opened_sequence", "source_locator_id"],
        schema=SCHEMA_NAME,
    )
    op.create_index(
        "uq_source_locators_active_workspace_path",
        "source_locators",
        ["workspace_id", "normalized_locator"],
        unique=True,
        schema=SCHEMA_NAME,
        postgresql_where=sa.text("closed_event_id IS NULL"),
    )
    op.create_index(
        "uq_source_locators_active_source",
        "source_locators",
        ["source_id"],
        unique=True,
        schema=SCHEMA_NAME,
        postgresql_where=sa.text("closed_event_id IS NULL"),
    )
    op.create_index(
        "uq_source_tombstones_open_source",
        "source_tombstones",
        ["source_id"],
        unique=True,
        schema=SCHEMA_NAME,
        postgresql_where=sa.text("restore_event_id IS NULL"),
    )
    # Task 3: persist the bound initial locator evidence on the durable
    # operation row. ``normalized_locator`` is the transient path the create
    # binds, cleared on terminal transition; ``locator_fingerprint`` is the
    # retained lowercase SHA-256 digest the exact replay compares. Both
    # columns are nullable so pre-migration rows and update operations
    # remain readable.
    op.add_column(
        "small_file_upload_operations",
        sa.Column("normalized_locator", sa.Text(), nullable=True),
        schema=SCHEMA_NAME,
    )
    op.add_column(
        "small_file_upload_operations",
        sa.Column(
            "locator_fingerprint",
            sa.String(length=64),
            nullable=True,
        ),
        schema=SCHEMA_NAME,
    )

    # Older deployed baseline revisions admitted only create/update events.
    # Re-state the lifecycle vocabulary here rather than relying on a mutable
    # historical migration file.  The source checks keep deletion a lifecycle
    # state with a required timestamp, never an exclusion-policy synonym.
    op.drop_constraint(
        "ck_sync_events__event_type", "sync_events", schema=SCHEMA_NAME, type_="check"
    )
    op.create_check_constraint(
        "ck_sync_events__event_type", "sync_events", _SYNC_EVENT_TYPE_CHECK, schema=SCHEMA_NAME
    )
    op.drop_constraint("ck_sources__sync_state", "sources", schema=SCHEMA_NAME, type_="check")
    op.create_check_constraint(
        "ck_sources__sync_state", "sources", _SOURCE_SYNC_STATE_CHECK, schema=SCHEMA_NAME
    )
    op.drop_constraint("ck_sources__deletion", "sources", schema=SCHEMA_NAME, type_="check")
    op.create_check_constraint(
        "ck_sources__deletion", "sources", _SOURCE_DELETION_CHECK, schema=SCHEMA_NAME
    )
    op.drop_constraint(
        "ck_projection_intents__operation_version",
        "projection_intents",
        schema=SCHEMA_NAME,
        type_="check",
    )
    op.create_check_constraint(
        "ck_projection_intents__operation_version",
        "projection_intents",
        "(operation <> 'upsert' OR source_version_id IS NOT NULL) "
        "AND (origin_kind <> 'source_event' OR source_version_id IS NOT NULL)",
        schema=SCHEMA_NAME,
    )
    op.execute(sa.text(_FINAL_CATALOG_ASSERTION_SQL))


def downgrade() -> None:
    """Drop lifecycle history only when its recorded evidence may be discarded."""

    protected_row_count = int(
        op.get_bind().execute(sa.text(_DOWNGRADE_GATE_COUNT_SQL)).scalar_one()
    )
    if protected_row_count > 0 and not _downgrade_gate_open():
        raise RuntimeError(_DOWNGRADE_REFUSAL_MESSAGE)

    op.drop_constraint(
        "ck_projection_intents__operation_version",
        "projection_intents",
        schema=SCHEMA_NAME,
        type_="check",
    )
    op.create_check_constraint(
        "ck_projection_intents__operation_version",
        "projection_intents",
        "operation <> 'upsert' OR source_version_id IS NOT NULL",
        schema=SCHEMA_NAME,
    )
    op.drop_column("locator_fingerprint", "small_file_upload_operations", schema=SCHEMA_NAME)
    op.drop_column("normalized_locator", "small_file_upload_operations", schema=SCHEMA_NAME)
    op.drop_index(
        "uq_source_tombstones_open_source",
        table_name="source_tombstones",
        schema=SCHEMA_NAME,
    )
    op.drop_index(
        "uq_source_locators_active_source",
        table_name="source_locators",
        schema=SCHEMA_NAME,
    )
    op.drop_index(
        "uq_source_locators_active_workspace_path",
        table_name="source_locators",
        schema=SCHEMA_NAME,
    )
    op.drop_index(
        "ix_source_locators__workspace_source_history",
        table_name="source_locators",
        schema=SCHEMA_NAME,
    )
    op.drop_table("source_tombstones", schema=SCHEMA_NAME)
    op.drop_table("source_locators", schema=SCHEMA_NAME)
    op.execute(sa.text(_LIFECYCLE_EVENT_CLEANUP_SQL))
    op.drop_constraint(
        "ck_sync_events__event_type", "sync_events", schema=SCHEMA_NAME, type_="check"
    )
    op.create_check_constraint(
        "ck_sync_events__event_type",
        "sync_events",
        _PREDECESSOR_SYNC_EVENT_TYPE_CHECK,
        schema=SCHEMA_NAME,
    )
    op.drop_constraint("ck_sources__sync_state", "sources", schema=SCHEMA_NAME, type_="check")
    op.create_check_constraint(
        "ck_sources__sync_state",
        "sources",
        _PREDECESSOR_SOURCE_SYNC_STATE_CHECK,
        schema=SCHEMA_NAME,
    )
    op.drop_constraint("ck_sources__deletion", "sources", schema=SCHEMA_NAME, type_="check")
    op.create_check_constraint(
        "ck_sources__deletion",
        "sources",
        _PREDECESSOR_SOURCE_DELETION_CHECK,
        schema=SCHEMA_NAME,
    )
    op.execute(sa.text(_FINAL_DOWNGRADE_ASSERTION_SQL))
