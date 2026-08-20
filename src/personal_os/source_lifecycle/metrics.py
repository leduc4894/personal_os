"""Closed low-cardinality lifecycle telemetry contracts."""

from __future__ import annotations

from collections.abc import Mapping
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
