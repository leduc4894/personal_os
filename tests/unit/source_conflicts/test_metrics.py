"""Closed low-cardinality source-conflict metric contracts and recorder.

Asserts the pinned metric names and label dimensions, the closed label
vocabularies mirroring the domain kinds/outcomes/error codes, and that the
in-memory recorder accepts only closed enum labels with finite non-negative
durations while rejecting any open text, identifier or locator before it
can become a label.
"""

from __future__ import annotations

import pytest

from personal_os.source_conflicts.contracts import (
    ConflictKind,
    ConflictResolutionKind,
    ConflictResolutionOutcome,
)
from personal_os.source_conflicts.metrics import (
    SOURCE_CONFLICT_METRIC_CONTRACTS,
    ConflictCaptureOutcome,
    ConflictResolutionMetricOutcome,
    InMemorySourceConflictMetrics,
    SourceConflictOperation,
    SourceConflictRejectionReason,
)


def test_metric_contracts_pin_exact_names_and_label_dimensions() -> None:
    expected = {
        "source_conflict_capture_total": frozenset({"conflict_kind", "outcome"}),
        "source_conflict_capture_duration_seconds": frozenset({"conflict_kind", "outcome"}),
        "source_conflict_resolution_total": frozenset({"resolution_kind", "outcome"}),
        "source_conflict_resolution_duration_seconds": frozenset({"resolution_kind", "outcome"}),
        "source_conflict_rejection_total": frozenset({"operation", "reason_code"}),
    }
    assert expected == SOURCE_CONFLICT_METRIC_CONTRACTS


def test_metric_label_names_never_name_an_identifier_or_content() -> None:
    for dimensions in SOURCE_CONFLICT_METRIC_CONTRACTS.values():
        for dimension in dimensions:
            assert dimension in {
                "conflict_kind",
                "resolution_kind",
                "outcome",
                "operation",
                "reason_code",
            }


def test_metric_label_values_are_closed_enums() -> None:
    assert {outcome.value for outcome in ConflictCaptureOutcome} == {
        "captured",
        "replayed",
        "rejected",
    }
    assert {outcome.value for outcome in ConflictResolutionMetricOutcome} == {
        "resolved",
        "stale_successor",
        "replayed",
        "rejected",
    }
    assert {operation.value for operation in SourceConflictOperation} == {
        "capture",
        "resolve",
    }
    assert {reason.value for reason in SourceConflictRejectionReason} == {
        "source_conflict_input_invalid",
        "source_conflict_not_found",
        "source_conflict_state_invalid",
        "source_conflict_idempotency_mismatch",
        "source_conflict_evidence_unavailable",
        "source_conflict_evidence_integrity_failed",
        "source_conflict_dependency_unavailable",
        "source_conflict_commit_outcome_unknown",
    }


def test_in_memory_metrics_record_and_count_closed_labels() -> None:
    recorder = InMemorySourceConflictMetrics()
    recorder.record_capture(
        conflict_kind=ConflictKind.STALE_CONTENT,
        outcome=ConflictCaptureOutcome.CAPTURED,
        duration_seconds=0.25,
    )
    recorder.record_resolution(
        resolution_kind=ConflictResolutionKind.KEEP_REMOTE,
        outcome=ConflictResolutionOutcome.RESOLVED,
        duration_seconds=0.5,
    )
    recorder.record_resolution(
        resolution_kind=ConflictResolutionKind.KEEP_LOCAL,
        outcome=ConflictResolutionOutcome.STALE_SUCCESSOR,
        duration_seconds=0.75,
    )
    recorder.record_rejection(
        operation=SourceConflictOperation.RESOLVE,
        reason_code=SourceConflictRejectionReason.SOURCE_CONFLICT_STATE_INVALID,
    )
    assert (
        recorder.capture_count(ConflictKind.STALE_CONTENT, ConflictCaptureOutcome.CAPTURED) == 1
    )
    assert (
        recorder.resolution_count(
            ConflictResolutionKind.KEEP_REMOTE, ConflictResolutionOutcome.RESOLVED
        )
        == 1
    )
    assert (
        recorder.resolution_count(
            ConflictResolutionKind.KEEP_LOCAL, ConflictResolutionOutcome.STALE_SUCCESSOR
        )
        == 1
    )
    assert (
        recorder.rejection_count(
            SourceConflictOperation.RESOLVE,
            SourceConflictRejectionReason.SOURCE_CONFLICT_STATE_INVALID,
        )
        == 1
    )
    assert repr(recorder) == "InMemorySourceConflictMetrics(redacted)"


def test_in_memory_metrics_reject_open_text_labels_and_bad_durations() -> None:
    recorder = InMemorySourceConflictMetrics()
    with pytest.raises(ValueError, match="closed enum member"):
        recorder.record_capture(
            conflict_kind="stale_content",  # type: ignore[arg-type]
            outcome=ConflictCaptureOutcome.CAPTURED,
            duration_seconds=0.1,
        )
    with pytest.raises(ValueError, match="closed enum member"):
        recorder.record_resolution(
            resolution_kind=ConflictResolutionKind.KEEP_REMOTE,
            outcome="resolved",  # type: ignore[arg-type]
            duration_seconds=0.1,
        )
    with pytest.raises(ValueError, match="finite and non-negative"):
        recorder.record_capture(
            conflict_kind=ConflictKind.STALE_CONTENT,
            outcome=ConflictCaptureOutcome.CAPTURED,
            duration_seconds=-1.0,
        )
    with pytest.raises(ValueError, match="finite and non-negative"):
        recorder.record_resolution(
            resolution_kind=ConflictResolutionKind.KEEP_REMOTE,
            outcome=ConflictResolutionOutcome.RESOLVED,
            duration_seconds=float("inf"),
        )
    with pytest.raises(ValueError, match="closed enum member"):
        recorder.record_rejection(
            operation=SourceConflictOperation.CAPTURE,
            reason_code="source_conflict_not_found",  # type: ignore[arg-type]
        )
