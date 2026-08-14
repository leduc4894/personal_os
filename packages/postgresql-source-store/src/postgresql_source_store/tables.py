"""Schema-qualified SQLAlchemy Core table metadata for DML against the baseline.

The Alembic migration ``20260813_01`` is the DDL authority: it owns the
schema, columns, constraints, indexes and triggers. This module is the typed
DML representation of exactly the nine migrated tables: identical table names,
schema (``knowledge``), column names, column types, nullability and primary
keys, contract-tested against the migration source. There is deliberately no
``create_all()`` path and no constraint duplication: check, unique and foreign
key constraints stay owned by the migration, while reads and writes address
the tables through this metadata.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

import sqlalchemy as sa
from sqlalchemy import Column, MetaData, Table

SOURCE_STORE_SCHEMA: Final[str] = "knowledge"

_TIMESTAMP_WITH_TIME_ZONE: Final[sa.TIMESTAMP] = sa.TIMESTAMP(timezone=True)

_SOURCE_STORE_METADATA = MetaData(schema=SOURCE_STORE_SCHEMA)

users: Final[Table] = Table(
    "users",
    _SOURCE_STORE_METADATA,
    Column("user_id", sa.Uuid(), nullable=False),
    Column("username", sa.String(length=64), nullable=False),
    Column("display_name", sa.String(length=200), nullable=False),
    Column("status", sa.Text(), nullable=False),
    Column("created_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=False),
    Column("updated_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=False),
    sa.PrimaryKeyConstraint("user_id", name="pk_users"),
)

workspaces: Final[Table] = Table(
    "workspaces",
    _SOURCE_STORE_METADATA,
    Column("workspace_id", sa.Uuid(), nullable=False),
    Column("owner_user_id", sa.Uuid(), nullable=False),
    Column("workspace_key", sa.String(length=64), nullable=False),
    Column("display_name", sa.String(length=200), nullable=False),
    Column("status", sa.Text(), nullable=False),
    Column("created_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=False),
    Column("updated_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=False),
    sa.PrimaryKeyConstraint("workspace_id", name="pk_workspaces"),
)

devices: Final[Table] = Table(
    "devices",
    _SOURCE_STORE_METADATA,
    Column("device_id", sa.Uuid(), nullable=False),
    Column("workspace_id", sa.Uuid(), nullable=False),
    Column("user_id", sa.Uuid(), nullable=False),
    Column("device_name", sa.String(length=200), nullable=False),
    Column("device_kind", sa.Text(), nullable=False),
    Column("status", sa.Text(), nullable=False),
    Column("registered_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=False),
    Column("last_seen_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=True),
    Column("revoked_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=True),
    sa.PrimaryKeyConstraint("device_id", name="pk_devices"),
)

content_objects: Final[Table] = Table(
    "content_objects",
    _SOURCE_STORE_METADATA,
    Column("content_object_id", sa.Uuid(), nullable=False),
    Column("content_hash", sa.String(length=64), nullable=False),
    Column("object_key", sa.String(length=128), nullable=False),
    Column("byte_size", sa.BigInteger(), nullable=False),
    Column("media_type", sa.String(length=255), nullable=False),
    Column("verified_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=False),
    Column("created_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=False),
    sa.PrimaryKeyConstraint("content_object_id", name="pk_content_objects"),
)

sources: Final[Table] = Table(
    "sources",
    _SOURCE_STORE_METADATA,
    Column("source_id", sa.Uuid(), nullable=False),
    Column("workspace_id", sa.Uuid(), nullable=False),
    Column("source_type", sa.Text(), nullable=False),
    Column("title", sa.String(length=500), nullable=False),
    Column("sync_state", sa.Text(), nullable=False),
    Column("current_version_id", sa.Uuid(), nullable=True),
    Column("created_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=False),
    Column("updated_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=False),
    Column("deleted_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=True),
    sa.PrimaryKeyConstraint("source_id", name="pk_sources"),
)

source_versions: Final[Table] = Table(
    "source_versions",
    _SOURCE_STORE_METADATA,
    Column("source_version_id", sa.Uuid(), nullable=False),
    Column("workspace_id", sa.Uuid(), nullable=False),
    Column("source_id", sa.Uuid(), nullable=False),
    Column("content_object_id", sa.Uuid(), nullable=False),
    Column("content_version", sa.BigInteger(), nullable=False),
    Column("parent_version_id", sa.Uuid(), nullable=True),
    Column("author_kind", sa.Text(), nullable=False),
    Column("author_id", sa.Uuid(), nullable=True),
    Column("client_timestamp", _TIMESTAMP_WITH_TIME_ZONE, nullable=True),
    Column("committed_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=False),
    sa.PrimaryKeyConstraint("source_version_id", name="pk_source_versions"),
)

sync_events: Final[Table] = Table(
    "sync_events",
    _SOURCE_STORE_METADATA,
    Column("event_id", sa.Uuid(), nullable=False),
    Column("workspace_id", sa.Uuid(), nullable=False),
    Column("event_sequence", sa.BigInteger(), nullable=False),
    Column("source_id", sa.Uuid(), nullable=False),
    Column("device_id", sa.Uuid(), nullable=True),
    Column("committed_version_id", sa.Uuid(), nullable=True),
    Column("base_version_id", sa.Uuid(), nullable=True),
    Column("idempotency_key", sa.String(length=200), nullable=False),
    Column("request_fingerprint", sa.String(length=64), nullable=False),
    Column("event_type", sa.Text(), nullable=False),
    Column("client_timestamp", _TIMESTAMP_WITH_TIME_ZONE, nullable=True),
    Column("committed_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=False),
    sa.PrimaryKeyConstraint("event_id", name="pk_sync_events"),
)

projection_intents: Final[Table] = Table(
    "projection_intents",
    _SOURCE_STORE_METADATA,
    Column("projection_intent_id", sa.Uuid(), nullable=False),
    Column("workspace_id", sa.Uuid(), nullable=False),
    Column("event_id", sa.Uuid(), nullable=False),
    Column("source_id", sa.Uuid(), nullable=False),
    Column("source_version_id", sa.Uuid(), nullable=True),
    Column("projection_kind", sa.Text(), nullable=False),
    Column("operation", sa.Text(), nullable=False),
    Column("status", sa.Text(), nullable=False),
    Column("attempt_count", sa.Integer(), nullable=False),
    Column("available_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=False),
    Column("lease_token", sa.Uuid(), nullable=True),
    Column("leased_until", _TIMESTAMP_WITH_TIME_ZONE, nullable=True),
    Column("dispatched_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=True),
    Column("last_error_code", sa.String(length=100), nullable=True),
    Column("created_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=False),
    Column("updated_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=False),
    sa.PrimaryKeyConstraint("projection_intent_id", name="pk_projection_intents"),
)

audit_events: Final[Table] = Table(
    "audit_events",
    _SOURCE_STORE_METADATA,
    Column("audit_event_id", sa.Uuid(), nullable=False),
    Column("workspace_id", sa.Uuid(), nullable=False),
    Column("actor_kind", sa.Text(), nullable=False),
    Column("actor_id", sa.Uuid(), nullable=True),
    Column("actor_reference", sa.String(length=128), nullable=True),
    Column("action", sa.String(length=100), nullable=False),
    Column("target_kind", sa.String(length=100), nullable=False),
    Column("target_id", sa.Uuid(), nullable=True),
    Column("request_id", sa.Uuid(), nullable=False),
    Column("client_request_id", sa.Uuid(), nullable=True),
    Column("trace_id", sa.String(length=32), nullable=True),
    Column("result", sa.Text(), nullable=False),
    Column("reason_code", sa.String(length=100), nullable=True),
    Column("safe_diff_hash", sa.String(length=64), nullable=True),
    Column("occurred_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=False),
    sa.PrimaryKeyConstraint("audit_event_id", name="pk_audit_events"),
)

#: Single frozen metadata collection owning every DML table.
SOURCE_STORE_METADATA: Final[MetaData] = _SOURCE_STORE_METADATA

#: Immutable name-indexed view of the nine migrated tables, keyed by their
#: unqualified table names (``metadata.tables`` itself is schema-qualified).
SOURCE_STORE_TABLES: Final[Mapping[str, Table]] = MappingProxyType(
    {
        table.name: table
        for table in (
            users,
            workspaces,
            devices,
            content_objects,
            sources,
            source_versions,
            sync_events,
            projection_intents,
            audit_events,
        )
    }
)
