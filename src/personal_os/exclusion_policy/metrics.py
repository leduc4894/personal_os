"""Low-cardinality exclusion-policy evaluation and preview metrics contracts.

Spec 21 requires ``exclusion_policy_evaluation_total{boundary,decision}`` and
``exclusion_policy_evaluation_duration_seconds{boundary,decision}`` for
per-source evaluations plus ``exclusion_policy_preview_total{outcome}`` and
``exclusion_policy_preview_duration_seconds{outcome}`` for complete preview
executions, and ``exclusion_policy_publication_total{outcome}`` for known
durable publication outcomes. Every label is a closed :class:`enum.StrEnum`
member: the boundary vocabulary mirrors the mandatory boundaries of spec 14.2,
the decision label is the raw three-value decision so indeterminacy stays
observable, and the preview and publication outcomes are recorded only after
the durable outcome is known. Workspace, source, rule, preview, revision,
path, media type and key ID are prohibited labels and can never be recorded.

:class:`ExclusionPolicyMetrics` is the injectable Protocol enforcement paths
depend on; :class:`InMemoryExclusionPolicyMetrics` is the bounded test and
standalone recorder. A production sink implements the same Protocol behind
the boundary and, like the in-memory recorder, must reject non-enum labels
and negative or non-finite durations.
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final, Protocol, runtime_checkable


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


@dataclass(frozen=True, slots=True)
class EvaluationRecord:
    """One recorded evaluation outcome.

    Carries only the closed boundary and decision enums plus a finite
    non-negative duration; never a UUID, locator, operand or subject
    fingerprint.
    """

    boundary: PolicyBoundary
    decision: EvaluationMetricOutcome
    duration_seconds: float


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


def _validate_label(field_name: str, expected_type: type, value: object) -> None:
    if not isinstance(value, expected_type):
        raise ValueError(f"{field_name} label must be a closed enum member")


def _validate_finite_non_negative(field_name: str, value: float) -> None:
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{field_name} must be finite and non-negative")


@runtime_checkable
class ExclusionPolicyMetrics(Protocol):
    """The low-cardinality exclusion-policy metrics sink."""

    def record_evaluation(
        self,
        *,
        boundary: PolicyBoundary,
        decision: EvaluationMetricOutcome,
        duration_seconds: float,
    ) -> None:
        """Record one completed evaluation outcome and its duration in seconds."""
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


class InMemoryExclusionPolicyMetrics:
    """Bounded in-memory recorder implementing :class:`ExclusionPolicyMetrics`.

    Keeps at most :data:`_MAXIMUM_EVALUATION_RECORDS` evaluation and preview
    records in ring buffers keyed only by the closed enum labels, and rejects
    negative or non-finite durations and any non-enum label so a UUID, locator
    or operand can never become a label.
    """

    def __init__(self) -> None:
        self._evaluations: deque[EvaluationRecord] = deque(maxlen=_MAXIMUM_EVALUATION_RECORDS)
        self._previews: deque[PreviewRecord] = deque(maxlen=_MAXIMUM_EVALUATION_RECORDS)
        self._publications: deque[PublicationRecord] = deque(maxlen=_MAXIMUM_EVALUATION_RECORDS)

    def record_evaluation(
        self,
        *,
        boundary: PolicyBoundary,
        decision: EvaluationMetricOutcome,
        duration_seconds: float,
    ) -> None:
        _validate_label("boundary", PolicyBoundary, boundary)
        _validate_label("decision", EvaluationMetricOutcome, decision)
        _validate_finite_non_negative("duration_seconds", duration_seconds)
        self._evaluations.append(
            EvaluationRecord(
                boundary=boundary,
                decision=decision,
                duration_seconds=duration_seconds,
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

    def publication_records(self) -> list[PublicationRecord]:
        """A snapshot list of recorded publication outcomes (oldest first)."""

        return list(self._publications)

    def publication_count(self, outcome: PublicationMetricOutcome) -> int:
        return sum(1 for record in self._publications if record.outcome is outcome)

    def evaluation_count(self, boundary: PolicyBoundary, decision: EvaluationMetricOutcome) -> int:
        return sum(
            1
            for record in self._evaluations
            if record.boundary is boundary and record.decision is decision
        )

    def __repr__(self) -> str:
        return "InMemoryExclusionPolicyMetrics(redacted)"
