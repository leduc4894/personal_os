"""Exclusion-policy publication store pure-helper contracts (PostgreSQL side).

These tests pin the publication persistence helpers without touching a
database: the policy idempotency advisory-lock namespace and key derivation
(distinct from the source families), the serialization-row ``FOR UPDATE``
statement, the replay lookup joined with the signing key and reconciliation
state, the deterministic reconciliation workflow identity satisfying the
column CHECK grammar, the typed rule-row mapping for every closed kind, the
guarded preview-consumption / draft-rebase / active-pointer statements, the
published and rejected audit-row values carrying identifiers and safe hashes
only, and the replay hydration failing closed on containment or shape
violations.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from tests.unit.exclusion_policy.fakes import rule

# Imported first: loading the diagnostics package before the error-contracts
# exceptions module keeps their module-level re-export cycle resolvable.
from personal_os.diagnostics.events import SafeToken  # noqa: F401
from personal_os.error_contracts.exceptions import InternalApplicationError
from personal_os.exclusion_policy.contracts import ExclusionRule, RuleKind
from personal_os.exclusion_policy.ports import PolicyActor, PolicyActorKind
from personal_os.sources.commands import IdempotencyKey
from postgresql_source_store.locks import (
    IDEMPOTENCY_LOCK_NAMESPACE,
    SOURCE_LOCK_NAMESPACE,
    idempotency_lock_key,
    signed_first_sha256_word,
)
from postgresql_source_store.policy_publication import (
    AUDIT_RESULT_REJECTED,
    AUDIT_RESULT_SUCCEEDED,
    POLICY_IDEMPOTENCY_LOCK_NAMESPACE,
    POLICY_REVISION_AUDIT_TARGET_KIND,
    PUBLISH_REJECTED_AUDIT_ACTION,
    PUBLISHED_AUDIT_ACTION,
    RECONCILIATION_WORKFLOW_ID_PREFIX,
    PolicyPublicationIdentities,
    build_policy_rule_values,
    build_publish_rejected_audit_values,
    build_published_audit_values,
    build_reconciliation_intent_values,
    hydrate_replay_result,
    hydration_key_id,
    mark_preview_consumed_statement,
    policy_idempotency_lock_key,
    policy_idempotency_lock_statement,
    policy_state_lock_statement,
    rebase_draft_after_publication_statement,
    reconciliation_workflow_id,
    replay_lookup_by_key_statement,
    swap_active_pointer_statement,
)

WORKSPACE_ID = UUID("018f47a0-7b00-7000-8000-0000000000d1")
OTHER_WORKSPACE_ID = UUID("018f47a0-7b00-7000-8000-0000000000d2")
DRAFT_ID = UUID("018f47a0-7b00-7000-8000-0000000000d3")
PREVIEW_ID = UUID("018f47a0-7b00-7000-8000-0000000000d4")
REVISION_ID = UUID("018f47a0-7b00-7000-8000-0000000000d5")
USER_ID = UUID("018f47a0-7b00-7000-8000-0000000000d6")
SOURCE_ID_OPERAND = UUID("018f47a0-7b00-7000-8000-0000000000e1")
OCCURRED_AT = datetime(2026, 8, 17, 10, 0, 0, tzinfo=UTC)
REQUEST_ID = UUID("018f47a0-7b00-7000-8000-0000000000e2")
IDEMPOTENCY_KEY = IdempotencyKey("publish-replay-001")
PAYLOAD_SHA256 = "ab" * 32
PUBLIC_KEY_BYTES = bytes(range(32))


def _actor() -> PolicyActor:
    return PolicyActor(actor_kind=PolicyActorKind.USER, user_id=USER_ID)


def _compile(statement: sa.ClauseElement) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


def _all_kinds_rules() -> tuple[ExclusionRule, ...]:
    return (
        rule(RuleKind.EXACT_SOURCE_ID, source_id_operand=SOURCE_ID_OPERAND),
        rule(RuleKind.FOLDER_PREFIX, text_operand="private/notes"),
        rule(RuleKind.PATH_GLOB, text_operand="attachments/**/*.tmp"),
        rule(RuleKind.EXTENSION, text_operand=".tmp"),
        rule(RuleKind.MEDIA_TYPE, text_operand="text/markdown"),
        rule(RuleKind.MAXIMUM_SIZE, size_bytes_operand=104857600),
        rule(RuleKind.SOURCE_TYPE, text_operand="pdf"),
    )


# --- advisory lock family ------------------------------------------------------------


def test_policy_lock_namespace_is_distinct_from_the_source_families() -> None:
    assert POLICY_IDEMPOTENCY_LOCK_NAMESPACE != IDEMPOTENCY_LOCK_NAMESPACE
    assert POLICY_IDEMPOTENCY_LOCK_NAMESPACE != SOURCE_LOCK_NAMESPACE


def test_policy_lock_key_derives_from_workspace_and_key_material() -> None:
    derived = policy_idempotency_lock_key(WORKSPACE_ID, IDEMPOTENCY_KEY)
    material = WORKSPACE_ID.bytes + b"\x00" + IDEMPOTENCY_KEY.value.encode("ascii")
    assert derived == signed_first_sha256_word(material)
    assert derived != policy_idempotency_lock_key(OTHER_WORKSPACE_ID, IDEMPOTENCY_KEY)
    assert derived != policy_idempotency_lock_key(
        WORKSPACE_ID, IdempotencyKey("publish-replay-002")
    )
    # The derivation may collide with the source family's derived key over the
    # same material; disjointness comes from the namespace integer, which the
    # dedicated namespace test pins.
    assert derived == idempotency_lock_key(WORKSPACE_ID, IDEMPOTENCY_KEY)


def test_policy_lock_statement_is_bound_and_transaction_scoped() -> None:
    statement = policy_idempotency_lock_statement(WORKSPACE_ID, IDEMPOTENCY_KEY)
    compiled = _compile(statement)
    assert "pg_advisory_xact_lock" in compiled
    assert compiled.count("%(namespace)s") == 1
    assert compiled.count("%(derived_key)s") == 1


# --- statements ----------------------------------------------------------------------


def test_policy_state_lock_statement_locks_the_serialization_row() -> None:
    compiled = _compile(policy_state_lock_statement(WORKSPACE_ID))
    assert "FOR UPDATE" in compiled
    assert "knowledge.workspace_policy_state" in compiled
    assert "active_policy_revision_id" in compiled
    assert "active_revision_number" in compiled


def test_replay_lookup_joins_signing_key_and_reconciliation_state() -> None:
    compiled = _compile(replay_lookup_by_key_statement(WORKSPACE_ID, IDEMPOTENCY_KEY))
    assert "knowledge.source_policies" in compiled
    assert "knowledge.policy_signing_keys" in compiled
    assert "knowledge.policy_reconciliation_intents" in compiled
    assert "knowledge.policy_rules" in compiled
    assert "publication_idempotency_key" in compiled


def test_mark_preview_consumed_is_guarded_on_the_ready_state() -> None:
    compiled = _compile(mark_preview_consumed_statement(PREVIEW_ID, OCCURRED_AT))
    assert "knowledge.policy_previews" in compiled
    assert "state" in compiled
    assert "consumed_at" in compiled


def test_rebase_draft_is_guarded_on_the_exact_version() -> None:
    compiled = _compile(
        rebase_draft_after_publication_statement(DRAFT_ID, 4, REVISION_ID, USER_ID, OCCURRED_AT)
    )
    assert "knowledge.policy_drafts" in compiled
    assert "draft_version" in compiled
    assert "base_policy_revision_id" in compiled


def test_swap_active_pointer_is_guarded_on_both_expected_members() -> None:
    compiled = _compile(
        swap_active_pointer_statement(WORKSPACE_ID, None, 0, REVISION_ID, 1, OCCURRED_AT)
    )
    assert "knowledge.workspace_policy_state" in compiled
    assert "active_policy_revision_id" in compiled
    assert "active_revision_number" in compiled


# --- reconciliation work -------------------------------------------------------------


def test_reconciliation_workflow_identity_is_derived_and_check_grammar_safe() -> None:
    workflow_id = reconciliation_workflow_id(WORKSPACE_ID, REVISION_ID)
    assert workflow_id.startswith(RECONCILIATION_WORKFLOW_ID_PREFIX + "/")
    assert workflow_id == (f"{RECONCILIATION_WORKFLOW_ID_PREFIX}/{WORKSPACE_ID}/{REVISION_ID}")
    assert 20 <= len(workflow_id) <= 200
    assert all(char.islower() or char.isdigit() or char in "._/-" for char in workflow_id)


def test_reconciliation_workflow_identity_rejects_nil_identities() -> None:
    with pytest.raises(ValueError):
        reconciliation_workflow_id(UUID(int=0), REVISION_ID)
    with pytest.raises(ValueError):
        reconciliation_workflow_id(WORKSPACE_ID, UUID(int=0))


def test_reconciliation_intent_values_are_complete_and_pending() -> None:
    values = build_reconciliation_intent_values(
        policy_reconciliation_intent_id=UUID(int=1),
        workspace_id=WORKSPACE_ID,
        policy_revision_id=REVISION_ID,
        workflow_id=reconciliation_workflow_id(WORKSPACE_ID, REVISION_ID),
        occurred_at=OCCURRED_AT,
    )
    assert values["state"] == "pending"
    assert values["attempt_count"] == 0
    assert values["available_at"] == OCCURRED_AT
    assert values["created_at"] == OCCURRED_AT
    assert values["updated_at"] == OCCURRED_AT
    assert values["lease_token"] is None
    assert values["dispatched_at"] is None
    assert values["safe_error_code"] is None


def test_publication_identities_allocate_fresh_uuid7_values() -> None:
    first = PolicyPublicationIdentities.allocate()
    second = PolicyPublicationIdentities.allocate()
    assert first.policy_revision_id != second.policy_revision_id
    assert first.audit_event_id != second.audit_event_id
    assert first.policy_reconciliation_intent_id != second.policy_reconciliation_intent_id


# --- rule rows -----------------------------------------------------------------------


def test_policy_rule_values_map_every_kind_to_one_typed_operand() -> None:
    for domain_rule in _all_kinds_rules():
        values = build_policy_rule_values(REVISION_ID, domain_rule)
        populated = [
            name
            for name in ("source_id_operand", "text_operand", "size_bytes_operand")
            if values[name] is not None
        ]
        assert len(populated) == 1
        assert values["policy_revision_id"] == REVISION_ID
        assert values["rule_id"] == domain_rule.rule_id
        assert values["rule_kind"] == domain_rule.rule_kind.value
        assert values["semantic_fingerprint"] == domain_rule.semantic_fingerprint


# --- audit values --------------------------------------------------------------------


def test_published_audit_values_carry_identifiers_and_safe_hash_only() -> None:
    values = build_published_audit_values(
        policy_revision_id=REVISION_ID,
        workspace_id=WORKSPACE_ID,
        actor=_actor(),
        payload_sha256=PAYLOAD_SHA256,
        occurred_at=OCCURRED_AT,
        request_id=REQUEST_ID,
        client_request_id=None,
        trace_id=None,
    )
    assert values["action"] == PUBLISHED_AUDIT_ACTION
    assert values["target_kind"] == POLICY_REVISION_AUDIT_TARGET_KIND
    assert values["target_id"] == REVISION_ID
    assert values["result"] == AUDIT_RESULT_SUCCEEDED
    assert values["safe_diff_hash"] == PAYLOAD_SHA256
    assert values["reason_code"] is None
    assert values["occurred_at"] == OCCURRED_AT
    assert not any(isinstance(value, (bytes, bytearray)) for value in values.values())


def test_publish_rejected_audit_values_carry_closed_reason_and_no_hash() -> None:
    values = build_publish_rejected_audit_values(
        workspace_id=WORKSPACE_ID,
        actor=_actor(),
        target_id=PREVIEW_ID,
        target_kind="policy_preview",
        reason_code="preview_expired",
        occurred_at=OCCURRED_AT,
        request_id=REQUEST_ID,
        client_request_id=None,
        trace_id=None,
    )
    assert values["action"] == PUBLISH_REJECTED_AUDIT_ACTION
    assert values["result"] == AUDIT_RESULT_REJECTED
    assert values["reason_code"] == "preview_expired"
    assert values["safe_diff_hash"] is None


# --- replay hydration ----------------------------------------------------------------


def _replay_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "workspace_id": WORKSPACE_ID,
        "policy_revision_id": REVISION_ID,
        "revision_number": 2,
        "parent_policy_revision_id": None,
        "request_fingerprint": "cd" * 32,
        "snapshot_payload_sha256": PAYLOAD_SHA256,
        "published_at": OCCURRED_AT,
        "public_key_bytes": PUBLIC_KEY_BYTES,
        "reconciliation_state": "pending",
        "rule_count": 3,
    }
    row.update(overrides)
    return row


def test_hydrate_replay_result_builds_the_exact_replay_value() -> None:
    from personal_os.exclusion_policy.signatures import derive_ed25519_key_id

    result = hydrate_replay_result(_replay_row(), WORKSPACE_ID)
    assert result.is_replay is True
    assert result.policy_revision_id == REVISION_ID
    assert result.revision_number == 2
    assert result.payload_sha256 == PAYLOAD_SHA256
    assert result.signing_key_id == derive_ed25519_key_id(PUBLIC_KEY_BYTES)
    assert result.reconciliation_status == "pending"
    assert result.rule_count == 3


def test_hydrate_replay_result_fails_closed_on_foreign_workspace() -> None:
    with pytest.raises(InternalApplicationError):
        hydrate_replay_result(_replay_row(), OTHER_WORKSPACE_ID)


@pytest.mark.parametrize(
    "overrides",
    [
        {"revision_number": 0},
        {"published_at": datetime(2026, 8, 17, 10, 0, 0)},
        {"snapshot_payload_sha256": "nothex"},
        {"public_key_bytes": b"short"},
        {"public_key_bytes": None},
    ],
)
def test_hydrate_replay_result_fails_closed_on_bad_shapes(overrides: dict[str, Any]) -> None:
    with pytest.raises(InternalApplicationError):
        hydrate_replay_result(_replay_row(**overrides), WORKSPACE_ID)


def test_hydration_key_id_derivation_matches_the_domain_derivation() -> None:
    from personal_os.exclusion_policy.signatures import derive_ed25519_key_id

    assert hydration_key_id(PUBLIC_KEY_BYTES) == derive_ed25519_key_id(PUBLIC_KEY_BYTES)
