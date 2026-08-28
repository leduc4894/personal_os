"""Add the canonical multipart upload session and completed-part tables.

Revision ID: 20260828_01
Revises: 20260827_01
Create Date: 2026-08-28

Creates two schema-qualified tables inside the existing ``knowledge`` schema:
``multipart_uploads``, the canonical durable state of one resumable multipart
staging transfer (spec 4/4.1), and ``multipart_parts``, its completed-part
evidence. Each session row binds the credential-derived workspace and device
identity, the frozen small-file upload operation that owns it, the opaque
public session ID, the declared fingerprint (exact SHA-256, byte size inside
the multipart routing range, canonical media type, optional update base
version) and the accepted policy revision, the exact part geometry, the exact
private staging key and provider upload ID, the closed twelve-state session
vocabulary, the durable completion claim lease, the frozen terminal canonical
result retained for exact replay, and the cleanup obligation state with its
attempt count, next-retry deadline and last closed cleanup reason. Each part
row carries the session reference, the bounded part number, the exact byte
range, the private provider ETag, the verified provider byte count and the
completion timestamp.

The table deliberately stores no bytes, raw path, locator, receipt, object
key or any URL shape: the presigned part URL is short-lived authorization,
never durable state, and the provider identifiers are database-sensitive
private text. Named CHECK constraints close the vocabularies and shapes
(session-ID grammar, fingerprint bounds, geometry consistency and its
equality with the domain constants, state vocabulary, claim-lease
biconditional, terminal-result biconditional, cleanup shapes), a unique
constraint deduplicates public session IDs, a unique constraint locks one
session per frozen operation for the session's whole lifetime (the exact-
replay guarantee: a terminal or cleanup-obligation session is never reused
and no second session may recreate provider work for the same frozen event),
and the per-session part-number uniqueness is unique on
``(multipart_upload_id, part_number)``. Workspace/device/operation
containment is enforced by three RESTRICT foreign keys; part evidence is
restricting on its session. Three indexes serve the owner/status lookup, the
expiry sweep over the states the deadline can still strike, and the cleanup
claim over sessions whose cleanup retry is due.

The downgrade returns exactly to the ``20260827_01`` head. It first counts
the session and part rows — the sessions include the frozen terminal results
that carry the multipart exact-replay evidence — and refuses with
``multipart_downgrade_requires_explicit_gate`` unless the explicit
``allow_destructive`` x-argument is present; under that gate it drops the
three indexes, then the part table, then the session table, and finishes
with the catalog assertion (thirty-seven tables).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260828_01"
down_revision: str | None = "20260827_01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA_NAME: Final[str] = "knowledge"

_TIMESTAMP_TYPE: Final[sa.TIMESTAMP] = sa.TIMESTAMP(timezone=True)

_SHA256_CHECK: Final[str] = r"~ '^[0-9a-f]{64}$'"

_SAFE_ERROR_CODE_CHECK: Final[str] = r"~ '^[a-z][a-z0-9_]{0,99}$'"

#: Canonical MIME ``type/subtype`` grammar mirroring the domain media type.
_MEDIA_TYPE_CHECK: Final[str] = (
    r"declared_media_type ~ '^[a-z0-9!#$&^_.+\-]+/[a-z0-9!#$&^_.+\-]+$' "
    "AND octet_length(declared_media_type) <= 255"
)

_NIL_UUID_LITERAL: Final[str] = "'00000000-0000-0000-0000-000000000000'::uuid"

#: Ordinary multipart part size: exactly 8 MiB (spec 4). The migration cannot
#: import the domain constant, so the bound is stated here once as the DDL
#: authority's own value; the migration test pins the equality with the
#: domain contract constants.
_MULTIPART_PART_SIZE_BYTES: Final[int] = 8 * 1024 * 1024

#: Maximum number of parts one session geometry may declare (spec 4): the
#: 100 MiB product maximum over the 8 MiB ordinary part.
_MAXIMUM_PART_COUNT: Final[int] = 13

#: Exclusive lower bound of the multipart routing range: the server-owned
#: single-part upload ceiling (spec 4). A larger declared size routes here.
_MINIMUM_MULTIPART_SIZE_BYTES: Final[int] = 16 * 1024 * 1024

#: Inclusive upper bound of the multipart routing range: the product upload
#: maximum (spec 4).
_MAXIMUM_UPLOAD_SIZE_BYTES: Final[int] = 100 * 1024 * 1024

#: Opaque public session-ID grammar: printable URL-safe base64url text of 32
#: to 128 characters that never takes the raw hyphenated UUID form.
_SESSION_ID_CHECK: Final[str] = (
    r"session_id ~ '^[A-Za-z0-9_-]{32,128}$' "
    r"AND session_id !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}$'"
)

#: The closed server session vocabulary of the spec 4.2 state machine.
_STATE_CHECK: Final[str] = (
    "state IN ('created', 'uploading', 'completing', 'verifying', 'promoting', "
    "'committed', 'cancelling', 'expired', 'integrity_failed', 'policy_denied', "
    "'cleanup_pending', 'cleaned')"
)

#: The declared geometry is the exact derivation of the declared size: the
#: ordinary part is exactly 8 MiB, at most 13 parts, and the part count is
#: the exact ceiling of the division.
_PART_GEOMETRY_CHECK: Final[str] = (
    f"part_size_bytes = {_MULTIPART_PART_SIZE_BYTES} "
    f"AND part_count BETWEEN 1 AND {_MAXIMUM_PART_COUNT} "
    "AND (part_count - 1) * part_size_bytes < declared_size_bytes "
    "AND declared_size_bytes <= part_count * part_size_bytes"
)

#: The durable completion claim is a token/expiry pair: a half-claim never
#: exists, so a lease loss is always observable as an expired pair.
_CLAIM_LEASE_CHECK: Final[str] = "(claim_token IS NULL) = (claim_expires_at IS NULL)"

#: Only a committed session carries the complete frozen terminal result, and
#: a committed session always carries it (spec 4.2: exact replay of a
#: terminal session receives its frozen safe result).
_TERMINAL_SHAPE_CHECK: Final[str] = (
    "(state = 'committed') = "
    "(result_kind IS NOT NULL AND result_source_id IS NOT NULL "
    "AND result_source_version_id IS NOT NULL AND result_content_version IS NOT NULL "
    "AND result_committed_at IS NOT NULL)"
)

#: The cleanup obligation vocabulary and shapes: the ``cleaned`` terminal
#: state is exactly the succeeded cleanup; only a failed cleanup carries its
#: last closed reason; an untouched session has zero attempts; a next-retry
#: deadline exists exactly while cleanup is unfinished.
_CLEANUP_STATE_CHECK: Final[str] = (
    "cleanup_state IN ('none', 'pending', 'running', 'failed', 'succeeded')"
)

_CLEANUP_SHAPE_CHECK: Final[str] = (
    "(state = 'cleaned') = (cleanup_state = 'succeeded') "
    "AND (cleanup_state = 'failed') = (cleanup_reason_code IS NOT NULL) "
    "AND (cleanup_state = 'none') = (cleanup_attempt_count = 0) "
    "AND (cleanup_next_retry_at IS NULL) = (cleanup_state IN ('none', 'succeeded'))"
)

_RESULT_KIND_CHECK: Final[str] = "result_kind IS NULL OR result_kind IN ('committed', 'no_change')"

#: A completed part is the exact numbered window of its session geometry: the
#: offset follows from the part number over the fixed ordinary part size, and
#: the provider-verified byte count matches the planned window exactly.
_PART_NUMBER_CHECK: Final[str] = f"part_number BETWEEN 1 AND {_MAXIMUM_PART_COUNT}"

_PART_RANGE_CHECK: Final[str] = (
    f"offset_bytes = (part_number - 1) * {_MULTIPART_PART_SIZE_BYTES} "
    f"AND size_bytes BETWEEN 1 AND {_MULTIPART_PART_SIZE_BYTES} "
    "AND verified_size_bytes = size_bytes"
)

#: The states the 24-hour expiry deadline can still strike: the five forward
#: states before their terminal exits.
_EXPIRY_SWEEP_STATES: Final[str] = (
    "state in ('created', 'uploading', 'completing', 'verifying', 'promoting')"
)

#: The cleanup worker claims sessions whose obligation is scheduled or whose
#: retry is due.
_CLEANUP_CLAIM_STATES: Final[str] = "cleanup_state in ('pending', 'failed')"

#: The explicit destructive operator/test gate: the same ``-x`` argument the
#: migration environment already requires for every CLI downgrade.
_DESTRUCTIVE_X_ARGUMENT: Final[str] = "allow_destructive"

_DOWNGRADE_REFUSAL_MESSAGE: Final[str] = "multipart_downgrade_requires_explicit_gate"

_DOWNGRADE_GATE_COUNT_SQL: Final[str] = """
SELECT
    (SELECT count(*) FROM knowledge.multipart_uploads)
    + (SELECT count(*) FROM knowledge.multipart_parts)
