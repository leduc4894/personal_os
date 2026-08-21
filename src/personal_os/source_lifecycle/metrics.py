"""Closed low-cardinality lifecycle telemetry contracts and in-memory recorder."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final, Protocol, runtime_checkable

from personal_os.source_lifecycle.commands import LifecycleOperation
from personal_os.source_lifecycle.errors import SourceLifecycleErrorCode


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


def _validate_finite_non_negative(field_name: str, value: float) -> None:
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{field_name} must be finite and non-negative")


def _validate_label(field_name: str, expected_type: type, value: object) -> None:
    if not isinstance(value, expected_type):
        raise ValueError(f"{field_name} label must be a closed enum member")


@dataclass(frozen=True, slots=True)
class SourceLifecycleMetricRecord:
    """One recorded lifecycle outcome: closed labels and a finite duration only."""

    operation: LifecycleOperation
    outcome: LifecycleMetricOutcome
    duration_seconds: float


class InMemorySourceLifecycleMetrics:
    """Bounded in-memory recorder implementing :class:`SourceLifecycleMetrics`.

    Carries counters and a bounded record stream keyed only by the closed
    enum labels, and rejects negative or non-finite durations so a UUID,
    locator, title, fingerprint, idempotency key or token can never become
    a label.
    """

    def __init__(self) -> None:
        self._records: list[SourceLifecycleMetricRecord] = []
        self._rejections: dict[tuple[LifecycleOperation, SourceLifecycleErrorCode], int] = {}

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

    def record_rejection(
        self,
        *,
        operation: LifecycleOperation,
        error_code: SourceLifecycleErrorCode,
    ) -> None:
        _validate_label("operation", LifecycleOperation, operation)
        _validate_label("error_code", SourceLifecycleErrorCode, error_code)
        key = (operation, error_code)
        self._rejections[key] = self._rejections.get(key, 0) + 1

    def commit_records(self) -> tuple[SourceLifecycleMetricRecord, ...]:
        return tuple(self._records)

    def rejection_count(
        self, operation: LifecycleOperation, error_code: SourceLifecycleErrorCode
    ) -> int:
        return self._rejections.get((operation, error_code), 0)

    def __repr__(self) -> str:
        return "InMemorySourceLifecycleMetrics(redacted)"
