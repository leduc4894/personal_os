"""Low-cardinality multipart upload metrics and in-memory recorder.

Every metric label is a closed :class:`enum.StrEnum` member: session,
completion and cleanup outcomes, the closed flow that stands in for the
route template, and rejection reason codes mirroring the domain error
registry. Session IDs, staging keys, provider upload IDs, ETags, presigned
URLs, digests, byte counts and provider messages are never accepted as
labels and never recorded. :data:`MULTIPART_METRIC_CONTRACTS` pins the
exact metric names and their label dimensions.

:class:`MultipartUploadMetrics` is the injectable Protocol the orchestration
service depends on; :class:`InMemoryMultipartUploadMetrics` is the bounded
test/standalone implementation sufficient for runtime checks and tests
without introducing Prometheus. A production sink implements the same
Protocol behind the boundary and, like the in-memory recorder, must reject
negative or non-finite durations and any non-enum label value.

The recorder additionally keeps a bounded ring of the most recent rejection
records — closed reason code, epoch-millisecond timestamp and the closed
flow label standing in for the route template — and exposes both ring and
counters through the read-side
:class:`MultipartRejectionDiagnosticsSource` protocol. The ring is the
durable-trail surface the committed session's inline exact staging-delete
failure records its closed reason token on: the committed terminal state
has no cleanup-obligation exit, so the ring plus the counter is the
readable reason surface for that one path.
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

#: Maximum number of retained per-path records. The recorder is a bounded
#: ring buffer for tests and standalone runs, never an unbounded audit log.
_MAXIMUM_UPLOAD_RECORDS: Final[int] = 4096

#: Maximum number of retained rejection records in the diagnostics ring.
_MAXIMUM_REJECTION_RECORDS: Final[int] = 50

_NANOSECONDS_PER_MILLISECOND: Final = 1_000_000


def _wall_clock_epoch_ms() -> int:
    """Return the current wall-clock moment in epoch milliseconds."""
    return time.time_ns() // _NANOSECONDS_PER_MILLISECOND


class MultipartSessionOutcome(StrEnum):
    """The closed create-or-resume outcomes used as metric labels."""

    CREATED = "created"
    REPLAYED = "replayed"
    REJECTED = "rejected"


class MultipartCompletionOutcome(StrEnum):
    """The closed completion-claim outcomes used as metric labels."""

    COMMITTED = "committed"
    REPLAYED = "replayed"
    INTEGRITY_FAILED = "integrity_failed"
    POLICY_DENIED = "policy_denied"
    CONFLICT = "conflict"
    REJECTED = "rejected"


class MultipartCleanupOutcome(StrEnum):
    """The closed exact-cleanup batch outcomes used as metric labels."""

    CLEANED = "cleaned"
    FAILED = "failed"


class MultipartMetricFlow(StrEnum):
    """The closed request flows whose rejections the ring records.

    Each member stands in for the diagnostics route-template token of one
    multipart boundary; the metrics layer sits below the
    request-correlation plumbing that owns route templates, so this closed
    label carries the diagnostic value without new plumbing. Never a UUID,
    key, locator, digest, token, path or free-form string.
    """

    SESSION_CREATE = "session_create"
    SESSION_STATUS = "session_status"
    PART_URL = "part_url"
    COMPLETION = "completion"
    SESSION_ABORT = "session_abort"
    CLEANUP = "cleanup"


class MultipartRejectionReason(StrEnum):
    """The closed rejection reason codes mirroring the domain error registry."""

    MULTIPART_SESSION_NOT_FOUND = "multipart_session_not_found"
    MULTIPART_SESSION_EXPIRED = "multipart_session_expired"
    MULTIPART_SESSION_STATE_INVALID = "multipart_session_state_invalid"
    MULTIPART_PART_INVALID = "multipart_part_invalid"
    MULTIPART_PART_URL_REJECTED = "multipart_part_url_rejected"
    MULTIPART_PROVIDER_STATE_INVALID = "multipart_provider_state_invalid"
    MULTIPART_COMPLETION_IN_PROGRESS = "multipart_completion_in_progress"
    MULTIPART_INTEGRITY_FAILED = "multipart_integrity_failed"
    MULTIPART_POLICY_DENIED = "multipart_policy_denied"
    MULTIPART_CLEANUP_FAILED = "multipart_cleanup_failed"
    MULTIPART_LOCAL_CONTENT_CHANGED = "multipart_local_content_changed"
    MULTIPART_DEPENDENCY_UNAVAILABLE = "multipart_dependency_unavailable"


#: The exact required metric names and their label dimensions. IDs, keys,
#: locators, digests, URLs, provider identities and tokens are never metric
#: labels, so no dimension names one.
MULTIPART_METRIC_CONTRACTS: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {
        "multipart_session_total": frozenset({"outcome"}),
        "multipart_session_duration_seconds": frozenset({"outcome"}),
        "multipart_completion_total": frozenset({"outcome"}),
        "multipart_completion_duration_seconds": frozenset({"outcome"}),
        "multipart_cleanup_total": frozenset({"outcome"}),
        "multipart_rejection_total": frozenset({"flow", "reason_code"}),
    }
)


@dataclass(frozen=True, slots=True)
class MultipartUploadRecord:
    """One recorded session, completion or cleanup outcome.

    Carries only the low-cardinality outcome enum and a finite non-negative
    duration; never a UUID, key, locator, digest, URL, provider identity or
    byte count of the transferred content.
    """

    outcome: MultipartSessionOutcome | MultipartCompletionOutcome | MultipartCleanupOutcome
    duration_seconds: float


@dataclass(frozen=True, slots=True)
class MultipartRejectionRecord:
    """One recent rejection of the bounded diagnostics ring.

    Carries only the closed reason code, the epoch-millisecond moment of the
    rejection and the closed flow label. Never a UUID, key, locator, digest,
    URL, provider identity, path or free-form string.
    """

    error_code: MultipartRejectionReason
    at_epoch_ms: int
    flow: MultipartMetricFlow


@dataclass(frozen=True, slots=True)
class MultipartRejectionDiagnostics:
    """One immutable snapshot of the rejection evidence a reader serves.

    ``rejection_counters`` maps every observed (flow, reason code) pair to
    its count; ``recent_rejections`` is the bounded ring in recorded order,
    oldest first. Both are copies: later recordings never mutate a snapshot
    already taken.
    """

    rejection_counters: Mapping[tuple[MultipartMetricFlow, MultipartRejectionReason], int]
    recent_rejections: tuple[MultipartRejectionRecord, ...]


def _validate_finite_non_negative(field_name: str, value: float) -> None:
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{field_name} must be finite and non-negative")


def _validate_label(field_name: str, expected_type: type, value: object) -> None:
    if not isinstance(value, expected_type):
        raise ValueError(f"{field_name} label must be a closed enum member")


def _validate_epoch_ms(value: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("epoch_ms_clock must return a non-negative integer")


@runtime_checkable
class MultipartUploadMetrics(Protocol):
    """The low-cardinality multipart upload metrics sink every path uses."""

    def record_session(
        self,
        *,
        outcome: MultipartSessionOutcome,
        duration_seconds: float,
    ) -> None:
        """Record one completed create-or-resume outcome and its duration."""
        ...

    def record_completion(
        self,
        *,
        outcome: MultipartCompletionOutcome,
        duration_seconds: float,
    ) -> None:
        """Record one completed completion-claim outcome and its duration."""
        ...

    def record_cleanup(
        self,
        *,
        outcome: MultipartCleanupOutcome,
    ) -> None:
        """Record one exact-cleanup outcome of one batch execution."""
        ...

    def record_rejection(
        self,
        *,
        flow: MultipartMetricFlow,
        reason_code: MultipartRejectionReason,
    ) -> None:
        """Increment the rejection counter for one closed reason."""
        ...


@runtime_checkable
class MultipartRejectionDiagnosticsSource(Protocol):
    """The read side of a rejection-recording sink a diagnostics route consumes."""

    def rejection_diagnostics(self) -> MultipartRejectionDiagnostics:
        """Return one immutable snapshot of counters and the bounded ring."""
        ...


@runtime_checkable
class MultipartUploadMetricsWithRejectionDiagnostics(
    MultipartUploadMetrics, MultipartRejectionDiagnosticsSource, Protocol
):
    """Composition seam: a write sink that also exposes its rejection ring."""


class InMemoryMultipartUploadMetrics:
    """Bounded in-memory recorder implementing :class:`MultipartUploadMetrics`.

    Sufficient for runtime checks and tests without introducing Prometheus.
    It keeps at most :data:`_MAXIMUM_UPLOAD_RECORDS` path records in a ring
    buffer plus counters keyed only by the closed enum labels, and rejects
    negative or non-finite durations and any non-enum label value so a UUID,
    key, locator, digest, URL or provider identity can never become a label.
    Rejections additionally append to a bounded ring of
    :data:`_MAXIMUM_REJECTION_RECORDS` closed records stamped through the
    injected epoch-millisecond clock (the wall clock by default), and
    :meth:`rejection_diagnostics` serves immutable snapshots of both.
    """

    def __init__(self, *, epoch_ms_clock: Callable[[], int] = _wall_clock_epoch_ms) -> None:
        self._records: deque[MultipartUploadRecord] = deque(maxlen=_MAXIMUM_UPLOAD_RECORDS)
        self._rejection_records: deque[MultipartRejectionRecord] = deque(
            maxlen=_MAXIMUM_REJECTION_RECORDS
        )
        self._cleanups: dict[MultipartCleanupOutcome, int] = {}
        self._rejections: dict[tuple[MultipartMetricFlow, MultipartRejectionReason], int] = {}
        self._epoch_ms_clock = epoch_ms_clock

    def record_session(
        self,
        *,
        outcome: MultipartSessionOutcome,
        duration_seconds: float,
    ) -> None:
        _validate_label("outcome", MultipartSessionOutcome, outcome)
        _validate_finite_non_negative("duration_seconds", duration_seconds)
        self._records.append(
            MultipartUploadRecord(outcome=outcome, duration_seconds=duration_seconds)
        )

    def record_completion(
        self,
        *,
        outcome: MultipartCompletionOutcome,
        duration_seconds: float,
    ) -> None:
        _validate_label("outcome", MultipartCompletionOutcome, outcome)
        _validate_finite_non_negative("duration_seconds", duration_seconds)
        self._records.append(
            MultipartUploadRecord(outcome=outcome, duration_seconds=duration_seconds)
        )

    def record_cleanup(self, *, outcome: MultipartCleanupOutcome) -> None:
        _validate_label("outcome", MultipartCleanupOutcome, outcome)
        self._cleanups[outcome] = self._cleanups.get(outcome, 0) + 1

    def record_rejection(
        self,
        *,
        flow: MultipartMetricFlow,
        reason_code: MultipartRejectionReason,
    ) -> None:
        _validate_label("flow", MultipartMetricFlow, flow)
        _validate_label("reason_code", MultipartRejectionReason, reason_code)
        at_epoch_ms = self._epoch_ms_clock()
        _validate_epoch_ms(at_epoch_ms)
        key = (flow, reason_code)
        self._rejections[key] = self._rejections.get(key, 0) + 1
        self._rejection_records.append(
            MultipartRejectionRecord(
                error_code=reason_code,
                at_epoch_ms=at_epoch_ms,
                flow=flow,
            )
        )

    def session_count(self, outcome: MultipartSessionOutcome) -> int:
        return sum(1 for record in self._records if record.outcome is outcome)

    def completion_count(self, outcome: MultipartCompletionOutcome) -> int:
        return sum(1 for record in self._records if record.outcome is outcome)

    def cleanup_count(self, outcome: MultipartCleanupOutcome) -> int:
        return self._cleanups.get(outcome, 0)

    def rejection_count(
        self, flow: MultipartMetricFlow, reason_code: MultipartRejectionReason
    ) -> int:
        return self._rejections.get((flow, reason_code), 0)

    def rejection_diagnostics(self) -> MultipartRejectionDiagnostics:
        """Return one immutable snapshot of the rejection evidence."""
        return MultipartRejectionDiagnostics(
            rejection_counters=MappingProxyType(dict(self._rejections)),
            recent_rejections=tuple(self._rejection_records),
        )

    def __repr__(self) -> str:
        return "InMemoryMultipartUploadMetrics(redacted)"


__all__ = [
    "MULTIPART_METRIC_CONTRACTS",
    "InMemoryMultipartUploadMetrics",
    "MultipartCleanupOutcome",
    "MultipartCompletionOutcome",
    "MultipartMetricFlow",
    "MultipartRejectionDiagnostics",
    "MultipartRejectionDiagnosticsSource",
    "MultipartRejectionReason",
    "MultipartRejectionRecord",
    "MultipartSessionOutcome",
    "MultipartUploadMetrics",
    "MultipartUploadMetricsWithRejectionDiagnostics",
    "MultipartUploadRecord",
]
