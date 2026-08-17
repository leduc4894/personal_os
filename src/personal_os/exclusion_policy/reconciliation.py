"""Provider-neutral publication-reconciliation contracts (spec 15/21).

Publication commits one durable reconciliation intent per revision; this module
owns the closed vocabulary that execution shares across the PostgreSQL adapter
and the worker composition: the ``exclusion_policy_reconciliation/v1`` input
carrying only the contract tag, the two opaque UUIDs and the publication source
checkpoint; the deterministic workflow identity; the immutable evaluation
identity ``(policy_revision_id, source_id, subject_event_sequence)``; the
closed previous/proposed transition derivation with its deterministic
Qdrant/Neo4j intent plans gated on a non-null current version; the closed
previous-decision fallbacks (fail-closed excluded before any policy existed,
allowed when a source simply has no prior evaluation row, because a source
exists only after passing enforcement); the pinned 500-source batch and the
20-batch/10,000-source continue-as-new bounds; and the closed low-cardinality
metrics surface — transition counters and reconciliation lag only — recorded
after durable state transitions. Like the rest of the package it imports no
workflow engine, database driver or web framework; time, leases and
persistence belong to the adapters.

Evidence carries only opaque IDs, closed decisions and counts — never a
locator, title, operand, path or subject fingerprint.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Literal, Protocol, runtime_checkable
from uuid import UUID

from personal_os.exclusion_policy.contracts import (
    EnforcedPolicyDecision,
    RawPolicyDecision,
)
from personal_os.sources.actors import reject_nil_uuid

#: Contract tag identifying the reconciliation workflow input semantics.
#: The inferred literal type feeds the frozen ``Literal`` field below.
RECONCILIATION_CONTRACT: Final = "exclusion_policy_reconciliation/v1"

#: The deterministic reconciliation workflow ID prefix (spec 15); the full ID
#: is ``{prefix}/{workspace_id}/{policy_revision_id}``.
RECONCILIATION_WORKFLOW_ID_PREFIX: Final[str] = "exclusion-policy-reconciliation"

#: One activity batch evaluates at most this many sources (spec 15).
RECONCILIATION_BATCH_SIZE: Final[int] = 500

#: The workflow continues as new after this many committed batches (spec 15).
RECONCILIATION_CONTINUE_AS_NEW_BATCHES: Final[int] = 20

#: The workflow continues as new after this many evaluated sources (spec 15).
RECONCILIATION_CONTINUE_AS_NEW_SOURCES: Final[int] = 10_000

#: The exact reconciliation metric names and their closed label dimensions
#: (spec 21). Workspace, source, revision and rule identities are prohibited
#: labels and can never be recorded.
RECONCILIATION_SOURCES_METRIC: Final[str] = "exclusion_policy_reconciliation_sources_total"
RECONCILIATION_LAG_METRIC: Final[str] = "exclusion_policy_reconciliation_lag_seconds"

#: The closed projection kinds a policy-transition intent targets (spec 8.5:
#: the migration CHECK set of ``projection_intents``).
POLICY_TRANSITION_PROJECTION_KINDS: Final[tuple[str, ...]] = ("qdrant", "neo4j")

#: Maximum retained per-transition records; a bounded ring for tests and
#: standalone runs, never an unbounded audit log.
_MAXIMUM_SOURCE_RECORDS: Final[int] = 4096


class ReconciliationState(StrEnum):
    """The closed reconciliation-intent lifecycle (the migration CHECK set).

    ``pending`` rows await dispatch (retryable failures return here with
    bounded backoff), ``leased`` covers only the workflow start call,
    ``dispatched`` is the durable resting state once the deterministic
    workflow owns the revision's reconciliation — completion evidence is the
    append-only audit row plus the evaluations themselves, not a further state
    — and ``terminal`` is the closed non-retryable failure that must carry a
    safe error code.
    """

    PENDING = "pending"
    LEASED = "leased"
    DISPATCHED = "dispatched"
    TERMINAL = "terminal"


class ReconciliationTransition(StrEnum):
    """The closed previous/proposed enforced-decision transitions (spec 15).

    The label vocabulary of the ``{transition}`` metric dimension: a raw
    indeterminate proposal is enforced-excluded before the comparison, so
    ``to_excluded`` covers allowed -> denied or indeterminate exactly as the
    spec table requires.
    """

    TO_EXCLUDED = "to_excluded"
    TO_ALLOWED = "to_allowed"
    UNCHANGED = "unchanged"


class PolicyTransitionProjectionKind(StrEnum):
    """The closed projection kinds of one policy-transition intent."""

    QDRANT = "qdrant"
    NEO4J = "neo4j"


class PolicyTransitionOperation(StrEnum):
    """The closed operations of one policy-transition intent."""

    UPSERT = "upsert"
    DELETE = "delete"


class ReconciliationExecutionOutcome(StrEnum):
    """The closed outcomes of one complete reconciliation workflow run."""

    COMPLETED = "completed"
    SUPERSEDED = "superseded"


@dataclass(frozen=True, slots=True)
class ReconciliationInput:
    """The closed ``exclusion_policy_reconciliation/v1`` workflow input.

    Only the contract tag, the two opaque UUIDs and the publication source
    checkpoint are members, so the default JSON codec can serialize nothing
    else into workflow history: rule operands, locators, titles, paths and
    content bytes have no field to occupy.
    """

    contract: Literal["exclusion_policy_reconciliation/v1"]
    workspace_id: UUID
    policy_revision_id: UUID
    source_checkpoint_event_sequence: int

    def __post_init__(self) -> None:
        if self.contract != RECONCILIATION_CONTRACT:
            raise ValueError("contract must be exclusion_policy_reconciliation/v1")
        reject_nil_uuid("workspace_id", self.workspace_id)
        reject_nil_uuid("policy_revision_id", self.policy_revision_id)
        if self.source_checkpoint_event_sequence < 0:
            raise ValueError("source_checkpoint_event_sequence must not be negative")


@dataclass(frozen=True, slots=True)
class ReconciliationContinuation:
    """The closed continue-as-new input of the reconciliation workflow.

    Extends the workflow input with the stable keyset cursor and the
    cumulative closed transition counters. The per-run batch/source counts
    that bound one history are intentionally absent: the continue-as-new
    budget resets with every new run, so carrying them would re-trigger the
    bound immediately in the next run.
    """

    contract: Literal["exclusion_policy_reconciliation/v1"]
    workspace_id: UUID
    policy_revision_id: UUID
    source_checkpoint_event_sequence: int
    after_source_id: UUID | None
    counters: ReconciliationCounters

    def __post_init__(self) -> None:
        if self.contract != RECONCILIATION_CONTRACT:
            raise ValueError("contract must be exclusion_policy_reconciliation/v1")
        reject_nil_uuid("workspace_id", self.workspace_id)
        reject_nil_uuid("policy_revision_id", self.policy_revision_id)
        if self.source_checkpoint_event_sequence < 0:
            raise ValueError("source_checkpoint_event_sequence must not be negative")
        if self.after_source_id is not None:
            reject_nil_uuid("after_source_id", self.after_source_id)


@dataclass(frozen=True, slots=True)
class PolicyEvaluation:
    """Insert-once evaluation evidence for one source state under a revision.

    ``subject_event_sequence`` is the source's current canonical event
    sequence the evaluation covers; ``(policy_revision_id, source_id,
    subject_event_sequence)`` is the immutable identity, so a later locator or
    version change under the same revision creates a new evaluation instead of
    overwriting history. An idempotent replay verifies exact equality.
    """

    policy_revision_id: UUID
    source_id: UUID
    subject_event_sequence: int
    raw_decision: RawPolicyDecision
    enforced_decision: EnforcedPolicyDecision

    def __post_init__(self) -> None:
        reject_nil_uuid("policy_revision_id", self.policy_revision_id)
        reject_nil_uuid("source_id", self.source_id)
        if self.subject_event_sequence < 1:
            raise ValueError("subject_event_sequence must be at least 1")


@dataclass(frozen=True, slots=True)
class ReconciliationCounters:
    """The closed transition counters of one reconciliation run (spec 21).

    Every member is a closed low-cardinality count; no workspace, source or
    revision identity is ever recorded here.
    """

    evaluated_sources: int = 0
    to_excluded_sources: int = 0
    to_allowed_sources: int = 0
    unchanged_sources: int = 0

    def __post_init__(self) -> None:
        for name in (
            "evaluated_sources",
            "to_excluded_sources",
            "to_allowed_sources",
            "unchanged_sources",
        ):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must not be negative")

    def record(self, transition: ReconciliationTransition) -> ReconciliationCounters:
        """Return new counters with one transition recorded."""
        return ReconciliationCounters(
            evaluated_sources=self.evaluated_sources + 1,
            to_excluded_sources=self.to_excluded_sources
            + (transition is ReconciliationTransition.TO_EXCLUDED),
            to_allowed_sources=self.to_allowed_sources
            + (transition is ReconciliationTransition.TO_ALLOWED),
            unchanged_sources=self.unchanged_sources
            + (transition is ReconciliationTransition.UNCHANGED),
        )


@dataclass(frozen=True, slots=True)
class ReconciliationProgress:
    """Heartbeat payload: evaluated source count and committed batch count."""

    evaluated_sources: int
    batch_count: int


@dataclass(frozen=True, slots=True)
class PolicyTransitionIntentPlan:
    """One deterministic policy-origin projection intent of a transition."""

    projection_kind: PolicyTransitionProjectionKind
    operation: PolicyTransitionOperation


def reconciliation_workflow_id(workspace_id: UUID, policy_revision_id: UUID) -> str:
    """Derive the deterministic reconciliation workflow identity (spec 15).

    Retried and concurrent dispatches of one revision converge on the same
    execution because the identity is a pure function of the two opaque IDs.
    """

    reject_nil_uuid("workspace_id", workspace_id)
    reject_nil_uuid("policy_revision_id", policy_revision_id)
    return f"{RECONCILIATION_WORKFLOW_ID_PREFIX}/{workspace_id}/{policy_revision_id}"


def derive_reconciliation_transition(
    *, previous_enforced: EnforcedPolicyDecision, proposed_enforced: EnforcedPolicyDecision
) -> ReconciliationTransition:
    """Classify one previous/proposed enforced-decision comparison (spec 15).

    The comparison is over the binary enforced decisions: a raw indeterminate
    proposal arrives already mapped to excluded, so ``to_excluded`` covers
    allowed -> denied or indeterminate.
    """

    if previous_enforced is proposed_enforced:
        return ReconciliationTransition.UNCHANGED
    if proposed_enforced is EnforcedPolicyDecision.EXCLUDED:
        return ReconciliationTransition.TO_EXCLUDED
    return ReconciliationTransition.TO_ALLOWED


def policy_transition_intent_plans(
    transition: ReconciliationTransition, *, has_current_version: bool
) -> tuple[PolicyTransitionIntentPlan, ...]:
    """Derive the deterministic Qdrant/Neo4j intent plans of one transition.

    ``to_excluded`` plans one delete per projection kind and ``to_allowed``
    one upsert per kind (spec 15's table); an unchanged enforced decision
    never plans an intent regardless of any raw reason change, and no source
    without a non-null current version ever receives a policy-transition
    intent — it gets the evaluation evidence only.
    """

    if not has_current_version or transition is ReconciliationTransition.UNCHANGED:
        return ()
    if transition is ReconciliationTransition.TO_EXCLUDED:
        operation = PolicyTransitionOperation.DELETE
    else:
        operation = PolicyTransitionOperation.UPSERT
    return tuple(
        PolicyTransitionIntentPlan(projection_kind=kind, operation=operation)
        for kind in PolicyTransitionProjectionKind
    )


def previous_enforced_without_policy() -> EnforcedPolicyDecision:
    """The closed previous decision before any published policy existed.

    The fail-closed no-policy semantics deny every content operation, so the
    first publication's reconciliation treats every prior decision as
    excluded and proposes upsert intents for whatever it now allows.
    """

    return EnforcedPolicyDecision.EXCLUDED


def previous_enforced_without_prior_evaluation() -> EnforcedPolicyDecision:
    """The closed previous decision when no prior evaluation row exists.

    A source exists only after passing enforcement under the policy active at
    its last canonical write, and deny-only revisions default to allow, so a
    source with no recorded evaluation under the parent revision was
    effectively allowed. The parent revision's recorded evaluation rows always
    win over this fallback.
    """

    return EnforcedPolicyDecision.ALLOWED


@dataclass(frozen=True, slots=True)
class SourceTransitionRecord:
    """One recorded transition-counter increment.

    Carries only the closed transition enum and a non-negative count; never a
    UUID, locator, operand or subject fingerprint.
    """

    transition: ReconciliationTransition
    count: int


@runtime_checkable
class ReconciliationMetrics(Protocol):
    """The low-cardinality reconciliation metrics sink (spec 21)."""

    def record_sources(self, *, transition: ReconciliationTransition, count: int) -> None:
        """Record evaluated-source counts per closed transition label."""
        ...

    def record_lag(self, *, lag_seconds: float) -> None:
        """Record the reconciliation lag once, after the durable completion."""
        ...


class InMemoryReconciliationMetrics:
    """Bounded in-memory recorder implementing :class:`ReconciliationMetrics`.

    Keeps at most :data:`_MAXIMUM_SOURCE_RECORDS` counter records in a ring
    and one lag reading per completion, and rejects non-enum labels and
    negative or non-finite values so a UUID, locator or operand can never
    become a metric label.
    """

    def __init__(self) -> None:
        self._sources: deque[SourceTransitionRecord] = deque(maxlen=_MAXIMUM_SOURCE_RECORDS)
        self._lag_readings: list[float] = []

    def record_sources(self, *, transition: ReconciliationTransition, count: int) -> None:
        if not isinstance(transition, ReconciliationTransition):
            raise ValueError("transition label must be a closed enum member")
        if count < 1:
            raise ValueError("count must be positive")
        self._sources.append(SourceTransitionRecord(transition=transition, count=count))

    def record_lag(self, *, lag_seconds: float) -> None:
        if math.isfinite(lag_seconds) is False or lag_seconds < 0:
            raise ValueError("lag_seconds must be finite and non-negative")
        self._lag_readings.append(lag_seconds)

    def source_count(self, transition: ReconciliationTransition) -> int:
        """Sum the recorded counts of one closed transition label."""

        return sum(record.count for record in self._sources if record.transition is transition)

    def lag_readings(self) -> list[float]:
        """A snapshot list of recorded lag readings (oldest first)."""

        return list(self._lag_readings)

    def __repr__(self) -> str:
        return "InMemoryReconciliationMetrics(redacted)"


__all__ = [
    "POLICY_TRANSITION_PROJECTION_KINDS",
    "RECONCILIATION_BATCH_SIZE",
    "RECONCILIATION_CONTINUE_AS_NEW_BATCHES",
    "RECONCILIATION_CONTINUE_AS_NEW_SOURCES",
    "RECONCILIATION_CONTRACT",
    "RECONCILIATION_LAG_METRIC",
    "RECONCILIATION_SOURCES_METRIC",
    "RECONCILIATION_WORKFLOW_ID_PREFIX",
    "InMemoryReconciliationMetrics",
    "PolicyEvaluation",
    "PolicyTransitionIntentPlan",
    "PolicyTransitionOperation",
    "PolicyTransitionProjectionKind",
    "ReconciliationContinuation",
    "ReconciliationCounters",
    "ReconciliationExecutionOutcome",
    "ReconciliationInput",
    "ReconciliationMetrics",
    "ReconciliationProgress",
    "ReconciliationState",
    "ReconciliationTransition",
    "SourceTransitionRecord",
    "derive_reconciliation_transition",
    "policy_transition_intent_plans",
    "previous_enforced_without_policy",
    "previous_enforced_without_prior_evaluation",
    "reconciliation_workflow_id",
]
