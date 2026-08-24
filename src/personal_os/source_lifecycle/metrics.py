"""Closed low-cardinality lifecycle telemetry contracts and in-memory recorder.

Every metric label is a closed :class:`enum.StrEnum` member: operation,
outcome and error code. UUIDs, idempotency keys, locators, digests, titles,
fingerprints and tokens are never accepted as labels and never recorded.

:class:`SourceLifecycleMetrics` is the injectable write-side Protocol the
service and the durable store depend on. :class:`InMemorySourceLifecycleMetrics`
additionally keeps commit counters, a bounded ring of commit records and a
bounded ring of the most recent rejection records — closed error code,
epoch-millisecond timestamp and the closed operation label standing in for
the route template, because the metrics layer sits below the
request-correlation plumbing that owns route templates — and exposes the
commit counters and the rejection ring through the read-side
:class:`SourceLifecycleDiagnosticsSource` protocol the Web Admin lifecycle
diagnostics route consumes.
"""

from __future__ import annotations

import math
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final, Protocol, runtime_checkable

from personal_os.source_lifecycle.commands import LifecycleOperation
from personal_os.source_lifecycle.errors import SourceLifecycleErrorCode

#: Maximum number of retained commit records. The recorder is a bounded ring
#: buffer, never an unbounded audit log.
_MAXIMUM_LIFECYCLE_RECORDS: Final[int] = 4096

#: Maximum number of retained rejection records in the diagnostics ring.
_MAXIMUM_REJECTION_RECORDS: Final[int] = 50

_NANOSECONDS_PER_MILLISECOND: Final = 1_000_000


def _wall_clock_epoch_ms() -> int:
    """Return the current wall-clock moment in epoch milliseconds."""
    return time.time_ns() // _NANOSECONDS_PER_MILLISECOND


class LifecycleMetricOutcome(StrEnum):
    COMMITTED = "committed"
    REJECTED = "rejected"
    REPLAYED = "replayed"


SOURCE_LIFECYCLE_METRIC_CONTRACTS: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {
        "source_lifecycle_commit_total": frozenset({"operation", "outcome"}),
        "source_lifecycle_commit_duration_seconds": frozenset({"operation", "outcome"}),
        "source_lifecycle_rejection_total": frozenset({"operation", "error_code"}),
    }
)


