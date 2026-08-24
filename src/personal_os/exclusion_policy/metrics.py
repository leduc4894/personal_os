"""Low-cardinality exclusion-policy evaluation and preview metrics contracts.

Spec 21 requires ``exclusion_policy_evaluation_total{boundary,decision}`` and
``exclusion_policy_evaluation_duration_seconds{boundary,decision}`` for
per-source evaluations plus ``exclusion_policy_preview_total{outcome}`` and
``exclusion_policy_preview_duration_seconds{outcome}`` for complete preview
executions, and ``exclusion_policy_publication_total{outcome}`` for known
durable publication outcomes. Every label is a closed :class:`enum.StrEnum`
member: the boundary vocabulary mirrors the mandatory boundaries of spec 14.2,
the decision label is the raw evaluation decision so indeterminacy stays
observable, a policy system failure records the closed ``failed`` decision
together with its registry code, and the preview and publication outcomes are
recorded only after the durable outcome is known. Workspace, source, rule,
preview, revision, path, media type and key ID are prohibited labels and can
never be recorded.

:class:`ExclusionPolicyMetrics` is the injectable Protocol enforcement paths
depend on; :class:`InMemoryExclusionPolicyMetrics` is the bounded test and
standalone recorder. A production sink implements the same Protocol behind
the boundary and, like the in-memory recorder, must reject non-enum labels
and negative or non-finite durations.

The recorder additionally exposes a read side for the Web Admin diagnostics
route (spec 2026-08-24 C2): :class:`ExclusionPolicyDiagnosticsSource` serves
one immutable :class:`ExclusionPolicyDiagnostics` snapshot — the exact
evaluation counters by boundary and decision (``failed`` included), the exact
publication outcome counters, and a bounded ring of the most recent policy
system failures carrying exactly the closed registry code, the closed
boundary label and the epoch-millisecond timestamp stamped through the
injected clock.
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


class PolicyBoundary(StrEnum):
    """The closed enforcement-boundary vocabulary (spec 14.2)."""

    SYNC_PREFLIGHT = "sync_preflight"
    SINGLE_PART_UPLOAD = "single_part_upload"
    MULTIPART_UPLOAD = "multipart_upload"
    SOURCE_CREATE_UPDATE = "source_create_update"
    CANONICAL_READ = "canonical_read"
    MANIFEST_RECONCILE = "manifest_reconcile"
    CONFLICT_CAPTURE = "conflict_capture"
    INGESTION = "ingestion"
    REBUILD_REPAIR = "rebuild_repair"
    RETRIEVAL = "retrieval"
    MCP_ACTION = "mcp_action"


class EvaluationMetricOutcome(StrEnum):
    """The closed evaluation outcomes used as metric labels."""

    ALLOWED = "allowed"
    EXCLUDED = "excluded"
    INDETERMINATE = "indeterminate"
    #: The policy SYSTEM itself failed before it could decide (no active
    #: signed policy, corrupt signing material): the fail-closed raises
    #: record this outcome together with the closed registry code instead of
    #: recording nothing (spec 2026-08-24 C1).
    FAILED = "failed"


class PreviewMetricOutcome(StrEnum):
    """The closed preview outcomes used as metric labels (spec 21).

    Recorded only after the durable outcome is known: ``ready`` for a complete
    atomic result set and ``failed`` for every durable failure transition.
    Workspace, preview and source IDs are prohibited labels.
    """

    READY = "ready"
    FAILED = "failed"


class PublicationMetricOutcome(StrEnum):
    """The closed publication outcomes used as metric labels (spec 21).

    Recorded only after the durable outcome is known: ``published`` for a
    fresh committed revision, ``replayed`` for an exact replay
    acknowledgement (including recovery-resolved ones) and ``rejected`` for
    a terminal business rejection. An unknown commit outcome records
    nothing. Workspace, preview, revision and key IDs are prohibited labels.
    """

    PUBLISHED = "published"
    REPLAYED = "replayed"
    REJECTED = "rejected"


#: The exact evaluation and preview metric names and their label dimensions
#: (spec 21). IDs, locators, operands and revision numbers are never labels.
EXCLUSION_POLICY_METRIC_CONTRACTS: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {
        "exclusion_policy_evaluation_total": frozenset({"boundary", "decision"}),
        "exclusion_policy_evaluation_duration_seconds": frozenset({"boundary", "decision"}),
        "exclusion_policy_preview_total": frozenset({"outcome"}),
        "exclusion_policy_preview_duration_seconds": frozenset({"outcome"}),
        "exclusion_policy_publication_total": frozenset({"outcome"}),
    }
)

#: Maximum retained per-evaluation records; a bounded ring for tests and
#: standalone runs, never an unbounded audit log.
_MAXIMUM_EVALUATION_RECORDS: Final[int] = 4096

#: Maximum number of retained failure records in the diagnostics ring (spec
#: 2026-08-24 C2): the bounded recent-failure surface of the Web Admin
#: diagnostics route.
_MAXIMUM_FAILURE_RECORDS: Final[int] = 50

_NANOSECONDS_PER_MILLISECOND: Final = 1_000_000


def _wall_clock_epoch_ms() -> int:
    """Return the current wall-clock moment in epoch milliseconds."""
    return time.time_ns() // _NANOSECONDS_PER_MILLISECOND


@dataclass(frozen=True, slots=True)
class EvaluationRecord:
    """One recorded evaluation outcome.

    Carries only the closed boundary and decision enums plus a finite
    non-negative duration; never a UUID, locator, operand or subject
    fingerprint. A ``failed`` decision additionally carries the closed
    registry ``error_code`` that names the policy system failure.
    """

    boundary: PolicyBoundary
    decision: EvaluationMetricOutcome
    duration_seconds: float
    error_code: ErrorCode | None = None


@dataclass(frozen=True, slots=True)
class PreviewRecord:
    """One recorded preview outcome.

    Carries only the closed outcome enum plus a finite non-negative duration;
    never a workspace, preview or source ID.
    """

    outcome: PreviewMetricOutcome
    duration_seconds: float


@dataclass(frozen=True, slots=True)
class PublicationRecord:
    """One recorded publication outcome.

    Carries only the closed outcome enum plus a finite non-negative duration;
    never a workspace, preview, revision or key ID.
    """

    outcome: PublicationMetricOutcome
    duration_seconds: float


@dataclass(frozen=True, slots=True)
class PolicyFailureRecord:
    """One recent policy system failure of the bounded diagnostics ring.

    Carries only the closed boundary label, the closed registry error code
    that names the policy system failure and the epoch-millisecond moment of
    the recording. The boundary label stands in for the design's
    route-template token: the metrics layer sits below the request-correlation
    plumbing that owns route templates. Never a UUID, locator, operand,
    subject fingerprint, digest, path or free-form string.
    """

    boundary: PolicyBoundary
    error_code: ErrorCode
    at_epoch_ms: int


@dataclass(frozen=True, slots=True)
class ExclusionPolicyDiagnostics:
    """One immutable snapshot of the policy evidence the Admin route serves.

    ``evaluation_counters`` maps every observed (boundary, decision) pair to
    its exact count and ``publication_counters`` every observed publication
    outcome to its exact count — both exact regardless of ring eviction.
    ``recent_failures`` is the bounded ring in recorded order, oldest first.
    All three are copies: later recordings never mutate a snapshot already
    taken.
    """

    evaluation_counters: Mapping[tuple[PolicyBoundary, EvaluationMetricOutcome], int]
    publication_counters: Mapping[PublicationMetricOutcome, int]
    recent_failures: tuple[PolicyFailureRecord, ...]


def _validate_label(field_name: str, expected_type: type, value: object) -> None:
    if not isinstance(value, expected_type):
        raise ValueError(f"{field_name} label must be a closed enum member")


def _validate_finite_non_negative(field_name: str, value: float) -> None:
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{field_name} must be finite and non-negative")


def _validate_evaluation_error_code(
    decision: EvaluationMetricOutcome, error_code: ErrorCode | None
) -> None:
    """Keep the failure code closed: only a ``failed`` decision may carry one."""

    if decision is EvaluationMetricOutcome.FAILED:
        if error_code is None:
            raise ValueError("the failed decision requires its closed error_code")
    elif error_code is not None:
        raise ValueError("error_code is recordable only on the failed decision")


def _validate_epoch_ms(value: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("epoch_ms_clock must return a non-negative integer")


@runtime_checkable
class ExclusionPolicyMetrics(Protocol):
    """The low-cardinality exclusion-policy metrics sink."""

    def record_evaluation(
        self,
        *,
        boundary: PolicyBoundary,
        decision: EvaluationMetricOutcome,
        duration_seconds: float,
        error_code: ErrorCode | None = None,
    ) -> None:
        """Record one completed evaluation outcome and its duration in seconds.

        A ``failed`` decision must carry its closed registry ``error_code``;
        every other decision carries none.
        """
        ...

    def record_preview(
        self,
        *,
        outcome: PreviewMetricOutcome,
        duration_seconds: float,
    ) -> None:
        """Record one durable preview outcome and its duration in seconds."""
        ...

    def record_publication(
        self,
        *,
        outcome: PublicationMetricOutcome,
        duration_seconds: float,
    ) -> None:
        """Record one known durable publication outcome and its duration."""
        ...


@runtime_checkable
class ExclusionPolicyDiagnosticsSource(Protocol):
    """The read side of a policy metrics sink the Admin route consumes."""

    def policy_diagnostics(self) -> ExclusionPolicyDiagnostics:
        """Return one immutable snapshot of counters and the failure ring."""
        ...


@runtime_checkable
class ExclusionPolicyMetricsWithDiagnostics(
    ExclusionPolicyMetrics, ExclusionPolicyDiagnosticsSource, Protocol
):
    """Composition seam: a write sink that also exposes its read side."""


class InMemoryExclusionPolicyMetrics:
    """Bounded in-memory recorder implementing :class:`ExclusionPolicyMetrics`.

    Keeps at most :data:`_MAXIMUM_EVALUATION_RECORDS` evaluation and preview
    records in ring buffers keyed only by the closed enum labels, and rejects
    negative or non-finite durations and any non-enum label so a UUID, locator
    or operand can never become a label. The diagnostics read side keeps
    exact counters keyed only by the closed labels — exact regardless of ring
    eviction — plus a bounded ring of :data:`_MAXIMUM_FAILURE_RECORDS` closed
    failure records stamped through the injected epoch-millisecond clock (the
    wall clock by default); :meth:`policy_diagnostics` serves immutable
    snapshots of all three.
    """

    def __init__(self, *, epoch_ms_clock: Callable[[], int] = _wall_clock_epoch_ms) -> None:
        self._evaluations: deque[EvaluationRecord] = deque(maxlen=_MAXIMUM_EVALUATION_RECORDS)
        self._previews: deque[PreviewRecord] = deque(maxlen=_MAXIMUM_EVALUATION_RECORDS)
        self._publications: deque[PublicationRecord] = deque(maxlen=_MAXIMUM_EVALUATION_RECORDS)
        self._evaluation_counters: dict[tuple[PolicyBoundary, EvaluationMetricOutcome], int] = {}
        self._publication_counters: dict[PublicationMetricOutcome, int] = {}
        self._failure_records: deque[PolicyFailureRecord] = deque(maxlen=_MAXIMUM_FAILURE_RECORDS)
        self._epoch_ms_clock = epoch_ms_clock

    def record_evaluation(
        self,
        *,
        boundary: PolicyBoundary,
        decision: EvaluationMetricOutcome,
        duration_seconds: float,
        error_code: ErrorCode | None = None,
    ) -> None:
        _validate_label("boundary", PolicyBoundary, boundary)
        _validate_label("decision", EvaluationMetricOutcome, decision)
        _validate_finite_non_negative("duration_seconds", duration_seconds)
        _validate_evaluation_error_code(decision, error_code)
        self._evaluations.append(
            EvaluationRecord(
                boundary=boundary,
                decision=decision,
                duration_seconds=duration_seconds,
                error_code=error_code,
            )
        )
        counter_key = (boundary, decision)
        self._evaluation_counters[counter_key] = self._evaluation_counters.get(counter_key, 0) + 1
        if decision is EvaluationMetricOutcome.FAILED:
            # ``_validate_evaluation_error_code`` guarantees the code is present
            # exactly on the failed decision; the check keeps the narrowing
            # explicit for the closed record constructor below.
            if error_code is None:  # pragma: no cover - guarded above
                raise ValueError("the failed decision requires its closed error_code")
            at_epoch_ms = self._epoch_ms_clock()
            _validate_epoch_ms(at_epoch_ms)
            self._failure_records.append(
                PolicyFailureRecord(
                    boundary=boundary,
                    error_code=error_code,
                    at_epoch_ms=at_epoch_ms,
                )
            )

    def evaluation_records(self) -> list[EvaluationRecord]:
        """A snapshot list of recorded evaluation outcomes (oldest first)."""

        return list(self._evaluations)

    def record_preview(
        self,
        *,
        outcome: PreviewMetricOutcome,
        duration_seconds: float,
    ) -> None:
        _validate_label("outcome", PreviewMetricOutcome, outcome)
        _validate_finite_non_negative("duration_seconds", duration_seconds)
        self._previews.append(PreviewRecord(outcome=outcome, duration_seconds=duration_seconds))

    def preview_records(self) -> list[PreviewRecord]:
        """A snapshot list of recorded preview outcomes (oldest first)."""

        return list(self._previews)

    def preview_count(self, outcome: PreviewMetricOutcome) -> int:
        return sum(1 for record in self._previews if record.outcome is outcome)

    def record_publication(
        self,
        *,
        outcome: PublicationMetricOutcome,
        duration_seconds: float,
    ) -> None:
        _validate_label("outcome", PublicationMetricOutcome, outcome)
        _validate_finite_non_negative("duration_seconds", duration_seconds)
        self._publications.append(
            PublicationRecord(outcome=outcome, duration_seconds=duration_seconds)
        )
        self._publication_counters[outcome] = self._publication_counters.get(outcome, 0) + 1

    def publication_records(self) -> list[PublicationRecord]:
        """A snapshot list of recorded publication outcomes (oldest first)."""

        return list(self._publications)

    def publication_count(self, outcome: PublicationMetricOutcome) -> int:
        return self._publication_counters.get(outcome, 0)

    def evaluation_count(self, boundary: PolicyBoundary, decision: EvaluationMetricOutcome) -> int:
        return self._evaluation_counters.get((boundary, decision), 0)

    def policy_diagnostics(self) -> ExclusionPolicyDiagnostics:
        """Return one immutable snapshot of the policy evidence."""

        return ExclusionPolicyDiagnostics(
            evaluation_counters=MappingProxyType(dict(self._evaluation_counters)),
            publication_counters=MappingProxyType(dict(self._publication_counters)),
            recent_failures=tuple(self._failure_records),
        )

    def __repr__(self) -> str:
        return "InMemoryExclusionPolicyMetrics(redacted)"
