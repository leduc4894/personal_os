"""Add the durable small-file sync upload-operation table.

Revision ID: 20260818_01
Revises: 20260817_01
Create Date: 2026-08-18

Creates one schema-qualified table inside the existing ``knowledge`` schema:
``small_file_upload_operations``, the durable implementation state of the
authenticated two-step small-file upload (spec 10.1/10.3). Each row binds the
credential-derived workspace and device identity, the journal event identity
and its idempotency key, the operation kind (create/update), the declared
fingerprint (exact SHA-256, byte size at or below the server-owned single-part
ceiling, canonical media type), the accepted policy revision number, the
nullable server-reserved source UUID of a create (reserved without inserting
any ``sources`` row), the nullable update source/base pair, the closed
operation state, the expiry deadline and the frozen terminal canonical result
retained for exact replay. The table deliberately stores no bytes, raw path,
locator, operation token, receipt or provider key: the opaque client token
survives only as its one-way SHA-256 hash column, and every canonical result
field is a plain ID, ordinal or timestamp. Named CHECK constraints close the
vocabularies and shapes (identity grammar, fingerprint bounds, create/update
operand shape, terminal-result/failed biconditionals), a unique constraint
locks the ``(workspace_id, device_id, event_id, idempotency_key)`` identity,
a unique constraint deduplicates token hashes, a partial index serves the
non-terminal expiry sweep, and workspace/device containment is enforced by
two RESTRICT foreign keys.

The downgrade returns exactly to the ``20260817_01`` head. It first counts
the operation rows — including the terminal results that carry the small-file
exact-replay evidence — and refuses with
``small_file_sync_downgrade_requires_explicit_gate`` unless the explicit
``allow_destructive`` x-argument is present; under that gate it drops the
partial index and the table through plain drops only, and finishes
with the catalog assertion (29 tables, 4 functions, 11 triggers).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

import sqlalchemy as sa
from alembic import op
from migrations.database_migration_runtime import allow_destructive_requested

# revision identifiers, used by Alembic.
revision: str = "20260818_01"
down_revision: str | None = "20260817_01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA_NAME: Final[str] = "knowledge"

_TIMESTAMP_TYPE: Final[sa.TIMESTAMP] = sa.TIMESTAMP(timezone=True)

_SHA256_CHECK: Final[str] = r"~ '^[0-9a-f]{64}$'"

_SAFE_ERROR_CODE_CHECK: Final[str] = r"~ '^[a-z][a-z0-9_]{0,99}$'"

#: Canonical lowercase hyphenated UUID grammar of the journal idempotency key.
_IDEMPOTENCY_KEY_CHECK: Final[str] = (
    r"idempotency_key ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'"
)

#: Canonical MIME ``type/subtype`` grammar mirroring the domain media type.
_MEDIA_TYPE_CHECK: Final[str] = (
    r"declared_media_type ~ '^[a-z0-9!#$&^_.+\-]+/[a-z0-9!#$&^_.+\-]+$' "
    "AND octet_length(declared_media_type) <= 255"
)

_NIL_UUID_LITERAL: Final[str] = "'00000000-0000-0000-0000-000000000000'::uuid"

#: Server-owned single-part upload ceiling: exactly 16 MiB (spec 3.1, 10.1).
#: The migration cannot import the domain constant, so the ceiling is stated
#: here once as the DDL authority's own bound.
_MAXIMUM_DECLARED_SIZE_BYTES: Final[int] = 16 * 1024 * 1024

_OPERATION_KIND_CHECK: Final[str] = "operation_kind IN ('create', 'update')"

#: Create carries neither update operand; update carries both and reserves
#: nothing (spec 10.1: the client never mints a canonical source). A create
#: may or may not carry the server-reserved UUID — the reservation is an
#: internal optimization, not a row invariant.
_OPERATION_SHAPE_CHECK: Final[str] = (
    "(operation_kind = 'create') = "
    "(update_source_id IS NULL AND update_base_version_id IS NULL) "
    "AND (operation_kind = 'update') = "
    "(update_source_id IS NOT NULL AND update_base_version_id IS NOT NULL) "
    "AND (operation_kind <> 'update' OR reserved_source_id IS NULL) "
    f"AND (update_source_id IS NULL OR update_source_id <> {_NIL_UUID_LITERAL}) "
    f"AND (update_base_version_id IS NULL OR update_base_version_id <> {_NIL_UUID_LITERAL}) "
    f"AND (reserved_source_id IS NULL OR reserved_source_id <> {_NIL_UUID_LITERAL})"
)

#: Pending/receiving rows carry no terminal evidence at all; a committed row
#: carries the complete frozen result and no error; a failed row carries
#: exactly one closed safe error code.
_TERMINAL_SHAPE_CHECK: Final[str] = (
    "(state IN ('pending', 'receiving')) = "
    "(result_kind IS NULL AND result_source_id IS NULL AND result_source_version_id IS NULL "
    "AND result_content_version IS NULL AND result_committed_at IS NULL "
    "AND safe_error_code IS NULL) "
    "AND (state = 'committed') = "
    "(result_kind IS NOT NULL AND result_source_id IS NOT NULL "
    "AND result_source_version_id IS NOT NULL AND result_content_version IS NOT NULL "
    "AND result_committed_at IS NOT NULL) "
    "AND (state = 'failed') = (safe_error_code IS NOT NULL)"
)

_RESULT_KIND_CHECK: Final[str] = "result_kind IS NULL OR result_kind IN ('committed', 'no_change')"

_STATE_CHECK: Final[str] = "state IN ('pending', 'receiving', 'committed', 'failed')"

_DOWNGRADE_REFUSAL_MESSAGE: Final[str] = "small_file_sync_downgrade_requires_explicit_gate"

_DOWNGRADE_GATE_COUNT_SQL: Final[str] = (
    "SELECT count(*) FROM knowledge.small_file_upload_operations"
)

_FINAL_CATALOG_ASSERTION_SQL: Final[str] = """
DO $$
DECLARE
    application_table_count integer;
    trigger_function_count integer;
    protection_trigger_count integer;
