"""Schema-qualified SQLAlchemy Core table metadata for DML against the baseline.

The Alembic migrations ``20260813_01``, ``20260816_01``, ``20260817_01``,
``20260818_01``, ``20260820_01``, ``20260826_01`` and ``20260828_01`` are the DDL
authority: they own the schema, columns, constraints, indexes and triggers. This
module is the typed DML representation of exactly the thirty-nine migrated tables:
identical table names, schema (``knowledge``), column names, column types,
nullability and primary keys, contract-tested against the migration sources. There
is deliberately no ``create_all()`` path and no constraint duplication: check,
unique and foreign key constraints stay owned by the migrations, while reads and
writes address the tables through this metadata.
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
    Column("origin_kind", sa.Text(), nullable=False),
    Column("event_id", sa.Uuid(), nullable=True),
    Column("policy_revision_id", sa.Uuid(), nullable=True),
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

source_locators: Final[Table] = Table(
    "source_locators",
    _SOURCE_STORE_METADATA,
    Column("source_locator_id", sa.Uuid(), nullable=False),
    Column("workspace_id", sa.Uuid(), nullable=False),
    Column("source_id", sa.Uuid(), nullable=False),
    Column("normalized_locator", sa.Text(), nullable=False),
    Column("display_locator", sa.Text(), nullable=False),
    Column("opened_event_id", sa.Uuid(), nullable=False),
    Column("opened_sequence", sa.BigInteger(), nullable=False),
    Column("closed_event_id", sa.Uuid(), nullable=True),
    Column("closed_sequence", sa.BigInteger(), nullable=True),
    Column("opened_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=False),
    Column("closed_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=True),
    sa.PrimaryKeyConstraint("source_locator_id", name="pk_source_locators"),
)

source_tombstones: Final[Table] = Table(
    "source_tombstones",
    _SOURCE_STORE_METADATA,
    Column("source_tombstone_id", sa.Uuid(), nullable=False),
    Column("workspace_id", sa.Uuid(), nullable=False),
    Column("source_id", sa.Uuid(), nullable=False),
    Column("delete_event_id", sa.Uuid(), nullable=False),
    Column("retained_version_id", sa.Uuid(), nullable=False),
    Column("retained_locator", sa.Text(), nullable=False),
    Column("actor_kind", sa.Text(), nullable=False),
    Column("actor_id", sa.Uuid(), nullable=False),
    Column("deleted_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=False),
    Column("restore_event_id", sa.Uuid(), nullable=True),
    Column("restored_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=True),
    sa.PrimaryKeyConstraint("source_tombstone_id", name="pk_source_tombstones"),
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

workspace_policy_state: Final[Table] = Table(
    "workspace_policy_state",
    _SOURCE_STORE_METADATA,
    Column("workspace_id", sa.Uuid(), nullable=False),
    Column("active_policy_revision_id", sa.Uuid(), nullable=True),
    Column("active_revision_number", sa.BigInteger(), nullable=False),
    Column("created_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=False),
    Column("updated_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=False),
    sa.PrimaryKeyConstraint("workspace_id", name="pk_workspace_policy_state"),
)

policy_drafts: Final[Table] = Table(
    "policy_drafts",
    _SOURCE_STORE_METADATA,
    Column("policy_draft_id", sa.Uuid(), nullable=False),
    Column("workspace_id", sa.Uuid(), nullable=False),
    Column("draft_version", sa.BigInteger(), nullable=False),
    Column("base_policy_revision_id", sa.Uuid(), nullable=True),
    Column("created_by_user_id", sa.Uuid(), nullable=True),
    Column("updated_by_user_id", sa.Uuid(), nullable=True),
    Column("created_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=False),
    Column("updated_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=False),
    sa.PrimaryKeyConstraint("policy_draft_id", name="pk_policy_drafts"),
)

policy_draft_rules: Final[Table] = Table(
    "policy_draft_rules",
    _SOURCE_STORE_METADATA,
    Column("policy_draft_id", sa.Uuid(), nullable=False),
    Column("rule_id", sa.Uuid(), nullable=False),
    Column("rule_kind", sa.Text(), nullable=False),
    Column("source_id_operand", sa.Uuid(), nullable=True),
    Column("text_operand", sa.String(length=4096), nullable=True),
    Column("size_bytes_operand", sa.BigInteger(), nullable=True),
    Column("semantic_fingerprint", sa.String(length=64), nullable=False),
    sa.PrimaryKeyConstraint("policy_draft_id", "rule_id", name="pk_policy_draft_rules"),
)

source_policies: Final[Table] = Table(
    "source_policies",
    _SOURCE_STORE_METADATA,
    Column("policy_revision_id", sa.Uuid(), nullable=False),
    Column("workspace_id", sa.Uuid(), nullable=False),
    Column("revision_number", sa.BigInteger(), nullable=False),
    Column("parent_policy_revision_id", sa.Uuid(), nullable=True),
    Column("default_decision", sa.Text(), nullable=False),
    Column("source_checkpoint_event_sequence", sa.BigInteger(), nullable=False),
    Column("policy_preview_id", sa.Uuid(), nullable=False),
    Column("publication_idempotency_key", sa.String(length=200), nullable=False),
    Column("request_fingerprint", sa.String(length=64), nullable=False),
    Column("snapshot_contract", sa.String(length=100), nullable=False),
    Column("snapshot_payload_bytes", sa.LargeBinary(), nullable=False),
    Column("snapshot_payload_sha256", sa.String(length=64), nullable=False),
    Column("signing_key_id", sa.Uuid(), nullable=False),
    Column("signature_bytes", sa.LargeBinary(), nullable=False),
    Column("published_by_user_id", sa.Uuid(), nullable=False),
    Column("published_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=False),
    sa.PrimaryKeyConstraint("policy_revision_id", name="pk_source_policies"),
)

policy_rules: Final[Table] = Table(
    "policy_rules",
    _SOURCE_STORE_METADATA,
    Column("policy_revision_id", sa.Uuid(), nullable=False),
    Column("rule_id", sa.Uuid(), nullable=False),
    Column("rule_kind", sa.Text(), nullable=False),
    Column("source_id_operand", sa.Uuid(), nullable=True),
    Column("text_operand", sa.String(length=4096), nullable=True),
    Column("size_bytes_operand", sa.BigInteger(), nullable=True),
    Column("semantic_fingerprint", sa.String(length=64), nullable=False),
    sa.PrimaryKeyConstraint("policy_revision_id", "rule_id", name="pk_policy_rules"),
)

policy_previews: Final[Table] = Table(
    "policy_previews",
    _SOURCE_STORE_METADATA,
    Column("policy_preview_id", sa.Uuid(), nullable=False),
    Column("workspace_id", sa.Uuid(), nullable=False),
    Column("policy_draft_id", sa.Uuid(), nullable=False),
    Column("draft_version", sa.BigInteger(), nullable=False),
    Column("draft_sha256", sa.String(length=64), nullable=False),
    Column("base_policy_revision_id", sa.Uuid(), nullable=True),
    Column("source_checkpoint_event_sequence", sa.BigInteger(), nullable=False),
    Column("state", sa.Text(), nullable=False),
    Column("newly_excluded_count", sa.Integer(), nullable=False),
    Column("still_excluded_count", sa.Integer(), nullable=False),
    Column("newly_allowed_count", sa.Integer(), nullable=False),
    Column("still_allowed_count", sa.Integer(), nullable=False),
    Column("indeterminate_count", sa.Integer(), nullable=False),
    Column("impact_digest", sa.String(length=64), nullable=True),
    Column("attempt_count", sa.Integer(), nullable=False),
    Column("available_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=False),
    Column("lease_token", sa.Uuid(), nullable=True),
    Column("leased_until", _TIMESTAMP_WITH_TIME_ZONE, nullable=True),
    Column("safe_error_code", sa.String(length=100), nullable=True),
    Column("created_by_user_id", sa.Uuid(), nullable=False),
    Column("created_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=False),
    Column("ready_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=True),
    Column("expires_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=True),
    Column("consumed_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=True),
    sa.PrimaryKeyConstraint("policy_preview_id", name="pk_policy_previews"),
)

policy_preview_results: Final[Table] = Table(
    "policy_preview_results",
    _SOURCE_STORE_METADATA,
    Column("policy_preview_id", sa.Uuid(), nullable=False),
    Column("source_id", sa.Uuid(), nullable=False),
    Column("previous_raw_decision", sa.Text(), nullable=False),
    Column("previous_enforced_decision", sa.Text(), nullable=False),
    Column("proposed_raw_decision", sa.Text(), nullable=False),
    Column("proposed_enforced_decision", sa.Text(), nullable=False),
    Column("proposed_match_state", sa.Text(), nullable=False),
    Column("impact_class", sa.Text(), nullable=False),
    Column("matched_rule_ids", sa.Text(), nullable=False),
    Column("missing_fields", sa.Text(), nullable=False),
    Column("subject_fingerprint", sa.String(length=64), nullable=False),
    sa.PrimaryKeyConstraint("policy_preview_id", "source_id", name="pk_policy_preview_results"),
)

policy_evaluations: Final[Table] = Table(
    "policy_evaluations",
    _SOURCE_STORE_METADATA,
    Column("policy_evaluation_id", sa.Uuid(), nullable=False),
    Column("policy_revision_id", sa.Uuid(), nullable=False),
    Column("source_id", sa.Uuid(), nullable=False),
    Column("subject_event_sequence", sa.BigInteger(), nullable=False),
    Column("raw_decision", sa.Text(), nullable=False),
    Column("enforced_decision", sa.Text(), nullable=False),
    Column("matched_rule_ids", sa.Text(), nullable=False),
    Column("missing_fields", sa.Text(), nullable=False),
    Column("subject_fingerprint", sa.String(length=64), nullable=False),
    Column("evaluated_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=False),
    sa.PrimaryKeyConstraint("policy_evaluation_id", name="pk_policy_evaluations"),
)

policy_reconciliation_intents: Final[Table] = Table(
    "policy_reconciliation_intents",
    _SOURCE_STORE_METADATA,
    Column("policy_reconciliation_intent_id", sa.Uuid(), nullable=False),
    Column("workspace_id", sa.Uuid(), nullable=False),
    Column("policy_revision_id", sa.Uuid(), nullable=False),
    Column("workflow_id", sa.String(length=200), nullable=False),
    Column("state", sa.Text(), nullable=False),
    Column("attempt_count", sa.Integer(), nullable=False),
    Column("available_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=False),
    Column("lease_token", sa.Uuid(), nullable=True),
    Column("leased_until", _TIMESTAMP_WITH_TIME_ZONE, nullable=True),
    Column("dispatched_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=True),
    Column("safe_error_code", sa.String(length=100), nullable=True),
    Column("created_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=False),
    Column("updated_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=False),
    sa.PrimaryKeyConstraint(
        "policy_reconciliation_intent_id", name="pk_policy_reconciliation_intents"
    ),
)

policy_signing_keys: Final[Table] = Table(
    "policy_signing_keys",
    _SOURCE_STORE_METADATA,
    Column("signing_key_id", sa.Uuid(), nullable=False),
    Column("workspace_id", sa.Uuid(), nullable=False),
    Column("algorithm", sa.Text(), nullable=False),
    Column("public_key_bytes", sa.LargeBinary(), nullable=False),
    Column("introduced_keyset_revision", sa.BigInteger(), nullable=False),
    Column("created_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=False),
    sa.PrimaryKeyConstraint("signing_key_id", name="pk_policy_signing_keys"),
)

policy_keysets: Final[Table] = Table(
    "policy_keysets",
    _SOURCE_STORE_METADATA,
    Column("policy_keyset_id", sa.Uuid(), nullable=False),
    Column("workspace_id", sa.Uuid(), nullable=False),
    Column("keyset_revision", sa.BigInteger(), nullable=False),
    Column("parent_keyset_revision", sa.BigInteger(), nullable=True),
    Column("canonical_payload_bytes", sa.LargeBinary(), nullable=False),
    Column("payload_sha256", sa.String(length=64), nullable=False),
    Column("created_by_user_id", sa.Uuid(), nullable=True),
    Column("created_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=False),
    sa.PrimaryKeyConstraint("policy_keyset_id", name="pk_policy_keysets"),
)

policy_keyset_signatures: Final[Table] = Table(
    "policy_keyset_signatures",
    _SOURCE_STORE_METADATA,
    Column("policy_keyset_id", sa.Uuid(), nullable=False),
    Column("signing_key_id", sa.Uuid(), nullable=False),
    Column("signature_bytes", sa.LargeBinary(), nullable=False),
    sa.PrimaryKeyConstraint(
        "policy_keyset_id", "signing_key_id", name="pk_policy_keyset_signatures"
    ),
)

small_file_upload_operations: Final[Table] = Table(
    "small_file_upload_operations",
    _SOURCE_STORE_METADATA,
    Column("operation_id", sa.Uuid(), nullable=False),
    Column("operation_token_hash", sa.String(length=64), nullable=False),
    Column("workspace_id", sa.Uuid(), nullable=False),
    Column("device_id", sa.Uuid(), nullable=False),
    Column("event_id", sa.Uuid(), nullable=False),
    Column("idempotency_key", sa.String(length=36), nullable=False),
    Column("operation_kind", sa.Text(), nullable=False),
    Column("declared_sha256", sa.String(length=64), nullable=False),
    Column("declared_size_bytes", sa.BigInteger(), nullable=False),
    Column("declared_media_type", sa.String(length=255), nullable=False),
    Column("policy_revision_number", sa.BigInteger(), nullable=False),
    Column("reserved_source_id", sa.Uuid(), nullable=True),
    Column("update_source_id", sa.Uuid(), nullable=True),
    Column("update_base_version_id", sa.Uuid(), nullable=True),
    Column("normalized_locator", sa.Text(), nullable=True),
    Column("locator_fingerprint", sa.String(length=64), nullable=True),
    Column("state", sa.Text(), nullable=False),
    Column("safe_error_code", sa.String(length=100), nullable=True),
    Column("result_kind", sa.Text(), nullable=True),
    Column("result_source_id", sa.Uuid(), nullable=True),
    Column("result_source_version_id", sa.Uuid(), nullable=True),
    Column("result_content_version", sa.BigInteger(), nullable=True),
    Column("result_committed_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=True),
    Column("expires_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=False),
    Column("created_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=False),
    Column("updated_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=False),
    sa.PrimaryKeyConstraint("operation_id", name="pk_small_file_upload_operations"),
)

device_cursors: Final[Table] = Table(
    "device_cursors",
    _SOURCE_STORE_METADATA,
    Column("device_cursor_id", sa.Uuid(), nullable=False),
    Column("workspace_id", sa.Uuid(), nullable=False),
    Column("device_id", sa.Uuid(), nullable=False),
    Column("acknowledged_sequence", sa.BigInteger(), nullable=False),
    Column("delivered_through_sequence", sa.BigInteger(), nullable=False),
    Column("created_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=False),
    Column("updated_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=False),
    sa.PrimaryKeyConstraint("device_cursor_id", name="pk_device_cursors"),
)

manifest_runs: Final[Table] = Table(
    "manifest_runs",
    _SOURCE_STORE_METADATA,
    Column("manifest_run_id", sa.Uuid(), nullable=False),
    Column("workspace_id", sa.Uuid(), nullable=False),
    Column("device_id", sa.Uuid(), nullable=False),
    Column("base_acknowledged_sequence", sa.BigInteger(), nullable=False),
    Column("checkpoint_sequence", sa.BigInteger(), nullable=False),
    Column("policy_revision_number", sa.BigInteger(), nullable=False),
    Column("client_observation_generation", sa.BigInteger(), nullable=False),
    Column("state", sa.Text(), nullable=False),
    Column("next_page_number", sa.Integer(), nullable=False),
    Column("entry_count", sa.Integer(), nullable=False),
    Column("final_digest", sa.String(length=64), nullable=True),
    Column("safe_error_code", sa.String(length=100), nullable=True),
    Column("created_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=False),
    Column("expires_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=False),
    Column("last_client_activity_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=False),
    Column("planned_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=True),
    Column("completed_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=True),
    sa.PrimaryKeyConstraint("manifest_run_id", name="pk_manifest_runs"),
)

manifest_pages: Final[Table] = Table(
    "manifest_pages",
    _SOURCE_STORE_METADATA,
    Column("manifest_run_id", sa.Uuid(), nullable=False),
    Column("page_number", sa.Integer(), nullable=False),
    Column("entry_count", sa.Integer(), nullable=False),
    Column("page_digest", sa.String(length=64), nullable=False),
    Column("received_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=False),
    sa.PrimaryKeyConstraint("manifest_run_id", "page_number", name="pk_manifest_pages"),
)

manifest_entry_resolutions: Final[Table] = Table(
    "manifest_entry_resolutions",
    _SOURCE_STORE_METADATA,
    Column("manifest_run_id", sa.Uuid(), nullable=False),
    Column("page_number", sa.Integer(), nullable=False),
    Column("entry_index", sa.Integer(), nullable=False),
    Column("local_entry_id", sa.String(length=256), nullable=False),
    Column("known_source_id", sa.Uuid(), nullable=True),
    Column("known_version_id", sa.Uuid(), nullable=True),
    Column("submitted_sha256", sa.String(length=64), nullable=False),
    Column("submitted_size_bytes", sa.BigInteger(), nullable=False),
    Column("submitted_media_type", sa.String(length=255), nullable=False),
    Column("locator_evidence_digest", sa.String(length=64), nullable=False),
    Column("resolved_source_id", sa.Uuid(), nullable=True),
    Column("resolved_source_version_id", sa.Uuid(), nullable=True),
    Column("resolved_source_locator_id", sa.Uuid(), nullable=True),
    Column("resolved_source_tombstone_id", sa.Uuid(), nullable=True),
    Column("match_kind", sa.Text(), nullable=False),
    sa.PrimaryKeyConstraint(
        "manifest_run_id", "page_number", "entry_index", name="pk_manifest_entry_resolutions"
    ),
)

manifest_actions: Final[Table] = Table(
    "manifest_actions",
    _SOURCE_STORE_METADATA,
    Column("manifest_run_id", sa.Uuid(), nullable=False),
    Column("action_index", sa.Integer(), nullable=False),
    Column("action_kind", sa.Text(), nullable=False),
    Column("local_entry_id", sa.String(length=256), nullable=True),
    Column("source_id", sa.Uuid(), nullable=True),
    Column("source_version_id", sa.Uuid(), nullable=True),
    Column("source_locator_id", sa.Uuid(), nullable=True),
    Column("source_tombstone_id", sa.Uuid(), nullable=True),
    Column("safe_reason_code", sa.String(length=100), nullable=True),
    sa.PrimaryKeyConstraint("manifest_run_id", "action_index", name="pk_manifest_actions"),
)

#: Durable multipart upload session state (migrations ``20260828_01``,
#: ``20260828_03`` and ``20260828_04``). The ``staging_key`` and
#: ``provider_upload_id`` columns are private provider identity: they land
#: only through the store's fenced post-create write (spec 6.1
#: persist-before-create), stay NULL until then, and never render in a repr,
#: log, metric or API schema; no presigned URL is ever durable state. The
#: three ``operation_token_*`` columns are the AEAD-sealed raw preimage of
#: the frozen operation's token hash plus its keyring key ID: sealed
#: secret-bearing text, nullable (a composition without a codec reserves
#: with no seal) and never rendered either.
multipart_uploads: Final[Table] = Table(
    "multipart_uploads",
    _SOURCE_STORE_METADATA,
    Column("multipart_upload_id", sa.Uuid(), nullable=False),
    Column("session_id", sa.String(length=128), nullable=False),
    Column("workspace_id", sa.Uuid(), nullable=False),
    Column("device_id", sa.Uuid(), nullable=False),
    Column("operation_id", sa.Uuid(), nullable=False),
    Column("declared_sha256", sa.String(length=64), nullable=False),
    Column("declared_size_bytes", sa.BigInteger(), nullable=False),
    Column("declared_media_type", sa.String(length=255), nullable=False),
    Column("base_version_id", sa.Uuid(), nullable=True),
    Column("policy_revision_number", sa.BigInteger(), nullable=False),
    Column("part_size_bytes", sa.BigInteger(), nullable=False),
    Column("part_count", sa.Integer(), nullable=False),
    Column("staging_key", sa.Text(), nullable=True),
    Column("provider_upload_id", sa.Text(), nullable=True),
    Column("operation_token_ciphertext", sa.String(length=255), nullable=True),
    Column("operation_token_nonce", sa.String(length=64), nullable=True),
    Column("operation_token_key_id", sa.String(length=100), nullable=True),
    Column("state", sa.Text(), nullable=False),
    Column("claim_token", sa.Uuid(), nullable=True),
    Column("claim_expires_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=True),
    Column("result_kind", sa.Text(), nullable=True),
    Column("result_source_id", sa.Uuid(), nullable=True),
    Column("result_source_version_id", sa.Uuid(), nullable=True),
    Column("result_content_version", sa.BigInteger(), nullable=True),
    Column("result_committed_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=True),
    Column("cleanup_state", sa.Text(), nullable=False),
    Column("cleanup_attempt_count", sa.Integer(), nullable=False),
    Column("cleanup_next_retry_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=True),
    Column("cleanup_reason_code", sa.String(length=100), nullable=True),
    Column("expires_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=False),
    Column("created_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=False),
    Column("updated_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=False),
    sa.PrimaryKeyConstraint("multipart_upload_id", name="pk_multipart_uploads"),
)

#: Completed multipart part evidence (migration ``20260828_01``). The
#: ``provider_etag`` column is private provider identity.
multipart_parts: Final[Table] = Table(
    "multipart_parts",
    _SOURCE_STORE_METADATA,
    Column("multipart_part_id", sa.Uuid(), nullable=False),
    Column("multipart_upload_id", sa.Uuid(), nullable=False),
    Column("part_number", sa.Integer(), nullable=False),
    Column("offset_bytes", sa.BigInteger(), nullable=False),
    Column("size_bytes", sa.BigInteger(), nullable=False),
    Column("provider_etag", sa.Text(), nullable=False),
    Column("verified_size_bytes", sa.BigInteger(), nullable=False),
    Column("completed_at", _TIMESTAMP_WITH_TIME_ZONE, nullable=False),
    sa.PrimaryKeyConstraint("multipart_part_id", name="pk_multipart_parts"),
)

#: Single frozen metadata collection owning every DML table.
SOURCE_STORE_METADATA: Final[MetaData] = _SOURCE_STORE_METADATA

#: Immutable name-indexed view of the thirty-nine migrated tables, keyed by
#: their unqualified table names (``metadata.tables`` itself is
#: schema-qualified).
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
            source_locators,
            source_tombstones,
            audit_events,
            user_credentials,
            web_sessions,
            totp_credentials,
            totp_recovery_codes,
            device_token_families,
            device_tokens,
            device_authorization_grants,
            authentication_throttle_buckets,
            workspace_policy_state,
            policy_drafts,
            policy_draft_rules,
            source_policies,
            policy_rules,
            policy_previews,
            policy_preview_results,
            policy_evaluations,
            policy_reconciliation_intents,
            policy_signing_keys,
            policy_keysets,
            policy_keyset_signatures,
            small_file_upload_operations,
            device_cursors,
            manifest_runs,
            manifest_pages,
            manifest_entry_resolutions,
            manifest_actions,
            multipart_uploads,
            multipart_parts,
        )
    }
)
