"""Create the canonical PostgreSQL baseline schema.

Revision ID: 20260813_01
Revises:
Create Date: 2026-08-13

Creates the ``knowledge`` application schema with the nine baseline tables,
their named constraints and query indexes, the deferrable circular source
current-version pointer, and the database-enforced immutability and
append-only triggers. Every identifier is caller-supplied except
``sync_events.event_sequence``; no seed row, extension, PostgreSQL enum,
JSON column or uncontrolled drop dependency is created.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260813_01"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA_NAME: Final[str] = "knowledge"

_TIMESTAMP_TYPE: Final[sa.TIMESTAMP] = sa.TIMESTAMP(timezone=True)

_SLUG_CHECK: Final[str] = r"~ '^[a-z0-9][a-z0-9._-]{0,63}$'"
_SHA256_CHECK: Final[str] = r"~ '^[0-9a-f]{64}$'"
_SAFE_TOKEN_CHECK: Final[str] = r"~ '^[a-z][a-z0-9_.:-]*$'"

_CREATE_SCHEMA_SQL: Final[str] = "CREATE SCHEMA knowledge AUTHORIZATION knowledge_app"
_REVOKE_SCHEMA_SQL: Final[str] = "REVOKE ALL ON SCHEMA knowledge FROM PUBLIC"

_REJECT_IMMUTABLE_UPDATE_FUNCTION_SQL: Final[str] = """
CREATE FUNCTION knowledge.reject_immutable_update()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    RAISE EXCEPTION USING ERRCODE = '55000', MESSAGE = 'immutable_row_update_rejected';
END;
$$
"""

_REJECT_AUDIT_MUTATION_FUNCTION_SQL: Final[str] = """
CREATE FUNCTION knowledge.reject_audit_mutation()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    RAISE EXCEPTION USING ERRCODE = '55000', MESSAGE = 'audit_events_append_only';
END;
$$
"""

_CONTENT_OBJECTS_TRIGGER_SQL: Final[str] = """
CREATE TRIGGER trg_content_objects__reject_update
BEFORE UPDATE ON knowledge.content_objects
FOR EACH ROW EXECUTE FUNCTION knowledge.reject_immutable_update()
"""

_SOURCE_VERSIONS_TRIGGER_SQL: Final[str] = """
CREATE TRIGGER trg_source_versions__reject_update
BEFORE UPDATE ON knowledge.source_versions
FOR EACH ROW EXECUTE FUNCTION knowledge.reject_immutable_update()
"""

_SYNC_EVENTS_TRIGGER_SQL: Final[str] = """
CREATE TRIGGER trg_sync_events__reject_update
BEFORE UPDATE ON knowledge.sync_events
FOR EACH ROW EXECUTE FUNCTION knowledge.reject_immutable_update()
"""

_AUDIT_EVENTS_TRIGGER_SQL: Final[str] = """
CREATE TRIGGER trg_audit_events__reject_mutation
BEFORE UPDATE OR DELETE ON knowledge.audit_events
FOR EACH ROW EXECUTE FUNCTION knowledge.reject_audit_mutation()
"""

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
    IF application_table_count <> 9 THEN
        RAISE EXCEPTION 'knowledge_baseline_table_count_invalid';
    END IF;
    SELECT count(*) INTO trigger_function_count
    FROM pg_catalog.pg_proc pgp
    JOIN pg_catalog.pg_namespace pgn ON pgn.oid = pgp.pronamespace
    WHERE pgn.nspname = 'knowledge';
    IF trigger_function_count <> 2 THEN
        RAISE EXCEPTION 'knowledge_baseline_function_count_invalid';
    END IF;
    SELECT count(*) INTO protection_trigger_count
    FROM pg_catalog.pg_trigger pgt
    JOIN pg_catalog.pg_class pgc ON pgc.oid = pgt.tgrelid
    JOIN pg_catalog.pg_namespace pgn ON pgn.oid = pgc.relnamespace
    WHERE pgn.nspname = 'knowledge' AND NOT pgt.tgisinternal;
    IF protection_trigger_count <> 4 THEN
        RAISE EXCEPTION 'knowledge_baseline_trigger_count_invalid';
    END IF;
END;
$$
"""


