"""Schema-qualified SQLAlchemy Core table metadata for DML against the baseline.

The Alembic migrations ``20260813_01`` and ``20260816_01`` are the DDL
authority: they own the schema, columns, constraints, indexes and triggers.
This module is the typed DML representation of exactly the seventeen migrated
tables: identical table names, schema (``knowledge``), column names, column
types, nullability and primary keys, contract-tested against the migration
sources. There is deliberately no ``create_all()`` path and no constraint
duplication: check, unique and foreign key constraints stay owned by the
migrations, while reads and writes address the tables through this metadata.
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

user_credentials: Final[Table] = Table(
    "user_credentials",
    _SOURCE_STORE_METADATA,
    Column("user_id", sa.Uuid(), nullable=False),
    Column("workspace_id", sa.Uuid(), nullable=False),
    Column("password_hash", sa.String(length=255), nullable=False),
    Column("credential_revision", sa.BigInteger(), nullable=False),
    Column("totp_prompt_dismissed_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=True),
    Column("password_changed_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=False),
    Column("created_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=False),
    Column("updated_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=False),
    sa.PrimaryKeyConstraint("user_id", name="pk_user_credentials"),
)

web_sessions: Final[Table] = Table(
    "web_sessions",
    _SOURCE_STORE_METADATA,
    Column("web_session_id", sa.Uuid(), nullable=False),
    Column("user_id", sa.Uuid(), nullable=False),
    Column("workspace_id", sa.Uuid(), nullable=False),
    Column("session_secret_hash", sa.String(length=64), nullable=False),
    Column("csrf_secret_hash", sa.String(length=64), nullable=False),
    Column("state", sa.Text(), nullable=False),
    Column("credential_revision", sa.BigInteger(), nullable=False),
    Column("authentication_method", sa.Text(), nullable=False),
    Column("created_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=False),
    Column("authenticated_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=True),
    Column("reauthenticated_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=True),
    Column("last_seen_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=True),
    Column("idle_expires_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=False),
    Column("absolute_expires_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=False),
    Column("revoked_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=True),
    Column("revocation_reason", sa.String(length=100), nullable=True),
    sa.PrimaryKeyConstraint("web_session_id", name="pk_web_sessions"),
)

totp_credentials: Final[Table] = Table(
    "totp_credentials",
    _SOURCE_STORE_METADATA,
    Column("totp_credential_id", sa.Uuid(), nullable=False),
    Column("user_id", sa.Uuid(), nullable=False),
    Column("workspace_id", sa.Uuid(), nullable=False),
    Column("state", sa.Text(), nullable=False),
    Column("secret_ciphertext", sa.String(length=255), nullable=False),
    Column("secret_nonce", sa.String(length=64), nullable=False),
    Column("key_id", sa.String(length=100), nullable=False),
    Column("algorithm", sa.Text(), nullable=False),
    Column("digits", sa.Integer(), nullable=False),
    Column("period_seconds", sa.Integer(), nullable=False),
    Column("last_accepted_time_step", sa.BigInteger(), nullable=True),
    Column("enrollment_expires_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=True),
    Column("revision", sa.BigInteger(), nullable=False),
    Column("created_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=False),
    Column("activated_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=True),
    Column("replaced_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=True),
    sa.PrimaryKeyConstraint("totp_credential_id", name="pk_totp_credentials"),
)

totp_recovery_codes: Final[Table] = Table(
    "totp_recovery_codes",
    _SOURCE_STORE_METADATA,
    Column("recovery_code_id", sa.Uuid(), nullable=False),
    Column("totp_credential_id", sa.Uuid(), nullable=False),
    Column("user_id", sa.Uuid(), nullable=False),
    Column("workspace_id", sa.Uuid(), nullable=False),
    Column("revision", sa.BigInteger(), nullable=False),
    Column("code_hash", sa.String(length=64), nullable=False),
    Column("created_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=False),
    Column("used_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=True),
    sa.PrimaryKeyConstraint("recovery_code_id", name="pk_totp_recovery_codes"),
)

device_token_families: Final[Table] = Table(
    "device_token_families",
    _SOURCE_STORE_METADATA,
    Column("token_family_id", sa.Uuid(), nullable=False),
    Column("user_id", sa.Uuid(), nullable=False),
    Column("workspace_id", sa.Uuid(), nullable=False),
    Column("device_id", sa.Uuid(), nullable=False),
    Column("state", sa.Text(), nullable=False),
    Column("current_refresh_generation", sa.BigInteger(), nullable=False),
    Column("created_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=False),
    Column("last_refreshed_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=False),
    Column("inactivity_expires_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=False),
    Column("absolute_expires_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=False),
    Column("revoked_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=True),
    Column("revocation_reason", sa.String(length=100), nullable=True),
    sa.PrimaryKeyConstraint("token_family_id", name="pk_device_token_families"),
)

device_tokens: Final[Table] = Table(
    "device_tokens",
    _SOURCE_STORE_METADATA,
    Column("device_token_id", sa.Uuid(), nullable=False),
    Column("token_family_id", sa.Uuid(), nullable=False),
    Column("user_id", sa.Uuid(), nullable=False),
    Column("workspace_id", sa.Uuid(), nullable=False),
    Column("device_id", sa.Uuid(), nullable=False),
    Column("token_kind", sa.Text(), nullable=False),
    Column("generation", sa.BigInteger(), nullable=False),
    Column("secret_hash", sa.String(length=64), nullable=False),
    Column("state", sa.Text(), nullable=False),
    Column("predecessor_token_id", sa.Uuid(), nullable=True),
    Column("successor_token_id", sa.Uuid(), nullable=True),
    Column("rotation_id", sa.Uuid(), nullable=True),
    Column("derivation_key_id", sa.String(length=100), nullable=False),
    Column("issued_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=False),
    Column("expires_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=False),
    Column("rotated_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=True),
    Column("revoked_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=True),
    sa.PrimaryKeyConstraint("device_token_id", name="pk_device_tokens"),
)

device_authorization_grants: Final[Table] = Table(
    "device_authorization_grants",
    _SOURCE_STORE_METADATA,
    Column("grant_id", sa.Uuid(), nullable=False),
    Column("user_code_hash", sa.String(length=64), nullable=False),
    Column("polling_secret_hash", sa.String(length=64), nullable=False),
    Column("client_instance_id", sa.Uuid(), nullable=False),
    Column("claimed_device_id", sa.Uuid(), nullable=True),
    Column("device_name", sa.String(length=80), nullable=False),
    Column("platform_class", sa.Text(), nullable=False),
    Column("platform_name", sa.String(length=64), nullable=False),
    Column("plugin_version", sa.String(length=64), nullable=False),
    Column("requested_scope", sa.Text(), nullable=False),
    Column("state", sa.Text(), nullable=False),
    Column("created_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=False),
    Column("expires_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=False),
    Column("approved_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=True),
    Column("denied_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=True),
    Column("exchanged_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=True),
    Column("approved_by_user_id", sa.Uuid(), nullable=True),
    Column("approved_web_session_id", sa.Uuid(), nullable=True),
    Column("device_id", sa.Uuid(), nullable=True),
    Column("token_family_id", sa.Uuid(), nullable=True),
    Column("initial_access_token_id", sa.Uuid(), nullable=True),
    Column("initial_refresh_token_id", sa.Uuid(), nullable=True),
    Column("derivation_key_id", sa.String(length=100), nullable=True),
    sa.PrimaryKeyConstraint("grant_id", name="pk_device_authorization_grants"),
)

authentication_throttle_buckets: Final[Table] = Table(
    "authentication_throttle_buckets",
    _SOURCE_STORE_METADATA,
    Column("throttle_bucket_id", sa.Uuid(), nullable=False),
    Column("bucket_kind", sa.Text(), nullable=False),
    Column("bucket_hash", sa.String(length=64), nullable=False),
    Column("window_started_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=False),
    Column("failed_attempt_count", sa.Integer(), nullable=False),
    Column("locked_until", _TIMESTAMP_WITH_TIME_ZONE, nullable=True),
    Column("updated_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=False),
    sa.PrimaryKeyConstraint("throttle_bucket_id", name="pk_authentication_throttle_buckets"),
)

#: Single frozen metadata collection owning every DML table.
SOURCE_STORE_METADATA: Final[MetaData] = _SOURCE_STORE_METADATA

#: Immutable name-indexed view of the seventeen migrated tables, keyed by their
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
            user_credentials,
            web_sessions,
            totp_credentials,
            totp_recovery_codes,
            device_token_families,
            device_tokens,
            device_authorization_grants,
            authentication_throttle_buckets,
        )
    }
)
