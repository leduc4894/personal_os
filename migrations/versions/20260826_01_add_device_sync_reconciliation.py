"""Add canonical device cursors and manifest reconciliation schema.

Revision ID: 20260826_01
Revises: 20260820_01
Create Date: 2026-08-26

The migration makes server-to-device synchronization state canonical
relational state: one frozen cursor watermark row per workspace/device, one
bounded temporary manifest run per device with its contiguous pages, entry
identity resolutions and the immutable deterministic action plan.  Cursor and
run ownership is restricting: workspaces and devices cannot be deleted while
cursor or run evidence references them.  Manifest runs alone cascade: their
pages, resolutions and actions are temporary protocol state whose lifetime is
exactly the run's.

Run and cursor timestamps are database times (``CURRENT_TIMESTAMP``; the run
expiry deadline is created plus exactly one hour).  Delete rules, the
unfinished-run partial uniqueness, the run state vocabulary, the page/run
entry bounds and the per-kind action shapes are DDL invariants here so no
writer shape can bypass them.

Downgrade discards device sync evidence only under the standard explicit
destructive gate; it refuses while any cursor or manifest row still exists.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

import sqlalchemy as sa
from alembic import op

revision: str = "20260826_01"
down_revision: str | None = "20260820_01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA_NAME: Final[str] = "knowledge"
_TIMESTAMP_TYPE: Final[sa.TIMESTAMP] = sa.TIMESTAMP(timezone=True)

_SHA256_CHECK: Final[str] = r"~ '^[0-9a-f]{64}$'"

_SAFE_ERROR_CODE_CHECK: Final[str] = r"~ '^[a-z][a-z0-9_]{0,99}$'"

#: Canonical MIME ``type/subtype`` grammar mirroring the domain media type.
_MEDIA_TYPE_CHECK: Final[str] = (
    "submitted_media_type ~ '^[a-z0-9!#$&^_.+\\-]+/[a-z0-9!#$&^_.+\\-]+$' "
    "AND octet_length(submitted_media_type) <= 255"
)

_LOCAL_ENTRY_ID_CHECK: Final[str] = "octet_length(local_entry_id) BETWEEN 1 AND 256"

#: Maximum cumulative entries one manifest run accepts; the DDL authority's
#: own statement of the domain ``MAX_MANIFEST_RUN_ENTRIES`` bound.
_MAXIMUM_RUN_ENTRY_COUNT: Final[int] = 100_000

#: Maximum entries one manifest page carries; the DDL authority's own
#: statement of the domain ``MAX_MANIFEST_PAGE_ENTRIES`` bound.
_MAXIMUM_PAGE_ENTRY_COUNT: Final[int] = 500

_RUN_STATE_CHECK: Final[str] = (
    "state IN ('collecting', 'planned', 'applying', 'completed', 'expired', 'failed')"
)

#: A failed run records exactly one closed safe error code; a collecting run
#: carries no finalized digest; the planned evidence (digest and database
#: planning time) belongs to the finalized states; completion is the only
#: state with its completion time.
_RUN_STATE_SHAPE_CHECK: Final[str] = (
    "(state = 'failed') = (safe_error_code IS NOT NULL) "
    "AND (state = 'collecting') = (final_digest IS NULL) "
    "AND (state IN ('planned', 'applying', 'completed')) "
    "= (final_digest IS NOT NULL AND planned_at IS NOT NULL) "
    "AND (state = 'completed') = (completed_at IS NOT NULL)"
)

_RESOLUTION_MATCH_KIND_CHECK: Final[str] = (
    "match_kind IN ('current_locator', 'historical_locator_fingerprint', "
    "'open_tombstone_fingerprint', 'unproven')"
)

#: An unproven entry proves nothing; proven version evidence never exists
#: without its proven source.
_RESOLUTION_IDENTITY_SHAPE_CHECK: Final[str] = (
    "(match_kind = 'unproven') = "
    "(resolved_source_id IS NULL AND resolved_source_version_id IS NULL "
    "AND resolved_source_locator_id IS NULL AND resolved_source_tombstone_id IS NULL) "
    "AND (resolved_source_id IS NULL) = (resolved_source_version_id IS NULL)"
)

_ACTION_KIND_CHECK: Final[str] = (
    "action_kind IN ('upload', 'download', 'apply_tombstone', 'conflict', 'no_change', 'excluded')"
)

#: The required and forbidden operands of every action kind: a download is
#: the only canonical-only action (no local entry) and always names its exact
#: canonical source and version; a no-change action names the matching source
#: and version; a tombstone application names its open tombstone and source;
#: conflict and exclusion are the only reasons and always name the local
#: entry; subordinate canonical evidence never exists without its source.
_ACTION_SHAPE_CHECK: Final[str] = (
    "(action_kind = 'download') = (local_entry_id IS NULL) "
    "AND (action_kind IN ('conflict', 'excluded')) = (safe_reason_code IS NOT NULL) "
    "AND (action_kind NOT IN ('download', 'no_change') "
    "OR (source_id IS NOT NULL AND source_version_id IS NOT NULL)) "
    "AND (action_kind <> 'apply_tombstone' OR source_tombstone_id IS NOT NULL) "
    "AND (action_kind <> 'apply_tombstone' OR source_id IS NOT NULL) "
    "AND (source_version_id IS NULL OR source_id IS NOT NULL) "
    "AND (source_locator_id IS NULL OR source_id IS NOT NULL) "
    "AND (source_tombstone_id IS NULL OR source_id IS NOT NULL)"
)

_DESTRUCTIVE_X_ARGUMENT: Final[str] = "allow_destructive"
_DOWNGRADE_REFUSAL_MESSAGE: Final[str] = "device_sync_downgrade_requires_explicit_gate"

_DOWNGRADE_GATE_COUNT_SQL: Final[str] = """
SELECT
    (SELECT count(*) FROM knowledge.device_cursors)
    + (SELECT count(*) FROM knowledge.manifest_runs)
    + (SELECT count(*) FROM knowledge.manifest_pages)
    + (SELECT count(*) FROM knowledge.manifest_entry_resolutions)
    + (SELECT count(*) FROM knowledge.manifest_actions)