BEGIN
    SELECT count(*) INTO application_table_count
    FROM pg_catalog.pg_tables
    WHERE schemaname = 'knowledge';
    IF application_table_count <> 30 THEN
        RAISE EXCEPTION 'small_file_schema_table_count_invalid';
    END IF;
    SELECT count(*) INTO trigger_function_count
    FROM pg_catalog.pg_proc pgp
    JOIN pg_catalog.pg_namespace pgn ON pgn.oid = pgp.pronamespace
    WHERE pgn.nspname = 'knowledge';
    IF trigger_function_count <> 4 THEN
        RAISE EXCEPTION 'small_file_schema_function_count_invalid';
    END IF;
    SELECT count(*) INTO protection_trigger_count
    FROM pg_catalog.pg_trigger pgt
    JOIN pg_catalog.pg_class pgc ON pgc.oid = pgt.tgrelid
    JOIN pg_catalog.pg_namespace pgn ON pgn.oid = pgc.relnamespace
    WHERE pgn.nspname = 'knowledge' AND NOT pgt.tgisinternal;
    IF protection_trigger_count <> 11 THEN
        RAISE EXCEPTION 'small_file_schema_trigger_count_invalid';
    END IF;
END;
$$
"""

_FINAL_DOWNGRADE_ASSERTION_SQL: Final[str] = """
DO $$
DECLARE
    application_table_count integer;
    trigger_function_count integer;
    protection_trigger_count integer;
BEGIN
    SELECT count(*) INTO application_table_count
    FROM pg_catalog.pg_tables
    WHERE schemaname = 'knowledge';
    IF application_table_count <> 29 THEN
        RAISE EXCEPTION 'small_file_downgrade_table_count_invalid';
    END IF;
    SELECT count(*) INTO trigger_function_count
    FROM pg_catalog.pg_proc pgp
    JOIN pg_catalog.pg_namespace pgn ON pgn.oid = pgp.pronamespace
    WHERE pgn.nspname = 'knowledge';
    IF trigger_function_count <> 4 THEN
        RAISE EXCEPTION 'small_file_downgrade_function_count_invalid';
    END IF;
    SELECT count(*) INTO protection_trigger_count
    FROM pg_catalog.pg_trigger pgt
    JOIN pg_catalog.pg_class pgc ON pgc.oid = pgt.tgrelid
    JOIN pg_catalog.pg_namespace pgn ON pgn.oid = pgc.relnamespace
    WHERE pgn.nspname = 'knowledge' AND NOT pgt.tgisinternal;
    IF protection_trigger_count <> 11 THEN
        RAISE EXCEPTION 'small_file_downgrade_trigger_count_invalid';
    END IF;
