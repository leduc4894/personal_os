"""Closed device-sync metric contracts: vocabularies, labels and bounds.

Asserts the exact operation and outcome vocabularies, the pinned metric-name
and label-dimension contracts, and that the in-memory recorder keeps its
labels closed: only enum members are accepted for operation, outcome and
reason, ``duration_ms`` must be a non-negative true integer, and a ``None``
reason is the only success-carried reason shape. Identifiers, locators,
digests and provider text are never accepted as labels.
"""

from __future__ import annotations

import pytest

from personal_os.device_sync.errors import DeviceSyncErrorCode
from personal_os.device_sync.metrics import (
    DEVICE_SYNC_METRIC_CONTRACTS,
    DeviceSyncOperation,
    DeviceSyncOutcome,
    InMemoryDeviceSyncMetrics,
)


def test_operation_vocabulary_is_closed() -> None:
    assert {operation.value for operation in DeviceSyncOperation} == {
        "pull",
        "acknowledge",
        "manifest_start",
        "manifest_page",
        "manifest_finalize",
        "manifest_actions",
        "manifest_complete",
        "download",
    }
    with pytest.raises(ValueError):
        DeviceSyncOperation("push")


def test_outcome_vocabulary_is_closed() -> None:
    assert {outcome.value for outcome in DeviceSyncOutcome} == {
        "succeeded",
        "rejected",
        "failed",
        "replayed",
    }
    with pytest.raises(ValueError):
        DeviceSyncOutcome("skipped")


def test_metric_contracts_pin_names_and_label_dimensions() -> None:
    assert dict(DEVICE_SYNC_METRIC_CONTRACTS) == {
        "device_sync_operation_total": frozenset({"operation", "outcome", "reason"}),
        "device_sync_operation_duration_ms": frozenset({"operation", "outcome", "reason"}),
    }
    for labels in DEVICE_SYNC_METRIC_CONTRACTS.values():
        assert "workspace_id" not in labels
        assert "device_id" not in labels
        assert "source_id" not in labels
        assert "manifest_run_id" not in labels


def test_recorder_counts_closed_operation_outcomes() -> None:
    metrics = InMemoryDeviceSyncMetrics()
    metrics.record_operation(
        operation=DeviceSyncOperation.PULL,
        outcome=DeviceSyncOutcome.SUCCEEDED,
        reason=None,
        duration_ms=12,
    )
    metrics.record_operation(
        operation=DeviceSyncOperation.PULL,
        outcome=DeviceSyncOutcome.REJECTED,
        reason=DeviceSyncErrorCode.CURSOR_GAP,
        duration_ms=5,
    )
    metrics.record_operation(
        operation=DeviceSyncOperation.DOWNLOAD,
        outcome=DeviceSyncOutcome.FAILED,
        reason=DeviceSyncErrorCode.DEPENDENCY_UNAVAILABLE,
        duration_ms=7,
    )
    assert (
        metrics.operation_count(
            operation=DeviceSyncOperation.PULL, outcome=DeviceSyncOutcome.SUCCEEDED
        )
        == 1
    )
    assert (
        metrics.operation_count(
            operation=DeviceSyncOperation.PULL,
            outcome=DeviceSyncOutcome.REJECTED,
            reason=DeviceSyncErrorCode.CURSOR_GAP,
        )
        == 1
    )
    assert (
        metrics.operation_count(
            operation=DeviceSyncOperation.DOWNLOAD,
            outcome=DeviceSyncOutcome.FAILED,
            reason=DeviceSyncErrorCode.DEPENDENCY_UNAVAILABLE,
        )
        == 1
    )
    assert (
        metrics.operation_count(
            operation=DeviceSyncOperation.PULL,
            outcome=DeviceSyncOutcome.REJECTED,
            reason=DeviceSyncErrorCode.CURSOR_REGRESSION,
        )
        == 0
    )


def test_recorder_rejects_labels_outside_the_closed_vocabularies() -> None:
    metrics = InMemoryDeviceSyncMetrics()
    with pytest.raises(ValueError, match="operation"):
        metrics.record_operation(
            operation="pull",  # type: ignore[arg-type]
            outcome=DeviceSyncOutcome.SUCCEEDED,
            reason=None,
            duration_ms=1,
        )
    with pytest.raises(ValueError, match="outcome"):
        metrics.record_operation(
            operation=DeviceSyncOperation.PULL,
            outcome="succeeded",  # type: ignore[arg-type]
            reason=None,
            duration_ms=1,
        )
    with pytest.raises(ValueError, match="reason"):
        metrics.record_operation(
            operation=DeviceSyncOperation.PULL,
            outcome=DeviceSyncOutcome.FAILED,
            reason="device_cursor_gap",  # type: ignore[arg-type]
            duration_ms=1,
        )


def test_recorder_rejects_invalid_durations() -> None:
    metrics = InMemoryDeviceSyncMetrics()
    with pytest.raises(ValueError, match="duration_ms"):
        metrics.record_operation(
            operation=DeviceSyncOperation.PULL,
            outcome=DeviceSyncOutcome.SUCCEEDED,
            reason=None,
            duration_ms=-1,
        )
    with pytest.raises(ValueError, match="duration_ms"):
        metrics.record_operation(
            operation=DeviceSyncOperation.PULL,
            outcome=DeviceSyncOutcome.SUCCEEDED,
            reason=None,
            duration_ms=True,  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="duration_ms"):
        metrics.record_operation(
            operation=DeviceSyncOperation.PULL,
            outcome=DeviceSyncOutcome.SUCCEEDED,
            reason=None,
            duration_ms=1.5,  # type: ignore[arg-type]
        )
