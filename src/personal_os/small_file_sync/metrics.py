"""Low-cardinality small-file sync metrics contracts and in-memory recorder.

Every metric label is a closed :class:`enum.StrEnum` member: preflight
outcome, upload outcome, rejection reason code and operation. UUIDs,
idempotency keys, locators, digests, operation tokens, byte counts of the
streamed content and provider messages are never accepted as labels and
never recorded. :data:`SMALL_FILE_METRIC_CONTRACTS` pins the exact metric
names and their label dimensions.

:class:`SmallFileSyncMetrics` is the injectable Protocol the preflight and
receive paths depend on; :class:`InMemorySmallFileSyncMetrics` is the
bounded test/standalone implementation sufficient for runtime checks and
tests without introducing Prometheus. A production sink implements the same
Protocol behind the boundary and, like the in-memory recorder, must reject
negative or non-finite durations and any non-enum label value.
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final, Protocol, runtime_checkable

from personal_os.small_file_sync.contracts import SmallFileOperation, SmallFilePreflightOutcome

#: Maximum number of retained per-path records. The recorder is a bounded
#: ring buffer for tests and standalone runs, never an unbounded audit log.
_MAXIMUM_SYNC_RECORDS: Final[int] = 4096


class SmallFileMetricOutcome(StrEnum):
    """The closed set of content-stream outcomes used as metric labels."""

    COMMITTED = "committed"
    INTEGRITY_FAILED = "integrity_failed"
    REJECTED = "rejected"


class SmallFileRejectionReason(StrEnum):
    """The closed rejection reason codes mirroring the domain error registry."""

    SMALL_FILE_PREFLIGHT_INVALID = "small_file_preflight_invalid"
    SMALL_FILE_OPERATION_NOT_FOUND = "small_file_operation_not_found"
    SMALL_FILE_OPERATION_EXPIRED = "small_file_operation_expired"
    SMALL_FILE_OPERATION_IDENTITY_MISMATCH = "small_file_operation_identity_mismatch"
    SMALL_FILE_SIZE_LIMIT_EXCEEDED = "small_file_size_limit_exceeded"
    SMALL_FILE_CONTENT_INTEGRITY_FAILED = "small_file_content_integrity_failed"
    SMALL_FILE_UPLOAD_STATE_INVALID = "small_file_upload_state_invalid"


#: The exact required metric names and their label dimensions. IDs, keys,
#: locators, digests and tokens are never metric labels, so no dimension
#: names one.
SMALL_FILE_METRIC_CONTRACTS: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {
        "small_file_preflight_total": frozenset({"operation", "outcome"}),
        "small_file_preflight_duration_seconds": frozenset({"operation", "outcome"}),
        "small_file_upload_total": frozenset({"operation", "outcome"}),
        "small_file_upload_duration_seconds": frozenset({"operation", "outcome"}),
        "small_file_replay_total": frozenset({"operation"}),
        "small_file_rejection_total": frozenset({"operation", "reason_code"}),
    }
)


@dataclass(frozen=True, slots=True)
class SmallFileSyncRecord:
    """One recorded preflight or upload outcome.

    Carries only the operation, the low-cardinality outcome enum and a finite
    non-negative duration; never a UUID, key, locator, digest, token or byte
    count of the streamed content.
    """

    operation: SmallFileOperation
    outcome: SmallFilePreflightOutcome | SmallFileMetricOutcome
    duration_seconds: float


def _validate_finite_non_negative(field_name: str, value: float) -> None:
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{field_name} must be finite and non-negative")


def _validate_label(field_name: str, expected_type: type, value: object) -> None:
    if not isinstance(value, expected_type):
        raise ValueError(f"{field_name} label must be a closed enum member")


@runtime_checkable
class SmallFileSyncMetrics(Protocol):
    """The low-cardinality small-file sync metrics sink every path uses."""

    def record_preflight(
        self,
        *,
        operation: SmallFileOperation,
        outcome: SmallFilePreflightOutcome,
        duration_seconds: float,
    ) -> None:
        """Record one completed preflight outcome and its duration in seconds."""
        ...

    def record_upload(
        self,
        *,
        operation: SmallFileOperation,
        outcome: SmallFileMetricOutcome,
        duration_seconds: float,
    ) -> None:
        """Record one completed content-stream outcome and its duration."""
        ...

    def record_replay(self, *, operation: SmallFileOperation) -> None:
        """Increment the exact-replay counter for ``operation``."""
        ...

    def record_rejection(
        self,
        *,
        operation: SmallFileOperation,
        reason_code: SmallFileRejectionReason,
    ) -> None:
        """Increment the rejection counter for one closed reason."""
        ...


class InMemorySmallFileSyncMetrics:
    """Bounded in-memory recorder implementing :class:`SmallFileSyncMetrics`.

    Sufficient for runtime checks and tests without introducing Prometheus.
    It keeps at most :data:`_MAXIMUM_SYNC_RECORDS` path records in a ring
    buffer plus counters keyed only by the closed enum labels, and rejects
    negative or non-finite durations and any non-enum label value so a UUID,
    key, locator, digest or token can never become a label.
    """

    def __init__(self) -> None:
        self._records: deque[SmallFileSyncRecord] = deque(maxlen=_MAXIMUM_SYNC_RECORDS)
        self._replays: dict[SmallFileOperation, int] = {}
        self._rejections: dict[tuple[SmallFileOperation, SmallFileRejectionReason], int] = {}

    def record_preflight(
        self,
        *,
        operation: SmallFileOperation,
        outcome: SmallFilePreflightOutcome,
        duration_seconds: float,
    ) -> None:
        _validate_label("operation", SmallFileOperation, operation)
        _validate_label("outcome", SmallFilePreflightOutcome, outcome)
        _validate_finite_non_negative("duration_seconds", duration_seconds)
        self._records.append(
            SmallFileSyncRecord(
                operation=operation,
                outcome=outcome,
                duration_seconds=duration_seconds,
            )
        )

    def record_upload(
        self,
        *,
        operation: SmallFileOperation,
        outcome: SmallFileMetricOutcome,
        duration_seconds: float,
    ) -> None:
        _validate_label("operation", SmallFileOperation, operation)
        _validate_label("outcome", SmallFileMetricOutcome, outcome)
        _validate_finite_non_negative("duration_seconds", duration_seconds)
        self._records.append(
            SmallFileSyncRecord(
                operation=operation,
                outcome=outcome,
                duration_seconds=duration_seconds,
            )
        )

    def record_replay(self, *, operation: SmallFileOperation) -> None:
        _validate_label("operation", SmallFileOperation, operation)
        self._replays[operation] = self._replays.get(operation, 0) + 1

    def record_rejection(
        self,
        *,
        operation: SmallFileOperation,
        reason_code: SmallFileRejectionReason,
    ) -> None:
        _validate_label("operation", SmallFileOperation, operation)
        _validate_label("reason_code", SmallFileRejectionReason, reason_code)
        key = (operation, reason_code)
        self._rejections[key] = self._rejections.get(key, 0) + 1

    def preflight_count(
        self, operation: SmallFileOperation, outcome: SmallFilePreflightOutcome
    ) -> int:
        return sum(
            1
            for record in self._records
            if record.operation is operation and record.outcome is outcome
        )

    def upload_count(self, operation: SmallFileOperation, outcome: SmallFileMetricOutcome) -> int:
        return sum(
            1
            for record in self._records
            if record.operation is operation and record.outcome is outcome
        )

    def replay_count(self, operation: SmallFileOperation) -> int:
        return self._replays.get(operation, 0)

    def rejection_count(
        self, operation: SmallFileOperation, reason_code: SmallFileRejectionReason
    ) -> int:
        return self._rejections.get((operation, reason_code), 0)

    def __repr__(self) -> str:
        return "InMemorySmallFileSyncMetrics(redacted)"
