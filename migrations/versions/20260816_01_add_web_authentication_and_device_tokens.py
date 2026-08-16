"""Add the normalized Web authentication and device-token schema.

Revision ID: 20260816_01
Revises: 20260813_01
Create Date: 2026-08-16

Creates the eight normalized authentication tables of spec 15 inside the
existing ``knowledge`` schema: ``user_credentials``, ``web_sessions``,
``totp_credentials``, ``totp_recovery_codes``, ``device_token_families``,
``device_tokens``, ``device_authorization_grants`` and
``authentication_throttle_buckets``. Tables are created in foreign-key
dependency order, which places the grant table after the token family/token
tables it references. Every constraint and index carries a semantic name,
every state column is a closed check constraint mirroring the closed domain
state vocabularies exactly, every quantity carries its
unit and every timestamp is timezone-aware. Partial unique indexes enforce one
active and at most one pending TOTP credential per user, one active token
family per device, one current refresh generation per family and one successor
per predecessor. State/timestamp matrix checks reject inconsistent pending,
approved, denied, exchanged, rotated and revoked rows at the database
boundary, and a trigger keeps a consumed recovery code's ``used_at`` immutable.

The gated downgrade removes exactly these eight tables and the one trigger
function; the Phase 1 baseline is untouched. Every drop is explicit, and no
extension, seed row or PostgreSQL enum type is introduced.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260816_01"
down_revision: str | None = "20260813_01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA_NAME: Final[str] = "knowledge"

_TIMESTAMP_TYPE: Final[sa.TIMESTAMP] = sa.TIMESTAMP(timezone=True)

_SHA256_CHECK: Final[str] = r"~ '^[0-9a-f]{64}$'"
_SAFE_TOKEN_CHECK: Final[str] = r"~ '^[a-z][a-z0-9_.:-]*$'"
_PHC_HASH_CHECK: Final[str] = (
    r"~ '^\$argon2id\$v=[0-9]+\$m=[0-9]+,t=[0-9]+,p=[0-9]+\$"
    r"[A-Za-z0-9+/]+\$[A-Za-z0-9+/]+$'"
)
_SEMVER_CHECK: Final[str] = r"~ '^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?(\+[0-9A-Za-z.-]+)?$'"

_RECOVERY_USED_AT_FUNCTION_SQL: Final[str] = """
CREATE FUNCTION knowledge.reject_recovery_code_used_at_change()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    IF OLD.used_at IS NOT NULL AND NEW.used_at IS DISTINCT FROM OLD.used_at THEN
        RAISE EXCEPTION USING ERRCODE = '55000', MESSAGE = 'recovery_code_used_at_immutable';
    END IF;
    RETURN NEW;
END;
$$
"""

_RECOVERY_USED_AT_TRIGGER_SQL: Final[str] = (
    "CREATE TRIGGER trg_totp_recovery_codes__reject_used_at_change "
    "BEFORE UPDATE ON knowledge.totp_recovery_codes "
    "FOR EACH ROW WHEN (OLD.used_at IS NOT NULL) "
    "EXECUTE FUNCTION knowledge.reject_recovery_code_used_at_change()"
)

_REVOKE_ROUTINE_EXECUTE_SQL: Final[str] = (
    "REVOKE EXECUTE ON FUNCTION knowledge.reject_recovery_code_used_at_change FROM PUBLIC"
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
    IF application_table_count <> 17 THEN
        RAISE EXCEPTION 'authentication_schema_table_count_invalid';
    END IF;
    SELECT count(*) INTO trigger_function_count
    FROM pg_catalog.pg_proc pgp
    JOIN pg_catalog.pg_namespace pgn ON pgn.oid = pgp.pronamespace
    WHERE pgn.nspname = 'knowledge';
    IF trigger_function_count <> 3 THEN
        RAISE EXCEPTION 'authentication_schema_function_count_invalid';
    END IF;
    SELECT count(*) INTO protection_trigger_count
    FROM pg_catalog.pg_trigger pgt
    JOIN pg_catalog.pg_class pgc ON pgc.oid = pgt.tgrelid
    JOIN pg_catalog.pg_namespace pgn ON pgn.oid = pgc.relnamespace
    WHERE pgn.nspname = 'knowledge' AND NOT pgt.tgisinternal;
    IF protection_trigger_count <> 5 THEN
        RAISE EXCEPTION 'authentication_schema_trigger_count_invalid';
    END IF;
END;
$$
"""