"""

_FINAL_CATALOG_ASSERTION_SQL: Final[str] = """
DO $$
DECLARE
    application_table_count integer;
BEGIN
    SELECT count(*) INTO application_table_count
    FROM pg_catalog.pg_tables
    WHERE schemaname = 'knowledge';
    IF application_table_count <> 39 THEN
        RAISE EXCEPTION 'multipart_schema_table_count_invalid';
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
    IF application_table_count <> 37 THEN
        RAISE EXCEPTION 'multipart_downgrade_table_count_invalid';
    END IF;
END;
$$
"""


def _downgrade_gate_open() -> bool:
    """Report whether the explicit destructive x-argument is present.

    Mirrors ``EnvironmentContext.get_x_argument`` resolution without
    importing the environment surface: the ``-x`` values ride on the active
    command options of the Alembic config. Absent command options (every
    in-process API caller that did not opt in) leave the gate closed.
    """

    migration_context = op.get_context()
    context_config = getattr(migration_context, "config", None)
    command_options = getattr(context_config, "cmd_opts", None)
    x_arguments = getattr(command_options, "x", None) or []
    for argument in x_arguments:
        key, _, value = str(argument).partition("=")
        if key == _DESTRUCTIVE_X_ARGUMENT and value == "true":
            return True
    return False


def upgrade() -> None:
    """Create the multipart session state and its completed-part evidence."""

    op.create_table(
        "multipart_uploads",
        sa.Column("multipart_upload_id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.String(length=128), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("device_id", sa.Uuid(), nullable=False),
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.Column("declared_sha256", sa.String(length=64), nullable=False),
        sa.Column("declared_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("declared_media_type", sa.String(length=255), nullable=False),
        sa.Column("base_version_id", sa.Uuid(), nullable=True),
        sa.Column("policy_revision_number", sa.BigInteger(), nullable=False),
        sa.Column("part_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("part_count", sa.Integer(), nullable=False),
        sa.Column("staging_key", sa.Text(), nullable=False),
        sa.Column("provider_upload_id", sa.Text(), nullable=False),
        sa.Column(
            "state",
            sa.Text(),
            server_default=sa.text("'created'"),
            nullable=False,
        ),
        sa.Column("claim_token", sa.Uuid(), nullable=True),
        sa.Column("claim_expires_at", _TIMESTAMP_TYPE, nullable=True),
        sa.Column("result_kind", sa.Text(), nullable=True),
        sa.Column("result_source_id", sa.Uuid(), nullable=True),
        sa.Column("result_source_version_id", sa.Uuid(), nullable=True),
        sa.Column("result_content_version", sa.BigInteger(), nullable=True),
        sa.Column("result_committed_at", _TIMESTAMP_TYPE, nullable=True),
        sa.Column(
            "cleanup_state",
            sa.Text(),
            server_default=sa.text("'none'"),
            nullable=False,
        ),
        sa.Column(
            "cleanup_attempt_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("cleanup_next_retry_at", _TIMESTAMP_TYPE, nullable=True),
        sa.Column("cleanup_reason_code", sa.String(length=100), nullable=True),
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
        sa.PrimaryKeyConstraint("multipart_upload_id", name="pk_multipart_uploads"),
        sa.UniqueConstraint("session_id", name="uq_multipart_uploads__session_id"),
        sa.UniqueConstraint("operation_id", name="uq_multipart_uploads__operation"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["knowledge.workspaces.workspace_id"],
            name="fk_multipart_uploads__workspace",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "device_id"],
            ["knowledge.devices.workspace_id", "knowledge.devices.device_id"],
            name="fk_multipart_uploads__device",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["operation_id"],
            ["knowledge.small_file_upload_operations.operation_id"],
            name="fk_multipart_uploads__operation",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(_SESSION_ID_CHECK, name="ck_multipart_uploads__session_id"),
        sa.CheckConstraint(
            f"declared_sha256 {_SHA256_CHECK}",
            name="ck_multipart_uploads__declared_sha256",
        ),
        sa.CheckConstraint(
            f"declared_size_bytes > {_MINIMUM_MULTIPART_SIZE_BYTES} "
            f"AND declared_size_bytes <= {_MAXIMUM_UPLOAD_SIZE_BYTES}",
            name="ck_multipart_uploads__declared_size_bytes",
        ),
        sa.CheckConstraint(_MEDIA_TYPE_CHECK, name="ck_multipart_uploads__declared_media_type"),
        sa.CheckConstraint(
            f"base_version_id IS NULL OR base_version_id <> {_NIL_UUID_LITERAL}",
            name="ck_multipart_uploads__base_version",
        ),
        sa.CheckConstraint(
            "policy_revision_number >= 1", name="ck_multipart_uploads__policy_revision"
        ),
        sa.CheckConstraint(_PART_GEOMETRY_CHECK, name="ck_multipart_uploads__part_geometry"),
        sa.CheckConstraint(_STATE_CHECK, name="ck_multipart_uploads__state"),
        sa.CheckConstraint(_CLAIM_LEASE_CHECK, name="ck_multipart_uploads__claim_lease"),
        sa.CheckConstraint(_RESULT_KIND_CHECK, name="ck_multipart_uploads__result_kind"),
        sa.CheckConstraint(
            "result_content_version IS NULL OR result_content_version >= 1",
            name="ck_multipart_uploads__result_content_version",
        ),
        sa.CheckConstraint(_TERMINAL_SHAPE_CHECK, name="ck_multipart_uploads__terminal_shape"),
        sa.CheckConstraint(_CLEANUP_STATE_CHECK, name="ck_multipart_uploads__cleanup_state"),
        sa.CheckConstraint(
            f"cleanup_reason_code IS NULL OR cleanup_reason_code {_SAFE_ERROR_CODE_CHECK}",
            name="ck_multipart_uploads__cleanup_reason_code",
        ),
        sa.CheckConstraint(_CLEANUP_SHAPE_CHECK, name="ck_multipart_uploads__cleanup_shape"),
        sa.CheckConstraint(
            "expires_at > created_at AND updated_at >= created_at",
            name="ck_multipart_uploads__timestamps",
        ),
        schema=SCHEMA_NAME,
    )
    op.create_table(
        "multipart_parts",
        sa.Column("multipart_part_id", sa.Uuid(), nullable=False),
        sa.Column("multipart_upload_id", sa.Uuid(), nullable=False),
        sa.Column("part_number", sa.Integer(), nullable=False),
        sa.Column("offset_bytes", sa.BigInteger(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("provider_etag", sa.Text(), nullable=False),
        sa.Column("verified_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("completed_at", _TIMESTAMP_TYPE, nullable=False),
        sa.PrimaryKeyConstraint("multipart_part_id", name="pk_multipart_parts"),
        sa.UniqueConstraint(
            "multipart_upload_id",
            "part_number",
            name="uq_multipart_parts__session_part",
        ),
        sa.ForeignKeyConstraint(
            ["multipart_upload_id"],
            ["knowledge.multipart_uploads.multipart_upload_id"],
            name="fk_multipart_parts__session",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(_PART_NUMBER_CHECK, name="ck_multipart_parts__part_number"),
        sa.CheckConstraint(_PART_RANGE_CHECK, name="ck_multipart_parts__byte_range"),
        schema=SCHEMA_NAME,
    )
    op.create_index(
        "ix_multipart_uploads__workspace_state",
        "multipart_uploads",
        ["workspace_id", "state"],
        schema=SCHEMA_NAME,
    )
    op.create_index(
        "ix_multipart_uploads__expiry_sweep",
        "multipart_uploads",
        ["expires_at"],
        schema=SCHEMA_NAME,
        postgresql_where=sa.text(_EXPIRY_SWEEP_STATES),
    )
    op.create_index(
        "ix_multipart_uploads__cleanup_claim",
        "multipart_uploads",
        ["cleanup_next_retry_at"],
        schema=SCHEMA_NAME,
        postgresql_where=sa.text(_CLEANUP_CLAIM_STATES),
    )

    op.execute(sa.text(_FINAL_CATALOG_ASSERTION_SQL))


def downgrade() -> None:
    """Drop multipart evidence only when its recorded rows may be discarded."""

    bind = op.get_bind()
    protected_row_count = int(bind.execute(sa.text(_DOWNGRADE_GATE_COUNT_SQL)).scalar_one())
    if protected_row_count > 0 and not _downgrade_gate_open():
        raise RuntimeError(_DOWNGRADE_REFUSAL_MESSAGE)

    op.drop_index(
        "ix_multipart_uploads__cleanup_claim",
        table_name="multipart_uploads",
        schema=SCHEMA_NAME,
    )
    op.drop_index(
        "ix_multipart_uploads__expiry_sweep",
        table_name="multipart_uploads",
        schema=SCHEMA_NAME,
    )
    op.drop_index(
        "ix_multipart_uploads__workspace_state",
        table_name="multipart_uploads",
        schema=SCHEMA_NAME,
    )
    op.drop_table("multipart_parts", schema=SCHEMA_NAME)
    op.drop_table("multipart_uploads", schema=SCHEMA_NAME)

    op.execute(sa.text(_FINAL_DOWNGRADE_ASSERTION_SQL))