END;
$$
"""


def _downgrade_gate_open() -> bool:
    """Report whether the explicit destructive x-argument is present.

    Thin delegate: the ``Config.cmd_opts`` x-argument read moved to the
    shared :func:`migrations.database_migration_runtime.allow_destructive_requested`
    helper so the later locator-lifecycle revision (``20260820_01``) can
    preflight the same ``allow_destructive`` gate before any of its own
    drops commit. Absent command options (every in-process API caller that
    did not opt in) still leave the gate closed.
    """
    migration_context = op.get_context()
    context_config = getattr(migration_context, "config", None)
    return allow_destructive_requested(context_config)


def upgrade() -> None:
    op.create_table(
        "small_file_upload_operations",
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.Column("operation_token_hash", sa.String(length=64), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("device_id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=36), nullable=False),
        sa.Column("operation_kind", sa.Text(), nullable=False),
        sa.Column("declared_sha256", sa.String(length=64), nullable=False),
        sa.Column("declared_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("declared_media_type", sa.String(length=255), nullable=False),
        sa.Column("policy_revision_number", sa.BigInteger(), nullable=False),
        sa.Column("reserved_source_id", sa.Uuid(), nullable=True),
        sa.Column("update_source_id", sa.Uuid(), nullable=True),
        sa.Column("update_base_version_id", sa.Uuid(), nullable=True),
        sa.Column(
            "state",
            sa.Text(),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column("safe_error_code", sa.String(length=100), nullable=True),
        sa.Column("result_kind", sa.Text(), nullable=True),
        sa.Column("result_source_id", sa.Uuid(), nullable=True),
        sa.Column("result_source_version_id", sa.Uuid(), nullable=True),
        sa.Column("result_content_version", sa.BigInteger(), nullable=True),
        sa.Column("result_committed_at", _TIMESTAMP_TYPE, nullable=True),
        sa.Column("expires_at", _TIMESTAMP_TYPE, nullable=False),
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
        sa.PrimaryKeyConstraint("operation_id", name="pk_small_file_upload_operations"),
        sa.UniqueConstraint(
            "workspace_id",
            "device_id",
            "event_id",
            "idempotency_key",
            name="uq_small_file_upload_operations__identity",
        ),
        sa.UniqueConstraint(
            "operation_token_hash", name="uq_small_file_upload_operations__operation_token_hash"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["knowledge.workspaces.workspace_id"],
            name="fk_small_file_upload_operations__workspace",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "device_id"],
            ["knowledge.devices.workspace_id", "knowledge.devices.device_id"],
            name="fk_small_file_upload_operations__device",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            f"event_id <> {_NIL_UUID_LITERAL}", name="ck_small_file_upload_operations__event_id"
        ),
        sa.CheckConstraint(
            _IDEMPOTENCY_KEY_CHECK, name="ck_small_file_upload_operations__idempotency_key"
        ),
        sa.CheckConstraint(
            _OPERATION_KIND_CHECK, name="ck_small_file_upload_operations__operation_kind"
        ),
        sa.CheckConstraint(
            _OPERATION_SHAPE_CHECK, name="ck_small_file_upload_operations__operation_shape"
        ),
        sa.CheckConstraint(
            f"declared_sha256 {_SHA256_CHECK}",
            name="ck_small_file_upload_operations__declared_sha256",
        ),
        sa.CheckConstraint(
            f"declared_size_bytes BETWEEN 0 AND {_MAXIMUM_DECLARED_SIZE_BYTES}",
            name="ck_small_file_upload_operations__declared_size_bytes",
        ),
        sa.CheckConstraint(
            _MEDIA_TYPE_CHECK, name="ck_small_file_upload_operations__declared_media_type"
        ),
        sa.CheckConstraint(
            "policy_revision_number >= 1", name="ck_small_file_upload_operations__policy_revision"
        ),
        sa.CheckConstraint(_STATE_CHECK, name="ck_small_file_upload_operations__state"),
        sa.CheckConstraint(
            _TERMINAL_SHAPE_CHECK, name="ck_small_file_upload_operations__terminal_shape"
        ),
        sa.CheckConstraint(
            _RESULT_KIND_CHECK, name="ck_small_file_upload_operations__result_kind"
        ),
        sa.CheckConstraint(
            "result_content_version IS NULL OR result_content_version >= 1",
            name="ck_small_file_upload_operations__result_content_version",
        ),
        sa.CheckConstraint(
            f"safe_error_code IS NULL OR safe_error_code {_SAFE_ERROR_CODE_CHECK}",
            name="ck_small_file_upload_operations__safe_error_code",
        ),
        sa.CheckConstraint(
            "expires_at > created_at AND updated_at >= created_at",
            name="ck_small_file_upload_operations__timestamps",
        ),
        schema=SCHEMA_NAME,
    )
    op.create_index(
        "ix_small_file_upload_operations__nonterminal_expiry",
        "small_file_upload_operations",
        ["expires_at"],
        schema=SCHEMA_NAME,
        postgresql_where=sa.text("state IN ('pending', 'receiving')"),
    )

    op.execute(sa.text(_FINAL_CATALOG_ASSERTION_SQL))


def downgrade() -> None:
    bind = op.get_bind()
    operation_row_count = int(bind.execute(sa.text(_DOWNGRADE_GATE_COUNT_SQL)).scalar_one())
    if operation_row_count > 0 and not _downgrade_gate_open():
        raise RuntimeError(_DOWNGRADE_REFUSAL_MESSAGE)

    op.drop_index(
        "ix_small_file_upload_operations__nonterminal_expiry",
        table_name="small_file_upload_operations",
        schema=SCHEMA_NAME,
    )
    op.drop_table("small_file_upload_operations", schema=SCHEMA_NAME)

    op.execute(sa.text(_FINAL_DOWNGRADE_ASSERTION_SQL))
