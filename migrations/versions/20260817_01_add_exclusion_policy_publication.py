"""Add the exclusion policy publication schema and intent origin discriminator.

Revision ID: 20260817_01
Revises: 20260816_01
Create Date: 2026-08-17

Creates the twelve policy tables of spec section 8 inside the existing
``knowledge`` schema — ``workspace_policy_state``, ``policy_signing_keys``,
``policy_keysets``, ``policy_keyset_signatures``, ``policy_drafts``,
``policy_draft_rules``, ``policy_previews``, ``source_policies``,
``policy_rules``, ``policy_preview_results``, ``policy_evaluations`` and
``policy_reconciliation_intents`` — in foreign-key dependency order, with
named constraints, closed CHECK vocabularies, database timestamps, partial
indexes for pending/ready lookups and one shared append-only mutation
rejection trigger over the published artifacts and insert-once evidence.
``projection_intents`` gains the ``source_event``/``policy_transition`` origin
discriminator: ``event_id`` becomes nullable, ``policy_revision_id`` is added,
the origin CHECK closes the vocabulary and requires exactly one populated
origin reference, existing rows backfill ``source_event``, and a partial
unique index enforces policy-transition identity
``(policy_revision_id, source_id, projection_kind)``. The upgrade
seeds one unpublished ``workspace_policy_state`` row and one empty draft per
existing workspace through parameter-bound inserts with Python-generated
UUIDv7 identities; it never publishes or signs implicitly.

The downgrade returns exactly to the ``20260816_01`` head. It first counts the
protected row classes (published policies, previews, evaluations, keysets,
signing keys, reconciliation intents, active pointers and policy-origin
intents) and refuses with ``exclusion_policy_downgrade_requires_explicit_gate``
unless the explicit ``allow_destructive`` x-argument is present; under that
gate it deletes pending policy-transition intents, reverses the discriminator,
drops the trigger, function, tables and cross-table constraints without
CASCADE, and finishes with the Child 2 catalog assertion (17 tables, 3
functions, 5 triggers).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final
from uuid import uuid7

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260817_01"
down_revision: str | None = "20260816_01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA_NAME: Final[str] = "knowledge"

_TIMESTAMP_TYPE: Final[sa.TIMESTAMP] = sa.TIMESTAMP(timezone=True)

_SHA256_CHECK: Final[str] = r"~ '^[0-9a-f]{64}$'"
_PRINTABLE_KEY_CHECK: Final[str] = r"~ '^[!-~]{1,200}$'"
_SAFE_ERROR_CODE_CHECK: Final[str] = r"~ '^[a-z][a-z0-9_]{0,99}$'"

_NIL_UUID_LITERAL: Final[str] = "'00000000-0000-0000-0000-000000000000'::uuid"

#: Maximum signed snapshot or keyset payload: 256 KiB (spec 12/13).
_MAXIMUM_PAYLOAD_BYTES: Final[int] = 262144

#: Sorted canonical UUID text is 36 characters plus one separator per entry;
#: a revision carries at most 256 rules (spec 6.1).
_MATCHED_RULE_IDS_MAXIMUM_BYTES: Final[int] = 256 * 37

_RULE_KIND_CHECK: Final[str] = (
    "rule_kind IN ('exact_source_id', 'folder_prefix', 'path_glob', 'extension', "
    "'media_type', 'maximum_size', 'source_type')"
)

#: Every kind maps to exactly one populated typed operand column (spec 6.1).
_RULE_OPERAND_SHAPE_CHECK: Final[str] = (
    "(rule_kind = 'exact_source_id' "
    "AND source_id_operand IS NOT NULL AND text_operand IS NULL AND size_bytes_operand IS NULL) "
    "OR (rule_kind IN ('folder_prefix', 'path_glob', 'extension', 'media_type', 'source_type') "
    "AND source_id_operand IS NULL AND text_operand IS NOT NULL AND size_bytes_operand IS NULL) "
    "OR (rule_kind = 'maximum_size' "
    "AND source_id_operand IS NULL AND text_operand IS NULL AND size_bytes_operand IS NOT NULL)"
)

#: Per-kind operand bounds mirroring the closed normalization contract
#: (spec 6.2/6.3/6.4): locator byte ceilings, no backslash, segment-exact
#: folder prefixes, the bounded extension grammar, the canonical MIME grammar
#: with ``type/*`` families, the closed source types and the size ceiling.
_RULE_OPERAND_BOUNDS_CHECK: Final[str] = (
    "(rule_kind <> 'exact_source_id' "
    f"OR source_id_operand <> {_NIL_UUID_LITERAL}) "
    "AND (rule_kind <> 'folder_prefix' OR ("
    "octet_length(text_operand) BETWEEN 1 AND 4096 "
    "AND left(text_operand, 1) <> '/' AND right(text_operand, 1) <> '/' "
    "AND position('//' in text_operand) = 0 "
    "AND position(chr(92) in text_operand) = 0)) "
    "AND (rule_kind <> 'path_glob' OR ("
    "octet_length(text_operand) BETWEEN 1 AND 1024 "
    "AND left(text_operand, 1) <> '/' "
    "AND position(chr(92) in text_operand) = 0)) "
    "AND (rule_kind <> 'extension' OR "
    r"text_operand ~ '^\.[a-z0-9._-]{1,63}$') "
    "AND (rule_kind <> 'media_type' OR ("
    r"(text_operand ~ '^[a-z0-9!#$&^_.+\-]+/[a-z0-9!#$&^_.+\-]+$' "
    r"OR text_operand ~ '^[a-z0-9!#$&^_.+\-]+/\*$') "
    "AND octet_length(text_operand) <= 255)) "
    "AND (rule_kind <> 'source_type' OR text_operand IN ("
    "'markdown', 'text', 'pdf', 'image', 'audio', 'web', 'youtube')) "
    "AND (size_bytes_operand IS NULL OR size_bytes_operand BETWEEN 0 AND 104857600)"
)

_RULE_FINGERPRINT_CHECK: Final[str] = f"semantic_fingerprint {_SHA256_CHECK}"

#: Sorted canonical UUID text, space separated; empty means no matched rule.
_MATCHED_RULE_IDS_CHECK: Final[str] = (
    "(matched_rule_ids = '' OR ("
    r"matched_rule_ids ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"( [0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})*$' "
    f"AND octet_length(matched_rule_ids) <= {_MATCHED_RULE_IDS_MAXIMUM_BYTES}))"
)

#: Sorted closed subject-field names, space separated; empty means none missing.
_MISSING_FIELDS_CHECK: Final[str] = (
    "(missing_fields = '' OR "
    r"missing_fields ~ '^(source_id|normalized_locator|source_type|media_type|size_bytes)"
    r"( (source_id|normalized_locator|source_type|media_type|size_bytes))*$')"
)

_RAW_DECISION_CHECK: Final[str] = "{column} IN ('allowed', 'excluded', 'indeterminate')"
_ENFORCED_DECISION_CHECK: Final[str] = "{column} IN ('allowed', 'excluded')"

_REJECT_POLICY_MUTATION_FUNCTION_SQL: Final[str] = """
CREATE FUNCTION knowledge.reject_policy_history_mutation()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    RAISE EXCEPTION USING ERRCODE = '55000', MESSAGE = 'policy_history_append_only';
END;
$$
"""

_REVOKE_ROUTINE_EXECUTE_SQL: Final[str] = (
    "REVOKE EXECUTE ON FUNCTION knowledge.reject_policy_history_mutation FROM PUBLIC"
)

_APPEND_ONLY_TRIGGER_TABLES: Final[tuple[str, ...]] = (
    "source_policies",
    "policy_rules",
    "policy_evaluations",
    "policy_signing_keys",
    "policy_keysets",
    "policy_keyset_signatures",
)

_APPEND_ONLY_TRIGGER_STATEMENTS: Final[tuple[str, ...]] = tuple(
    f"CREATE TRIGGER trg_{table_name}__reject_mutation "
    f"BEFORE UPDATE OR DELETE ON knowledge.{table_name} "
    "FOR EACH ROW EXECUTE FUNCTION knowledge.reject_policy_history_mutation()"
    for table_name in _APPEND_ONLY_TRIGGER_TABLES
)

_SEED_WORKSPACES_SQL: Final[str] = "SELECT workspace_id FROM knowledge.workspaces"

_SEED_POLICY_STATE_SQL: Final[str] = (
    "INSERT INTO knowledge.workspace_policy_state "
    "(workspace_id, active_policy_revision_id, active_revision_number) "
    "VALUES (:workspace_id, NULL, 0)"
)

_SEED_POLICY_DRAFT_SQL: Final[str] = (
    "INSERT INTO knowledge.policy_drafts "
    "(policy_draft_id, workspace_id, draft_version, base_policy_revision_id, "
    "created_by_user_id, updated_by_user_id) "
    "VALUES (:policy_draft_id, :workspace_id, 1, NULL, NULL, NULL)"
)

#: The explicit destructive operator/test gate: the same ``-x`` argument the
#: migration environment already requires for every CLI downgrade.
_DESTRUCTIVE_X_ARGUMENT: Final[str] = "allow_destructive"

_DOWNGRADE_REFUSAL_MESSAGE: Final[str] = "exclusion_policy_downgrade_requires_explicit_gate"

#: One scalar over every protected row class (spec 8.7). Seeded-but-unused
#: state rows and empty drafts are excluded: the upgrade itself creates them.
_DOWNGRADE_GATE_COUNT_SQL: Final[str] = """
SELECT
    (SELECT count(*) FROM knowledge.source_policies)
    + (SELECT count(*) FROM knowledge.policy_previews)
    + (SELECT count(*) FROM knowledge.policy_evaluations)
    + (SELECT count(*) FROM knowledge.policy_keysets)
    + (SELECT count(*) FROM knowledge.policy_signing_keys)
    + (SELECT count(*) FROM knowledge.policy_reconciliation_intents)
    + (SELECT count(*) FROM knowledge.workspace_policy_state
       WHERE active_policy_revision_id IS NOT NULL)
    + (SELECT count(*) FROM knowledge.projection_intents
       WHERE origin_kind = 'policy_transition')
