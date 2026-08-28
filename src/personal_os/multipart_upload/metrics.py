"""Low-cardinality multipart upload metrics and in-memory recorder.

Every metric label is a closed :class:`enum.StrEnum` member: session,
completion and cleanup outcomes, the closed stage that stands in for the
route template, and rejection reason codes mirroring the domain error
registry. Session IDs, staging keys, provider upload IDs, ETags, presigned
URLs, digests, byte counts and provider messages are never accepted as
labels and never recorded. :data:`MULTIPART_METRIC_CONTRACTS` pins the
exact metric names and their label dimensions, and every dimension name is
itself a member of the closed label universe
:data:`MULTIPART_METRIC_LABEL_NAMES` — ``outcome``, ``state``,
``platform_class``, ``stage`` and ``error_code`` only (spec 7) — enforced
at import time by :func:`validate_multipart_metric_contracts`, so an
identifier-bearing dimension (a session ID, staging key, provider upload
ID, ETag, request ID, path, locator, digest, URL or signature) can never
be registered as a label name.

:class:`MultipartUploadMetrics` is the injectable Protocol the orchestration
service depends on; :class:`InMemoryMultipartUploadMetrics` is the bounded
test/standalone implementation sufficient for runtime checks and tests
without introducing Prometheus. A production sink implements the same
Protocol behind the boundary and, like the in-memory recorder, must reject
negative or non-finite durations and any non-enum label value.

The recorder additionally keeps a bounded ring of the most recent rejection
records — closed reason code, epoch-millisecond timestamp and the closed
stage label standing in for the route template — and exposes both ring and
counters through the read-side
:class:`MultipartRejectionDiagnosticsSource` protocol. The ring is the
durable-trail surface the committed session's inline exact staging-delete
failure records its closed reason token on (:meth:`record_cleanup_failed`
is the first-class entry point for that one path): the committed terminal
state has no cleanup-obligation exit, so the ring plus the counter is the
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

from personal_os.error_contracts.codes import ErrorCode

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
    """The closed stage label of one multipart boundary's rejections.

    Each member is the ``stage`` label value of one multipart boundary; the
    metrics layer sits below the request-correlation plumbing that owns
    route templates, so this closed label carries the diagnostic value
    without new plumbing. Never a UUID, key, locator, digest, token, path
    or free-form string.
    """

    SESSION_CREATE = "session_create"
    SESSION_STATUS = "session_status"
    PART_URL = "part_url"
    COMPLETION = "completion"
    SESSION_ABORT = "session_abort"
    CLEANUP = "cleanup"


class MultipartRejectionReason(StrEnum):
    """The closed ``error_code`` label values mirroring the domain registry."""

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


#: The closed universe of multipart metric label names (spec 7): outcome,
#: state, platform class, stage and safe error code — no other dimension
#: name may ever label a multipart metric, which by construction excludes
#: every identifier-bearing dimension: no session ID, staging key,
#: provider upload ID, ETag, request ID, filename/path, locator, digest,
#: URL, signature or workspace/device identifier is a member.
MULTIPART_METRIC_LABEL_NAMES: Final[frozenset[str]] = frozenset(
    {"outcome", "state", "platform_class", "stage", "error_code"}
)

#: The exact required metric names and their label dimensions. Every
#: dimension name is a member of :data:`MULTIPART_METRIC_LABEL_NAMES`;
#: IDs, keys, locators, digests, URLs, provider identities and tokens are
#: never metric labels, so no dimension names one.
MULTIPART_METRIC_CONTRACTS: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {
        "multipart_session_total": frozenset({"outcome"}),
        "multipart_session_duration_seconds": frozenset({"outcome"}),
        "multipart_completion_total": frozenset({"outcome"}),
        "multipart_completion_duration_seconds": frozenset({"outcome"}),
        "multipart_cleanup_total": frozenset({"outcome"}),
        "multipart_rejection_total": frozenset({"stage", "error_code"}),
    }
)


def validate_multipart_metric_contracts(
    contracts: Mapping[str, frozenset[str]] | None = None,
) -> None:
    """Validate that every multipart metric labels only the closed universe.

    The landed contracts are validated at import time; a caller may pass its
    own mapping (the registration seam a production sink uses) to validate a
    candidate contract before it is ever exported. Raises ``ValueError``
    naming the offending metric and label as soon as one label name falls
    outside :data:`MULTIPART_METRIC_LABEL_NAMES` — which is exactly how an
    identifier-bearing label name (``session_id``, ``staging_key``,
    ``provider_upload_id``, ``etag``, ``request_id``, ``path``, ``digest``,
    ``url``, ``signature``, …) is rejected: none of them is a member of the
    closed five-name universe, so none can ever be registered.
    """

    for metric_name, labels in (
        MULTIPART_METRIC_CONTRACTS if contracts is None else contracts
    ).items():
        rejected_label_names = sorted(labels - MULTIPART_METRIC_LABEL_NAMES)
        if rejected_label_names:
            raise ValueError(
                f"{metric_name} label {rejected_label_names[0]!r} is not a"
                " closed metric label name of MULTIPART_METRIC_LABEL_NAMES"
            )


validate_multipart_metric_contracts()


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
    rejection and the closed stage label. Never a UUID, key, locator,
    digest, URL, provider identity, path or free-form string.
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

    def record_cleanup_failed(self, error_code: ErrorCode) -> None:
        """Record one cleanup failure's closed registry reason on the ring.

        The committed session's inline exact staging-delete has no
        cleanup-obligation exit, so its swallowed closed reason surfaces
        here: the readable reason surface of that one path. Accepts only
        the closed ``multipart_*`` registry block; a foreign code raises
        ``ValueError`` and records nothing.
        """
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

    def record_cleanup_failed(self, error_code: ErrorCode) -> None:
        """Record one cleanup failure's closed registry reason on the ring.

        Only the closed ``multipart_*`` registry block records: a foreign
        code's constructor lookup raises ``ValueError`` before any counter
        or ring mutation runs.
        """

        self.record_rejection(
            flow=MultipartMetricFlow.CLEANUP,
            reason_code=MultipartRejectionReason(error_code.value),
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
    "MULTIPART_METRIC_LABEL_NAMES",
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
    "validate_multipart_metric_contracts",
]
