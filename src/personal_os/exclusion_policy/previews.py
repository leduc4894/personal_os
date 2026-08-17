"""Exact-snapshot asynchronous preview orchestration (spec 10).

The preview binds the exact ``draft_id``, ``draft_version``, the prior active
revision (nullable) and the workspace's ``source_event_checkpoint``, then one
single-activity execution over one ``REPEATABLE READ`` PostgreSQL transaction
compares the previous and proposed policy over every current valid source and
atomically writes the complete closed result set. This module owns the
provider-neutral half of that contract: the closed status and impact-class
vocabularies, the frozen binding and read-model values, the pure per-subject
comparison (no-active-policy semantics included), the deterministic impact
digest and subject fingerprint, the pinned scan/page/expiry/deadline bounds,
the deterministic workflow identity and the application service over the
preview port. Like the rest of the package it imports no web framework,
database driver, provider SDK or workflow engine; the single activity and its
leased dispatcher live in the worker composition.

Evidence carries only opaque IDs, closed decisions, sorted rule IDs and sorted
missing-field names — never a locator, title, operand or path.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from typing import Final, Protocol
from uuid import UUID

from personal_os.diagnostics.context import DiagnosticContext
from personal_os.exclusion_policy.canonical_json import (
    CanonicalJsonValue,
    canonicalize_json_value,
)
from personal_os.exclusion_policy.contracts import (
    EnforcedPolicyDecision,
    ExclusionPolicyRevision,
    PolicySubject,
    PolicySubjectField,
    PreviewMatchState,
    RawPolicyDecision,
    preview_match_state,
)
from personal_os.exclusion_policy.errors import (
    PREVIEW_LIMIT_INVALID,
    input_invalid,
)
from personal_os.exclusion_policy.evaluation import PolicyEvaluationOutcome, evaluate_policy
from personal_os.exclusion_policy.ports import PolicyActor
from personal_os.sources.actors import reject_nil_uuid

#: Preview activities stream subjects in pages of at most this many rows
#: (spec 10); the stable keyset order is ``source_id`` ascending.
PREVIEW_SCAN_PAGE_SIZE: Final[int] = 500

#: Preview result pages served to the API carry at most this many rows
#: (spec 10); the stable cursor order is ``(impact_class, source_id)``.
PREVIEW_RESULT_PAGE_MAXIMUM: Final[int] = 200

#: A ready preview expires this many seconds after its ``ready_at`` (spec 10).
PREVIEW_READY_EXPIRY_SECONDS: Final[int] = 15 * 60

#: Pending, leased and running previews carry a bounded execution deadline of
#: the same fifteen minutes (spec 10); the durable sweep fails overdue rows.
PREVIEW_EXECUTION_DEADLINE_SECONDS: Final[int] = 15 * 60

#: The deterministic Temporal workflow ID prefix; the full ID is
#: ``{prefix}/{workspace_id}/{policy_preview_id}`` (spec 10).
PREVIEW_WORKFLOW_ID_PREFIX: Final[str] = "exclusion-policy-preview"

#: Contract tag hashed into every impact digest.
IMPACT_DIGEST_CONTRACT: Final[str] = "exclusion_policy_preview_impact/v1"

#: Contract tag hashed into every subject fingerprint. The fingerprint stays
#: internal evidence: it is never logged because path-derived hashes may be
#: guessable (spec 7).
SUBJECT_FINGERPRINT_CONTRACT: Final[str] = "exclusion_policy_subject/v1"


class PreviewStatus(StrEnum):
    """The closed preview lifecycle states (exactly the migration CHECK set)."""

    PENDING = "pending"
    LEASED = "leased"
    RUNNING = "running"
    READY = "ready"
    FAILED = "failed"
    EXPIRED = "expired"
    CONSUMED = "consumed"


class PreviewImpactClass(StrEnum):
    """The closed impact classes (spec 10; exactly the migration CHECK set).

    ``indeterminate`` is reported separately even though its effective
    decision is deny.
    """

    NEWLY_EXCLUDED = "newly_excluded"
    STILL_EXCLUDED = "still_excluded"
    NEWLY_ALLOWED = "newly_allowed"
    STILL_ALLOWED = "still_allowed"
    INDETERMINATE = "indeterminate"


@dataclass(frozen=True, slots=True)
class PolicyPreviewBinding:
    """The exact snapshot one preview evaluates (spec 10).

    Construction rejects nil identities, a non-positive draft version and a
    negative checkpoint, so a bound preview can never name an ambiguous
    snapshot. ``active_policy_revision_id`` is null exactly before the first
    publication.
    """

    preview_id: UUID
    draft_id: UUID
    draft_version: int
    active_policy_revision_id: UUID | None
    source_event_checkpoint: int

    def __post_init__(self) -> None:
        reject_nil_uuid("preview_id", self.preview_id)
        reject_nil_uuid("draft_id", self.draft_id)
        if self.active_policy_revision_id is not None:
            reject_nil_uuid("active_policy_revision_id", self.active_policy_revision_id)
        if self.draft_version < 1:
            raise ValueError("draft_version must be at least 1")
        if self.source_event_checkpoint < 0:
            raise ValueError("source_event_checkpoint must not be negative")


@dataclass(frozen=True, slots=True)
class PreviewImpactCounters:
    """The five closed impact counters of one complete preview."""

    newly_excluded_count: int = 0
    still_excluded_count: int = 0
    newly_allowed_count: int = 0
    still_allowed_count: int = 0
    indeterminate_count: int = 0


@dataclass(frozen=True, slots=True)
class PolicyPreviewRecord:
    """Immutable read model of one preview row.

    ``status`` uses the closed lifecycle vocabulary; a failed row carries a
    closed safe error code; a consumed row carries its consumed timestamp.
    Construction re-validates those closed shapes so an inconsistent hydrated
    row can never cross the boundary silently.
    """

    policy_preview_id: UUID
    workspace_id: UUID
    policy_draft_id: UUID
    draft_version: int
    draft_sha256: str
    base_policy_revision_id: UUID | None
    source_checkpoint_event_sequence: int
    status: PreviewStatus
    impact_digest: str | None
    safe_error_code: str | None
    created_by_user_id: UUID
    created_at: datetime
    ready_at: datetime | None
    expires_at: datetime | None
    consumed_at: datetime | None
    newly_excluded_count: int = 0
    still_excluded_count: int = 0
    newly_allowed_count: int = 0
    still_allowed_count: int = 0
    indeterminate_count: int = 0

    def __post_init__(self) -> None:
        reject_nil_uuid("policy_preview_id", self.policy_preview_id)
        reject_nil_uuid("workspace_id", self.workspace_id)
        reject_nil_uuid("policy_draft_id", self.policy_draft_id)
        if self.status is PreviewStatus.FAILED and self.safe_error_code is None:
            raise ValueError("failed preview requires safe_error_code")
        if self.status is PreviewStatus.CONSUMED and self.consumed_at is None:
            raise ValueError("consumed preview requires consumed_at")

    @property
    def binding(self) -> PolicyPreviewBinding:
        """The exact snapshot this preview is bound to."""

        return PolicyPreviewBinding(
            preview_id=self.policy_preview_id,
            draft_id=self.policy_draft_id,
            draft_version=self.draft_version,
            active_policy_revision_id=self.base_policy_revision_id,
            source_event_checkpoint=self.source_checkpoint_event_sequence,
        )

    @property
    def counters(self) -> PreviewImpactCounters:
        return PreviewImpactCounters(
            newly_excluded_count=self.newly_excluded_count,
            still_excluded_count=self.still_excluded_count,
            newly_allowed_count=self.newly_allowed_count,
            still_allowed_count=self.still_allowed_count,
            indeterminate_count=self.indeterminate_count,
        )


@dataclass(frozen=True, slots=True)
class PreviewSubjectOutcome:
    """One subject's previous/proposed comparison evidence.

    Carries only the opaque source ID, the closed decisions and match state,
    the impact class, sorted matching rule IDs, sorted missing field names and
    the internal subject fingerprint — never a locator, operand or title.
    """

    source_id: UUID
    previous_raw: RawPolicyDecision
    previous_enforced: EnforcedPolicyDecision
    proposed_raw: RawPolicyDecision
    proposed_enforced: EnforcedPolicyDecision
    proposed_match_state: PreviewMatchState
    impact_class: PreviewImpactClass
    matched_rule_ids: tuple[UUID, ...]
    missing_fields: tuple[PolicySubjectField, ...]
    subject_fingerprint: str


@dataclass(frozen=True, slots=True)
class PolicyPreviewResultRow:
    """One persisted preview-result row exposed to Admin reads.

    Only IDs, closed match states and closed decisions; display joins happen
    at the API layer after the stale-checkpoint guard, never here.
    """

    source_id: UUID
    previous_raw_decision: RawPolicyDecision
    previous_enforced_decision: EnforcedPolicyDecision
    proposed_raw_decision: RawPolicyDecision
    proposed_enforced_decision: EnforcedPolicyDecision
    proposed_match_state: PreviewMatchState
    impact_class: PreviewImpactClass
    matched_rule_ids: tuple[UUID, ...]
    missing_fields: tuple[PolicySubjectField, ...]
    subject_fingerprint: str


@dataclass(frozen=True, slots=True)
class PreviewResultCursor:
    """The stable ``(impact_class, source_id)`` page cursor (spec 10)."""

    impact_class: PreviewImpactClass
    source_id: UUID

    def __post_init__(self) -> None:
        reject_nil_uuid("source_id", self.source_id)


@dataclass(frozen=True, slots=True)
class PolicyPreviewResultPage:
    """One bounded page of preview results plus the continuation cursor."""

    rows: tuple[PolicyPreviewResultRow, ...]
    next_cursor: PreviewResultCursor | None


@dataclass(frozen=True, slots=True)
class PreviewProgress:
    """Heartbeat payload: evaluated subject count and completed batch count.

    Both fields are from the closed diagnostic field set of spec section 21.
    """

    evaluated_subjects: int
    batch_count: int


def policy_preview_workflow_id(workspace_id: UUID, policy_preview_id: UUID) -> str:
    """Derive the deterministic preview workflow ID (spec 10).

    Retried and concurrent dispatches of one preview converge on the same
    execution because the identity is a pure function of the two opaque IDs.
    """

    reject_nil_uuid("workspace_id", workspace_id)
    reject_nil_uuid("policy_preview_id", policy_preview_id)
    return f"{PREVIEW_WORKFLOW_ID_PREFIX}/{workspace_id}/{policy_preview_id}"


def classify_preview_impact(
    *,
    previous_enforced: EnforcedPolicyDecision,
    proposed_raw: RawPolicyDecision,
    proposed_enforced: EnforcedPolicyDecision,
) -> PreviewImpactClass:
    """Classify one previous/proposed comparison (spec 10).

    A proposed indeterminate outcome is its own impact class even though its
    effective decision is deny; every other outcome maps over the enforced
    transition allowed→excluded, excluded→excluded, excluded→allowed,
    allowed→allowed.
    """

    if proposed_raw is RawPolicyDecision.INDETERMINATE:
        return PreviewImpactClass.INDETERMINATE
    if previous_enforced is EnforcedPolicyDecision.ALLOWED:
        if proposed_enforced is EnforcedPolicyDecision.EXCLUDED:
            return PreviewImpactClass.NEWLY_EXCLUDED
        return PreviewImpactClass.STILL_ALLOWED
    if proposed_enforced is EnforcedPolicyDecision.ALLOWED:
        return PreviewImpactClass.NEWLY_ALLOWED
    return PreviewImpactClass.STILL_EXCLUDED


def evaluate_preview_subject(
    *,
    previous_revision: ExclusionPolicyRevision | None,
    proposed_revision: ExclusionPolicyRevision,
    subject: PolicySubject,
) -> PreviewSubjectOutcome:
    """Compare one subject under the previous and proposed revisions.

    The proposed outcome is exactly the pure evaluator's outcome; the previous
    outcome is the evaluator over the bound active revision, or the closed
    no-active semantics when the workspace has never published — previous raw
    ``indeterminate``, enforced deny. Impact then follows
    :func:`classify_preview_impact`.
    """

    if subject.source_id is None:
        raise ValueError("preview subject requires a source_id")
    proposed: PolicyEvaluationOutcome = evaluate_policy(
        revision=proposed_revision, subject=subject
    )
    if previous_revision is None:
        previous_raw = RawPolicyDecision.INDETERMINATE
        previous_enforced = EnforcedPolicyDecision.EXCLUDED
    else:
        previous = evaluate_policy(revision=previous_revision, subject=subject)
        previous_raw = previous.raw
        previous_enforced = previous.enforced
    return PreviewSubjectOutcome(
        source_id=subject.source_id,
        previous_raw=previous_raw,
        previous_enforced=previous_enforced,
        proposed_raw=proposed.raw,
        proposed_enforced=proposed.enforced,
        proposed_match_state=preview_match_state(proposed.raw),
        impact_class=classify_preview_impact(
            previous_enforced=previous_enforced,
            proposed_raw=proposed.raw,
            proposed_enforced=proposed.enforced,
        ),
        matched_rule_ids=proposed.matched_rule_ids,
        missing_fields=proposed.missing_fields,
        subject_fingerprint=compute_subject_fingerprint(subject),
    )


def compute_subject_fingerprint(subject: PolicySubject) -> str:
    """Hash one subject over its closed canonical structure (spec 7).

    The fingerprint is SHA-256 over the RFC 8785 canonical JSON of the
    contract tag plus every subject field, with genuinely absent fields
    rendered as null. It stays internal evidence and is never logged.
    """

    payload: dict[str, CanonicalJsonValue] = {
        "contract": SUBJECT_FINGERPRINT_CONTRACT,
        "workspace_id": str(subject.workspace_id),
        "source_id": str(subject.source_id) if subject.source_id is not None else None,
        "normalized_locator": subject.normalized_locator,
        "source_type": subject.source_type.value if subject.source_type is not None else None,
        "media_type": subject.media_type.value if subject.media_type is not None else None,
        "size_bytes": subject.size_bytes,
    }
    return sha256(canonicalize_json_value(payload)).hexdigest()


def compute_impact_digest(outcomes: Sequence[PreviewSubjectOutcome]) -> str:
    """Hash the complete impact set into the frozen preview digest (spec 10).

    The digest is SHA-256 over the RFC 8785 canonical JSON of the contract tag
    plus one entry per source — sorted by the opaque source ID — carrying only
    the source ID, the previous/proposed effective decisions and the impact
    class. It contains no title, path or operand.
    """

    ordered = sorted(outcomes, key=lambda outcome: str(outcome.source_id))
    payload: dict[str, CanonicalJsonValue] = {
        "contract": IMPACT_DIGEST_CONTRACT,
        "entries": tuple(
            {
                "source_id": str(outcome.source_id),
                "previous_enforced_decision": outcome.previous_enforced.value,
                "proposed_enforced_decision": outcome.proposed_enforced.value,
                "impact_class": outcome.impact_class.value,
            }
            for outcome in ordered
        )
    }
    return sha256(canonicalize_json_value(payload)).hexdigest()


class PolicyPreviewStore(Protocol):
    """Provider-neutral preview persistence and execution port (spec 10).

    ``request_preview`` captures the binding and the durable pending row in
    one transaction; ``run_preview_activity`` executes the single
    repeatable-read snapshot comparison (the implementation streams bounded
    pages, heartbeats between them and writes the complete result set
    atomically — cancellation or failure rolls back every result);
    ``get_preview`` and ``list_preview_results`` are the bounded Admin reads
    (the listing re-verifies the source checkpoint and refuses stale display
    joins). No method exposes rows, SQL or driver payloads.
    """

    async def request_preview(
        self, workspace_id: UUID, actor: PolicyActor, context: DiagnosticContext
    ) -> PolicyPreviewRecord: ...

    async def run_preview_activity(
        self,
        preview_id: UUID,
        context: DiagnosticContext,
        heartbeat: Callable[[PreviewProgress], Awaitable[None]] | None = None,
    ) -> PolicyPreviewRecord: ...

    async def get_preview(
        self, preview_id: UUID, context: DiagnosticContext
    ) -> PolicyPreviewRecord: ...

    async def list_preview_results(
        self,
        preview_id: UUID,
        context: DiagnosticContext,
        cursor: PreviewResultCursor | None = None,
        limit: int = PREVIEW_RESULT_PAGE_MAXIMUM,
    ) -> PolicyPreviewResultPage: ...


class PolicyPreviewService:
    """Provider-neutral preview application service (spec 10).

    The service owns guard rails and orchestration only: nil-identity
    rejection, the API page bound and delegation to the injected port. The
    atomic snapshot execution and the leased dispatch belong to the store and
    the worker composition.
    """

    def __init__(self, *, preview_store: PolicyPreviewStore) -> None:
        self._preview_store = preview_store

    async def request_preview(
        self, workspace_id: UUID, actor: PolicyActor, context: DiagnosticContext
    ) -> PolicyPreviewRecord:
        """Bind and persist one pending preview for the workspace's draft."""

        reject_nil_uuid("workspace_id", workspace_id)
        return await self._preview_store.request_preview(workspace_id, actor, context)

    async def run_preview_activity(
        self,
        preview_id: UUID,
        context: DiagnosticContext,
        heartbeat: Callable[[PreviewProgress], Awaitable[None]] | None = None,
    ) -> PolicyPreviewRecord:
        """Execute the single snapshot comparison for one bound preview."""

        reject_nil_uuid("preview_id", preview_id)
        return await self._preview_store.run_preview_activity(preview_id, context, heartbeat)

    async def get_preview(
        self, preview_id: UUID, context: DiagnosticContext
    ) -> PolicyPreviewRecord:
        """Return one preview's current lifecycle record."""

        reject_nil_uuid("preview_id", preview_id)
        return await self._preview_store.get_preview(preview_id, context)

    async def list_preview_results(
        self,
        preview_id: UUID,
        context: DiagnosticContext,
        cursor: PreviewResultCursor | None = None,
        limit: int = PREVIEW_RESULT_PAGE_MAXIMUM,
    ) -> PolicyPreviewResultPage:
        """Return one bounded result page in stable cursor order."""

        reject_nil_uuid("preview_id", preview_id)
        if limit < 1 or limit > PREVIEW_RESULT_PAGE_MAXIMUM:
            raise input_invalid(PREVIEW_LIMIT_INVALID)
        return await self._preview_store.list_preview_results(preview_id, context, cursor, limit)


__all__ = [
    "IMPACT_DIGEST_CONTRACT",
    "PREVIEW_EXECUTION_DEADLINE_SECONDS",
    "PREVIEW_READY_EXPIRY_SECONDS",
    "PREVIEW_RESULT_PAGE_MAXIMUM",
    "PREVIEW_SCAN_PAGE_SIZE",
    "SUBJECT_FINGERPRINT_CONTRACT",
    "PolicyPreviewBinding",
    "PolicyPreviewRecord",
    "PolicyPreviewResultPage",
    "PolicyPreviewResultRow",
    "PolicyPreviewService",
    "PolicyPreviewStore",
    "PreviewImpactClass",
    "PreviewImpactCounters",
    "PreviewProgress",
    "PreviewResultCursor",
    "PreviewStatus",
    "PreviewSubjectOutcome",
    "classify_preview_impact",
    "compute_impact_digest",
    "compute_subject_fingerprint",
    "evaluate_preview_subject",
    "policy_preview_workflow_id",
]
