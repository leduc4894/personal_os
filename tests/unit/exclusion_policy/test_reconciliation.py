"""Pure contracts of the publication-reconciliation domain half (spec 15/21).

These tests pin the provider-neutral reconciliation vocabulary without any
database or workflow engine: the closed ``exclusion_policy_reconciliation/v1``
input serializing only the contract tag, the two opaque UUIDs and the source
checkpoint; the deterministic workflow identity; the immutable evaluation
identity ``(policy_revision_id, source_id, subject_event_sequence)``; the
closed previous/proposed transition derivation and its deterministic
Qdrant/Neo4j intent plans gated on a non-null current version; the closed
no-policy and no-prior-evaluation previous-decision semantics; the pinned
batch/continue-as-new bounds; and the closed low-cardinality metrics recorder
that rejects forbidden labels. Sensitive sentinels must never appear in any
serialized value, and the module must import no workflow engine, database
driver or web framework.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

import pytest
from temporalio.converter import DataConverter

from personal_os.exclusion_policy.contracts import (
    EnforcedPolicyDecision,
    RawPolicyDecision,
)
from personal_os.exclusion_policy.reconciliation import (
    POLICY_TRANSITION_PROJECTION_KINDS,
    RECONCILIATION_BATCH_SIZE,
    RECONCILIATION_CONTINUE_AS_NEW_BATCHES,
    RECONCILIATION_CONTINUE_AS_NEW_SOURCES,
    RECONCILIATION_CONTRACT,
    RECONCILIATION_SOURCES_METRIC,
    RECONCILIATION_WORKFLOW_ID_PREFIX,
    InMemoryReconciliationMetrics,
    PolicyEvaluation,
    PolicyTransitionIntentPlan,
    PolicyTransitionOperation,
    PolicyTransitionProjectionKind,
    ReconciliationContinuation,
    ReconciliationCounters,
    ReconciliationInput,
    ReconciliationProgress,
    ReconciliationTransition,
    derive_reconciliation_transition,
    policy_transition_intent_plans,
    previous_enforced_without_policy,
    previous_enforced_without_prior_evaluation,
    reconciliation_workflow_id,
)

WORKSPACE_ID = UUID("018f47a0-7b00-7000-8000-0000000000e1")
POLICY_REVISION_ID = UUID("018f47a0-7b00-7000-8000-0000000000e2")
SOURCE_ID = UUID("018f47a0-7b00-7000-8000-0000000000e3")
AFTER_SOURCE_ID = UUID("018f47a0-7b00-7000-8000-0000000000e4")

_LEAKAGE_SENTINELS: tuple[str, ...] = (
    "sentinel-title",
    "private/notes/sentinel-locator.md",
    "sentinel operand",
)


def _input(
    *,
    workspace_id: UUID = WORKSPACE_ID,
    policy_revision_id: UUID = POLICY_REVISION_ID,
    source_checkpoint_event_sequence: int = 7,
) -> ReconciliationInput:
    return ReconciliationInput(
        contract=RECONCILIATION_CONTRACT,
        workspace_id=workspace_id,
        policy_revision_id=policy_revision_id,
        source_checkpoint_event_sequence=source_checkpoint_event_sequence,
    )


def _serialize(value: object) -> bytes:
    (payload,) = DataConverter.default.payload_converter.to_payloads([value])
    return payload.data


# --- workflow identity and input contract -------------------------------------------


def test_workflow_identity_contract_is_pinned() -> None:
    assert RECONCILIATION_WORKFLOW_ID_PREFIX == "exclusion-policy-reconciliation"
    assert (
        reconciliation_workflow_id(WORKSPACE_ID, POLICY_REVISION_ID)
        == f"exclusion-policy-reconciliation/{WORKSPACE_ID}/{POLICY_REVISION_ID}"
    )
    with pytest.raises(ValueError):
        reconciliation_workflow_id(UUID(int=0), POLICY_REVISION_ID)
    with pytest.raises(ValueError):
        reconciliation_workflow_id(WORKSPACE_ID, UUID(int=0))


def test_input_serializes_only_the_contract_tag_ids_and_checkpoint() -> None:
    decoded = json.loads(_serialize(_input()))
    assert decoded == {
        "contract": RECONCILIATION_CONTRACT,
        "workspace_id": str(WORKSPACE_ID),
        "policy_revision_id": str(POLICY_REVISION_ID),
        "source_checkpoint_event_sequence": 7,
    }
    for sentinel in _LEAKAGE_SENTINELS:
        assert sentinel.encode() not in _serialize(_input())


def test_input_rejects_contract_drift_nil_identities_and_negative_checkpoints() -> None:
    with pytest.raises(ValueError):
        ReconciliationInput(
            contract="exclusion_policy_reconciliation/v2",
            workspace_id=WORKSPACE_ID,
            policy_revision_id=POLICY_REVISION_ID,
            source_checkpoint_event_sequence=0,
        )
    with pytest.raises(ValueError):
        ReconciliationInput(
            contract=RECONCILIATION_CONTRACT,
            workspace_id=UUID(int=0),
            policy_revision_id=POLICY_REVISION_ID,
            source_checkpoint_event_sequence=0,
        )
    with pytest.raises(ValueError):
        ReconciliationInput(
            contract=RECONCILIATION_CONTRACT,
            workspace_id=WORKSPACE_ID,
            policy_revision_id=UUID(int=0),
            source_checkpoint_event_sequence=0,
        )
    with pytest.raises(ValueError):
        ReconciliationInput(
            contract=RECONCILIATION_CONTRACT,
            workspace_id=WORKSPACE_ID,
            policy_revision_id=POLICY_REVISION_ID,
            source_checkpoint_event_sequence=-1,
        )


def test_input_and_evaluation_are_frozen() -> None:
    reference = _input()
    with pytest.raises(AttributeError):
        reference.source_checkpoint_event_sequence = 9  # type: ignore[misc]
    evaluation = PolicyEvaluation(
        policy_revision_id=POLICY_REVISION_ID,
        source_id=SOURCE_ID,
        subject_event_sequence=3,
        raw_decision=RawPolicyDecision.ALLOWED,
        enforced_decision=EnforcedPolicyDecision.ALLOWED,
    )
    with pytest.raises(AttributeError):
        evaluation.subject_event_sequence = 4  # type: ignore[misc]


def test_evaluation_identity_is_exactly_revision_source_and_sequence() -> None:
    base = PolicyEvaluation(
        policy_revision_id=POLICY_REVISION_ID,
        source_id=SOURCE_ID,
        subject_event_sequence=3,
        raw_decision=RawPolicyDecision.ALLOWED,
        enforced_decision=EnforcedPolicyDecision.ALLOWED,
    )
    same = PolicyEvaluation(
        policy_revision_id=POLICY_REVISION_ID,
        source_id=SOURCE_ID,
        subject_event_sequence=3,
        raw_decision=RawPolicyDecision.ALLOWED,
        enforced_decision=EnforcedPolicyDecision.ALLOWED,
    )
    changed_sequence = PolicyEvaluation(
        policy_revision_id=POLICY_REVISION_ID,
        source_id=SOURCE_ID,
        subject_event_sequence=4,
        raw_decision=RawPolicyDecision.ALLOWED,
        enforced_decision=EnforcedPolicyDecision.ALLOWED,
    )
    assert base == same
    assert base != changed_sequence
    with pytest.raises(ValueError):
        PolicyEvaluation(
            policy_revision_id=POLICY_REVISION_ID,
            source_id=SOURCE_ID,
            subject_event_sequence=0,
            raw_decision=RawPolicyDecision.ALLOWED,
            enforced_decision=EnforcedPolicyDecision.ALLOWED,
        )


# --- transition derivation and deterministic intent plans ---------------------------


def test_enforced_transitions_map_onto_the_closed_vocabulary() -> None:
    allowed = EnforcedPolicyDecision.ALLOWED
    excluded = EnforcedPolicyDecision.EXCLUDED
    assert (
        derive_reconciliation_transition(previous_enforced=allowed, proposed_enforced=excluded)
        is ReconciliationTransition.TO_EXCLUDED
    )
    assert (
        derive_reconciliation_transition(previous_enforced=excluded, proposed_enforced=allowed)
        is ReconciliationTransition.TO_ALLOWED
    )
    assert (
        derive_reconciliation_transition(previous_enforced=allowed, proposed_enforced=allowed)
        is ReconciliationTransition.UNCHANGED
    )
    assert (
        derive_reconciliation_transition(previous_enforced=excluded, proposed_enforced=excluded)
        is ReconciliationTransition.UNCHANGED
    )


def test_proposed_indeterminate_maps_through_the_enforced_deny() -> None:
    # A raw indeterminate proposal is enforced-excluded before the comparison,
    # so allowed -> indeterminate is the delete transition.
    assert (
        derive_reconciliation_transition(
            previous_enforced=EnforcedPolicyDecision.ALLOWED,
            proposed_enforced=EnforcedPolicyDecision.EXCLUDED,
        )
        is ReconciliationTransition.TO_EXCLUDED
    )


def test_to_excluded_plans_qdrant_and_neo4j_deletes_with_a_current_version() -> None:
    assert policy_transition_intent_plans(
        ReconciliationTransition.TO_EXCLUDED, has_current_version=True
    ) == (
        PolicyTransitionIntentPlan(
            projection_kind=PolicyTransitionProjectionKind.QDRANT,
            operation=PolicyTransitionOperation.DELETE,
        ),
        PolicyTransitionIntentPlan(
            projection_kind=PolicyTransitionProjectionKind.NEO4J,
            operation=PolicyTransitionOperation.DELETE,
        ),
    )


def test_to_allowed_plans_qdrant_and_neo4j_upserts_with_a_current_version() -> None:
    assert policy_transition_intent_plans(
        ReconciliationTransition.TO_ALLOWED, has_current_version=True
    ) == (
        PolicyTransitionIntentPlan(
            projection_kind=PolicyTransitionProjectionKind.QDRANT,
            operation=PolicyTransitionOperation.UPSERT,
        ),
        PolicyTransitionIntentPlan(
            projection_kind=PolicyTransitionProjectionKind.NEO4J,
            operation=PolicyTransitionOperation.UPSERT,
        ),
    )


def test_unchanged_transition_plans_no_intent() -> None:
    assert (
        policy_transition_intent_plans(ReconciliationTransition.UNCHANGED, has_current_version=True)
        == ()
    )


def test_null_current_version_sources_never_receive_policy_transition_intents() -> None:
    for transition in ReconciliationTransition:
        assert policy_transition_intent_plans(transition, has_current_version=False) == ()


def test_projection_kind_vocabulary_is_closed_to_qdrant_and_neo4j() -> None:
    assert frozenset(kind.value for kind in PolicyTransitionProjectionKind) == frozenset(
        POLICY_TRANSITION_PROJECTION_KINDS
    )
    assert frozenset(POLICY_TRANSITION_PROJECTION_KINDS) == frozenset({"qdrant", "neo4j"})


# --- closed previous-decision fallbacks ---------------------------------------------


def test_first_publication_previous_decision_is_fail_closed_excluded() -> None:
    assert previous_enforced_without_policy() is EnforcedPolicyDecision.EXCLUDED


def test_no_prior_evaluation_previous_decision_is_allowed() -> None:
    assert previous_enforced_without_prior_evaluation() is EnforcedPolicyDecision.ALLOWED


# --- counters, continuation, progress and bounds ------------------------------------


def test_counters_accumulate_closed_transition_counts() -> None:
    counters = ReconciliationCounters()
    counters = counters.record(ReconciliationTransition.TO_EXCLUDED)
    counters = counters.record(ReconciliationTransition.TO_EXCLUDED)
    counters = counters.record(ReconciliationTransition.TO_ALLOWED)
    counters = counters.record(ReconciliationTransition.UNCHANGED)
    assert counters.evaluated_sources == 4
    assert counters.to_excluded_sources == 2
    assert counters.to_allowed_sources == 1
    assert counters.unchanged_sources == 1
    assert ReconciliationCounters() == ReconciliationCounters()


def test_continuation_validates_and_serializes_closed_fields_only() -> None:
    continuation = ReconciliationContinuation(
        contract=RECONCILIATION_CONTRACT,
        workspace_id=WORKSPACE_ID,
        policy_revision_id=POLICY_REVISION_ID,
        source_checkpoint_event_sequence=7,
        after_source_id=AFTER_SOURCE_ID,
        counters=ReconciliationCounters(
            evaluated_sources=10,
            to_excluded_sources=3,
            to_allowed_sources=2,
            unchanged_sources=5,
        ),
    )
    decoded = json.loads(_serialize(continuation))
    assert decoded["after_source_id"] == str(AFTER_SOURCE_ID)
    for sentinel in _LEAKAGE_SENTINELS:
        assert sentinel.encode() not in _serialize(continuation)
    with pytest.raises(ValueError):
        ReconciliationContinuation(
            contract=RECONCILIATION_CONTRACT,
            workspace_id=WORKSPACE_ID,
            policy_revision_id=POLICY_REVISION_ID,
            source_checkpoint_event_sequence=-1,
            after_source_id=None,
            counters=ReconciliationCounters(),
        )


def test_pinned_batch_and_continue_as_new_bounds() -> None:
    assert RECONCILIATION_BATCH_SIZE == 500
    assert RECONCILIATION_CONTINUE_AS_NEW_BATCHES == 20
    assert RECONCILIATION_CONTINUE_AS_NEW_SOURCES == 10_000


def test_progress_payload_carries_only_closed_counts() -> None:
    decoded = json.loads(_serialize(ReconciliationProgress(evaluated_sources=5, batch_count=2)))
    assert decoded == {"evaluated_sources": 5, "batch_count": 2}


# --- metrics ------------------------------------------------------------------------


def test_metrics_recorder_keeps_labels_closed_and_low_cardinality() -> None:
    assert RECONCILIATION_SOURCES_METRIC == "exclusion_policy_reconciliation_sources_total"
    metrics = InMemoryReconciliationMetrics()
    metrics.record_sources(transition=ReconciliationTransition.TO_EXCLUDED, count=3)
    metrics.record_sources(transition=ReconciliationTransition.UNCHANGED, count=5)
    metrics.record_lag(lag_seconds=12.5)
    assert metrics.source_count(ReconciliationTransition.TO_EXCLUDED) == 3
    assert metrics.source_count(ReconciliationTransition.UNCHANGED) == 5
    assert metrics.lag_readings() == [12.5]


def test_metrics_recorder_rejects_forbidden_labels_and_values() -> None:
    metrics = InMemoryReconciliationMetrics()
    with pytest.raises(ValueError):
        metrics.record_sources(transition="workspace-42", count=1)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        metrics.record_sources(transition=ReconciliationTransition.TO_ALLOWED, count=-1)
    with pytest.raises(ValueError):
        metrics.record_lag(lag_seconds=-0.5)
    with pytest.raises(ValueError):
        metrics.record_lag(lag_seconds=float("nan"))


# --- architecture boundary -----------------------------------------------------------


def test_reconciliation_domain_module_imports_no_engine_or_framework() -> None:
    source = Path("src/personal_os/exclusion_policy/reconciliation.py").read_text(encoding="utf-8")
    for forbidden in ("temporalio", "sqlalchemy", "fastapi", "asyncpg", "psycopg"):
        assert forbidden not in source
