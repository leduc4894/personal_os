"""Low-cardinality object-storage metrics sink contract and in-memory recorder.

The adapter records only bounded, low-sensitivity values: an
:class:`ObjectStorageOperation` token, an :class:`ObjectStorageResult` token, an
optional closed :class:`ErrorCode`, integer byte/duration/attempts counts, a
retry counter, an in-flight gauge and a reserved-bytes gauge. Digests, object
keys, buckets, endpoints, media types, paths, credentials and provider request
ids are never recorded or used as labels.

:class:`ObjectStorageMetrics` is the injectable Protocol every adapter path
depends on; :class:`InMemoryObjectStorageMetrics` is the test/standalone
implementation sufficient for runtime checks and tests without introducing
Prometheus. A production sink implements the same Protocol behind the boundary.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Protocol, runtime_checkable

from personal_os.error_contracts.codes import ErrorCode

#: Maximum number of retained per-operation records. The recorder is a bounded
#: ring buffer for tests and standalone runs, never an unbounded audit log.
_MAXIMUM_OPERATION_RECORDS: Final[int] = 4096


class ObjectStorageOperation(StrEnum):
    """The closed set of object-storage operations recorded by the sink."""

    RESOLVE = "resolve"
    STORE = "store"
    VERIFY = "verify"
    READ = "read"


class ObjectStorageResult(StrEnum):
    """The closed set of per-operation outcomes recorded by the sink."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DEDUPLICATED = "deduplicated"


@dataclass(frozen=True, slots=True)
class OperationRecord:
    """One recorded object-storage operation outcome.

    Carries only low-cardinality enums and integer counts; never a digest, key,
    bucket, endpoint, path or provider value.
    """

    operation: ObjectStorageOperation
    result: ObjectStorageResult
    error_code: ErrorCode | None
    duration_ms: int
    size_bytes: int | None
    attempt_count: int


@runtime_checkable
class ObjectStorageMetrics(Protocol):
    """The low-cardinality object-storage metrics sink every adapter path uses."""

    def record_operation(
        self,
        *,
        operation: ObjectStorageOperation,
        result: ObjectStorageResult,
        error_code: ErrorCode | None = None,
        duration_ms: int = 0,
        size_bytes: int | None = None,
        attempt_count: int = 1,
    ) -> None:
        """Record one completed operation outcome and its bounded counters."""
        ...

    def record_retry(self, *, operation: ObjectStorageOperation) -> None:
        """Increment the retry counter for ``operation``."""
        ...

    def increment_in_flight(self, *, operation: ObjectStorageOperation) -> None:
        """Increment the in-flight operation gauge for ``operation``."""
        ...

    def decrement_in_flight(self, *, operation: ObjectStorageOperation) -> None:
        """Decrement the in-flight operation gauge for ``operation``."""
        ...

    def record_reserved_bytes(self, *, operation: ObjectStorageOperation, size_bytes: int) -> None:
        """Record the bytes currently reserved by ``operation`` spools."""
        ...


class InMemoryObjectStorageMetrics:
    """Bounded in-memory recorder implementing :class:`ObjectStorageMetrics`.

    Sufficient for runtime checks and tests without introducing Prometheus. It
    keeps at most :data:`_MAXIMUM_OPERATION_RECORDS` operation records in a ring
    buffer plus counters/gauges keyed by the low-cardinality operation token.
    """

    def __init__(self) -> None:
        self._operations: deque[OperationRecord] = deque(maxlen=_MAXIMUM_OPERATION_RECORDS)
        self._retries: dict[ObjectStorageOperation, int] = {}
        self._in_flight: dict[ObjectStorageOperation, int] = {}
        self._reserved_bytes: dict[ObjectStorageOperation, int] = {}

    def record_operation(
        self,
        *,
        operation: ObjectStorageOperation,
        result: ObjectStorageResult,
        error_code: ErrorCode | None = None,
        duration_ms: int = 0,
        size_bytes: int | None = None,
        attempt_count: int = 1,
    ) -> None:
        self._operations.append(
            OperationRecord(
                operation=operation,
                result=result,
                error_code=error_code,
                duration_ms=duration_ms,
                size_bytes=size_bytes,
                attempt_count=attempt_count,
            )
        )

    def record_retry(self, *, operation: ObjectStorageOperation) -> None:
        self._retries[operation] = self._retries.get(operation, 0) + 1

    def increment_in_flight(self, *, operation: ObjectStorageOperation) -> None:
        self._in_flight[operation] = self._in_flight.get(operation, 0) + 1

    def decrement_in_flight(self, *, operation: ObjectStorageOperation) -> None:
        current = self._in_flight.get(operation, 0)
        self._in_flight[operation] = max(0, current - 1)

    def record_reserved_bytes(self, *, operation: ObjectStorageOperation, size_bytes: int) -> None:
        self._reserved_bytes[operation] = size_bytes

    @property
    def operations(self) -> list[OperationRecord]:
        """A snapshot list of recorded operation outcomes (oldest first)."""

        return list(self._operations)

    def retry_count(self, operation: ObjectStorageOperation) -> int:
        return self._retries.get(operation, 0)

    def in_flight_count(self, operation: ObjectStorageOperation) -> int:
        return self._in_flight.get(operation, 0)

    def reserved_bytes(self, operation: ObjectStorageOperation) -> int:
        return self._reserved_bytes.get(operation, 0)

    def __repr__(self) -> str:
        return "InMemoryObjectStorageMetrics(redacted)"
