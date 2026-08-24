"""In-memory lifecycle metrics recorder: closed diagnostics read side.

The recorder is the sink the serve graph wires at the composition root, so
its read side must stay bounded and closed: commit counters keyed only by
the closed operation and outcome labels, a rejection ring retaining only the
last fifty closed records, snapshots immune to later recordings, and a
broken epoch clock rejected before any record is kept.
"""

from __future__ import annotations

from dataclasses import asdict

import pytest

from personal_os.source_lifecycle.commands import LifecycleOperation
from personal_os.source_lifecycle.errors import SourceLifecycleErrorCode
from personal_os.source_lifecycle.metrics import (
    InMemorySourceLifecycleMetrics,
    LifecycleMetricOutcome,
)


class _SteppingEpochClock:
    """Deterministic epoch-ms seam: every call advances by one millisecond."""

    def __init__(self, first_epoch_ms: int = 1_800_000_000_000) -> None:
        self._next_epoch_ms = first_epoch_ms

    def __call__(self) -> int:
        current = self._next_epoch_ms
        self._next_epoch_ms += 1
        return current


def test_commit_counters_count_every_closed_outcome() -> None:
    recorder = InMemorySourceLifecycleMetrics()
    recorder.record_commit(
        operation=LifecycleOperation.RENAME,
        outcome=LifecycleMetricOutcome.REPLAYED,
        duration_seconds=0.1,
    )
    recorder.record_commit(
        operation=LifecycleOperation.RENAME,
        outcome=LifecycleMetricOutcome.REPLAYED,
        duration_seconds=0.2,
    )
    recorder.record_commit(
        operation=LifecycleOperation.DELETE,
        outcome=LifecycleMetricOutcome.COMMITTED,
        duration_seconds=0.3,
    )
    diagnostics = recorder.lifecycle_diagnostics()
    assert dict(diagnostics.commit_counters) == {
        (LifecycleOperation.RENAME, LifecycleMetricOutcome.REPLAYED): 2,
        (LifecycleOperation.DELETE, LifecycleMetricOutcome.COMMITTED): 1,
    }
    assert diagnostics.recent_rejections == ()


def test_rejection_ring_retains_only_the_last_fifty_closed_records() -> None:
    recorder = InMemorySourceLifecycleMetrics(epoch_ms_clock=_SteppingEpochClock())
    for index in range(60):
        recorder.record_rejection(
            operation=LifecycleOperation.MOVE if index % 2 else LifecycleOperation.RENAME,
            error_code=SourceLifecycleErrorCode.LOCATOR_CONFLICT,
        )
    diagnostics = recorder.lifecycle_diagnostics()
    recent = diagnostics.recent_rejections
    assert len(recent) == 50
    # The oldest ten records were evicted; the ring starts at the eleventh.
    assert recent[0].operation is LifecycleOperation.RENAME
    assert recent[0].at_epoch_ms == 1_800_000_000_010
    assert recent[-1].at_epoch_ms == 1_800_000_000_059
    # Timestamps stay non-decreasing and every record carries exactly the
    # closed members: error code, epoch timestamp and operation label.
    assert [record.at_epoch_ms for record in recent] == sorted(
        record.at_epoch_ms for record in recent
    )
    for record in recent:
        assert set(asdict(record)) == {"error_code", "at_epoch_ms", "operation"}


def test_lifecycle_diagnostics_snapshot_is_isolated_from_later_recordings() -> None:
    recorder = InMemorySourceLifecycleMetrics(epoch_ms_clock=_SteppingEpochClock())
    recorder.record_rejection(
        operation=LifecycleOperation.RENAME,
        error_code=SourceLifecycleErrorCode.VERSION_CONFLICT,
    )
    diagnostics = recorder.lifecycle_diagnostics()
    assert len(diagnostics.recent_rejections) == 1

    recorder.record_rejection(
        operation=LifecycleOperation.RENAME,
        error_code=SourceLifecycleErrorCode.VERSION_CONFLICT,
    )
    recorder.record_commit(
        operation=LifecycleOperation.RENAME,
        outcome=LifecycleMetricOutcome.REPLAYED,
        duration_seconds=0.1,
    )
    assert len(diagnostics.recent_rejections) == 1
    assert dict(diagnostics.commit_counters) == {}
    assert len(recorder.lifecycle_diagnostics().recent_rejections) == 2


def test_rejection_ring_timestamps_reject_a_broken_epoch_clock() -> None:
    recorder = InMemorySourceLifecycleMetrics(epoch_ms_clock=lambda: -1)
    with pytest.raises(ValueError, match="non-negative integer"):
        recorder.record_rejection(
            operation=LifecycleOperation.RENAME,
            error_code=SourceLifecycleErrorCode.VERSION_CONFLICT,
        )