"""

_DELETE_POLICY_TRANSITION_INTENTS_SQL: Final[str] = (
    "DELETE FROM knowledge.projection_intents WHERE origin_kind = 'policy_transition'"
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
    IF application_table_count <> 29 THEN
        RAISE EXCEPTION 'policy_schema_table_count_invalid';
    END IF;
    SELECT count(*) INTO trigger_function_count
    FROM pg_catalog.pg_proc pgp
    JOIN pg_catalog.pg_namespace pgn ON pgn.oid = pgp.pronamespace
    WHERE pgn.nspname = 'knowledge';
    IF trigger_function_count <> 4 THEN
        RAISE EXCEPTION 'policy_schema_function_count_invalid';
    END IF;
    SELECT count(*) INTO protection_trigger_count
    FROM pg_catalog.pg_trigger pgt
    JOIN pg_catalog.pg_class pgc ON pgc.oid = pgt.tgrelid
    JOIN pg_catalog.pg_namespace pgn ON pgn.oid = pgc.relnamespace
    WHERE pgn.nspname = 'knowledge' AND NOT pgt.tgisinternal;
    IF protection_trigger_count <> 11 THEN
        RAISE EXCEPTION 'policy_schema_trigger_count_invalid';
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
    IF application_table_count <> 17 THEN
        RAISE EXCEPTION 'policy_downgrade_table_count_invalid';
    END IF;
    SELECT count(*) INTO trigger_function_count
    FROM pg_catalog.pg_proc pgp
    JOIN pg_catalog.pg_namespace pgn ON pgn.oid = pgp.pronamespace
    WHERE pgn.nspname = 'knowledge';
    IF trigger_function_count <> 3 THEN
        RAISE EXCEPTION 'policy_downgrade_function_count_invalid';
    END IF;
    SELECT count(*) INTO protection_trigger_count
    FROM pg_catalog.pg_trigger pgt
    JOIN pg_catalog.pg_class pgc ON pgc.oid = pgt.tgrelid
    JOIN pg_catalog.pg_namespace pgn ON pgn.oid = pgc.relnamespace
    WHERE pgn.nspname = 'knowledge' AND NOT pgt.tgisinternal;
    IF protection_trigger_count <> 5 THEN
        RAISE EXCEPTION 'policy_downgrade_trigger_count_invalid';
    END IF;
END;
$$
"""