"""

_FINAL_CATALOG_ASSERTION_SQL: Final[str] = """
DO $$
DECLARE
    application_table_count integer;
BEGIN
    SELECT count(*) INTO application_table_count
    FROM pg_catalog.pg_tables
    WHERE schemaname = 'knowledge';
    IF application_table_count <> 37 THEN
        RAISE EXCEPTION 'device_sync_schema_table_count_invalid';
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
    IF application_table_count <> 32 THEN
        RAISE EXCEPTION 'device_sync_downgrade_table_count_invalid';
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
    """Create the cursor watermarks and the manifest reconciliation evidence."""

    op.create_table(
        "device_cursors",
        sa.Column("device_cursor_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("device_id", sa.Uuid(), nullable=False),
        sa.Column("acknowledged_sequence", sa.BigInteger(), nullable=False),
        sa.Column("delivered_through_sequence", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            _TIMESTAMP_TYPE,
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            _TIMESTAMP_TYPE,
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("device_cursor_id", name="pk_device_cursors"),
        sa.UniqueConstraint(
            "workspace_id",
            "device_id",
            name="uq_device_cursors_workspace_device",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["knowledge.workspaces.workspace_id"],
            name="fk_device_cursors__workspace",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "device_id"],
            ["knowledge.devices.workspace_id", "knowledge.devices.device_id"],
            name="fk_device_cursors__device",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("acknowledged_sequence >= 0", name="ck_device_cursors__acknowledged"),
        sa.CheckConstraint(
            "delivered_through_sequence >= acknowledged_sequence",
            name="ck_device_cursors_delivery",
        ),
        schema=SCHEMA_NAME,
    )
    op.create_table(
        "manifest_runs",
        sa.Column("manifest_run_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("device_id", sa.Uuid(), nullable=False),
        sa.Column("base_acknowledged_sequence", sa.BigInteger(), nullable=False),
        sa.Column("checkpoint_sequence", sa.BigInteger(), nullable=False),
        sa.Column("policy_revision_number", sa.BigInteger(), nullable=False),
        sa.Column("client_observation_generation", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("next_page_number", sa.Integer(), server_default="0", nullable=False),
        sa.Column("entry_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("final_digest", sa.String(length=64), nullable=True),
        sa.Column("safe_error_code", sa.String(length=100), nullable=True),
        sa.Column(
            "created_at",
            _TIMESTAMP_TYPE,
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "expires_at",
            _TIMESTAMP_TYPE,
            server_default=sa.text("CURRENT_TIMESTAMP + interval '1 hour'"),
            nullable=False,
        ),
        sa.Column("planned_at", _TIMESTAMP_TYPE, nullable=True),
        sa.Column("completed_at", _TIMESTAMP_TYPE, nullable=True),
        sa.PrimaryKeyConstraint("manifest_run_id", name="pk_manifest_runs"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["knowledge.workspaces.workspace_id"],
            name="fk_manifest_runs__workspace",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "device_id"],
            ["knowledge.devices.workspace_id", "knowledge.devices.device_id"],
            name="fk_manifest_runs__device",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(_RUN_STATE_CHECK, name="ck_manifest_runs__state"),
        sa.CheckConstraint(
            "base_acknowledged_sequence >= 0 AND checkpoint_sequence >= base_acknowledged_sequence",
            name="ck_manifest_runs__sequences",
        ),
        sa.CheckConstraint("policy_revision_number >= 1", name="ck_manifest_runs__policy_revision"),
        sa.CheckConstraint(
            "client_observation_generation >= 0",
            name="ck_manifest_runs__observation_generation",
        ),
        sa.CheckConstraint("next_page_number >= 0", name="ck_manifest_runs__page_number"),
        sa.CheckConstraint(
            f"entry_count BETWEEN 0 AND {_MAXIMUM_RUN_ENTRY_COUNT}",
            name="ck_manifest_runs__entry_count",
        ),
        sa.CheckConstraint(
            f"final_digest IS NULL OR final_digest {_SHA256_CHECK}",
            name="ck_manifest_runs__final_digest",
        ),
        sa.CheckConstraint(
            f"safe_error_code IS NULL OR safe_error_code {_SAFE_ERROR_CODE_CHECK}",
            name="ck_manifest_runs__safe_error_code",
        ),
        sa.CheckConstraint(_RUN_STATE_SHAPE_CHECK, name="ck_manifest_runs__state_shape"),
        sa.CheckConstraint("expires_at > created_at", name="ck_manifest_runs__lifetime"),
        schema=SCHEMA_NAME,
    )
    op.create_table(
        "manifest_pages",
        sa.Column("manifest_run_id", sa.Uuid(), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("entry_count", sa.Integer(), nullable=False),
        sa.Column("page_digest", sa.String(length=64), nullable=False),
        sa.Column(
            "received_at",
            _TIMESTAMP_TYPE,
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("manifest_run_id", "page_number", name="pk_manifest_pages"),
        sa.ForeignKeyConstraint(
            ["manifest_run_id"],
            ["knowledge.manifest_runs.manifest_run_id"],
            name="fk_manifest_pages__run",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("page_number >= 0", name="ck_manifest_pages__page_number"),
        sa.CheckConstraint(
            f"entry_count BETWEEN 0 AND {_MAXIMUM_PAGE_ENTRY_COUNT}",
            name="ck_manifest_pages__entry_count",
        ),
        sa.CheckConstraint(f"page_digest {_SHA256_CHECK}", name="ck_manifest_pages__page_digest"),
        schema=SCHEMA_NAME,
    )
    op.create_table(
        "manifest_entry_resolutions",
        sa.Column("manifest_run_id", sa.Uuid(), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("entry_index", sa.Integer(), nullable=False),
        sa.Column("local_entry_id", sa.String(length=256), nullable=False),
        sa.Column("known_source_id", sa.Uuid(), nullable=True),
        sa.Column("known_version_id", sa.Uuid(), nullable=True),
        sa.Column("submitted_sha256", sa.String(length=64), nullable=False),
        sa.Column("submitted_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("submitted_media_type", sa.String(length=255), nullable=False),
        sa.Column("locator_evidence_digest", sa.String(length=64), nullable=False),
        sa.Column("resolved_source_id", sa.Uuid(), nullable=True),
        sa.Column("resolved_source_version_id", sa.Uuid(), nullable=True),
        sa.Column("resolved_source_locator_id", sa.Uuid(), nullable=True),
        sa.Column("resolved_source_tombstone_id", sa.Uuid(), nullable=True),
        sa.Column("match_kind", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint(
            "manifest_run_id",
            "page_number",
            "entry_index",
            name="pk_manifest_entry_resolutions",
        ),
        sa.ForeignKeyConstraint(
            ["manifest_run_id"],
            ["knowledge.manifest_runs.manifest_run_id"],
            name="fk_manifest_entry_resolutions__run",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("entry_index >= 0", name="ck_manifest_entry_resolutions__entry_index"),
        sa.CheckConstraint(
            _LOCAL_ENTRY_ID_CHECK, name="ck_manifest_entry_resolutions__local_entry_id"
        ),
        sa.CheckConstraint(
            f"submitted_sha256 {_SHA256_CHECK}",
            name="ck_manifest_entry_resolutions__submitted_sha256",
        ),
        sa.CheckConstraint(
            "submitted_size_bytes >= 0",
            name="ck_manifest_entry_resolutions__submitted_size_bytes",
        ),
        sa.CheckConstraint(
            _MEDIA_TYPE_CHECK, name="ck_manifest_entry_resolutions__submitted_media_type"
        ),
        sa.CheckConstraint(
            f"locator_evidence_digest {_SHA256_CHECK}",
            name="ck_manifest_entry_resolutions__locator_evidence_digest",
        ),
        sa.CheckConstraint(
            _RESOLUTION_MATCH_KIND_CHECK,
            name="ck_manifest_entry_resolutions__match_kind",
        ),
        sa.CheckConstraint(
            _RESOLUTION_IDENTITY_SHAPE_CHECK,
            name="ck_manifest_entry_resolutions__identity_shape",
        ),
        schema=SCHEMA_NAME,
    )
    op.create_table(
        "manifest_actions",
        sa.Column("manifest_run_id", sa.Uuid(), nullable=False),
        sa.Column("action_index", sa.Integer(), nullable=False),
        sa.Column("action_kind", sa.Text(), nullable=False),
        sa.Column("local_entry_id", sa.String(length=256), nullable=True),
        sa.Column("source_id", sa.Uuid(), nullable=True),
        sa.Column("source_version_id", sa.Uuid(), nullable=True),
        sa.Column("source_locator_id", sa.Uuid(), nullable=True),
        sa.Column("source_tombstone_id", sa.Uuid(), nullable=True),
        sa.Column("safe_reason_code", sa.String(length=100), nullable=True),
        sa.PrimaryKeyConstraint("manifest_run_id", "action_index", name="pk_manifest_actions"),
        sa.ForeignKeyConstraint(
            ["manifest_run_id"],
            ["knowledge.manifest_runs.manifest_run_id"],
            name="fk_manifest_actions__run",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("action_index >= 0", name="ck_manifest_actions__action_index"),
        sa.CheckConstraint(_ACTION_KIND_CHECK, name="ck_manifest_actions__action_kind"),
        sa.CheckConstraint(
            f"local_entry_id IS NULL OR {_LOCAL_ENTRY_ID_CHECK}",
            name="ck_manifest_actions__local_entry_id",
        ),
        sa.CheckConstraint(
            f"safe_reason_code IS NULL OR safe_reason_code {_SAFE_ERROR_CODE_CHECK}",
            name="ck_manifest_actions__safe_reason_code",
        ),
        sa.CheckConstraint(_ACTION_SHAPE_CHECK, name="ck_manifest_actions__shape"),
        schema=SCHEMA_NAME,
    )
    op.create_index(
        "ix_manifest_runs__workspace_device",
        "manifest_runs",
        ["workspace_id", "device_id"],
        schema=SCHEMA_NAME,
    )
    op.create_index(
        "uq_manifest_runs_unfinished_device",
        "manifest_runs",
        ["workspace_id", "device_id"],
        unique=True,
        schema=SCHEMA_NAME,
        postgresql_where=sa.text("state in ('collecting', 'planned', 'applying')"),
    )
    op.execute(sa.text(_FINAL_CATALOG_ASSERTION_SQL))


def downgrade() -> None:
    """Drop device sync evidence only when its recorded rows may be discarded."""

    protected_row_count = int(
        op.get_bind().execute(sa.text(_DOWNGRADE_GATE_COUNT_SQL)).scalar_one()
    )
    if protected_row_count > 0 and not _downgrade_gate_open():
        raise RuntimeError(_DOWNGRADE_REFUSAL_MESSAGE)

    op.drop_index(
        "uq_manifest_runs_unfinished_device",
        table_name="manifest_runs",
        schema=SCHEMA_NAME,
    )
    op.drop_index(
        "ix_manifest_runs__workspace_device",
        table_name="manifest_runs",
        schema=SCHEMA_NAME,
    )
    op.drop_table("manifest_actions", schema=SCHEMA_NAME)
    op.drop_table("manifest_entry_resolutions", schema=SCHEMA_NAME)
    op.drop_table("manifest_pages", schema=SCHEMA_NAME)
    op.drop_table("manifest_runs", schema=SCHEMA_NAME)
    op.drop_table("device_cursors", schema=SCHEMA_NAME)
    op.execute(sa.text(_FINAL_DOWNGRADE_ASSERTION_SQL))