def upgrade() -> None:
    op.execute(sa.text(_CREATE_SCHEMA_SQL))
    op.execute(sa.text(_REVOKE_SCHEMA_SQL))
    op.create_table(
        "users",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column(
            "status",
            sa.Text(),
            server_default=sa.text("'active'"),
            nullable=False,
        ),
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
        sa.PrimaryKeyConstraint("user_id", name="pk_users"),
        sa.UniqueConstraint("username", name="uq_users__username"),
        sa.CheckConstraint(f"username {_SLUG_CHECK}", name="ck_users__username_slug"),
        sa.CheckConstraint(
            "display_name = btrim(display_name) "
            "AND char_length(btrim(display_name)) BETWEEN 1 AND 200",
            name="ck_users__display_name",
        ),
        sa.CheckConstraint("status IN ('active', 'disabled')", name="ck_users__status"),
        sa.CheckConstraint("updated_at >= created_at", name="ck_users__timestamps"),
        schema=SCHEMA_NAME,
    )
    op.create_table(
        "workspaces",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_key", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column(
            "status",
            sa.Text(),
            server_default=sa.text("'active'"),
            nullable=False,
        ),
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
        sa.PrimaryKeyConstraint("workspace_id", name="pk_workspaces"),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["knowledge.users.user_id"],
            name="fk_workspaces__owner_user",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("owner_user_id", name="uq_workspaces__owner_user"),
        sa.UniqueConstraint("workspace_key", name="uq_workspaces__workspace_key"),
        sa.UniqueConstraint("workspace_id", "owner_user_id", name="uq_workspaces__workspace_owner"),
        sa.CheckConstraint(
            f"workspace_key {_SLUG_CHECK}", name="ck_workspaces__workspace_key_slug"
        ),
        sa.CheckConstraint(
            "display_name = btrim(display_name) "
            "AND char_length(btrim(display_name)) BETWEEN 1 AND 200",
            name="ck_workspaces__display_name",
        ),
        sa.CheckConstraint("status IN ('active', 'archived')", name="ck_workspaces__status"),
        sa.CheckConstraint("updated_at >= created_at", name="ck_workspaces__timestamps"),
        schema=SCHEMA_NAME,
    )
    op.create_table(
        "devices",
        sa.Column("device_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("device_name", sa.String(length=200), nullable=False),
        sa.Column("device_kind", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Text(),
            server_default=sa.text("'active'"),
            nullable=False,
        ),
        sa.Column(
            "registered_at",
            _TIMESTAMP_TYPE,
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("last_seen_at", _TIMESTAMP_TYPE, nullable=True),
        sa.Column("revoked_at", _TIMESTAMP_TYPE, nullable=True),
        sa.PrimaryKeyConstraint("device_id", name="pk_devices"),
        sa.UniqueConstraint("workspace_id", "device_id", name="uq_devices__workspace_device"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "user_id"],
            ["knowledge.workspaces.workspace_id", "knowledge.workspaces.owner_user_id"],
            name="fk_devices__workspace_owner",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "device_name = btrim(device_name) "
            "AND char_length(btrim(device_name)) BETWEEN 1 AND 200",
            name="ck_devices__device_name",
        ),
        sa.CheckConstraint(
            "device_kind IN ('obsidian', 'web', 'system')",
            name="ck_devices__device_kind",
        ),
        sa.CheckConstraint("status IN ('active', 'revoked')", name="ck_devices__status"),
        sa.CheckConstraint(
            "last_seen_at IS NULL OR last_seen_at >= registered_at",
            name="ck_devices__last_seen",
        ),
        sa.CheckConstraint(
            "(status = 'revoked') = (revoked_at IS NOT NULL) "
            "AND (revoked_at IS NULL OR revoked_at >= registered_at)",
            name="ck_devices__revocation",
        ),
        schema=SCHEMA_NAME,
    )
    op.create_table(
        "content_objects",
        sa.Column("content_object_id", sa.Uuid(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("object_key", sa.String(length=128), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("media_type", sa.String(length=255), nullable=False),
        sa.Column("verified_at", _TIMESTAMP_TYPE, nullable=False),
        sa.Column(
            "created_at",
            _TIMESTAMP_TYPE,
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("content_object_id", name="pk_content_objects"),
        sa.UniqueConstraint("content_hash", name="uq_content_objects__content_hash"),
        sa.UniqueConstraint("object_key", name="uq_content_objects__object_key"),
        sa.CheckConstraint(
            f"content_hash {_SHA256_CHECK}", name="ck_content_objects__content_hash"
        ),
        sa.CheckConstraint(
            "object_key = 'objects/sha256/' || substr(content_hash, 1, 2) "
            "|| '/' || substr(content_hash, 3, 2) || '/' || content_hash",
            name="ck_content_objects__object_key",
        ),
        sa.CheckConstraint("byte_size >= 0", name="ck_content_objects__byte_size"),
        sa.CheckConstraint(
            r"media_type = lower(btrim(media_type)) "
            r"AND media_type ~ '^[a-z0-9!#$&^_.+\-]+/[a-z0-9!#$&^_.+\-]+$'",
            name="ck_content_objects__media_type",
        ),
        sa.CheckConstraint("verified_at <= created_at", name="ck_content_objects__verification"),
        schema=SCHEMA_NAME,
    )
    op.create_table(
        "sources",
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("source_type", sa.Text(), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column(
            "sync_state",
            sa.Text(),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column("current_version_id", sa.Uuid(), nullable=True),
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
        sa.Column("deleted_at", _TIMESTAMP_TYPE, nullable=True),
        sa.PrimaryKeyConstraint("source_id", name="pk_sources"),
        sa.UniqueConstraint("workspace_id", "source_id", name="uq_sources__workspace_source"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["knowledge.workspaces.workspace_id"],
            name="fk_sources__workspace",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "source_type IN ('markdown', 'text', 'pdf', 'image', 'audio', 'web', 'youtube')",
            name="ck_sources__source_type",
        ),
        sa.CheckConstraint(
            "title = btrim(title) AND char_length(btrim(title)) BETWEEN 1 AND 500",
            name="ck_sources__title",
        ),
        sa.CheckConstraint(
            "sync_state IN ('pending', 'active', 'stored_not_indexed', 'deleted')",
            name="ck_sources__sync_state",
        ),
        sa.CheckConstraint(
            "(sync_state = 'pending') = (current_version_id IS NULL)",
            name="ck_sources__current_pointer",
        ),
        sa.CheckConstraint(
            "(sync_state = 'deleted') = (deleted_at IS NOT NULL) "
            "AND (deleted_at IS NULL OR deleted_at >= created_at)",
            name="ck_sources__deletion",
        ),
        sa.CheckConstraint("updated_at >= created_at", name="ck_sources__timestamps"),
        schema=SCHEMA_NAME,
    )
    op.create_table(
        "source_versions",
        sa.Column("source_version_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("content_object_id", sa.Uuid(), nullable=False),
        sa.Column("content_version", sa.BigInteger(), nullable=False),
        sa.Column("parent_version_id", sa.Uuid(), nullable=True),
        sa.Column("author_kind", sa.Text(), nullable=False),
        sa.Column("author_id", sa.Uuid(), nullable=True),
        sa.Column("client_timestamp", _TIMESTAMP_TYPE, nullable=True),
        sa.Column(
            "committed_at",
            _TIMESTAMP_TYPE,
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("source_version_id", name="pk_source_versions"),
        sa.UniqueConstraint(
            "workspace_id",
            "source_id",
            "source_version_id",
            name="uq_source_versions__workspace_source_version",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "source_id",
            "content_version",
            name="uq_source_versions__source_ordinal",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "source_id"],
            ["knowledge.sources.workspace_id", "knowledge.sources.source_id"],
            name="fk_source_versions__source",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["content_object_id"],
            ["knowledge.content_objects.content_object_id"],
            name="fk_source_versions__content_object",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "source_id", "parent_version_id"],
            [
                "knowledge.source_versions.workspace_id",
                "knowledge.source_versions.source_id",
                "knowledge.source_versions.source_version_id",
            ],
            name="fk_source_versions__parent",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("content_version >= 1", name="ck_source_versions__content_version"),
        sa.CheckConstraint(
            "parent_version_id IS NULL OR parent_version_id <> source_version_id",
            name="ck_source_versions__parent",
        ),
        sa.CheckConstraint(
            "author_kind IN ('user', 'device', 'system', 'approved_action') "
            "AND (author_kind = 'system') = (author_id IS NULL)",
            name="ck_source_versions__author",
        ),
        schema=SCHEMA_NAME,
    )
    op.create_foreign_key(
        "fk_sources__current_version",
        "sources",
        "source_versions",
        ["workspace_id", "source_id", "current_version_id"],
        ["workspace_id", "source_id", "source_version_id"],
        source_schema="knowledge",
        referent_schema="knowledge",
        ondelete="RESTRICT",
        deferrable=True,
        initially="IMMEDIATE",
    )
    op.create_table(
        "sync_events",
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column(
            "event_sequence",
            sa.BigInteger(),
            sa.Identity(always=True),
            nullable=False,
        ),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("device_id", sa.Uuid(), nullable=True),
        sa.Column("committed_version_id", sa.Uuid(), nullable=True),
        sa.Column("base_version_id", sa.Uuid(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("client_timestamp", _TIMESTAMP_TYPE, nullable=True),
        sa.Column(
            "committed_at",
            _TIMESTAMP_TYPE,
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("event_id", name="pk_sync_events"),
        sa.UniqueConstraint("workspace_id", "event_id", name="uq_sync_events__workspace_event"),
        sa.UniqueConstraint(
            "workspace_id",
            "source_id",
            "event_id",
            name="uq_sync_events__source_event",
        ),
        sa.UniqueConstraint("event_sequence", name="uq_sync_events__event_sequence"),
        sa.UniqueConstraint(
            "workspace_id",
            "idempotency_key",
            name="uq_sync_events__idempotency_key",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "source_id"],
            ["knowledge.sources.workspace_id", "knowledge.sources.source_id"],
            name="fk_sync_events__source",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "device_id"],
            ["knowledge.devices.workspace_id", "knowledge.devices.device_id"],
            name="fk_sync_events__device",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "source_id", "committed_version_id"],
            [
                "knowledge.source_versions.workspace_id",
                "knowledge.source_versions.source_id",
                "knowledge.source_versions.source_version_id",
            ],
            name="fk_sync_events__committed_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "source_id", "base_version_id"],
            [
                "knowledge.source_versions.workspace_id",
                "knowledge.source_versions.source_id",
                "knowledge.source_versions.source_version_id",
            ],
            name="fk_sync_events__base_version",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            r"idempotency_key ~ '^[!-~]{1,200}$'",
            name="ck_sync_events__idempotency_key",
        ),
        sa.CheckConstraint(
            f"request_fingerprint {_SHA256_CHECK}",
            name="ck_sync_events__request_fingerprint",
        ),
        sa.CheckConstraint(
            "event_type IN ('create', 'update', 'rename', 'move', 'delete', 'restore')",
            name="ck_sync_events__event_type",
        ),
        schema=SCHEMA_NAME,
    )
    op.create_table(
        "projection_intents",
        sa.Column("projection_intent_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("source_version_id", sa.Uuid(), nullable=True),
        sa.Column("projection_kind", sa.Text(), nullable=False),
        sa.Column("operation", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Text(),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "available_at",
            _TIMESTAMP_TYPE,
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("lease_token", sa.Uuid(), nullable=True),
        sa.Column("leased_until", _TIMESTAMP_TYPE, nullable=True),
        sa.Column("dispatched_at", _TIMESTAMP_TYPE, nullable=True),
        sa.Column("last_error_code", sa.String(length=100), nullable=True),
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
        sa.PrimaryKeyConstraint("projection_intent_id", name="pk_projection_intents"),
        sa.UniqueConstraint(
            "workspace_id",
            "projection_intent_id",
            name="uq_projection_intents__workspace_intent",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "event_id",
            "projection_kind",
            name="uq_projection_intents__event_kind",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "source_id", "event_id"],
            [
                "knowledge.sync_events.workspace_id",
                "knowledge.sync_events.source_id",
                "knowledge.sync_events.event_id",
            ],
            name="fk_projection_intents__event_source",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "source_id"],
            ["knowledge.sources.workspace_id", "knowledge.sources.source_id"],
            name="fk_projection_intents__source",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "source_id", "source_version_id"],
            [
                "knowledge.source_versions.workspace_id",
                "knowledge.source_versions.source_id",
                "knowledge.source_versions.source_version_id",
            ],
            name="fk_projection_intents__source_version",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "projection_kind IN ('qdrant', 'neo4j')",
            name="ck_projection_intents__projection_kind",
        ),
        sa.CheckConstraint(
            "operation IN ('upsert', 'delete')",
            name="ck_projection_intents__operation",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'leased', 'dispatched', 'terminal')",
            name="ck_projection_intents__status",
        ),
        sa.CheckConstraint("attempt_count >= 0", name="ck_projection_intents__attempt_count"),
        sa.CheckConstraint(
            "available_at >= created_at AND updated_at >= created_at",
            name="ck_projection_intents__timestamps",
        ),
        sa.CheckConstraint(
            "operation <> 'upsert' OR source_version_id IS NOT NULL",
            name="ck_projection_intents__operation_version",
        ),
        sa.CheckConstraint(
            "(status = 'leased') = (lease_token IS NOT NULL) "
            "AND (status = 'leased') = (leased_until IS NOT NULL) "
            "AND (status <> 'leased' OR leased_until > updated_at)",
            name="ck_projection_intents__lease",
        ),
        sa.CheckConstraint(
            "(status = 'dispatched') = (dispatched_at IS NOT NULL)",
            name="ck_projection_intents__dispatch",
        ),
        sa.CheckConstraint(
            "status <> 'terminal' OR last_error_code IS NOT NULL",
            name="ck_projection_intents__terminal_error",
        ),
        sa.CheckConstraint(
            r"last_error_code IS NULL OR last_error_code ~ '^[a-z][a-z0-9_]{0,99}$'",
            name="ck_projection_intents__error_code",
        ),
        schema=SCHEMA_NAME,
    )
    op.create_table(
        "audit_events",
        sa.Column("audit_event_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("actor_kind", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=True),
        sa.Column("actor_reference", sa.String(length=128), nullable=True),
        sa.Column("action", sa.String(length=100), nullable=False),
        sa.Column("target_kind", sa.String(length=100), nullable=False),
        sa.Column("target_id", sa.Uuid(), nullable=True),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("client_request_id", sa.Uuid(), nullable=True),
        sa.Column("trace_id", sa.String(length=32), nullable=True),
        sa.Column("result", sa.Text(), nullable=False),
        sa.Column("reason_code", sa.String(length=100), nullable=True),
        sa.Column("safe_diff_hash", sa.String(length=64), nullable=True),
        sa.Column(
            "occurred_at",
            _TIMESTAMP_TYPE,
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("audit_event_id", name="pk_audit_events"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["knowledge.workspaces.workspace_id"],
            name="fk_audit_events__workspace",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "actor_kind IN ('user', 'device', 'system', 'workflow') "
            "AND CASE WHEN actor_kind IN ('user', 'device') "
            "THEN actor_id IS NOT NULL AND actor_reference IS NULL "
            "WHEN actor_kind = 'system' "
            "THEN actor_id IS NULL AND actor_reference IS NULL "
            "ELSE actor_id IS NULL AND actor_reference IS NOT NULL END",
            name="ck_audit_events__actor",
        ),
        sa.CheckConstraint(
            rf"actor_reference IS NULL OR char_length(actor_reference) <= 128 "
            rf"AND actor_reference {_SAFE_TOKEN_CHECK}",
            name="ck_audit_events__actor_reference",
        ),
        sa.CheckConstraint(
            rf"char_length(action) <= 100 AND action {_SAFE_TOKEN_CHECK}",
            name="ck_audit_events__action",
        ),
        sa.CheckConstraint(
            rf"char_length(target_kind) <= 100 AND target_kind {_SAFE_TOKEN_CHECK}",
            name="ck_audit_events__target_kind",
        ),
        sa.CheckConstraint(
            r"trace_id IS NULL OR (trace_id ~ '^[0-9a-f]{32}$' "
            r"AND trace_id <> '00000000000000000000000000000000')",
            name="ck_audit_events__trace_id",
        ),
        sa.CheckConstraint(
            "result IN ('succeeded', 'rejected', 'failed')",
            name="ck_audit_events__result",
        ),
        sa.CheckConstraint(
            rf"reason_code IS NULL OR char_length(reason_code) <= 100 "
            rf"AND reason_code {_SAFE_TOKEN_CHECK}",
            name="ck_audit_events__reason_code",
        ),
        sa.CheckConstraint(
            f"safe_diff_hash IS NULL OR safe_diff_hash {_SHA256_CHECK}",
            name="ck_audit_events__safe_diff_hash",
        ),
        schema=SCHEMA_NAME,
    )
    op.create_index(
        "ix_devices__workspace_user",
        "devices",
        ["workspace_id", "user_id"],
        schema=SCHEMA_NAME,
    )
    op.create_index(
        "ix_devices__workspace_status_registered",
        "devices",
        ["workspace_id", "status", "registered_at", "device_id"],
        schema=SCHEMA_NAME,
    )
    op.create_index(
        "ix_sources__workspace_state_updated",
        "sources",
        ["workspace_id", "sync_state", "updated_at", "source_id"],
        schema=SCHEMA_NAME,
    )
    op.create_index(
        "ix_source_versions__content_object",
        "source_versions",
        ["content_object_id"],
        schema=SCHEMA_NAME,
    )
    op.create_index(
        "ix_source_versions__parent",
        "source_versions",
        ["workspace_id", "source_id", "parent_version_id"],
        schema=SCHEMA_NAME,
    )
    op.create_index(
        "ix_sync_events__source_sequence",
        "sync_events",
        ["workspace_id", "source_id", "event_sequence"],
        schema=SCHEMA_NAME,
    )
    op.create_index(
        "ix_sync_events__device",
        "sync_events",
        ["workspace_id", "device_id"],
        schema=SCHEMA_NAME,
    )
    op.create_index(
        "ix_sync_events__committed_version",
        "sync_events",
        ["workspace_id", "source_id", "committed_version_id"],
        schema=SCHEMA_NAME,
    )
    op.create_index(
        "ix_sync_events__base_version",
        "sync_events",
        ["workspace_id", "source_id", "base_version_id"],
        schema=SCHEMA_NAME,
    )
    op.create_index(
        "ix_projection_intents__event_source",
        "projection_intents",
        ["workspace_id", "source_id", "event_id"],
        schema=SCHEMA_NAME,
    )
    op.create_index(
        "ix_projection_intents__source_version",
        "projection_intents",
        ["workspace_id", "source_id", "source_version_id"],
        schema=SCHEMA_NAME,
    )
    op.create_index(
        "ix_projection_intents__pending_dispatch",
        "projection_intents",
        ["available_at", "created_at", "projection_intent_id"],
        schema=SCHEMA_NAME,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "ix_projection_intents__source_status",
        "projection_intents",
        ["workspace_id", "source_id", "created_at", "projection_intent_id"],
        schema=SCHEMA_NAME,
    )
    op.create_index(
        "ix_audit_events__workspace_occurred",
        "audit_events",
        ["workspace_id", sa.text("occurred_at DESC"), "audit_event_id"],
        schema=SCHEMA_NAME,
    )
    op.create_index(
        "ix_audit_events__target_lineage",
        "audit_events",
        ["workspace_id", "target_kind", "target_id", sa.text("occurred_at DESC")],
        schema=SCHEMA_NAME,
        postgresql_where=sa.text("target_id IS NOT NULL"),
    )
    op.create_index(
        "ix_audit_events__request",
        "audit_events",
        ["workspace_id", "request_id"],
        schema=SCHEMA_NAME,
    )
    op.execute(sa.text(_REJECT_IMMUTABLE_UPDATE_FUNCTION_SQL))
    op.execute(sa.text(_REJECT_AUDIT_MUTATION_FUNCTION_SQL))
    op.execute(sa.text(_CONTENT_OBJECTS_TRIGGER_SQL))
    op.execute(sa.text(_SOURCE_VERSIONS_TRIGGER_SQL))
    op.execute(sa.text(_SYNC_EVENTS_TRIGGER_SQL))
    op.execute(sa.text(_AUDIT_EVENTS_TRIGGER_SQL))
    op.execute(sa.text(_FINAL_CATALOG_ASSERTION_SQL))


def downgrade() -> None:
    op.execute(sa.text("DROP TRIGGER trg_audit_events__reject_mutation ON knowledge.audit_events"))
    op.execute(sa.text("DROP TRIGGER trg_sync_events__reject_update ON knowledge.sync_events"))
    op.execute(
        sa.text("DROP TRIGGER trg_source_versions__reject_update ON knowledge.source_versions")
    )
    op.execute(
        sa.text("DROP TRIGGER trg_content_objects__reject_update ON knowledge.content_objects")
    )
    op.execute(sa.text("DROP FUNCTION knowledge.reject_audit_mutation"))
    op.execute(sa.text("DROP FUNCTION knowledge.reject_immutable_update"))
    op.drop_table("audit_events", schema=SCHEMA_NAME)
    op.drop_table("projection_intents", schema=SCHEMA_NAME)
    op.drop_table("sync_events", schema=SCHEMA_NAME)
    op.drop_constraint(
        "fk_sources__current_version",
        "sources",
        schema=SCHEMA_NAME,
        type_="foreignkey",
    )
    op.drop_table("source_versions", schema=SCHEMA_NAME)
    op.drop_table("sources", schema=SCHEMA_NAME)
    op.drop_table("content_objects", schema=SCHEMA_NAME)
    op.drop_table("devices", schema=SCHEMA_NAME)
    op.drop_table("workspaces", schema=SCHEMA_NAME)
    op.drop_table("users", schema=SCHEMA_NAME)
    op.execute(sa.text(_REVOKE_SCHEMA_SQL))
    op.execute(sa.text("DROP SCHEMA knowledge RESTRICT"))