@runtime_checkable
class SourceLifecycleMetrics(Protocol):
    """Telemetry port accepting only operation, outcome and safe error labels."""

    def record_commit(
        self,
        *,
        operation: LifecycleOperation,
        outcome: LifecycleMetricOutcome,
        duration_seconds: float,
    ) -> None: ...

    def record_rejection(
        self,
        *,
        operation: LifecycleOperation,
        error_code: SourceLifecycleErrorCode,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class SourceLifecycleMetricRecord:
    """One recorded lifecycle outcome: closed labels and a finite duration only."""

    operation: LifecycleOperation
    outcome: LifecycleMetricOutcome
    duration_seconds: float


@dataclass(frozen=True, slots=True)
class SourceLifecycleRejectionRecord:
    """One recent rejection of the bounded diagnostics ring.

    Carries only the closed error code, the epoch-millisecond moment of the
    rejection and the closed operation label. The operation label stands in
    for the design's route-template token: the metrics layer sits below the
    request-correlation plumbing that owns route templates. Never a UUID,
    key, locator, digest, token, path or free-form string.
    """

    error_code: SourceLifecycleErrorCode
    at_epoch_ms: int
    operation: LifecycleOperation


@dataclass(frozen=True, slots=True)
class SourceLifecycleDiagnostics:
    """One immutable snapshot of the evidence the Admin route serves.

    ``commit_counters`` maps every observed (operation, outcome) pair to its
    count; ``recent_rejections`` is the bounded ring in recorded order,
    oldest first. Both are copies: later recordings never mutate a snapshot
    already taken.
    """

    commit_counters: Mapping[tuple[LifecycleOperation, LifecycleMetricOutcome], int]
    recent_rejections: tuple[SourceLifecycleRejectionRecord, ...]


@runtime_checkable
class SourceLifecycleDiagnosticsSource(Protocol):
    """The read side of a lifecycle-recording sink the Admin route consumes."""

    def lifecycle_diagnostics(self) -> SourceLifecycleDiagnostics:
        """Return one immutable snapshot of commit counters and the rejection ring."""
        ...


@runtime_checkable
class SourceLifecycleMetricsWithDiagnostics(
    SourceLifecycleMetrics, SourceLifecycleDiagnosticsSource, Protocol
):
    """Composition seam: a write sink that also exposes its diagnostics."""


def _validate_finite_non_negative(field_name: str, value: float) -> None:
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{field_name} must be finite and non-negative")


def _validate_label(field_name: str, expected_type: type, value: object) -> None:
    if not isinstance(value, expected_type):
        raise ValueError(f"{field_name} label must be a closed enum member")


def _validate_epoch_ms(value: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("epoch_ms_clock must return a non-negative integer")


class InMemorySourceLifecycleMetrics:
    """Bounded in-memory recorder implementing :class:`SourceLifecycleMetrics`.

    Carries commit counters, a bounded ring of at most
    :data:`_MAXIMUM_LIFECYCLE_RECORDS` commit records, rejection counters
    and a bounded ring of at most :data:`_MAXIMUM_REJECTION_RECORDS`
    rejection records keyed only by the closed enum labels, and rejects
    negative or non-finite durations so a UUID, locator, title, fingerprint,
    idempotency key or token can never become a label. Rejection ring
    records are stamped through the injected epoch-millisecond clock (the
    wall clock by default), and :meth:`lifecycle_diagnostics` serves one
    immutable snapshot of the commit counters and the rejection ring.
    """

    def __init__(self, *, epoch_ms_clock: Callable[[], int] = _wall_clock_epoch_ms) -> None:
        self._records: deque[SourceLifecycleMetricRecord] = deque(maxlen=_MAXIMUM_LIFECYCLE_RECORDS)
        self._rejection_records: deque[SourceLifecycleRejectionRecord] = deque(
            maxlen=_MAXIMUM_REJECTION_RECORDS
        )
        self._commit_counters: dict[tuple[LifecycleOperation, LifecycleMetricOutcome], int] = {}
        self._rejections: dict[tuple[LifecycleOperation, SourceLifecycleErrorCode], int] = {}
        self._epoch_ms_clock = epoch_ms_clock

    def record_commit(
        self,
        *,
        operation: LifecycleOperation,
        outcome: LifecycleMetricOutcome,
        duration_seconds: float,
    ) -> None:
        _validate_label("operation", LifecycleOperation, operation)
        _validate_label("outcome", LifecycleMetricOutcome, outcome)
        _validate_finite_non_negative("duration_seconds", duration_seconds)
        self._records.append(
            SourceLifecycleMetricRecord(
                operation=operation,
                outcome=outcome,
                duration_seconds=duration_seconds,
            )
        )
        commit_key = (operation, outcome)
        self._commit_counters[commit_key] = self._commit_counters.get(commit_key, 0) + 1

    def record_rejection(
        self,
        *,
        operation: LifecycleOperation,
        error_code: SourceLifecycleErrorCode,
    ) -> None:
        _validate_label("operation", LifecycleOperation, operation)
        _validate_label("error_code", SourceLifecycleErrorCode, error_code)
        at_epoch_ms = self._epoch_ms_clock()
        _validate_epoch_ms(at_epoch_ms)
        key = (operation, error_code)
        self._rejections[key] = self._rejections.get(key, 0) + 1
        self._rejection_records.append(
            SourceLifecycleRejectionRecord(
                error_code=error_code,
                at_epoch_ms=at_epoch_ms,
                operation=operation,
            )
        )

    def commit_records(self) -> tuple[SourceLifecycleMetricRecord, ...]:
        return tuple(self._records)

    def rejection_count(
        self, operation: LifecycleOperation, error_code: SourceLifecycleErrorCode
    ) -> int:
        return self._rejections.get((operation, error_code), 0)

    def lifecycle_diagnostics(self) -> SourceLifecycleDiagnostics:
        """Return one immutable snapshot of the lifecycle evidence."""
        return SourceLifecycleDiagnostics(
            commit_counters=MappingProxyType(dict(self._commit_counters)),
            recent_rejections=tuple(self._rejection_records),
        )

    def __repr__(self) -> str:
        return "InMemorySourceLifecycleMetrics(redacted)"
