"""Exclusion-policy metrics diagnostics read side: counters and failure ring.

These tests pin the read surface the Web Admin policy diagnostics route
consumes (spec 2026-08-24 C2): the in-memory recorder keeps exact evaluation
counters keyed by the closed boundary and decision labels (``failed``
included), exact publication outcome counters, and a bounded ring of the most
recent policy system failures carrying exactly the closed registry code, the
closed boundary label and the epoch-millisecond timestamp stamped through the
injected clock. Snapshots are immutable copies: later recordings never mutate
a snapshot already taken, and no UUID, locator, operand, digest or free-form
string can ever appear in one.
"""

from __future__ import annotations

from typing import Final

import pytest

from personal_os.error_contracts.codes import ErrorCode
from personal_os.exclusion_policy.metrics import (
    EvaluationMetricOutcome,
    InMemoryExclusionPolicyMetrics,
    PolicyBoundary,
    PublicationMetricOutcome,
)

_FIRST_EPOCH_MS: Final[int] = 1_800_000_000_000


class _SteppingEpochClock:
    """Deterministic epoch-ms seam: every call advances by one millisecond."""

    def __init__(self, first_epoch_ms: int = _FIRST_EPOCH_MS) -> None:
        self._next_epoch_ms = first_epoch_ms

    def __call__(self) -> int:
        current = self._next_epoch_ms
        self._next_epoch_ms += 1
        return current


def test_diagnostics_snapshot_carries_exact_closed_counters() -> None:
    recorder = InMemoryExclusionPolicyMetrics(epoch_ms_clock=_SteppingEpochClock())
    recorder.record_evaluation(
        boundary=PolicyBoundary.SINGLE_PART_UPLOAD,
        decision=EvaluationMetricOutcome.ALLOWED,
        duration_seconds=0.01,
    )
    recorder.record_evaluation(
        boundary=PolicyBoundary.SINGLE_PART_UPLOAD,
        decision=EvaluationMetricOutcome.ALLOWED,
        duration_seconds=0.02,
    )
    recorder.record_evaluation(
        boundary=PolicyBoundary.SINGLE_PART_UPLOAD,
        decision=EvaluationMetricOutcome.FAILED,
        duration_seconds=0.03,
        error_code=ErrorCode.EXCLUSION_POLICY_NOT_INITIALIZED,
    )
    recorder.record_publication(outcome=PublicationMetricOutcome.PUBLISHED, duration_seconds=0.5)
    recorder.record_publication(outcome=PublicationMetricOutcome.REJECTED, duration_seconds=0.2)

    snapshot = recorder.policy_diagnostics()

    assert dict(snapshot.evaluation_counters) == {
        (PolicyBoundary.SINGLE_PART_UPLOAD, EvaluationMetricOutcome.ALLOWED): 2,
        (PolicyBoundary.SINGLE_PART_UPLOAD, EvaluationMetricOutcome.FAILED): 1,
    }
    assert dict(snapshot.publication_counters) == {
        PublicationMetricOutcome.PUBLISHED: 1,
        PublicationMetricOutcome.REJECTED: 1,
    }
    assert len(snapshot.recent_failures) == 1
    failure = snapshot.recent_failures[0]
    assert failure.boundary is PolicyBoundary.SINGLE_PART_UPLOAD
    assert failure.error_code is ErrorCode.EXCLUSION_POLICY_NOT_INITIALIZED
    assert failure.at_epoch_ms == _FIRST_EPOCH_MS


def test_failure_ring_carries_closed_code_boundary_and_timestamp_only() -> None:
    recorder = InMemoryExclusionPolicyMetrics(epoch_ms_clock=_SteppingEpochClock())
    recorder.record_evaluation(
        boundary=PolicyBoundary.SYNC_PREFLIGHT,
        decision=EvaluationMetricOutcome.FAILED,
        duration_seconds=0.0,
        error_code=ErrorCode.EXCLUSION_POLICY_SIGNING_UNAVAILABLE,
    )
    recorder.record_evaluation(
        boundary=PolicyBoundary.CANONICAL_READ,
        decision=EvaluationMetricOutcome.FAILED,
        duration_seconds=0.0,
        error_code=ErrorCode.EXCLUSION_POLICY_NOT_INITIALIZED,
    )

    snapshot = recorder.policy_diagnostics()

    assert len(snapshot.recent_failures) == 2
    first, second = snapshot.recent_failures
    assert first.boundary is PolicyBoundary.SYNC_PREFLIGHT
    assert first.error_code is ErrorCode.EXCLUSION_POLICY_SIGNING_UNAVAILABLE
    assert first.at_epoch_ms == _FIRST_EPOCH_MS
    assert second.boundary is PolicyBoundary.CANONICAL_READ
    assert second.error_code is ErrorCode.EXCLUSION_POLICY_NOT_INITIALIZED
    assert second.at_epoch_ms == _FIRST_EPOCH_MS + 1
    for record in snapshot.recent_failures:
        rendered = repr(record)
        assert "duration" not in rendered


