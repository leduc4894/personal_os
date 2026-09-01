"""Closed low-cardinality source-conflict metrics contracts and recorder.

Every metric label is a closed :class:`enum.StrEnum` member: conflict kind,
resolution kind, closed capture/resolution outcomes, the operation that was
rejected and the rejection reason code mirroring the domain error registry.
UUIDs, idempotency keys, locators, digests, object keys, byte counts of
evidence streams and provider messages are never accepted as labels and
never recorded. :data:`SOURCE_CONFLICT_METRIC_CONTRACTS` pins the exact
metric names and their label dimensions.

:class:`SourceConflictMetrics` is the injectable Protocol the capture,
replay and resolution paths depend on; :class:`InMemorySourceConflictMetrics`
is the bounded test/standalone implementation sufficient for runtime
checks and tests without introducing Prometheus. A production sink
implements the same Protocol behind the boundary and, like the in-memory
recorder, must reject negative or non-finite durations and any non-enum
label value.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Final, Protocol, runtime_checkable

from personal_os.source_conflicts.contracts import (
    ConflictKind,
    ConflictResolutionKind,
    ConflictResolutionOutcome,
)


class ConflictCaptureOutcome(StrEnum):
    """The closed capture-path outcomes used as metric labels."""

    CAPTURED = "captured"
    REPLAYED = "replayed"
    REJECTED = "rejected"


class ConflictResolutionMetricOutcome(StrEnum):
    """The closed resolution-path outcomes used as metric labels.

    Extends the two terminal result outcomes with the replay and rejection
    branches so exactly one label exists per completed or rejected
    resolution path.
    """

    RESOLVED = "resolved"
    STALE_SUCCESSOR = "stale_successor"
    REPLAYED = "replayed"
    REJECTED = "rejected"


class SourceConflictOperation(StrEnum):
    """The closed operation label standing in for the route template."""

    CAPTURE = "capture"
    RESOLVE = "resolve"


class SourceConflictRejectionReason(StrEnum):
    """The closed rejection reason codes mirroring the domain error registry."""

    SOURCE_CONFLICT_INPUT_INVALID = "source_conflict_input_invalid"
    SOURCE_CONFLICT_NOT_FOUND = "source_conflict_not_found"
    SOURCE_CONFLICT_STATE_INVALID = "source_conflict_state_invalid"
    SOURCE_CONFLICT_IDEMPOTENCY_MISMATCH = "source_conflict_idempotency_mismatch"
    SOURCE_CONFLICT_EVIDENCE_UNAVAILABLE = "source_conflict_evidence_unavailable"
    SOURCE_CONFLICT_EVIDENCE_INTEGRITY_FAILED = "source_conflict_evidence_integrity_failed"
    SOURCE_CONFLICT_DEPENDENCY_UNAVAILABLE = "source_conflict_dependency_unavailable"
    SOURCE_CONFLICT_COMMIT_OUTCOME_UNKNOWN = "source_conflict_commit_outcome_unknown"


#: The exact required metric names and their label dimensions. IDs, keys,
#: locators, digests and object keys are never metric labels, so no
#: dimension names one.
SOURCE_CONFLICT_METRIC_CONTRACTS: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {
        "source_conflict_capture_total": frozenset({"conflict_kind", "outcome"}),
        "source_conflict_capture_duration_seconds": frozenset({"conflict_kind", "outcome"}),
        "source_conflict_resolution_total": frozenset({"resolution_kind", "outcome"}),
        "source_conflict_resolution_duration_seconds": frozenset(
            {"resolution_kind", "outcome"}
        ),
        "source_conflict_rejection_total": frozenset({"operation", "reason_code"}),
    }
)


def _validate_finite_non_negative(field_name: str, value: float) -> None:
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{field_name} must be finite and non-negative")


def _validate_label(field_name: str, expected_type: type, value: object) -> None:
    if not isinstance(value, expected_type):
        raise ValueError(f"{field_name} label must be a closed enum member")


@runtime_checkable
class SourceConflictMetrics(Protocol):
    """The low-cardinality source-conflict metrics sink every path uses."""

    def record_capture(
        self,
        *,
        conflict_kind: ConflictKind,
        outcome: ConflictCaptureOutcome,
        duration_seconds: float,
    ) -> None:
        """Record one completed capture-path outcome and its duration in seconds."""
        ...

    def record_resolution(
        self,
        *,
        resolution_kind: ConflictResolutionKind,
        outcome: ConflictResolutionOutcome | ConflictResolutionMetricOutcome,
        duration_seconds: float,
    ) -> None:
        """Record one completed resolution-path outcome and its duration."""
        ...

    def record_rejection(
        self,
        *,
        operation: SourceConflictOperation,
        reason_code: SourceConflictRejectionReason,
    ) -> None:
        """Increment the rejection counter for one closed reason."""
        ...


_RESOLUTION_OUTCOME_TYPES: Final[tuple[type, ...]] = (
    ConflictResolutionOutcome,
    ConflictResolutionMetricOutcome,
)

#: The closed label universe one resolution outcome may come from.
type _ResolutionOutcomeLabel = ConflictResolutionOutcome | ConflictResolutionMetricOutcome


class InMemorySourceConflictMetrics:
    """Bounded in-memory recorder implementing :class:`SourceConflictMetrics`.

    Sufficient for runtime checks and tests without introducing Prometheus.
    Counters are keyed only by the closed enum labels, and the recorder
    rejects negative or non-finite durations and any non-enum label value so
    a UUID, key, locator, digest or object key can never become a label.
    """

    def __init__(self) -> None:
        self._captures: dict[tuple[ConflictKind, ConflictCaptureOutcome], int] = {}
        self._resolutions: dict[
            tuple[ConflictResolutionKind, _ResolutionOutcomeLabel], int
        ] = {}
        self._rejections: dict[
            tuple[SourceConflictOperation, SourceConflictRejectionReason], int
        ] = {}

    def record_capture(
        self,
        *,
        conflict_kind: ConflictKind,
        outcome: ConflictCaptureOutcome,
        duration_seconds: float,
    ) -> None:
        _validate_label("conflict_kind", ConflictKind, conflict_kind)
        _validate_label("outcome", ConflictCaptureOutcome, outcome)
        _validate_finite_non_negative("duration_seconds", duration_seconds)
        key = (conflict_kind, outcome)
        self._captures[key] = self._captures.get(key, 0) + 1

    def record_resolution(
        self,
        *,
        resolution_kind: ConflictResolutionKind,
        outcome: ConflictResolutionOutcome | ConflictResolutionMetricOutcome,
        duration_seconds: float,
    ) -> None:
        _validate_label("resolution_kind", ConflictResolutionKind, resolution_kind)
        if not any(
            isinstance(outcome, outcome_type) for outcome_type in _RESOLUTION_OUTCOME_TYPES
        ):
            raise ValueError("outcome label must be a closed enum member")
        _validate_finite_non_negative("duration_seconds", duration_seconds)
        key = (resolution_kind, outcome)
        self._resolutions[key] = self._resolutions.get(key, 0) + 1

    def record_rejection(
        self,
        *,
        operation: SourceConflictOperation,
        reason_code: SourceConflictRejectionReason,
    ) -> None:
        _validate_label("operation", SourceConflictOperation, operation)
        _validate_label("reason_code", SourceConflictRejectionReason, reason_code)
        key = (operation, reason_code)
        self._rejections[key] = self._rejections.get(key, 0) + 1

    def capture_count(
        self, conflict_kind: ConflictKind, outcome: ConflictCaptureOutcome
    ) -> int:
        return self._captures.get((conflict_kind, outcome), 0)

    def resolution_count(
        self,
        resolution_kind: ConflictResolutionKind,
        outcome: ConflictResolutionOutcome | ConflictResolutionMetricOutcome,
    ) -> int:
        return self._resolutions.get((resolution_kind, outcome), 0)

    def rejection_count(
        self,
        operation: SourceConflictOperation,
        reason_code: SourceConflictRejectionReason,
    ) -> int:
        return self._rejections.get((operation, reason_code), 0)

    def __repr__(self) -> str:
        return "InMemorySourceConflictMetrics(redacted)"


__all__ = [
    "SOURCE_CONFLICT_METRIC_CONTRACTS",
    "ConflictCaptureOutcome",
    "ConflictResolutionMetricOutcome",
    "InMemorySourceConflictMetrics",
    "SourceConflictMetrics",
    "SourceConflictOperation",
    "SourceConflictRejectionReason",
]