def upgrade() -> None:
    op.create_table(
        "user_credentials",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("credential_revision", sa.BigInteger(), nullable=False),
        sa.Column("totp_prompt_dismissed_at", _TIMESTAMP_TYPE, nullable=True),
        sa.Column(
            "password_changed_at",
            _TIMESTAMP_TYPE,
            server_default=sa.text("CURRENT_TIMESTAMP"),
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
        sa.PrimaryKeyConstraint("user_id", name="pk_user_credentials"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["knowledge.users.user_id"],
            name="fk_user_credentials__user",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "user_id"],
            ["knowledge.workspaces.workspace_id", "knowledge.workspaces.owner_user_id"],
            name="fk_user_credentials__workspace_owner",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "password_hash " + _PHC_HASH_CHECK, name="ck_user_credentials__password_hash"
        ),
        sa.CheckConstraint(
            "credential_revision >= 1", name="ck_user_credentials__credential_revision"
        ),
        sa.CheckConstraint(
            "updated_at >= created_at "
            "AND password_changed_at >= created_at "
            "AND (totp_prompt_dismissed_at IS NULL OR totp_prompt_dismissed_at >= created_at)",
            name="ck_user_credentials__timestamps",
        ),
        schema=SCHEMA_NAME,
    )
    op.create_table(
        "web_sessions",
        sa.Column("web_session_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("session_secret_hash", sa.String(length=64), nullable=False),
        sa.Column("csrf_secret_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "state",
            sa.Text(),
            server_default=sa.text("'pending_totp'"),
            nullable=False,
        ),
        sa.Column("credential_revision", sa.BigInteger(), nullable=False),
        sa.Column("authentication_method", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            _TIMESTAMP_TYPE,
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("authenticated_at", _TIMESTAMP_TYPE, nullable=True),
        sa.Column("reauthenticated_at", _TIMESTAMP_TYPE, nullable=True),
        sa.Column("last_seen_at", _TIMESTAMP_TYPE, nullable=True),
        sa.Column("idle_expires_at", _TIMESTAMP_TYPE, nullable=False),
        sa.Column("absolute_expires_at", _TIMESTAMP_TYPE, nullable=False),
        sa.Column("revoked_at", _TIMESTAMP_TYPE, nullable=True),
        sa.Column("revocation_reason", sa.String(length=100), nullable=True),
        sa.PrimaryKeyConstraint("web_session_id", name="pk_web_sessions"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "user_id"],
            ["knowledge.workspaces.workspace_id", "knowledge.workspaces.owner_user_id"],
            name="fk_web_sessions__workspace_owner",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("session_secret_hash", name="uq_web_sessions__session_secret_hash"),
        sa.CheckConstraint(
            "session_secret_hash " + _SHA256_CHECK, name="ck_web_sessions__session_secret_hash"
        ),
        sa.CheckConstraint(
            "csrf_secret_hash " + _SHA256_CHECK, name="ck_web_sessions__csrf_secret_hash"
        ),
        sa.CheckConstraint(
            "state IN ('pending_totp', 'active', 'recovery_limited', 'revoked')",
            name="ck_web_sessions__state",
        ),
        sa.CheckConstraint("credential_revision >= 1", name="ck_web_sessions__credential_revision"),
        sa.CheckConstraint(
            "authentication_method IN ('password', 'password_totp', 'recovery_code')",
            name="ck_web_sessions__authentication_method",
        ),
        sa.CheckConstraint(
            "revocation_reason IS NULL OR (char_length(revocation_reason) <= 100 "
            "AND revocation_reason " + _SAFE_TOKEN_CHECK + ")",
            name="ck_web_sessions__revocation_reason",
        ),
        sa.CheckConstraint(
            "(state IN ('active', 'recovery_limited')) = (authenticated_at IS NOT NULL) "
            "AND (state = 'revoked') = (revoked_at IS NOT NULL)",
            name="ck_web_sessions__state_timestamps",
        ),
        sa.CheckConstraint(
            "reauthenticated_at IS NULL "
            "OR (authenticated_at IS NOT NULL AND reauthenticated_at >= authenticated_at)",
            name="ck_web_sessions__reauthentication",
        ),
        sa.CheckConstraint(
            "idle_expires_at <= absolute_expires_at "
            "AND absolute_expires_at >= created_at "
            "AND (last_seen_at IS NULL OR last_seen_at >= created_at) "
            "AND (revoked_at IS NULL OR revoked_at >= created_at)",
            name="ck_web_sessions__expiry",
        ),
        schema=SCHEMA_NAME,
    )
    op.create_table(
        "totp_credentials",
        sa.Column("totp_credential_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column(
            "state",
            sa.Text(),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column("secret_ciphertext", sa.String(length=255), nullable=False),
        sa.Column("secret_nonce", sa.String(length=64), nullable=False),
        sa.Column("key_id", sa.String(length=100), nullable=False),
        sa.Column("algorithm", sa.Text(), nullable=False),
        sa.Column("digits", sa.Integer(), nullable=False),
        sa.Column("period_seconds", sa.Integer(), nullable=False),
        sa.Column("last_accepted_time_step", sa.BigInteger(), nullable=True),
        sa.Column("enrollment_expires_at", _TIMESTAMP_TYPE, nullable=True),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            _TIMESTAMP_TYPE,
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("activated_at", _TIMESTAMP_TYPE, nullable=True),
        sa.Column("replaced_at", _TIMESTAMP_TYPE, nullable=True),
        sa.PrimaryKeyConstraint("totp_credential_id", name="pk_totp_credentials"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "user_id"],
            ["knowledge.workspaces.workspace_id", "knowledge.workspaces.owner_user_id"],
            name="fk_totp_credentials__workspace_owner",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'active', 'replaced')", name="ck_totp_credentials__state"
        ),
        sa.CheckConstraint(
            "char_length(secret_ciphertext) BETWEEN 16 AND 255",
            name="ck_totp_credentials__secret_ciphertext",
        ),
        sa.CheckConstraint(
            "char_length(secret_nonce) BETWEEN 8 AND 64",
            name="ck_totp_credentials__secret_nonce",
        ),
        sa.CheckConstraint(
            "char_length(key_id) <= 100 AND key_id " + _SAFE_TOKEN_CHECK,
            name="ck_totp_credentials__key_id",
        ),
        sa.CheckConstraint("algorithm = 'SHA1'", name="ck_totp_credentials__algorithm"),
        sa.CheckConstraint("digits = 6", name="ck_totp_credentials__digits"),
        sa.CheckConstraint("period_seconds = 30", name="ck_totp_credentials__period_seconds"),
        sa.CheckConstraint(
            "last_accepted_time_step IS NULL OR last_accepted_time_step >= 0",
            name="ck_totp_credentials__last_accepted_time_step",
        ),
        sa.CheckConstraint("revision >= 1", name="ck_totp_credentials__revision"),
        sa.CheckConstraint(
            "(state = 'pending') = (enrollment_expires_at IS NOT NULL) "
            "AND (state = 'pending') = (activated_at IS NULL AND replaced_at IS NULL) "
            "AND (state = 'active') = (activated_at IS NOT NULL AND replaced_at IS NULL) "
            "AND (state = 'replaced') = (replaced_at IS NOT NULL)",
            name="ck_totp_credentials__state_timestamps",
        ),
        sa.CheckConstraint(
            "(activated_at IS NULL OR activated_at >= created_at) "
            "AND (replaced_at IS NULL OR replaced_at >= created_at)",
            name="ck_totp_credentials__timestamps",
        ),
        schema=SCHEMA_NAME,
    )
    op.create_table(
        "totp_recovery_codes",
        sa.Column("recovery_code_id", sa.Uuid(), nullable=False),
        sa.Column("totp_credential_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            _TIMESTAMP_TYPE,
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("used_at", _TIMESTAMP_TYPE, nullable=True),
        sa.PrimaryKeyConstraint("recovery_code_id", name="pk_totp_recovery_codes"),
        sa.ForeignKeyConstraint(
            ["totp_credential_id"],
            ["knowledge.totp_credentials.totp_credential_id"],
            name="fk_totp_recovery_codes__credential",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "user_id"],
            ["knowledge.workspaces.workspace_id", "knowledge.workspaces.owner_user_id"],
            name="fk_totp_recovery_codes__workspace_owner",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "totp_credential_id",
            "revision",
            "code_hash",
            name="uq_totp_recovery_codes__credential_revision_hash",
        ),
        sa.CheckConstraint("code_hash " + _SHA256_CHECK, name="ck_totp_recovery_codes__code_hash"),
        sa.CheckConstraint("revision >= 1", name="ck_totp_recovery_codes__revision"),
        sa.CheckConstraint(
            "used_at IS NULL OR used_at >= created_at", name="ck_totp_recovery_codes__used_at"
        ),
        schema=SCHEMA_NAME,
    )
    op.create_table(
        "device_token_families",
        sa.Column("token_family_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("device_id", sa.Uuid(), nullable=False),
        sa.Column(
            "state",
            sa.Text(),
            server_default=sa.text("'active'"),
            nullable=False,
        ),
        sa.Column("current_refresh_generation", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            _TIMESTAMP_TYPE,
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "last_refreshed_at",
            _TIMESTAMP_TYPE,
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("inactivity_expires_at", _TIMESTAMP_TYPE, nullable=False),
        sa.Column("absolute_expires_at", _TIMESTAMP_TYPE, nullable=False),
        sa.Column("revoked_at", _TIMESTAMP_TYPE, nullable=True),
        sa.Column("revocation_reason", sa.String(length=100), nullable=True),
        sa.PrimaryKeyConstraint("token_family_id", name="pk_device_token_families"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "user_id"],
            ["knowledge.workspaces.workspace_id", "knowledge.workspaces.owner_user_id"],
            name="fk_device_token_families__workspace_owner",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "device_id"],
            ["knowledge.devices.workspace_id", "knowledge.devices.device_id"],
            name="fk_device_token_families__device",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "state IN ('active', 'revoked')", name="ck_device_token_families__state"
        ),
        sa.CheckConstraint(
            "current_refresh_generation >= 1",
            name="ck_device_token_families__current_refresh_generation",
        ),
        sa.CheckConstraint(
            "last_refreshed_at >= created_at "
            "AND inactivity_expires_at >= last_refreshed_at "
            "AND absolute_expires_at >= created_at "
            "AND (revoked_at IS NULL OR revoked_at >= created_at)",
            name="ck_device_token_families__timestamps",
        ),
        sa.CheckConstraint(
            "inactivity_expires_at <= absolute_expires_at",
            name="ck_device_token_families__expiry",
        ),
        sa.CheckConstraint(
            "(state = 'revoked') = (revoked_at IS NOT NULL)",
            name="ck_device_token_families__revocation",
        ),
        sa.CheckConstraint(
            "revocation_reason IS NULL OR (char_length(revocation_reason) <= 100 "
            "AND revocation_reason " + _SAFE_TOKEN_CHECK + ")",
            name="ck_device_token_families__revocation_reason",
        ),
        schema=SCHEMA_NAME,
    )
    op.create_table(
        "device_tokens",
        sa.Column("device_token_id", sa.Uuid(), nullable=False),
        sa.Column("token_family_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("device_id", sa.Uuid(), nullable=False),
        sa.Column("token_kind", sa.Text(), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("secret_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "state",
            sa.Text(),
            server_default=sa.text("'active'"),
            nullable=False,
        ),
        sa.Column("predecessor_token_id", sa.Uuid(), nullable=True),
        sa.Column("successor_token_id", sa.Uuid(), nullable=True),
        sa.Column("rotation_id", sa.Uuid(), nullable=True),
        sa.Column("derivation_key_id", sa.String(length=100), nullable=False),
        sa.Column(
            "issued_at",
            _TIMESTAMP_TYPE,
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("expires_at", _TIMESTAMP_TYPE, nullable=False),
        sa.Column("rotated_at", _TIMESTAMP_TYPE, nullable=True),
        sa.Column("revoked_at", _TIMESTAMP_TYPE, nullable=True),
        sa.PrimaryKeyConstraint("device_token_id", name="pk_device_tokens"),
        sa.ForeignKeyConstraint(
            ["token_family_id"],
            ["knowledge.device_token_families.token_family_id"],
            name="fk_device_tokens__family",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "user_id"],
            ["knowledge.workspaces.workspace_id", "knowledge.workspaces.owner_user_id"],
            name="fk_device_tokens__workspace_owner",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "device_id"],
            ["knowledge.devices.workspace_id", "knowledge.devices.device_id"],
            name="fk_device_tokens__device",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["predecessor_token_id"],
            ["knowledge.device_tokens.device_token_id"],
            name="fk_device_tokens__predecessor",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["successor_token_id"],
            ["knowledge.device_tokens.device_token_id"],
            name="fk_device_tokens__successor",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("secret_hash", name="uq_device_tokens__secret_hash"),
        sa.CheckConstraint("secret_hash " + _SHA256_CHECK, name="ck_device_tokens__secret_hash"),
        sa.CheckConstraint(
            "token_kind IN ('access', 'refresh')", name="ck_device_tokens__token_kind"
        ),
        sa.CheckConstraint("generation >= 1", name="ck_device_tokens__generation"),
        sa.CheckConstraint(
            "state IN ('active', 'rotated', 'revoked')", name="ck_device_tokens__state"
        ),
        sa.CheckConstraint(
            "char_length(derivation_key_id) <= 100 AND derivation_key_id " + _SAFE_TOKEN_CHECK,
            name="ck_device_tokens__derivation_key_id",
        ),
        sa.CheckConstraint(
            "(token_kind = 'refresh') "
            "OR (predecessor_token_id IS NULL AND successor_token_id IS NULL "
            "AND rotation_id IS NULL)",
            name="ck_device_tokens__rotation_lineage",
        ),
        sa.CheckConstraint(
            "(state = 'rotated') = (rotated_at IS NOT NULL) "
            "AND (state = 'revoked') = (revoked_at IS NOT NULL) "
            "AND (rotated_at IS NULL OR revoked_at IS NULL) "
            "AND (successor_token_id IS NULL OR state = 'rotated')",
            name="ck_device_tokens__state_lineage",
        ),
        sa.CheckConstraint(
            "expires_at >= issued_at "
            "AND (rotated_at IS NULL OR rotated_at >= issued_at) "
            "AND (revoked_at IS NULL OR revoked_at >= issued_at)",
            name="ck_device_tokens__timestamps",
        ),
        schema=SCHEMA_NAME,
    )
    op.create_table(
        "device_authorization_grants",
        sa.Column("grant_id", sa.Uuid(), nullable=False),
        sa.Column("user_code_hash", sa.String(length=64), nullable=False),
        sa.Column("polling_secret_hash", sa.String(length=64), nullable=False),
        sa.Column("client_instance_id", sa.Uuid(), nullable=False),
        sa.Column("claimed_device_id", sa.Uuid(), nullable=True),
        sa.Column("device_name", sa.String(length=80), nullable=False),
        sa.Column("platform_class", sa.Text(), nullable=False),
        sa.Column("platform_name", sa.String(length=64), nullable=False),
        sa.Column("plugin_version", sa.String(length=64), nullable=False),
        sa.Column("requested_scope", sa.Text(), nullable=False),
        sa.Column(
            "state",
            sa.Text(),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            _TIMESTAMP_TYPE,
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("expires_at", _TIMESTAMP_TYPE, nullable=False),
        sa.Column("approved_at", _TIMESTAMP_TYPE, nullable=True),
        sa.Column("denied_at", _TIMESTAMP_TYPE, nullable=True),
        sa.Column("exchanged_at", _TIMESTAMP_TYPE, nullable=True),
        sa.Column("approved_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("approved_web_session_id", sa.Uuid(), nullable=True),
        sa.Column("device_id", sa.Uuid(), nullable=True),
        sa.Column("token_family_id", sa.Uuid(), nullable=True),
        sa.Column("initial_access_token_id", sa.Uuid(), nullable=True),
        sa.Column("initial_refresh_token_id", sa.Uuid(), nullable=True),
        sa.Column("derivation_key_id", sa.String(length=100), nullable=True),
        sa.PrimaryKeyConstraint("grant_id", name="pk_device_authorization_grants"),
        sa.ForeignKeyConstraint(
            ["approved_by_user_id"],
            ["knowledge.users.user_id"],
            name="fk_device_authorization_grants__approved_by_user",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["approved_web_session_id"],
            ["knowledge.web_sessions.web_session_id"],
            name="fk_device_authorization_grants__approval_session",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["device_id"],
            ["knowledge.devices.device_id"],
            name="fk_device_authorization_grants__device",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["token_family_id"],
            ["knowledge.device_token_families.token_family_id"],
            name="fk_device_authorization_grants__token_family",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["initial_access_token_id"],
            ["knowledge.device_tokens.device_token_id"],
            name="fk_device_authorization_grants__initial_access_token",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["initial_refresh_token_id"],
            ["knowledge.device_tokens.device_token_id"],
            name="fk_device_authorization_grants__initial_refresh_token",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "user_code_hash", name="uq_device_authorization_grants__user_code_hash"
        ),
        sa.UniqueConstraint(
            "polling_secret_hash", name="uq_device_authorization_grants__polling_secret_hash"
        ),
        sa.CheckConstraint(
            "user_code_hash " + _SHA256_CHECK,
            name="ck_device_authorization_grants__user_code_hash",
        ),
        sa.CheckConstraint(
            "polling_secret_hash " + _SHA256_CHECK,
            name="ck_device_authorization_grants__polling_secret_hash",
        ),
        sa.CheckConstraint(
            "device_name = btrim(device_name) AND char_length(btrim(device_name)) BETWEEN 1 AND 80",
            name="ck_device_authorization_grants__device_name",
        ),
        sa.CheckConstraint(
            "platform_class IN ('obsidian_desktop', 'obsidian_mobile')",
            name="ck_device_authorization_grants__platform_class",
        ),
        sa.CheckConstraint(
            "char_length(platform_name) <= 64 AND platform_name " + _SAFE_TOKEN_CHECK,
            name="ck_device_authorization_grants__platform_name",
        ),
        sa.CheckConstraint(
            "char_length(plugin_version) <= 64 AND plugin_version " + _SEMVER_CHECK,
            name="ck_device_authorization_grants__plugin_version",
        ),
        sa.CheckConstraint(
            "requested_scope = 'obsidian_sync'",
            name="ck_device_authorization_grants__requested_scope",
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'approved', 'denied', 'exchanged')",
            name="ck_device_authorization_grants__state",
        ),
        sa.CheckConstraint(
            "(state = 'pending') = (approved_at IS NULL AND denied_at IS NULL "
            "AND exchanged_at IS NULL) "
            "AND (state = 'approved') = (approved_at IS NOT NULL AND denied_at IS NULL "
            "AND exchanged_at IS NULL) "
            "AND (state = 'denied') = (denied_at IS NOT NULL AND exchanged_at IS NULL) "
            "AND (state = 'exchanged') = (exchanged_at IS NOT NULL AND denied_at IS NULL "
            "AND device_id IS NOT NULL AND token_family_id IS NOT NULL "
            "AND initial_access_token_id IS NOT NULL AND initial_refresh_token_id IS NOT NULL) "
            "AND (approved_at IS NOT NULL) = (approved_by_user_id IS NOT NULL "
            "AND approved_web_session_id IS NOT NULL)",
            name="ck_device_authorization_grants__state_matrix",
        ),
        sa.CheckConstraint(
            "(token_family_id IS NULL OR device_id IS NOT NULL) "
            "AND (initial_access_token_id IS NULL) = (initial_refresh_token_id IS NULL) "
            "AND (initial_access_token_id IS NULL OR token_family_id IS NOT NULL)",
            name="ck_device_authorization_grants__exchange_links",
        ),
        sa.CheckConstraint(
            "expires_at > created_at "
            "AND (approved_at IS NULL OR approved_at >= created_at) "
            "AND (denied_at IS NULL OR denied_at >= created_at) "
            "AND (exchanged_at IS NULL "
            "OR (approved_at IS NOT NULL AND exchanged_at >= approved_at))",
            name="ck_device_authorization_grants__timestamps",
        ),
        schema=SCHEMA_NAME,
    )
    op.create_table(
        "authentication_throttle_buckets",
        sa.Column("throttle_bucket_id", sa.Uuid(), nullable=False),
        sa.Column("bucket_kind", sa.Text(), nullable=False),
        sa.Column("bucket_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "window_started_at",
            _TIMESTAMP_TYPE,
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "failed_attempt_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("locked_until", _TIMESTAMP_TYPE, nullable=True),
        sa.Column(
            "updated_at",
            _TIMESTAMP_TYPE,
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("throttle_bucket_id", name="pk_authentication_throttle_buckets"),
        sa.UniqueConstraint(
            "bucket_kind",
            "bucket_hash",
            name="uq_authentication_throttle_buckets__kind_hash",
        ),
        sa.CheckConstraint(
            "bucket_kind IN ('login_username', 'login_source', 'grant_creation', "
            "'user_code_lookup', 'totp_verification', 'recovery_verification')",
            name="ck_authentication_throttle_buckets__bucket_kind",
        ),
        sa.CheckConstraint(
            "bucket_hash " + _SHA256_CHECK,
            name="ck_authentication_throttle_buckets__bucket_hash",
        ),
        sa.CheckConstraint(
            "failed_attempt_count >= 0",
            name="ck_authentication_throttle_buckets__failed_attempt_count",
        ),
        sa.CheckConstraint(
            "updated_at >= window_started_at "
            "AND (locked_until IS NULL OR locked_until > window_started_at)",
            name="ck_authentication_throttle_buckets__timestamps",
        ),
        schema=SCHEMA_NAME,
    )
    op.create_index(
        "ix_user_credentials__workspace_user",
        "user_credentials",
        ["workspace_id", "user_id"],
        schema=SCHEMA_NAME,
    )
    op.create_index(
        "ix_web_sessions__workspace_user",
        "web_sessions",
        ["workspace_id", "user_id"],
        schema=SCHEMA_NAME,
    )
    op.create_index(
        "ix_web_sessions__state_idle_expiry",
        "web_sessions",
        ["state", "idle_expires_at", "web_session_id"],
        schema=SCHEMA_NAME,
    )
    op.create_index(
        "ix_totp_credentials__workspace_user",
        "totp_credentials",
        ["workspace_id", "user_id"],
        schema=SCHEMA_NAME,
    )
    op.create_index(
        "uq_totp_credentials__active_user",
        "totp_credentials",
        ["user_id"],
        unique=True,
        schema=SCHEMA_NAME,
        postgresql_where="state = 'active'",
    )
    op.create_index(
        "uq_totp_credentials__pending_user",
        "totp_credentials",
        ["user_id"],
        unique=True,
        schema=SCHEMA_NAME,
        postgresql_where="state = 'pending'",
    )
    op.create_index(
        "ix_totp_recovery_codes__workspace_user",
        "totp_recovery_codes",
        ["workspace_id", "user_id"],
        schema=SCHEMA_NAME,
    )
    op.create_index(
        "ix_totp_recovery_codes__credential_revision",
        "totp_recovery_codes",
        ["totp_credential_id", "revision"],
        schema=SCHEMA_NAME,
    )
    op.create_index(
        "ix_device_token_families__workspace_user",
        "device_token_families",
        ["workspace_id", "user_id"],
        schema=SCHEMA_NAME,
    )
    op.create_index(
        "ix_device_token_families__workspace_device",
        "device_token_families",
        ["workspace_id", "device_id", "state"],
        schema=SCHEMA_NAME,
    )
    op.create_index(
        "uq_device_token_families__active_device",
        "device_token_families",
        ["device_id"],
        unique=True,
        schema=SCHEMA_NAME,
        postgresql_where="state = 'active'",
    )
    op.create_index(
        "ix_device_tokens__family_kind_generation",
        "device_tokens",
        ["token_family_id", "token_kind", "generation"],
        schema=SCHEMA_NAME,
    )
    op.create_index(
        "ix_device_tokens__workspace_user",
        "device_tokens",
        ["workspace_id", "user_id"],
        schema=SCHEMA_NAME,
    )
    op.create_index(
        "ix_device_tokens__workspace_device",
        "device_tokens",
        ["workspace_id", "device_id"],
        schema=SCHEMA_NAME,
    )
    op.create_index(
        "ix_device_tokens__successor",
        "device_tokens",
        ["successor_token_id"],
        schema=SCHEMA_NAME,
    )
    op.create_index(
        "uq_device_tokens__current_refresh_generation",
        "device_tokens",
        ["token_family_id"],
        unique=True,
        schema=SCHEMA_NAME,
        postgresql_where="token_kind = 'refresh' AND state = 'active'",
    )
    op.create_index(
        "uq_device_tokens__successor_per_predecessor",
        "device_tokens",
        ["predecessor_token_id"],
        unique=True,
        schema=SCHEMA_NAME,
        postgresql_where="predecessor_token_id IS NOT NULL",
    )
    op.create_index(
        "ix_device_authorization_grants__client_state_expiry",
        "device_authorization_grants",
        ["client_instance_id", "state", "expires_at", "grant_id"],
        schema=SCHEMA_NAME,
    )
    op.create_index(
        "ix_device_authorization_grants__approved_by_user",
        "device_authorization_grants",
        ["approved_by_user_id"],
        schema=SCHEMA_NAME,
    )
    op.create_index(
        "ix_device_authorization_grants__approval_session",
        "device_authorization_grants",
        ["approved_web_session_id"],
        schema=SCHEMA_NAME,
    )
    op.create_index(
        "ix_device_authorization_grants__device",
        "device_authorization_grants",
        ["device_id"],
        schema=SCHEMA_NAME,
    )
    op.create_index(
        "ix_device_authorization_grants__token_family",
        "device_authorization_grants",
        ["token_family_id"],
        schema=SCHEMA_NAME,
    )
    op.create_index(
        "ix_device_authorization_grants__initial_access_token",
        "device_authorization_grants",
        ["initial_access_token_id"],
        schema=SCHEMA_NAME,
    )
    op.create_index(
        "ix_device_authorization_grants__initial_refresh_token",
        "device_authorization_grants",
        ["initial_refresh_token_id"],
        schema=SCHEMA_NAME,
    )
    op.create_index(
        "ix_authentication_throttle_buckets__locked_until",
        "authentication_throttle_buckets",
        ["locked_until"],
        schema=SCHEMA_NAME,
        postgresql_where="locked_until IS NOT NULL",
    )
    op.execute(sa.text(_RECOVERY_USED_AT_FUNCTION_SQL))
    op.execute(sa.text(_REVOKE_ROUTINE_EXECUTE_SQL))
    op.execute(sa.text(_RECOVERY_USED_AT_TRIGGER_SQL))
    op.execute(sa.text(_FINAL_CATALOG_ASSERTION_SQL))


def downgrade() -> None:
    op.execute(
        sa.text(
            "DROP TRIGGER trg_totp_recovery_codes__reject_used_at_change "
            "ON knowledge.totp_recovery_codes"
        )
    )
    op.execute(sa.text("DROP FUNCTION knowledge.reject_recovery_code_used_at_change"))
    op.drop_table("authentication_throttle_buckets", schema=SCHEMA_NAME)
    op.drop_table("device_authorization_grants", schema=SCHEMA_NAME)
    op.drop_table("device_tokens", schema=SCHEMA_NAME)
    op.drop_table("device_token_families", schema=SCHEMA_NAME)
    op.drop_table("totp_recovery_codes", schema=SCHEMA_NAME)
    op.drop_table("totp_credentials", schema=SCHEMA_NAME)
    op.drop_table("web_sessions", schema=SCHEMA_NAME)
    op.drop_table("user_credentials", schema=SCHEMA_NAME)
