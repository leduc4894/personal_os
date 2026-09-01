"""Add the canonical server-authoritative source-conflict aggregate.

Revision ID: 20260902_01
Revises: 20260901_03
Create Date: 2026-09-02

The migration makes conflict evidence canonical relational state (Child 8
spec 4.2): one ``knowledge.source_conflicts`` row holds the immutable
capture evidence — origin event/device, capture idempotency identity, base
and observed-remote versions, the content-versus-delete candidate shape, the
locator snapshot a locator collision requires — plus the closed
capture/resolve state machine and the successor binding of a stale
resolution. Every evidence reference is ``ON DELETE RESTRICT`` so a future
canonical GC can never delete bytes or history an open conflict or successor
still requires; composite foreign keys over ``(workspace_id, source_id,
event_or_version_id)`` stay vacuous for a locator collision that has not
identified a canonical source yet (``MATCH SIMPLE`` skips a NULL member).

The capture and resolution transactions of
:class:`postgresql_source_store.conflict_store.PostgresqlSourceConflictStore`
accept one sync event each, so the closed ``sync_events.event_type``
vocabulary gains the ``conflict_capture`` and ``conflict_resolve`` tokens.
Neither token is a device operation: the device pull page statement filters
them out and the manifest checkpoint-version lateral only reads rows that
commit a version, so no device or manifest consumer ever hydrates them.

Downgrade discards conflict evidence only under the standard explicit
destructive gate: it refuses while any conflict row exists, deletes the
conflict-only sync events, drops the aggregate and restores the predecessor
event vocabulary.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

import sqlalchemy as sa
from alembic import op
from migrations.database_migration_runtime import allow_destructive_requested

revision: str = "20260902_01"
down_revision: str | None = "20260901_03"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA_NAME: Final[str] = "knowledge"
_TIMESTAMP_TYPE: Final[sa.TIMESTAMP] = sa.TIMESTAMP(timezone=True)
_DOWNGRADE_REFUSAL_MESSAGE: Final[str] = "source_conflict_downgrade_requires_explicit_gate"

_UUID_TEXT_GRAMMAR: Final[str] = (
    r"~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'"
)
_NIL_UUID_TEXT: Final[str] = "'00000000-0000-0000-0000-000000000000'"

_SYNC_EVENT_TYPE_CHECK: Final[str] = (
    "event_type IN ('create', 'update', 'rename', 'move', 'delete', 'restore', "
    "'conflict_capture', 'conflict_resolve')"
)
_PREDECESSOR_SYNC_EVENT_TYPE_CHECK: Final[str] = (
    "event_type IN ('create', 'update', 'rename', 'move', 'delete', 'restore')"
)

_CONFLICT_KIND_CHECK: Final[str] = (
    "conflict_kind IN ('stale_content', 'edit_remote_delete', "
    "'delete_remote_edit', 'locator_collision')"
)
_STATUS_CHECK: Final[str] = "status IN ('open', 'resolving', 'resolved', 'superseded')"
_RESOLUTION_KIND_CHECK: Final[str] = (
    "resolution_kind IS NULL OR resolution_kind IN ('keep_remote', 'keep_local', 'save_merged')"
)
_CANDIDATE_KIND_CHECK: Final[str] = "candidate_kind IN ('content', 'delete')"
_CANDIDATE_SHAPE_CHECK: Final[str] = (
    "(candidate_kind = 'content') = (verified_candidate_object_id IS NOT NULL)"
)
_KIND_CANDIDATE_CHECK: Final[str] = (
    "(conflict_kind NOT IN ('stale_content', 'edit_remote_delete') OR candidate_kind = 'content') "
    "AND (conflict_kind <> 'delete_remote_edit' OR candidate_kind = 'delete')"
)
_SOURCE_BINDING_CHECK: Final[str] = "source_id IS NOT NULL OR conflict_kind = 'locator_collision'"
_LOCATOR_SNAPSHOT_CHECK: Final[str] = (
    "conflict_kind <> 'locator_collision' OR normalized_locator IS NOT NULL"
)
_CAPTURE_KEY_GRAMMAR_CHECK: Final[str] = (
    f"capture_idempotency_key {_UUID_TEXT_GRAMMAR} AND capture_idempotency_key <> {_NIL_UUID_TEXT}"
)
_RESOLUTION_KEY_GRAMMAR_CHECK: Final[str] = (
    f"resolution_idempotency_key IS NULL OR (resolution_idempotency_key {_UUID_TEXT_GRAMMAR} "
    f"AND resolution_idempotency_key <> {_NIL_UUID_TEXT})"
)
_OPEN_SHAPE_CHECK: Final[str] = (
    "status <> 'open' OR (resolution_kind IS NULL AND resolution_event_id IS NULL "
    "AND resolution_idempotency_key IS NULL AND resulting_version_id IS NULL "
    "AND successor_conflict_id IS NULL AND closed_at IS NULL)"
)
_RESOLVING_SHAPE_CHECK: Final[str] = (
    "status <> 'resolving' OR (resolution_kind IS NOT NULL "
    "AND resolution_event_id IS NOT NULL AND resolution_idempotency_key IS NOT NULL "
    "AND resulting_version_id IS NULL AND successor_conflict_id IS NULL "
    "AND closed_at IS NULL)"
)
_RESOLVED_SHAPE_CHECK: Final[str] = (
    "status <> 'resolved' OR (resolution_kind IS NOT NULL "
    "AND resolution_event_id IS NOT NULL AND resolution_idempotency_key IS NOT NULL "
    "AND closed_at IS NOT NULL AND successor_conflict_id IS NULL "
    "AND ((resolution_kind = 'keep_remote' AND resulting_version_id IS NULL) "
    "OR (resolution_kind IN ('keep_local', 'save_merged') "
    "AND resulting_version_id IS NOT NULL)))"
)
_SUPERSEDED_SHAPE_CHECK: Final[str] = (
    "status <> 'superseded' OR (resolution_event_id IS NOT NULL "
    "AND resolution_idempotency_key IS NOT NULL AND successor_conflict_id IS NOT NULL "
    "AND closed_at IS NOT NULL AND resulting_version_id IS NULL)"
)
_CLOSURE_TIME_CHECK: Final[str] = "closed_at IS NULL OR closed_at >= captured_at"
_SUCCESSOR_DISTINCT_CHECK: Final[str] = (
    "successor_conflict_id IS NULL OR successor_conflict_id <> conflict_id"
)

_DOWNGRADE_GATE_COUNT_SQL: Final[str] = "SELECT count(*) FROM knowledge.source_conflicts"

_CONFLICT_EVENT_CLEANUP_SQL: Final[str] = (
    "DELETE FROM knowledge.sync_events WHERE event_type IN ('conflict_capture', 'conflict_resolve')"
)

_FINAL_CATALOG_ASSERTION_SQL: Final[str] = """
DO $$
DECLARE
    application_table_count integer;