def _downgrade_gate_open() -> bool:
    """Report whether the explicit destructive x-argument is present.

    Mirrors ``EnvironmentContext.get_x_argument`` resolution without importing
    the environment surface: the ``-x`` values ride on the active command
    options of the Alembic config. Absent command options (every in-process
    API caller that did not opt in) leave the gate closed.
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
    op.create_table(
        "workspace_policy_state",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("active_policy_revision_id", sa.Uuid(), nullable=True),
        sa.Column(
            "active_revision_number",
            sa.BigInteger(),
            server_default=sa.text("0"),
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
        sa.PrimaryKeyConstraint("workspace_id", name="pk_workspace_policy_state"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["knowledge.workspaces.workspace_id"],
            name="fk_workspace_policy_state__workspace",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "active_revision_number >= 0",
            name="ck_workspace_policy_state__active_revision_number",
        ),
        sa.CheckConstraint(
            "(active_policy_revision_id IS NULL) = (active_revision_number = 0)",
            name="ck_workspace_policy_state__active_pointer",
        ),
        sa.CheckConstraint(
            "updated_at >= created_at", name="ck_workspace_policy_state__timestamps"
        ),
        schema=SCHEMA_NAME,
    )
    op.create_table(
        "policy_signing_keys",
        sa.Column("signing_key_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column(
            "algorithm",
            sa.Text(),
            server_default=sa.text("'Ed25519'"),
            nullable=False,
        ),
        sa.Column("public_key_bytes", sa.LargeBinary(), nullable=False),
        sa.Column("introduced_keyset_revision", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            _TIMESTAMP_TYPE,
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("signing_key_id", name="pk_policy_signing_keys"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["knowledge.workspaces.workspace_id"],
            name="fk_policy_signing_keys__workspace",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("public_key_bytes", name="uq_policy_signing_keys__public_key_bytes"),
        sa.CheckConstraint("algorithm = 'Ed25519'", name="ck_policy_signing_keys__algorithm"),
        sa.CheckConstraint(
            "octet_length(public_key_bytes) = 32",
            name="ck_policy_signing_keys__public_key_bytes",
        ),
        sa.CheckConstraint(
            "introduced_keyset_revision >= 1",
            name="ck_policy_signing_keys__introduced_keyset_revision",
        ),
        schema=SCHEMA_NAME,
    )
    op.create_table(
        "policy_keysets",
        sa.Column("policy_keyset_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("keyset_revision", sa.BigInteger(), nullable=False),
        sa.Column("parent_keyset_revision", sa.BigInteger(), nullable=True),
        sa.Column("canonical_payload_bytes", sa.LargeBinary(), nullable=False),
        sa.Column("payload_sha256", sa.String(length=64), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            _TIMESTAMP_TYPE,
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("policy_keyset_id", name="pk_policy_keysets"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["knowledge.workspaces.workspace_id"],
            name="fk_policy_keysets__workspace",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["knowledge.users.user_id"],
            name="fk_policy_keysets__created_by_user",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "workspace_id", "keyset_revision", name="uq_policy_keysets__workspace_revision"
        ),
        sa.CheckConstraint("keyset_revision >= 1", name="ck_policy_keysets__keyset_revision"),
        sa.CheckConstraint(
            "(keyset_revision = 1) = (parent_keyset_revision IS NULL) "
            "AND (parent_keyset_revision IS NULL OR parent_keyset_revision < keyset_revision)",
            name="ck_policy_keysets__parent_lineage",
        ),
        sa.CheckConstraint(
            f"octet_length(canonical_payload_bytes) BETWEEN 1 AND {_MAXIMUM_PAYLOAD_BYTES}",
            name="ck_policy_keysets__payload_bytes",
        ),
        sa.CheckConstraint(
            f"payload_sha256 {_SHA256_CHECK}", name="ck_policy_keysets__payload_sha256"
        ),
        schema=SCHEMA_NAME,
    )
    op.create_table(
        "policy_keyset_signatures",
        sa.Column("policy_keyset_id", sa.Uuid(), nullable=False),
        sa.Column("signing_key_id", sa.Uuid(), nullable=False),
        sa.Column("signature_bytes", sa.LargeBinary(), nullable=False),
        sa.PrimaryKeyConstraint(
            "policy_keyset_id", "signing_key_id", name="pk_policy_keyset_signatures"
        ),
        sa.ForeignKeyConstraint(
            ["policy_keyset_id"],
            ["knowledge.policy_keysets.policy_keyset_id"],
            name="fk_policy_keyset_signatures__keyset",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["signing_key_id"],
            ["knowledge.policy_signing_keys.signing_key_id"],
            name="fk_policy_keyset_signatures__signing_key",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "octet_length(signature_bytes) = 64",
            name="ck_policy_keyset_signatures__signature_bytes",
        ),
        schema=SCHEMA_NAME,
    )
    op.create_table(
        "policy_drafts",
        sa.Column("policy_draft_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("draft_version", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("base_policy_revision_id", sa.Uuid(), nullable=True),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("updated_by_user_id", sa.Uuid(), nullable=True),
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
        sa.PrimaryKeyConstraint("policy_draft_id", name="pk_policy_drafts"),
        sa.UniqueConstraint("workspace_id", name="uq_policy_drafts__workspace"),
        sa.UniqueConstraint(
            "workspace_id", "policy_draft_id", name="uq_policy_drafts__workspace_draft"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["knowledge.workspaces.workspace_id"],
            name="fk_policy_drafts__workspace",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["knowledge.users.user_id"],
            name="fk_policy_drafts__created_by_user",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_user_id"],
            ["knowledge.users.user_id"],
            name="fk_policy_drafts__updated_by_user",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("draft_version >= 1", name="ck_policy_drafts__draft_version"),
        sa.CheckConstraint("updated_at >= created_at", name="ck_policy_drafts__timestamps"),
        schema=SCHEMA_NAME,
    )
    op.create_table(
        "policy_draft_rules",
        sa.Column("policy_draft_id", sa.Uuid(), nullable=False),
        sa.Column("rule_id", sa.Uuid(), nullable=False),
        sa.Column("rule_kind", sa.Text(), nullable=False),
        sa.Column("source_id_operand", sa.Uuid(), nullable=True),
        sa.Column("text_operand", sa.String(length=4096), nullable=True),
        sa.Column("size_bytes_operand", sa.BigInteger(), nullable=True),
        sa.Column("semantic_fingerprint", sa.String(length=64), nullable=False),
        sa.PrimaryKeyConstraint("policy_draft_id", "rule_id", name="pk_policy_draft_rules"),
        sa.ForeignKeyConstraint(
            ["policy_draft_id"],
            ["knowledge.policy_drafts.policy_draft_id"],
            name="fk_policy_draft_rules__draft",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(_RULE_KIND_CHECK, name="ck_policy_draft_rules__rule_kind"),
        sa.CheckConstraint(_RULE_OPERAND_SHAPE_CHECK, name="ck_policy_draft_rules__operand_shape"),
        sa.CheckConstraint(
            _RULE_OPERAND_BOUNDS_CHECK, name="ck_policy_draft_rules__operand_bounds"
        ),
        sa.CheckConstraint(
            _RULE_FINGERPRINT_CHECK, name="ck_policy_draft_rules__semantic_fingerprint"
        ),
        schema=SCHEMA_NAME,
    )
    op.create_table(
        "policy_previews",
        sa.Column("policy_preview_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("policy_draft_id", sa.Uuid(), nullable=False),
        sa.Column("draft_version", sa.BigInteger(), nullable=False),
        sa.Column("draft_sha256", sa.String(length=64), nullable=False),
        sa.Column("base_policy_revision_id", sa.Uuid(), nullable=True),
        sa.Column("source_checkpoint_event_sequence", sa.BigInteger(), nullable=False),
        sa.Column(
            "state",
            sa.Text(),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column(
            "newly_excluded_count", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "still_excluded_count", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("newly_allowed_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("still_allowed_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("indeterminate_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("impact_digest", sa.String(length=64), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "available_at",
            _TIMESTAMP_TYPE,
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("lease_token", sa.Uuid(), nullable=True),
        sa.Column("leased_until", _TIMESTAMP_TYPE, nullable=True),
        sa.Column("safe_error_code", sa.String(length=100), nullable=True),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            _TIMESTAMP_TYPE,
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("ready_at", _TIMESTAMP_TYPE, nullable=True),
        sa.Column("expires_at", _TIMESTAMP_TYPE, nullable=True),
        sa.Column("consumed_at", _TIMESTAMP_TYPE, nullable=True),
        sa.PrimaryKeyConstraint("policy_preview_id", name="pk_policy_previews"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["knowledge.workspaces.workspace_id"],
            name="fk_policy_previews__workspace",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "policy_draft_id"],
            ["knowledge.policy_drafts.workspace_id", "knowledge.policy_drafts.policy_draft_id"],
            name="fk_policy_previews__draft",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["knowledge.users.user_id"],
            name="fk_policy_previews__created_by_user",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'leased', 'running', 'ready', 'failed', 'expired', 'consumed')",
            name="ck_policy_previews__state",
        ),
        sa.CheckConstraint("draft_version >= 1", name="ck_policy_previews__draft_version"),
        sa.CheckConstraint(
            f"draft_sha256 {_SHA256_CHECK}", name="ck_policy_previews__draft_sha256"
        ),
        sa.CheckConstraint(
            "source_checkpoint_event_sequence >= 0",
            name="ck_policy_previews__source_checkpoint_event_sequence",
        ),
        sa.CheckConstraint(
            "newly_excluded_count >= 0 AND still_excluded_count >= 0 "
            "AND newly_allowed_count >= 0 AND still_allowed_count >= 0 "
            "AND indeterminate_count >= 0",
            name="ck_policy_previews__impact_counters",
        ),
        sa.CheckConstraint(
            "((state IN ('pending', 'leased', 'running', 'failed')) = (impact_digest IS NULL)) "
            "OR state = 'expired'",
            name="ck_policy_previews__impact_digest",
        ),
        sa.CheckConstraint("attempt_count >= 0", name="ck_policy_previews__attempt_count"),
        sa.CheckConstraint(
            "(state = 'leased') = (lease_token IS NOT NULL AND leased_until IS NOT NULL) "
            "AND (state <> 'leased' OR leased_until > created_at)",
            name="ck_policy_previews__lease",
        ),
        sa.CheckConstraint(
            "(((state IN ('pending', 'leased', 'running', 'failed')) "
            "= (ready_at IS NULL)) OR state = 'expired') "
            "AND (state = 'consumed') = (consumed_at IS NOT NULL)",
            name="ck_policy_previews__ready_consumed",
        ),
        sa.CheckConstraint(
            "state <> 'failed' OR safe_error_code IS NOT NULL",
            name="ck_policy_previews__failed_error",
        ),
        sa.CheckConstraint(
            f"safe_error_code IS NULL OR safe_error_code {_SAFE_ERROR_CODE_CHECK}",
            name="ck_policy_previews__error_code",
        ),
        sa.CheckConstraint(
            "(ready_at IS NULL OR ready_at >= created_at) "
            "AND (expires_at IS NULL OR (ready_at IS NOT NULL AND expires_at >= ready_at)) "
            "AND (consumed_at IS NULL "
            "OR (ready_at IS NOT NULL AND consumed_at >= ready_at))",
            name="ck_policy_previews__timestamps",
        ),
        schema=SCHEMA_NAME,
    )
    op.create_table(
        "source_policies",
        sa.Column("policy_revision_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("revision_number", sa.BigInteger(), nullable=False),
        sa.Column("parent_policy_revision_id", sa.Uuid(), nullable=True),
        sa.Column(
            "default_decision",
            sa.Text(),
            server_default=sa.text("'allowed'"),
            nullable=False,
        ),
        sa.Column("source_checkpoint_event_sequence", sa.BigInteger(), nullable=False),
        sa.Column("policy_preview_id", sa.Uuid(), nullable=False),
        sa.Column("publication_idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("snapshot_contract", sa.String(length=100), nullable=False),
        sa.Column("snapshot_payload_bytes", sa.LargeBinary(), nullable=False),
        sa.Column("snapshot_payload_sha256", sa.String(length=64), nullable=False),
        sa.Column("signing_key_id", sa.Uuid(), nullable=False),
        sa.Column("signature_bytes", sa.LargeBinary(), nullable=False),
        sa.Column("published_by_user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "published_at",
            _TIMESTAMP_TYPE,
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("policy_revision_id", name="pk_source_policies"),
        sa.UniqueConstraint(
            "workspace_id", "revision_number", name="uq_source_policies__workspace_revision"
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "policy_revision_id",
            name="uq_source_policies__workspace_revision_id",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "publication_idempotency_key",
            name="uq_source_policies__workspace_idempotency_key",
        ),
        sa.UniqueConstraint("policy_preview_id", name="uq_source_policies__policy_preview_id"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["knowledge.workspaces.workspace_id"],
            name="fk_source_policies__workspace",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "parent_policy_revision_id"],
            [
                "knowledge.source_policies.workspace_id",
                "knowledge.source_policies.policy_revision_id",
            ],
            name="fk_source_policies__parent",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["policy_preview_id"],
            ["knowledge.policy_previews.policy_preview_id"],
            name="fk_source_policies__preview",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["signing_key_id"],
            ["knowledge.policy_signing_keys.signing_key_id"],
            name="fk_source_policies__signing_key",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["published_by_user_id"],
            ["knowledge.users.user_id"],
            name="fk_source_policies__published_by_user",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("revision_number >= 1", name="ck_source_policies__revision_number"),
        sa.CheckConstraint(
            "(revision_number = 1) = (parent_policy_revision_id IS NULL) "
            "AND (parent_policy_revision_id IS NULL "
            "OR parent_policy_revision_id <> policy_revision_id)",
            name="ck_source_policies__parent_lineage",
        ),
        sa.CheckConstraint(
            "default_decision = 'allowed'", name="ck_source_policies__default_decision"
        ),
        sa.CheckConstraint(
            "source_checkpoint_event_sequence >= 0",
            name="ck_source_policies__source_checkpoint_event_sequence",
        ),
        sa.CheckConstraint(
            f"publication_idempotency_key {_PRINTABLE_KEY_CHECK}",
            name="ck_source_policies__publication_idempotency_key",
        ),
        sa.CheckConstraint(
            f"request_fingerprint {_SHA256_CHECK}",
            name="ck_source_policies__request_fingerprint",
        ),
        sa.CheckConstraint(
            r"snapshot_contract ~ '^[a-z][a-z0-9_.-]*/[a-z0-9_.-]+$' "
            "AND char_length(snapshot_contract) <= 100",
            name="ck_source_policies__snapshot_contract",
        ),
        sa.CheckConstraint(
            f"octet_length(snapshot_payload_bytes) BETWEEN 1 AND {_MAXIMUM_PAYLOAD_BYTES}",
            name="ck_source_policies__snapshot_payload_bytes",
        ),
        sa.CheckConstraint(
            f"snapshot_payload_sha256 {_SHA256_CHECK}",
            name="ck_source_policies__snapshot_payload_sha256",
        ),
        sa.CheckConstraint(
            "octet_length(signature_bytes) = 64",
            name="ck_source_policies__signature_bytes",
        ),
        schema=SCHEMA_NAME,
    )
    op.create_table(
        "policy_rules",
        sa.Column("policy_revision_id", sa.Uuid(), nullable=False),
        sa.Column("rule_id", sa.Uuid(), nullable=False),
        sa.Column("rule_kind", sa.Text(), nullable=False),
        sa.Column("source_id_operand", sa.Uuid(), nullable=True),
        sa.Column("text_operand", sa.String(length=4096), nullable=True),
        sa.Column("size_bytes_operand", sa.BigInteger(), nullable=True),
        sa.Column("semantic_fingerprint", sa.String(length=64), nullable=False),
        sa.PrimaryKeyConstraint("policy_revision_id", "rule_id", name="pk_policy_rules"),
        sa.ForeignKeyConstraint(
            ["policy_revision_id"],
            ["knowledge.source_policies.policy_revision_id"],
            name="fk_policy_rules__policy_revision",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(_RULE_KIND_CHECK, name="ck_policy_rules__rule_kind"),
        sa.CheckConstraint(_RULE_OPERAND_SHAPE_CHECK, name="ck_policy_rules__operand_shape"),
        sa.CheckConstraint(_RULE_OPERAND_BOUNDS_CHECK, name="ck_policy_rules__operand_bounds"),
        sa.CheckConstraint(_RULE_FINGERPRINT_CHECK, name="ck_policy_rules__semantic_fingerprint"),
        schema=SCHEMA_NAME,
    )
    op.create_table(
        "policy_preview_results",
        sa.Column("policy_preview_id", sa.Uuid(), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("previous_raw_decision", sa.Text(), nullable=False),
        sa.Column("previous_enforced_decision", sa.Text(), nullable=False),
        sa.Column("proposed_raw_decision", sa.Text(), nullable=False),
        sa.Column("proposed_enforced_decision", sa.Text(), nullable=False),
        sa.Column("proposed_match_state", sa.Text(), nullable=False),
        sa.Column("impact_class", sa.Text(), nullable=False),
        sa.Column("matched_rule_ids", sa.Text(), nullable=False),
        sa.Column("missing_fields", sa.Text(), nullable=False),
        sa.Column("subject_fingerprint", sa.String(length=64), nullable=False),
        sa.PrimaryKeyConstraint("policy_preview_id", "source_id", name="pk_policy_preview_results"),
        sa.ForeignKeyConstraint(
            ["policy_preview_id"],
            ["knowledge.policy_previews.policy_preview_id"],
            name="fk_policy_preview_results__preview",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["knowledge.sources.source_id"],
            name="fk_policy_preview_results__source",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            _RAW_DECISION_CHECK.format(column="previous_raw_decision"),
            name="ck_policy_preview_results__previous_raw_decision",
        ),
        sa.CheckConstraint(
            _ENFORCED_DECISION_CHECK.format(column="previous_enforced_decision"),
            name="ck_policy_preview_results__previous_enforced_decision",
        ),
        sa.CheckConstraint(
            _RAW_DECISION_CHECK.format(column="proposed_raw_decision"),
            name="ck_policy_preview_results__proposed_raw_decision",
        ),
        sa.CheckConstraint(
            _ENFORCED_DECISION_CHECK.format(column="proposed_enforced_decision"),
            name="ck_policy_preview_results__proposed_enforced_decision",
        ),
        sa.CheckConstraint(
            "proposed_match_state IN ('matched', 'not_matched', 'indeterminate')",
            name="ck_policy_preview_results__proposed_match_state",
        ),
        sa.CheckConstraint(
            "impact_class IN ('newly_excluded', 'still_excluded', 'newly_allowed',"
            " 'still_allowed', 'indeterminate')",
            name="ck_policy_preview_results__impact_class",
        ),
        sa.CheckConstraint(
            _MATCHED_RULE_IDS_CHECK, name="ck_policy_preview_results__matched_rule_ids"
        ),
        sa.CheckConstraint(_MISSING_FIELDS_CHECK, name="ck_policy_preview_results__missing_fields"),
        sa.CheckConstraint(
            f"subject_fingerprint {_SHA256_CHECK}",
            name="ck_policy_preview_results__subject_fingerprint",
        ),
        schema=SCHEMA_NAME,
    )
    op.create_table(
        "policy_evaluations",
        sa.Column("policy_evaluation_id", sa.Uuid(), nullable=False),
        sa.Column("policy_revision_id", sa.Uuid(), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("subject_event_sequence", sa.BigInteger(), nullable=False),
        sa.Column("raw_decision", sa.Text(), nullable=False),
        sa.Column("enforced_decision", sa.Text(), nullable=False),
        sa.Column("matched_rule_ids", sa.Text(), nullable=False),
        sa.Column("missing_fields", sa.Text(), nullable=False),
        sa.Column("subject_fingerprint", sa.String(length=64), nullable=False),
        sa.Column(
            "evaluated_at",
            _TIMESTAMP_TYPE,
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("policy_evaluation_id", name="pk_policy_evaluations"),
        sa.UniqueConstraint(
            "policy_revision_id",
            "source_id",
            "subject_event_sequence",
            name="uq_policy_evaluations__revision_source_sequence",
        ),
        sa.ForeignKeyConstraint(
            ["policy_revision_id"],
            ["knowledge.source_policies.policy_revision_id"],
            name="fk_policy_evaluations__policy_revision",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["knowledge.sources.source_id"],
            name="fk_policy_evaluations__source",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "subject_event_sequence >= 1", name="ck_policy_evaluations__subject_event_sequence"
        ),
        sa.CheckConstraint(
            _RAW_DECISION_CHECK.format(column="raw_decision"),
            name="ck_policy_evaluations__raw_decision",
        ),
        sa.CheckConstraint(
            _ENFORCED_DECISION_CHECK.format(column="enforced_decision"),
            name="ck_policy_evaluations__enforced_decision",
        ),
        sa.CheckConstraint(_MATCHED_RULE_IDS_CHECK, name="ck_policy_evaluations__matched_rule_ids"),
        sa.CheckConstraint(_MISSING_FIELDS_CHECK, name="ck_policy_evaluations__missing_fields"),
        sa.CheckConstraint(
            f"subject_fingerprint {_SHA256_CHECK}",
            name="ck_policy_evaluations__subject_fingerprint",
        ),
        schema=SCHEMA_NAME,
    )
    op.create_table(
        "policy_reconciliation_intents",
        sa.Column("policy_reconciliation_intent_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("policy_revision_id", sa.Uuid(), nullable=False),
        sa.Column("workflow_id", sa.String(length=200), nullable=False),
        sa.Column(
            "state",
            sa.Text(),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "available_at",
            _TIMESTAMP_TYPE,
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("lease_token", sa.Uuid(), nullable=True),
        sa.Column("leased_until", _TIMESTAMP_TYPE, nullable=True),
        sa.Column("dispatched_at", _TIMESTAMP_TYPE, nullable=True),
        sa.Column("safe_error_code", sa.String(length=100), nullable=True),
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
        sa.PrimaryKeyConstraint(
            "policy_reconciliation_intent_id", name="pk_policy_reconciliation_intents"
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "policy_revision_id",
            name="uq_policy_reconciliation_intents__workspace_revision",
        ),
        sa.UniqueConstraint("workflow_id", name="uq_policy_reconciliation_intents__workflow_id"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["knowledge.workspaces.workspace_id"],
            name="fk_policy_reconciliation_intents__workspace",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "policy_revision_id"],
            [
                "knowledge.source_policies.workspace_id",
                "knowledge.source_policies.policy_revision_id",
            ],
            name="fk_policy_reconciliation_intents__policy_revision",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'leased', 'dispatched', 'terminal')",
            name="ck_policy_reconciliation_intents__state",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0", name="ck_policy_reconciliation_intents__attempt_count"
        ),
        sa.CheckConstraint(
            "char_length(workflow_id) BETWEEN 20 AND 200 "
            r"AND workflow_id ~ '^[a-z][a-z0-9._/-]+$'",
            name="ck_policy_reconciliation_intents__workflow_id",
        ),
        sa.CheckConstraint(
            "(state = 'leased') = (lease_token IS NOT NULL) "
            "AND (state = 'leased') = (leased_until IS NOT NULL) "
            "AND (state <> 'leased' OR leased_until > updated_at)",
            name="ck_policy_reconciliation_intents__lease",
        ),
        sa.CheckConstraint(
            "(state = 'dispatched') = (dispatched_at IS NOT NULL)",
            name="ck_policy_reconciliation_intents__dispatch",
        ),
        sa.CheckConstraint(
            "state <> 'terminal' OR safe_error_code IS NOT NULL",
            name="ck_policy_reconciliation_intents__terminal_error",
        ),
        sa.CheckConstraint(
            f"safe_error_code IS NULL OR safe_error_code {_SAFE_ERROR_CODE_CHECK}",
            name="ck_policy_reconciliation_intents__error_code",
        ),
        sa.CheckConstraint(
            "available_at >= created_at AND updated_at >= created_at",
            name="ck_policy_reconciliation_intents__timestamps",
        ),
        schema=SCHEMA_NAME,
    )

    # Cross-table pointers that could not be declared inline: the active
    # revision pointer and the draft/preview bases reference source_policies.
    op.create_foreign_key(
        "fk_workspace_policy_state__active_revision",
        "workspace_policy_state",
        "source_policies",
        ["workspace_id", "active_policy_revision_id"],
        ["workspace_id", "policy_revision_id"],
        source_schema=SCHEMA_NAME,
        referent_schema=SCHEMA_NAME,
        ondelete="RESTRICT",
        deferrable=True,
        initially="IMMEDIATE",
    )
    op.create_foreign_key(
        "fk_policy_drafts__base_revision",
        "policy_drafts",
        "source_policies",
        ["workspace_id", "base_policy_revision_id"],
        ["workspace_id", "policy_revision_id"],
        source_schema=SCHEMA_NAME,
        referent_schema=SCHEMA_NAME,
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_policy_previews__base_revision",
        "policy_previews",
        "source_policies",
        ["workspace_id", "base_policy_revision_id"],
        ["workspace_id", "policy_revision_id"],
        source_schema=SCHEMA_NAME,
        referent_schema=SCHEMA_NAME,
        ondelete="RESTRICT",
    )

    op.create_index(
        "ix_policy_signing_keys__workspace_revision",
        "policy_signing_keys",
        ["workspace_id", "introduced_keyset_revision"],
        schema=SCHEMA_NAME,
    )
    op.create_index(
        "ix_policy_previews__workspace_state",
        "policy_previews",
        ["workspace_id", "state", "created_at", "policy_preview_id"],
        schema=SCHEMA_NAME,
    )
    op.create_index(
        "ix_policy_previews__pending_dispatch",
        "policy_previews",
        ["available_at", "created_at", "policy_preview_id"],
        schema=SCHEMA_NAME,
        postgresql_where=sa.text("state = 'pending'"),
    )
    op.create_index(
        "ix_policy_preview_results__impact_cursor",
        "policy_preview_results",
        ["policy_preview_id", "impact_class", "source_id"],
        schema=SCHEMA_NAME,
    )
    op.create_index(
        "ix_policy_evaluations__revision_sequence",
        "policy_evaluations",
        ["policy_revision_id", "subject_event_sequence", "source_id"],
        schema=SCHEMA_NAME,
    )
    op.create_index(
        "ix_policy_reconciliation_intents__pending_dispatch",
        "policy_reconciliation_intents",
        ["available_at", "created_at", "policy_reconciliation_intent_id"],
        schema=SCHEMA_NAME,
        postgresql_where=sa.text("state = 'pending'"),
    )

    # The projection-intent origin discriminator: existing rows backfill the
    # source-event origin through the NOT NULL server default.
    op.add_column(
        "projection_intents",
        sa.Column(
            "origin_kind",
            sa.Text(),
            server_default=sa.text("'source_event'"),
            nullable=False,
        ),
        schema=SCHEMA_NAME,
    )
    op.add_column(
        "projection_intents",
        sa.Column("policy_revision_id", sa.Uuid(), nullable=True),
        schema=SCHEMA_NAME,
    )
    op.alter_column(
        "projection_intents",
        "event_id",
        existing_type=sa.Uuid(),
        nullable=True,
        schema=SCHEMA_NAME,
    )
    op.create_check_constraint(
        "ck_projection_intents__origin",
        "projection_intents",
        "origin_kind IN ('source_event', 'policy_transition') "
        "AND ((origin_kind = 'source_event') = (event_id IS NOT NULL)) "
        "AND ((origin_kind = 'policy_transition') = (policy_revision_id IS NOT NULL))",
        schema=SCHEMA_NAME,
    )
    op.create_foreign_key(
        "fk_projection_intents__policy_revision",
        "projection_intents",
        "source_policies",
        ["workspace_id", "policy_revision_id"],
        ["workspace_id", "policy_revision_id"],
        source_schema=SCHEMA_NAME,
        referent_schema=SCHEMA_NAME,
        ondelete="RESTRICT",
    )
    op.create_index(
        "uq_projection_intents__policy_transition",
        "projection_intents",
        ["policy_revision_id", "source_id", "projection_kind"],
        unique=True,
        schema=SCHEMA_NAME,
        postgresql_where=sa.text("origin_kind = 'policy_transition'"),
    )

    op.execute(sa.text(_REJECT_POLICY_MUTATION_FUNCTION_SQL))
    op.execute(sa.text(_REVOKE_ROUTINE_EXECUTE_SQL))
    for trigger_statement in _APPEND_ONLY_TRIGGER_STATEMENTS:
        op.execute(sa.text(trigger_statement))

    _seed_workspace_policy_rows()

    op.execute(sa.text(_FINAL_CATALOG_ASSERTION_SQL))


def _seed_workspace_policy_rows() -> None:
    """Seed one unpublished state row and one empty draft per workspace.

    Every identity is a Python-generated UUIDv7; every statement is
    parameter-bound. The seeded rows never publish or sign a policy, so the
    active pointer stays null and the revision number stays zero.
    """
    bind = op.get_bind()
    workspace_rows = bind.execute(sa.text(_SEED_WORKSPACES_SQL)).fetchall()
    for workspace_row in workspace_rows:
        bind.execute(
            sa.text(_SEED_POLICY_STATE_SQL),
            {"workspace_id": workspace_row.workspace_id},
        )
        bind.execute(
            sa.text(_SEED_POLICY_DRAFT_SQL),
            {"policy_draft_id": uuid7(), "workspace_id": workspace_row.workspace_id},
        )


def downgrade() -> None:
    bind = op.get_bind()
    protected_row_count = int(bind.execute(sa.text(_DOWNGRADE_GATE_COUNT_SQL)).scalar_one())
    if protected_row_count > 0 and not _downgrade_gate_open():
        raise RuntimeError(_DOWNGRADE_REFUSAL_MESSAGE)

    op.execute(sa.text(_DELETE_POLICY_TRANSITION_INTENTS_SQL))
    op.drop_index(
        "uq_projection_intents__policy_transition",
        table_name="projection_intents",
        schema=SCHEMA_NAME,
    )
    op.drop_constraint(
        "fk_projection_intents__policy_revision",
        "projection_intents",
        schema=SCHEMA_NAME,
        type_="foreignkey",
    )
    op.drop_constraint(
        "ck_projection_intents__origin",
        "projection_intents",
        schema=SCHEMA_NAME,
        type_="check",
    )
    op.drop_column("projection_intents", "policy_revision_id", schema=SCHEMA_NAME)
    op.alter_column(
        "projection_intents",
        "event_id",
        existing_type=sa.Uuid(),
        nullable=False,
        schema=SCHEMA_NAME,
    )
    op.drop_column("projection_intents", "origin_kind", schema=SCHEMA_NAME)

    for table_name in _APPEND_ONLY_TRIGGER_TABLES:
        op.execute(
            sa.text(f"DROP TRIGGER trg_{table_name}__reject_mutation ON knowledge.{table_name}")
        )
    op.execute(sa.text("DROP FUNCTION knowledge.reject_policy_history_mutation"))

    op.drop_constraint(
        "fk_policy_previews__base_revision",
        "policy_previews",
        schema=SCHEMA_NAME,
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_policy_drafts__base_revision",
        "policy_drafts",
        schema=SCHEMA_NAME,
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_workspace_policy_state__active_revision",
        "workspace_policy_state",
        schema=SCHEMA_NAME,
        type_="foreignkey",
    )
    op.drop_table("policy_keyset_signatures", schema=SCHEMA_NAME)
    op.drop_table("policy_keysets", schema=SCHEMA_NAME)
    op.drop_table("policy_evaluations", schema=SCHEMA_NAME)
    op.drop_table("policy_preview_results", schema=SCHEMA_NAME)
    op.drop_table("policy_reconciliation_intents", schema=SCHEMA_NAME)
    op.drop_table("policy_rules", schema=SCHEMA_NAME)
    op.drop_table("source_policies", schema=SCHEMA_NAME)
    op.drop_table("policy_previews", schema=SCHEMA_NAME)
    op.drop_table("policy_draft_rules", schema=SCHEMA_NAME)
    op.drop_table("policy_drafts", schema=SCHEMA_NAME)
    op.drop_table("workspace_policy_state", schema=SCHEMA_NAME)
    op.drop_table("policy_signing_keys", schema=SCHEMA_NAME)

    op.execute(sa.text(_FINAL_DOWNGRADE_ASSERTION_SQL))