def test_failure_ring_is_bounded_at_fifty_records() -> None:
    recorder = InMemoryExclusionPolicyMetrics(epoch_ms_clock=_SteppingEpochClock())
    for _ in range(55):
        recorder.record_evaluation(
            boundary=PolicyBoundary.SINGLE_PART_UPLOAD,
            decision=EvaluationMetricOutcome.FAILED,
            duration_seconds=0.0,
            error_code=ErrorCode.EXCLUSION_POLICY_NOT_INITIALIZED,
        )

    snapshot = recorder.policy_diagnostics()

    assert len(snapshot.recent_failures) == 50
    timestamps = [record.at_epoch_ms for record in snapshot.recent_failures]
    assert timestamps == sorted(timestamps)
    assert timestamps[0] == _FIRST_EPOCH_MS + 5
    assert timestamps[-1] == _FIRST_EPOCH_MS + 54
    assert dict(snapshot.evaluation_counters) == {
        (PolicyBoundary.SINGLE_PART_UPLOAD, EvaluationMetricOutcome.FAILED): 55
    }


def test_diagnostics_snapshot_is_an_immutable_copy() -> None:
    recorder = InMemoryExclusionPolicyMetrics(epoch_ms_clock=_SteppingEpochClock())
    recorder.record_evaluation(
        boundary=PolicyBoundary.SINGLE_PART_UPLOAD,
        decision=EvaluationMetricOutcome.ALLOWED,
        duration_seconds=0.0,
    )
    snapshot = recorder.policy_diagnostics()

    recorder.record_evaluation(
        boundary=PolicyBoundary.SINGLE_PART_UPLOAD,
        decision=EvaluationMetricOutcome.ALLOWED,
        duration_seconds=0.0,
    )
    recorder.record_evaluation(
        boundary=PolicyBoundary.SINGLE_PART_UPLOAD,
        decision=EvaluationMetricOutcome.FAILED,
        duration_seconds=0.0,
        error_code=ErrorCode.EXCLUSION_POLICY_NOT_INITIALIZED,
    )

    assert dict(snapshot.evaluation_counters) == {
        (PolicyBoundary.SINGLE_PART_UPLOAD, EvaluationMetricOutcome.ALLOWED): 1
    }
    assert snapshot.recent_failures == ()


def test_counters_stay_exact_beyond_the_evaluation_ring_bound() -> None:
    """Ring eviction must never lose a closed counter (spec 21 exactness)."""

    recorder = InMemoryExclusionPolicyMetrics(epoch_ms_clock=_SteppingEpochClock())
    for _ in range(5000):
        recorder.record_evaluation(
            boundary=PolicyBoundary.SINGLE_PART_UPLOAD,
            decision=EvaluationMetricOutcome.ALLOWED,
            duration_seconds=0.0,
        )

    snapshot = recorder.policy_diagnostics()
    assert dict(snapshot.evaluation_counters) == {
        (PolicyBoundary.SINGLE_PART_UPLOAD, EvaluationMetricOutcome.ALLOWED): 5000
    }


def test_failed_decision_without_error_code_is_rejected_by_the_diagnostics_sink() -> None:
    recorder = InMemoryExclusionPolicyMetrics(epoch_ms_clock=_SteppingEpochClock())
    with pytest.raises(ValueError, match="failed decision requires"):
        recorder.record_evaluation(
            boundary=PolicyBoundary.SINGLE_PART_UPLOAD,
            decision=EvaluationMetricOutcome.FAILED,
            duration_seconds=0.0,
        )