BEGIN
    SELECT count(*) INTO application_table_count
    FROM pg_catalog.pg_tables
    WHERE schemaname = 'knowledge';
    IF application_table_count <> 40 THEN
        RAISE EXCEPTION 'source_conflict_schema_table_count_invalid';
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
    IF application_table_count <> 39 THEN
        RAISE EXCEPTION 'source_conflict_downgrade_table_count_invalid';
    END IF;
END;
$$
"""


def _downgrade_gate_open() -> bool:
    """Report whether the explicit destructive Alembic x-argument is present.

    Thin delegate over the shared
    :func:`migrations.database_migration_runtime.allow_destructive_requested`
    flag reader, so this revision and every earlier evidence-owning revision
    gate on exactly the same ``allow_destructive`` value.
    """
    migration_context = op.get_context()
    context_config = getattr(migration_context, "config", None)
    return allow_destructive_requested(context_config)


def upgrade() -> None:
    """Create the conflict aggregate and admit the conflict event tokens."""

    op.create_table(
        "source_conflicts",
        sa.Column("conflict_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=True),
        sa.Column("conflict_kind", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Text(),
            server_default=sa.text("'open'"),
            nullable=False,
        ),
        sa.Column("originating_event_id", sa.Uuid(), nullable=False),
        sa.Column("originating_device_id", sa.Uuid(), nullable=False),
        sa.Column("capture_idempotency_key", sa.String(length=36), nullable=False),
        sa.Column("base_version_id", sa.Uuid(), nullable=True),
        sa.Column("observed_remote_version_id", sa.Uuid(), nullable=True),
        sa.Column("candidate_kind", sa.Text(), nullable=False),
        sa.Column("verified_candidate_object_id", sa.Uuid(), nullable=True),
        sa.Column("normalized_locator", sa.Text(), nullable=True),
        sa.Column("resolution_kind", sa.Text(), nullable=True),
        sa.Column("resolution_event_id", sa.Uuid(), nullable=True),
        sa.Column("resolution_idempotency_key", sa.String(length=36), nullable=True),
        sa.Column("resulting_version_id", sa.Uuid(), nullable=True),
        sa.Column("successor_conflict_id", sa.Uuid(), nullable=True),
        sa.Column(
            "captured_at",
            _TIMESTAMP_TYPE,
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("closed_at", _TIMESTAMP_TYPE, nullable=True),
        sa.PrimaryKeyConstraint("conflict_id", name="pk_source_conflicts"),
        sa.UniqueConstraint(
            "workspace_id",
            "capture_idempotency_key",
            name="uq_source_conflicts__capture_idempotency",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "originating_event_id",
            name="uq_source_conflicts__originating_event",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["knowledge.workspaces.workspace_id"],
            name="fk_source_conflicts__workspace",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "source_id"],
            ["knowledge.sources.workspace_id", "knowledge.sources.source_id"],
            name="fk_source_conflicts__source",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "source_id", "originating_event_id"],
            [
                "knowledge.sync_events.workspace_id",
                "knowledge.sync_events.source_id",
                "knowledge.sync_events.event_id",
            ],
            name="fk_source_conflicts__originating_event",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "source_id", "resolution_event_id"],
            [
                "knowledge.sync_events.workspace_id",
                "knowledge.sync_events.source_id",
                "knowledge.sync_events.event_id",
            ],
            name="fk_source_conflicts__resolution_event",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "originating_device_id"],
            ["knowledge.devices.workspace_id", "knowledge.devices.device_id"],
            name="fk_source_conflicts__device",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "source_id", "base_version_id"],
            [
                "knowledge.source_versions.workspace_id",
                "knowledge.source_versions.source_id",
                "knowledge.source_versions.source_version_id",
            ],
            name="fk_source_conflicts__base_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "source_id", "observed_remote_version_id"],
            [
                "knowledge.source_versions.workspace_id",
                "knowledge.source_versions.source_id",
                "knowledge.source_versions.source_version_id",
            ],
            name="fk_source_conflicts__observed_remote_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "source_id", "resulting_version_id"],
            [
                "knowledge.source_versions.workspace_id",
                "knowledge.source_versions.source_id",
                "knowledge.source_versions.source_version_id",
            ],
            name="fk_source_conflicts__resulting_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["verified_candidate_object_id"],
            ["knowledge.content_objects.content_object_id"],
            name="fk_source_conflicts__candidate_object",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["successor_conflict_id"],
            ["knowledge.source_conflicts.conflict_id"],
            name="fk_source_conflicts__successor",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(_CONFLICT_KIND_CHECK, name="ck_source_conflicts__conflict_kind"),
        sa.CheckConstraint(_STATUS_CHECK, name="ck_source_conflicts__status"),
        sa.CheckConstraint(_RESOLUTION_KIND_CHECK, name="ck_source_conflicts__resolution_kind"),
        sa.CheckConstraint(_CANDIDATE_KIND_CHECK, name="ck_source_conflicts__candidate_kind"),
        sa.CheckConstraint(_CANDIDATE_SHAPE_CHECK, name="ck_source_conflicts__candidate_shape"),
        sa.CheckConstraint(_KIND_CANDIDATE_CHECK, name="ck_source_conflicts__kind_candidate"),
        sa.CheckConstraint(_SOURCE_BINDING_CHECK, name="ck_source_conflicts__source_binding"),
        sa.CheckConstraint(_LOCATOR_SNAPSHOT_CHECK, name="ck_source_conflicts__locator_snapshot"),
        sa.CheckConstraint(
            _CAPTURE_KEY_GRAMMAR_CHECK, name="ck_source_conflicts__capture_key_grammar"
        ),
        sa.CheckConstraint(
            _RESOLUTION_KEY_GRAMMAR_CHECK, name="ck_source_conflicts__resolution_key_grammar"
        ),
        sa.CheckConstraint(_OPEN_SHAPE_CHECK, name="ck_source_conflicts__open_shape"),
        sa.CheckConstraint(_RESOLVING_SHAPE_CHECK, name="ck_source_conflicts__resolving_shape"),
        sa.CheckConstraint(_RESOLVED_SHAPE_CHECK, name="ck_source_conflicts__resolved_shape"),
        sa.CheckConstraint(_SUPERSEDED_SHAPE_CHECK, name="ck_source_conflicts__superseded_shape"),
        sa.CheckConstraint(_CLOSURE_TIME_CHECK, name="ck_source_conflicts__closure_time"),
        sa.CheckConstraint(
            _SUCCESSOR_DISTINCT_CHECK, name="ck_source_conflicts__successor_distinct"
        ),
        schema=SCHEMA_NAME,
    )
    op.create_index(
        "ix_source_conflicts__workspace_open_listing",
        "source_conflicts",
        ["workspace_id", "conflict_id"],
        schema=SCHEMA_NAME,
        postgresql_where=sa.text("status = 'open'"),
    )
    op.create_index(
        "ix_source_conflicts__source_history",
        "source_conflicts",
        ["workspace_id", "source_id", "captured_at", "conflict_id"],
        schema=SCHEMA_NAME,
    )
    op.create_index(
        "uq_source_conflicts__resolution_event",
        "source_conflicts",
        ["workspace_id", "resolution_event_id"],
        unique=True,
        schema=SCHEMA_NAME,
        postgresql_where=sa.text("resolution_event_id IS NOT NULL"),
    )
    # The conflict store accepts one sync event per capture and resolution;
    # the device pull page filters these tokens out and the manifest
    # checkpoint lateral reads only version-committing rows, so admitting
    # them here cannot leak an unknown token into device hydration.
    op.drop_constraint(
        "ck_sync_events__event_type", "sync_events", schema=SCHEMA_NAME, type_="check"
    )
    op.create_check_constraint(
        "ck_sync_events__event_type", "sync_events", _SYNC_EVENT_TYPE_CHECK, schema=SCHEMA_NAME
    )
    op.execute(sa.text(_FINAL_CATALOG_ASSERTION_SQL))


def downgrade() -> None:
    """Drop the conflict aggregate only when its evidence may be discarded."""

    bind = op.get_bind()
    conflict_row_count = int(bind.execute(sa.text(_DOWNGRADE_GATE_COUNT_SQL)).scalar_one())
    if conflict_row_count > 0 and not _downgrade_gate_open():
        raise RuntimeError(_DOWNGRADE_REFUSAL_MESSAGE)

    op.drop_index(
        "uq_source_conflicts__resolution_event",
        table_name="source_conflicts",
        schema=SCHEMA_NAME,
    )
    op.drop_index(
        "ix_source_conflicts__source_history",
        table_name="source_conflicts",
        schema=SCHEMA_NAME,
    )
    op.drop_index(
        "ix_source_conflicts__workspace_open_listing",
        table_name="source_conflicts",
        schema=SCHEMA_NAME,
    )
    op.drop_table("source_conflicts", schema=SCHEMA_NAME)
    op.execute(sa.text(_CONFLICT_EVENT_CLEANUP_SQL))
    op.drop_constraint(
        "ck_sync_events__event_type", "sync_events", schema=SCHEMA_NAME, type_="check"
    )
    op.create_check_constraint(
        "ck_sync_events__event_type",
        "sync_events",
        _PREDECESSOR_SYNC_EVENT_TYPE_CHECK,
        schema=SCHEMA_NAME,
    )
    op.execute(sa.text(_FINAL_DOWNGRADE_ASSERTION_SQL))
