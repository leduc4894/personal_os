"""Domain contracts of the exact-snapshot asynchronous preview (spec 10).

These tests pin the provider-neutral preview orchestration: the closed status
and impact-class vocabularies (exactly the migration CHECK sets), the frozen
binding invariants, no-active-policy semantics (previous raw indeterminate,
enforced deny, so an initial empty policy previews existing valid sources as
``newly_allowed``), the impact classification including the separate
``indeterminate`` class that overrides the effective-decision mapping, the
deterministic impact digest over sorted opaque tuples, the deterministic
subject fingerprint, the pinned scan/page/expiry/deadline bounds, the
deterministic Temporal workflow identity, and the service's guard rails over
the preview port. No rule operand, locator, title or path may appear in any
digest input or error surface.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from tests.unit.exclusion_policy.fakes import (
    POLICY_REVISION_ID,
    WORKSPACE_ID,
    rule,
    subject,
)

from personal_os.diagnostics.context import DiagnosticContext, TraceContext

# Imported first: loading the diagnostics package before the error-contracts
# exceptions module keeps their module-level re-export cycle resolvable.
from personal_os.diagnostics.events import SafeToken  # noqa: F401
from personal_os.diagnostics.trace_context import SpanId, TraceId
from personal_os.error_contracts.exceptions import ApplicationError
from personal_os.exclusion_policy.contracts import (
    EnforcedPolicyDecision,
    ExclusionPolicyRevision,
    PolicySubject,
    PreviewMatchState,
    RawPolicyDecision,
    RuleKind,
)
from personal_os.exclusion_policy.evaluation import evaluate_policy
from personal_os.exclusion_policy.ports import PolicyActor, PolicyActorKind
from personal_os.exclusion_policy.previews import (
    PREVIEW_EXECUTION_DEADLINE_SECONDS,
    PREVIEW_READY_EXPIRY_SECONDS,
    PREVIEW_RESULT_PAGE_MAXIMUM,
    PREVIEW_SCAN_PAGE_SIZE,
    PREVIEW_WORKFLOW_ID_PREFIX,
    PolicyPreviewBinding,
    PolicyPreviewRecord,
    PolicyPreviewResultPage,
    PolicyPreviewResultRow,
    PolicyPreviewService,
    PolicyPreviewStore,
    PreviewImpactClass,
    PreviewProgress,
    PreviewStatus,
    classify_preview_impact,
    compute_impact_digest,
    compute_subject_fingerprint,
    evaluate_preview_subject,
    policy_preview_workflow_id,
)
from personal_os.object_storage import CanonicalMediaType
from personal_os.sources.commands import SourceType

DRAFT_ID = UUID("018f47a0-7b00-7000-8000-0000000000d2")
DRAFT_REVISION_ID = UUID("018f47a0-7b00-7000-8000-0000000000d3")
USER_ID = UUID("018f47a0-7b00-7000-8000-0000000000d4")
PREVIEW_ID = UUID("018f47a0-7b00-7000-8000-0000000000d5")
REQUEST_ID = uuid4()
OCCURRED_AT = datetime(2026, 8, 17, 9, 30, 0, tzinfo=UTC)

_TRACE = TraceContext(
    trace_id=TraceId("0123456789abcdef0123456789abcdef"),
    remote_parent_span_id=None,
    local_span_id=SpanId("0123456789abcdef"),
    trace_flags=0,
)


def _context() -> DiagnosticContext:
    return DiagnosticContext(request_id=REQUEST_ID, client_request_id=None, trace=_TRACE)


_LEAKAGE_SENTINELS: tuple[str, ...] = (
    "sentinel-title",
    "private/notes/sentinel-locator.md",
    "sentinel .tmp",
    "sentinel operand",
)


def _context() -> DiagnosticContext:
    return DiagnosticContext(request_id=REQUEST_ID, client_request_id=None, trace=_TRACE)


def _draft_revision(*rules: Any) -> ExclusionPolicyRevision:
    return ExclusionPolicyRevision(
        policy_revision_id=DRAFT_REVISION_ID,
        workspace_id=WORKSPACE_ID,
        revision_number=3,
        rules=rules,
    )


def _published_revision(*rules: Any) -> ExclusionPolicyRevision:
    return ExclusionPolicyRevision(
        policy_revision_id=POLICY_REVISION_ID,
        workspace_id=WORKSPACE_ID,
        revision_number=2,
        rules=rules,
    )


# --- closed vocabularies and pinned bounds --------------------------------------


def test_preview_status_vocabulary_is_exactly_the_migration_check_set() -> None:
    assert {status.value for status in PreviewStatus} == {
        "pending",
        "leased",
        "running",
        "ready",
        "failed",
        "expired",
        "consumed",
    }


def test_impact_class_vocabulary_is_exactly_the_migration_check_set() -> None:
    assert {impact.value for impact in PreviewImpactClass} == {
        "newly_excluded",
        "still_excluded",
        "newly_allowed",
        "still_allowed",
        "indeterminate",
    }


def test_preview_bounds_are_pinned() -> None:
    assert PREVIEW_SCAN_PAGE_SIZE == 500
    assert PREVIEW_RESULT_PAGE_MAXIMUM == 200
    assert PREVIEW_READY_EXPIRY_SECONDS == 15 * 60
    assert PREVIEW_EXECUTION_DEADLINE_SECONDS == 15 * 60


def test_workflow_identity_is_deterministic_and_pinned() -> None:
    assert PREVIEW_WORKFLOW_ID_PREFIX == "exclusion-policy-preview"
    assert (
        policy_preview_workflow_id(WORKSPACE_ID, PREVIEW_ID)
        == f"exclusion-policy-preview/{WORKSPACE_ID}/{PREVIEW_ID}"
    )


# --- binding invariants ----------------------------------------------------------


def test_binding_rejects_nil_identities_and_non_positive_members() -> None:
    with pytest.raises(ValueError):
        PolicyPreviewBinding(
            preview_id=UUID(int=0),
            draft_id=DRAFT_ID,
            draft_version=1,
            active_policy_revision_id=None,
            source_event_checkpoint=0,
        )
    with pytest.raises(ValueError):
        PolicyPreviewBinding(
            preview_id=PREVIEW_ID,
            draft_id=UUID(int=0),
            draft_version=1,
            active_policy_revision_id=None,
            source_event_checkpoint=0,
        )
    with pytest.raises(ValueError):
        PolicyPreviewBinding(
            preview_id=PREVIEW_ID,
            draft_id=DRAFT_ID,
            draft_version=0,
            active_policy_revision_id=None,
            source_event_checkpoint=0,
        )
    with pytest.raises(ValueError):
        PolicyPreviewBinding(
            preview_id=PREVIEW_ID,
            draft_id=DRAFT_ID,
            draft_version=1,
            active_policy_revision_id=UUID(int=0),
            source_event_checkpoint=0,
        )
    with pytest.raises(ValueError):
        PolicyPreviewBinding(
            preview_id=PREVIEW_ID,
            draft_id=DRAFT_ID,
            draft_version=1,
            active_policy_revision_id=None,
            source_event_checkpoint=-1,
        )


def test_binding_accepts_the_first_publication_shape() -> None:
    binding = PolicyPreviewBinding(
        preview_id=PREVIEW_ID,
        draft_id=DRAFT_ID,
        draft_version=1,
        active_policy_revision_id=None,
        source_event_checkpoint=0,
    )
    assert binding.active_policy_revision_id is None
    assert binding.source_event_checkpoint == 0


# --- impact classification -------------------------------------------------------


def test_impact_classification_covers_every_effective_transition() -> None:
    excluded = EnforcedPolicyDecision.EXCLUDED
    allowed = EnforcedPolicyDecision.ALLOWED

    assert (
        classify_preview_impact(
            previous_enforced=allowed,
            proposed_raw=RawPolicyDecision.EXCLUDED,
            proposed_enforced=excluded,
        )
        is PreviewImpactClass.NEWLY_EXCLUDED
    )
    assert (
        classify_preview_impact(
            previous_enforced=excluded,
            proposed_raw=RawPolicyDecision.EXCLUDED,
            proposed_enforced=excluded,
        )
        is PreviewImpactClass.STILL_EXCLUDED
    )
    assert (
        classify_preview_impact(
            previous_enforced=excluded,
            proposed_raw=RawPolicyDecision.ALLOWED,
            proposed_enforced=allowed,
        )
        is PreviewImpactClass.NEWLY_ALLOWED
    )
    assert (
        classify_preview_impact(
            previous_enforced=allowed,
            proposed_raw=RawPolicyDecision.ALLOWED,
            proposed_enforced=allowed,
        )
        is PreviewImpactClass.STILL_ALLOWED
    )


def test_proposed_indeterminacy_is_reported_separately_even_though_deny() -> None:
    assert (
        classify_preview_impact(
            previous_enforced=EnforcedPolicyDecision.ALLOWED,
            proposed_raw=RawPolicyDecision.INDETERMINATE,
            proposed_enforced=EnforcedPolicyDecision.EXCLUDED,
        )
        is PreviewImpactClass.INDETERMINATE
    )
    assert (
        classify_preview_impact(
            previous_enforced=EnforcedPolicyDecision.EXCLUDED,
            proposed_raw=RawPolicyDecision.INDETERMINATE,
            proposed_enforced=EnforcedPolicyDecision.EXCLUDED,
        )
        is PreviewImpactClass.INDETERMINATE
    )


# --- no-active-policy semantics --------------------------------------------------


def test_no_active_revision_previews_previous_as_indeterminate_denied() -> None:
    markdown_subject = subject(
        source_id=uuid4(),
        source_type=SourceType.MARKDOWN,
        media_type=CanonicalMediaType.parse("text/markdown"),
        size_bytes=120,
    )
    outcome = evaluate_preview_subject(
        previous_revision=None,
        proposed_revision=_draft_revision(),
        subject=markdown_subject,
    )
    assert outcome.previous_raw is RawPolicyDecision.INDETERMINATE
    assert outcome.previous_enforced is EnforcedPolicyDecision.EXCLUDED
    assert outcome.proposed_raw is RawPolicyDecision.ALLOWED
    assert outcome.proposed_enforced is EnforcedPolicyDecision.ALLOWED
    assert outcome.proposed_match_state is PreviewMatchState.NOT_MATCHED
    assert outcome.impact_class is PreviewImpactClass.NEWLY_ALLOWED
    assert outcome.matched_rule_ids == ()
    assert outcome.missing_fields == ()


def test_initial_empty_policy_previews_valid_sources_as_newly_allowed() -> None:
    digest_source_id = uuid4()
    outcome = evaluate_preview_subject(
        previous_revision=None,
        proposed_revision=_draft_revision(),
        subject=subject(source_id=digest_source_id, source_type=SourceType.TEXT),
    )
    assert outcome.impact_class is PreviewImpactClass.NEWLY_ALLOWED


def test_proposed_match_state_maps_the_closed_vocabulary() -> None:
    matched_source = uuid4()
    excluded_rule = rule(RuleKind.SOURCE_TYPE, text_operand="pdf")
    matched = evaluate_preview_subject(
        previous_revision=None,
        proposed_revision=_draft_revision(excluded_rule),
        subject=subject(source_id=matched_source, source_type=SourceType.PDF),
    )
    assert matched.proposed_match_state is PreviewMatchState.MATCHED
    assert matched.impact_class is PreviewImpactClass.STILL_EXCLUDED

    missing_evidence_source = uuid4()
    locator_rule = rule(RuleKind.EXTENSION, text_operand=".tmp")
    indeterminate = evaluate_preview_subject(
        previous_revision=None,
        proposed_revision=_draft_revision(locator_rule),
        subject=subject(source_id=missing_evidence_source, source_type=SourceType.TEXT),
    )
    assert indeterminate.proposed_raw is RawPolicyDecision.INDETERMINATE
    assert indeterminate.proposed_match_state is PreviewMatchState.INDETERMINATE
    assert indeterminate.impact_class is PreviewImpactClass.INDETERMINATE


def test_proposed_outcome_matches_the_pure_evaluator_exactly() -> None:
    excluded_rule = rule(RuleKind.MAXIMUM_SIZE, size_bytes_operand=100)
    proposed = _draft_revision(excluded_rule)
    previous = _published_revision(excluded_rule)
    large_subject = subject(source_id=uuid4(), size_bytes=101)

    outcome = evaluate_preview_subject(
        previous_revision=previous, proposed_revision=proposed, subject=large_subject
    )
    direct = evaluate_policy(revision=proposed, subject=large_subject)
    assert outcome.proposed_raw is direct.raw
    assert outcome.proposed_enforced is direct.enforced
    assert outcome.matched_rule_ids == direct.matched_rule_ids
    assert outcome.missing_fields == direct.missing_fields
    assert outcome.impact_class is PreviewImpactClass.STILL_EXCLUDED


# --- digests and fingerprints ----------------------------------------------------


def _outcome(source_id: UUID) -> Any:
    return evaluate_preview_subject(
        previous_revision=None,
        proposed_revision=_draft_revision(),
        subject=subject(source_id=source_id, source_type=SourceType.MARKDOWN),
    )


def test_impact_digest_is_order_independent_and_deterministic() -> None:
    first = uuid4()
    second = uuid4()
    assert compute_impact_digest([_outcome(first), _outcome(second)]) == (
        compute_impact_digest([_outcome(second), _outcome(first)])
    )
    assert compute_impact_digest([_outcome(first)]) != compute_impact_digest([_outcome(second)])


def test_impact_digest_contains_no_operand_or_display_value() -> None:
    digest = compute_impact_digest([_outcome(uuid4())])
    for sentinel in _LEAKAGE_SENTINELS:
        assert sentinel not in digest


def test_subject_fingerprint_is_deterministic_and_field_sensitive() -> None:
    base = PolicySubject(
        workspace_id=WORKSPACE_ID,
        source_id=uuid4(),
        source_type=SourceType.MARKDOWN,
        media_type=CanonicalMediaType.parse("text/markdown"),
        size_bytes=42,
    )
    assert compute_subject_fingerprint(base) == compute_subject_fingerprint(
        PolicySubject(
            workspace_id=WORKSPACE_ID,
            source_id=base.source_id,
            source_type=SourceType.MARKDOWN,
            media_type=CanonicalMediaType.parse("text/markdown"),
            size_bytes=42,
        )
    )
    assert compute_subject_fingerprint(base) != compute_subject_fingerprint(
        PolicySubject(
            workspace_id=WORKSPACE_ID,
            source_id=base.source_id,
            source_type=SourceType.MARKDOWN,
            media_type=CanonicalMediaType.parse("text/markdown"),
            size_bytes=43,
        )
    )
    assert len(compute_subject_fingerprint(base)) == 64


# --- record, result row and progress values --------------------------------------


def _record(**overrides: Any) -> PolicyPreviewRecord:
    values: dict[str, Any] = {
        "policy_preview_id": PREVIEW_ID,
        "workspace_id": WORKSPACE_ID,
        "policy_draft_id": DRAFT_ID,
        "draft_version": 3,
        "draft_sha256": "a" * 64,
        "base_policy_revision_id": None,
        "source_checkpoint_event_sequence": 7,
        "status": PreviewStatus.PENDING,
        "impact_digest": None,
        "safe_error_code": None,
        "created_by_user_id": USER_ID,
        "created_at": OCCURRED_AT,
        "ready_at": None,
        "expires_at": None,
        "consumed_at": None,
        "newly_excluded_count": 0,
        "still_excluded_count": 0,
        "newly_allowed_count": 0,
        "still_allowed_count": 0,
        "indeterminate_count": 0,
    }
    values.update(overrides)
    return PolicyPreviewRecord(**values)


def test_record_exposes_its_binding() -> None:
    record = _record()
    assert record.binding == PolicyPreviewBinding(
        preview_id=PREVIEW_ID,
        draft_id=DRAFT_ID,
        draft_version=3,
        active_policy_revision_id=None,
        source_event_checkpoint=7,
    )


def test_ready_record_carries_digest_and_expiry() -> None:
    record = _record(
        status=PreviewStatus.READY,
        impact_digest="b" * 64,
        ready_at=OCCURRED_AT,
        expires_at=datetime(2026, 8, 17, 9, 45, 0, tzinfo=UTC),
        newly_allowed_count=4,
    )
    assert record.impact_digest == "b" * 64
    assert (
        record.expires_at - record.ready_at  # type: ignore[operator]
    ).total_seconds() == PREVIEW_READY_EXPIRY_SECONDS


def test_failed_record_requires_safe_error_code() -> None:
    with pytest.raises(ValueError):
        _record(status=PreviewStatus.FAILED, safe_error_code=None)
    assert (
        _record(
            status=PreviewStatus.FAILED, safe_error_code="preview_execution_failed"
        ).safe_error_code
        == "preview_execution_failed"
    )


def test_result_page_and_row_shapes_are_closed() -> None:
    row = PolicyPreviewResultRow(
        source_id=uuid4(),
        previous_raw_decision=RawPolicyDecision.INDETERMINATE,
        previous_enforced_decision=EnforcedPolicyDecision.EXCLUDED,
        proposed_raw_decision=RawPolicyDecision.ALLOWED,
        proposed_enforced_decision=EnforcedPolicyDecision.ALLOWED,
        proposed_match_state=PreviewMatchState.NOT_MATCHED,
        impact_class=PreviewImpactClass.NEWLY_ALLOWED,
        matched_rule_ids=(),
        missing_fields=(),
        subject_fingerprint="c" * 64,
    )
    page = PolicyPreviewResultPage(rows=(row,), next_cursor=None)
    assert page.rows == (row,)
    assert page.next_cursor is None
    progress = PreviewProgress(evaluated_subjects=500, batch_count=1)
    assert isinstance(progress.evaluated_subjects, int)


# --- service guard rails over the preview port -----------------------------------


class RecordingPreviewStore:
    """In-memory preview port double recording every call."""

    def __init__(self) -> None:
        self.requested_workspaces: list[UUID] = []
        self.listed_limits: list[int] = []
        self.heartbeats: list[PreviewProgress] = []

    async def request_preview(
        self,
        workspace_id: UUID,
        actor: PolicyActor,
        context: DiagnosticContext,
    ) -> PolicyPreviewRecord:
        self.requested_workspaces.append(workspace_id)
        return _record()

    async def run_preview_activity(
        self,
        preview_id: UUID,
        context: DiagnosticContext,
        heartbeat: Any = None,
    ) -> PolicyPreviewRecord:
        if heartbeat is not None:
            await heartbeat(PreviewProgress(evaluated_subjects=1, batch_count=1))
        self.heartbeats.append(PreviewProgress(evaluated_subjects=1, batch_count=1))
        return _record(status=PreviewStatus.READY, impact_digest="b" * 64, ready_at=OCCURRED_AT)

    async def get_preview(
        self, preview_id: UUID, context: DiagnosticContext
    ) -> PolicyPreviewRecord:
        return _record()

    async def list_preview_results(
        self,
        preview_id: UUID,
        context: DiagnosticContext,
        cursor: Any = None,
        limit: int = PREVIEW_RESULT_PAGE_MAXIMUM,
    ) -> PolicyPreviewResultPage:
        self.listed_limits.append(limit)
        return PolicyPreviewResultPage(rows=(), next_cursor=None)


def _service() -> tuple[PolicyPreviewService, RecordingPreviewStore]:
    store = RecordingPreviewStore()
    return PolicyPreviewService(preview_store=store), store


def test_service_requests_delegates_with_actor_validation() -> None:
    service, store = _service()
    actor = PolicyActor(actor_kind=PolicyActorKind.USER, user_id=USER_ID)

    record = asyncio.run(service.request_preview(WORKSPACE_ID, actor, _context()))

    assert record.status is PreviewStatus.PENDING
    assert store.requested_workspaces == [WORKSPACE_ID]
    # Nil identities fail the same plain-value guard the draft service uses.
    with pytest.raises(ValueError):
        asyncio.run(service.request_preview(UUID(int=0), actor, _context()))


def test_service_list_rejects_out_of_bound_page_requests() -> None:
    service, store = _service()

    asyncio.run(
        service.list_preview_results(PREVIEW_ID, _context(), limit=PREVIEW_RESULT_PAGE_MAXIMUM)
    )
    assert store.listed_limits == [PREVIEW_RESULT_PAGE_MAXIMUM]

    with pytest.raises(ApplicationError):
        asyncio.run(service.list_preview_results(PREVIEW_ID, _context(), limit=201))
    with pytest.raises(ApplicationError):
        asyncio.run(service.list_preview_results(PREVIEW_ID, _context(), limit=0))


def test_service_run_activity_propagates_heartbeat_and_ready_record() -> None:
    service, _store = _service()
    record = asyncio.run(service.run_preview_activity(PREVIEW_ID, _context()))
    assert record.status is PreviewStatus.READY


def test_preview_store_protocol_remains_satisfiable_by_the_double() -> None:
    store: PolicyPreviewStore = RecordingPreviewStore()  # type: ignore[assignment]
    assert store is not None
