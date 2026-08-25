"""Low-cardinality device sync metric contracts and in-memory recorder.

Every metric label is a closed :class:`enum.StrEnum` member: the operation,
the outcome and the closed reason code (or ``None`` for a success). UUIDs,
locators, digests, media titles, object keys and provider messages are never
accepted as labels. :data:`DEVICE_SYNC_METRIC_CONTRACTS` pins the exact
metric names and their label dimensions.

:class:`DeviceSyncMetrics` is the injectable Protocol the service depends on;
:class:`InMemoryDeviceSyncMetrics` is the bounded test/standalone
implementation sufficient for runtime checks and tests without introducing
Prometheus. A production sink implements the same Protocol behind the
boundary and, like the in-memory recorder, must reject non-enum label values
and durations that are not non-negative integers.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Final, Protocol, runtime_checkable

from personal_os.device_sync.errors import DeviceSyncErrorCode


class DeviceSyncOperation(StrEnum):
    """The closed set of device sync operations used as metric labels."""

    PULL = "pull"
    ACKNOWLEDGE = "acknowledge"
    MANIFEST_START = "manifest_start"
    MANIFEST_PAGE = "manifest_page"
    MANIFEST_FINALIZE = "manifest_finalize"
    MANIFEST_ACTIONS = "manifest_actions"
    MANIFEST_COMPLETE = "manifest_complete"
    DOWNLOAD = "download"


class DeviceSyncOutcome(StrEnum):
    """The closed set of operation outcomes used as metric labels."""

    SUCCEEDED = "succeeded"
    REJECTED = "rejected"
    FAILED = "failed"
    REPLAYED = "replayed"


#: The exact required metric names and their label dimensions. IDs, keys,
#: locators, digests and titles are never metric labels, so no dimension
#: names one.
DEVICE_SYNC_METRIC_CONTRACTS: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {
        "device_sync_operation_total": frozenset({"operation", "outcome", "reason"}),
        "device_sync_operation_duration_ms": frozenset({"operation", "outcome", "reason"}),
    }
)


def _validate_metric_label(field_name: str, expected_type: type, value: object) -> None:
    if not isinstance(value, expected_type):
        raise ValueError(f"{field_name} label must be a closed enum member")


@runtime_checkable
class DeviceSyncMetrics(Protocol):
    """The low-cardinality device sync metrics sink every path uses."""

    def record_operation(
        self,
        *,
        operation: DeviceSyncOperation,
        outcome: DeviceSyncOutcome,
        reason: DeviceSyncErrorCode | None,
        duration_ms: int,
    ) -> None:
        """Record one completed operation outcome, reason and duration in ms."""
        ...


class InMemoryDeviceSyncMetrics:
    """Bounded in-memory recorder implementing :class:`DeviceSyncMetrics`.

    Sufficient for runtime checks and tests without introducing Prometheus.
    Counters are keyed only by the closed enum labels plus the ``None``
    reason of a success, and the recorder rejects any non-enum label value
    and any duration that is not a non-negative true integer, so a UUID,
    locator, digest or title can never become a label.
    """

    def __init__(self) -> None:
        self._operation_counts: dict[
            tuple[DeviceSyncOperation, DeviceSyncOutcome, DeviceSyncErrorCode | None], int
        ] = {}

    def record_operation(
        self,
        *,
        operation: DeviceSyncOperation,
        outcome: DeviceSyncOutcome,
        reason: DeviceSyncErrorCode | None,
        duration_ms: int,
    ) -> None:
        _validate_metric_label("operation", DeviceSyncOperation, operation)
        _validate_metric_label("outcome", DeviceSyncOutcome, outcome)
        if reason is not None:
            _validate_metric_label("reason", DeviceSyncErrorCode, reason)
        if not isinstance(duration_ms, int) or isinstance(duration_ms, bool) or duration_ms < 0:
            raise ValueError("duration_ms must be a non-negative integer")
        key = (operation, outcome, reason)
        self._operation_counts[key] = self._operation_counts.get(key, 0) + 1

    def operation_count(
        self,
        *,
        operation: DeviceSyncOperation,
        outcome: DeviceSyncOutcome,
        reason: DeviceSyncErrorCode | None = None,
    ) -> int:
        """Return how often one exact label combination was recorded."""

        return self._operation_counts.get((operation, outcome, reason), 0)

    def __repr__(self) -> str:
        return "InMemoryDeviceSyncMetrics(redacted)"


__all__ = [
    "DEVICE_SYNC_METRIC_CONTRACTS",
    "DeviceSyncMetrics",
    "DeviceSyncOperation",
    "DeviceSyncOutcome",
    "InMemoryDeviceSyncMetrics",
]
