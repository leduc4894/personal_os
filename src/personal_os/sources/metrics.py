"""Low-cardinality source metrics contracts and in-memory recorders.

Every metric label is a closed :class:`enum.StrEnum` member: publication
operation/outcome/rejection reason, transaction retry reason, projection
kind/outcome/backlog status, dispatch error code and canonical read outcome.
UUIDs, idempotency keys, digests, titles, SQL text and provider messages are
never accepted as labels and never recorded. :data:`SOURCE_METRIC_CONTRACTS`
pins the exact metric names and their label dimensions from the source version
publication and canonical read specs.

:class:`SourcePublicationMetrics` and :class:`CanonicalReadMetrics` are the
injectable Protocols their paths depend on;
:class:`InMemorySourcePublicationMetrics` and
:class:`InMemoryCanonicalReadMetrics` are the test/standalone implementations
sufficient for runtime checks and tests without introducing Prometheus. A
production sink implements the same Protocol behind the boundary and, like the
in-memory recorders, must reject negative or non-finite duration/age values.
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final, Protocol, runtime_checkable

#: Maximum number of retained per-publication records. The recorder is a bounded
#: ring buffer for tests and standalone runs, never an unbounded audit log.
_MAXIMUM_PUBLICATION_RECORDS: Final[int] = 4096

#: Maximum number of retained per-read records, bounded like the publication ring.
_MAXIMUM_READ_RECORDS: Final[int] = 4096


class PublicationOperation(StrEnum):
    """The closed set of source-publication operations (spec: create/update only)."""

    CREATE = "create"
    UPDATE = "update"


class PublicationMetricOutcome(StrEnum):
    """The closed set of publication outcomes used as metric labels."""

    SUCCEEDED = "succeeded"
    REPLAYED = "replayed"
    REJECTED = "rejected"


class PublicationRejectionReason(StrEnum):
    """The closed business-rejection reason codes (spec section 10.3)."""

    SOURCE_PUBLISH_INPUT_INVALID = "source_publish_input_invalid"
    SOURCE_NOT_FOUND = "source_not_found"
    SOURCE_ALREADY_EXISTS = "source_already_exists"
    SOURCE_STATE_INVALID = "source_state_invalid"
    SOURCE_VERSION_CONFLICT = "source_version_conflict"
    SOURCE_IDEMPOTENCY_MISMATCH = "source_idempotency_mismatch"
    SOURCE_EVENT_IDENTITY_MISMATCH = "source_event_identity_mismatch"
    SOURCE_VERIFIED_RECEIPT_STALE = "source_verified_receipt_stale"
    SOURCE_CONTENT_OBJECT_CONFLICT = "source_content_object_conflict"


class TransactionRetryReason(StrEnum):
    """The closed database retry reasons (spec section 9.3)."""

    DEADLOCK = "deadlock"
    SERIALIZATION_FAILURE = "serialization_failure"
    LOCK_CONTENTION = "lock_contention"


class ProjectionKind(StrEnum):
    """The closed set of projection kinds receiving durable dispatch intents."""

    QDRANT = "qdrant"
    NEO4J = "neo4j"


class ProjectionDispatchOutcome(StrEnum):
    """The closed set of projection dispatch outcomes."""

    DISPATCHED = "dispatched"
    PENDING = "pending"
    TERMINAL = "terminal"


class ProjectionBacklogStatus(StrEnum):
    """The closed set of projection intent backlog statuses."""

    PENDING = "pending"
    LEASED = "leased"


class ProjectionDispatchErrorCode(StrEnum):
    """The closed error-code labels for failed dispatch attempts."""

    PROJECTION_DISPATCH_UNAVAILABLE = "projection_dispatch_unavailable"
    PROJECTION_INTENT_CONTRACT_INVALID = "projection_intent_contract_invalid"


class ReadOutcome(StrEnum):
    """The closed set of canonical current-source read outcomes used as labels."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"


#: The exact required metric names and their label dimensions. IDs, keys and
#: digests are never metric labels, so no dimension names one.
SOURCE_METRIC_CONTRACTS: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {
        "source_version_publish_total": frozenset({"operation", "outcome"}),
        "source_version_publish_duration_seconds": frozenset({"operation", "outcome"}),
        "source_version_replay_total": frozenset({"operation"}),
        "source_version_rejection_total": frozenset({"operation", "reason_code"}),
        "source_version_transaction_retry_total": frozenset({"reason_code"}),
        "projection_intent_backlog": frozenset({"status", "projection_kind"}),
        "projection_intent_oldest_pending_seconds": frozenset({"projection_kind"}),
        "projection_dispatch_total": frozenset({"projection_kind", "outcome", "error_code"}),
        "projection_dispatch_duration_seconds": frozenset({"projection_kind", "outcome"}),
        "projection_lease_reclaimed_total": frozenset({"projection_kind"}),
        "canonical_source_read_total": frozenset({"outcome"}),
        "canonical_source_read_duration_seconds": frozenset({"outcome"}),
    }
)


@dataclass(frozen=True, slots=True)
class PublicationRecord:
    """One recorded publication outcome.

    Carries only low-cardinality enums and a finite non-negative duration;
    never a UUID, key, digest, title or provider value.
    """

    operation: PublicationOperation
    outcome: PublicationMetricOutcome
    duration_seconds: float


@dataclass(frozen=True, slots=True)
class CanonicalReadRecord:
    """One recorded canonical current-source read outcome.

    Carries only the closed outcome enum and a finite non-negative duration;
    never a UUID, digest, title or byte count of the read content.
    """

    outcome: ReadOutcome
    duration_seconds: float


def _validate_finite_non_negative(field_name: str, value: float) -> None:
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{field_name} must be finite and non-negative")


def _validate_label(field_name: str, expected_type: type, value: object) -> None:
    if not isinstance(value, expected_type):
        raise ValueError(f"{field_name} label must be a closed enum member")


@runtime_checkable
class CanonicalReadMetrics(Protocol):
    """The low-cardinality canonical current-source read metrics sink."""

    def record_read(self, *, outcome: ReadOutcome, duration_seconds: float) -> None:
        """Record one completed read outcome and its duration in seconds."""
        ...


@runtime_checkable
class SourcePublicationMetrics(Protocol):
    """The low-cardinality source-publication metrics sink every path uses."""

    def record_publication(
        self,
        *,
        operation: PublicationOperation,
        outcome: PublicationMetricOutcome,
        duration_seconds: float,
    ) -> None:
        """Record one completed publication outcome and its duration in seconds."""
        ...

    def record_replay(
        self,
        *,
        operation: PublicationOperation,
    ) -> None:
        """Increment the replay counter for ``operation``."""
        ...

    def record_rejection(
        self,
        *,
        operation: PublicationOperation,
        reason_code: PublicationRejectionReason,
    ) -> None:
        """Increment the business-rejection counter for one closed reason."""
        ...

    def record_transaction_retry(self, *, reason_code: TransactionRetryReason) -> None:
        """Increment the transaction retry counter for one closed reason."""
        ...

    def record_dispatch(
        self,
        *,
        projection_kind: ProjectionKind,
        outcome: ProjectionDispatchOutcome,
        duration_seconds: float,
        error_code: ProjectionDispatchErrorCode | None = None,
    ) -> None:
        """Record one projection dispatch outcome and its duration in seconds."""
        ...

    def record_lease_reclaimed(self, *, projection_kind: ProjectionKind) -> None:
        """Increment the expired-lease reclaim counter for ``projection_kind``."""
        ...

    def set_projection_backlog(
        self,
        *,
        status: ProjectionBacklogStatus,
        projection_kind: ProjectionKind,
        count: int,
    ) -> None:
        """Set the backlog gauge for one status and projection kind."""
        ...

    def set_oldest_pending_age(
        self, *, projection_kind: ProjectionKind, age_seconds: float
    ) -> None:
        """Set the oldest pending intent age gauge, in seconds."""
        ...


class InMemorySourcePublicationMetrics:
    """Bounded in-memory recorder implementing :class:`SourcePublicationMetrics`.

    Sufficient for runtime checks and tests without introducing Prometheus. It
    keeps at most :data:`_MAXIMUM_PUBLICATION_RECORDS` publication records in a
    ring buffer plus counters/gauges keyed only by the closed enum labels, and
    rejects negative or non-finite duration/age values and any non-enum label
    value so a UUID, key or digest can never become a label.
    """

    def __init__(self) -> None:
        self._publications: deque[PublicationRecord] = deque(maxlen=_MAXIMUM_PUBLICATION_RECORDS)
        self._replays: dict[PublicationOperation, int] = {}
        self._rejections: dict[tuple[PublicationOperation, PublicationRejectionReason], int] = {}
        self._transaction_retries: dict[TransactionRetryReason, int] = {}
        self._dispatches: dict[tuple[ProjectionKind, ProjectionDispatchOutcome], int] = {}
        self._lease_reclaims: dict[ProjectionKind, int] = {}
        self._backlog: dict[tuple[ProjectionBacklogStatus, ProjectionKind], int] = {}
        self._oldest_pending_age: dict[ProjectionKind, float] = {}

    def record_publication(
        self,
        *,
        operation: PublicationOperation,
        outcome: PublicationMetricOutcome,
        duration_seconds: float,
    ) -> None:
        _validate_label("operation", PublicationOperation, operation)
        _validate_label("outcome", PublicationMetricOutcome, outcome)
        _validate_finite_non_negative("duration_seconds", duration_seconds)
        self._publications.append(
            PublicationRecord(
                operation=operation,
                outcome=outcome,
                duration_seconds=duration_seconds,
            )
        )

    def record_replay(self, *, operation: PublicationOperation) -> None:
        _validate_label("operation", PublicationOperation, operation)
        self._replays[operation] = self._replays.get(operation, 0) + 1

    def record_rejection(
        self,
        *,
        operation: PublicationOperation,
        reason_code: PublicationRejectionReason,
    ) -> None:
        _validate_label("operation", PublicationOperation, operation)
        _validate_label("reason_code", PublicationRejectionReason, reason_code)
        key = (operation, reason_code)
        self._rejections[key] = self._rejections.get(key, 0) + 1

    def record_transaction_retry(self, *, reason_code: TransactionRetryReason) -> None:
        _validate_label("reason_code", TransactionRetryReason, reason_code)
        self._transaction_retries[reason_code] = self._transaction_retries.get(reason_code, 0) + 1

    def record_dispatch(
        self,
        *,
        projection_kind: ProjectionKind,
        outcome: ProjectionDispatchOutcome,
        duration_seconds: float,
        error_code: ProjectionDispatchErrorCode | None = None,
    ) -> None:
        _validate_label("projection_kind", ProjectionKind, projection_kind)
        _validate_label("outcome", ProjectionDispatchOutcome, outcome)
        if error_code is not None:
            _validate_label("error_code", ProjectionDispatchErrorCode, error_code)
        _validate_finite_non_negative("duration_seconds", duration_seconds)
        key = (projection_kind, outcome)
        self._dispatches[key] = self._dispatches.get(key, 0) + 1

    def record_lease_reclaimed(self, *, projection_kind: ProjectionKind) -> None:
        _validate_label("projection_kind", ProjectionKind, projection_kind)
        self._lease_reclaims[projection_kind] = self._lease_reclaims.get(projection_kind, 0) + 1

    def set_projection_backlog(
        self,
        *,
        status: ProjectionBacklogStatus,
        projection_kind: ProjectionKind,
        count: int,
    ) -> None:
        _validate_label("status", ProjectionBacklogStatus, status)
        _validate_label("projection_kind", ProjectionKind, projection_kind)
        if count < 0:
            raise ValueError("count must be non-negative")
        self._backlog[(status, projection_kind)] = count

    def set_oldest_pending_age(
        self, *, projection_kind: ProjectionKind, age_seconds: float
    ) -> None:
        _validate_label("projection_kind", ProjectionKind, projection_kind)
        _validate_finite_non_negative("age_seconds", age_seconds)
        self._oldest_pending_age[projection_kind] = age_seconds

    def publication_records(self) -> list[PublicationRecord]:
        """A snapshot list of recorded publication outcomes (oldest first)."""

        return list(self._publications)

    def publication_count(
        self, operation: PublicationOperation, outcome: PublicationMetricOutcome
    ) -> int:
        return sum(
            1
            for record in self._publications
            if record.operation is operation and record.outcome is outcome
        )

    def replay_count(self, operation: PublicationOperation) -> int:
        return self._replays.get(operation, 0)

    def rejection_count(
        self, operation: PublicationOperation, reason_code: PublicationRejectionReason
    ) -> int:
        return self._rejections.get((operation, reason_code), 0)

    def transaction_retry_count(self, reason_code: TransactionRetryReason) -> int:
        return self._transaction_retries.get(reason_code, 0)

    def dispatch_count(
        self, projection_kind: ProjectionKind, outcome: ProjectionDispatchOutcome
    ) -> int:
        return self._dispatches.get((projection_kind, outcome), 0)

    def lease_reclaimed_count(self, projection_kind: ProjectionKind) -> int:
        return self._lease_reclaims.get(projection_kind, 0)

    def backlog_count(
        self, status: ProjectionBacklogStatus, projection_kind: ProjectionKind
    ) -> int:
        return self._backlog.get((status, projection_kind), 0)

    def oldest_pending_age(self, projection_kind: ProjectionKind) -> float:
        return self._oldest_pending_age.get(projection_kind, 0.0)

    def __repr__(self) -> str:
        return "InMemorySourcePublicationMetrics(redacted)"


class InMemoryCanonicalReadMetrics:
    """Bounded in-memory recorder implementing :class:`CanonicalReadMetrics`.

    Sufficient for runtime checks and tests without introducing Prometheus. It
    keeps at most :data:`_MAXIMUM_READ_RECORDS` read records in a ring buffer,
    keyed only by the closed outcome enum, and rejects negative or non-finite
    durations and any non-enum outcome so a UUID, digest or byte count of the
    read content can never become a label.
    """

    def __init__(self) -> None:
        self._reads: deque[CanonicalReadRecord] = deque(maxlen=_MAXIMUM_READ_RECORDS)

    def record_read(self, *, outcome: ReadOutcome, duration_seconds: float) -> None:
        _validate_label("outcome", ReadOutcome, outcome)
        _validate_finite_non_negative("duration_seconds", duration_seconds)
        self._reads.append(CanonicalReadRecord(outcome=outcome, duration_seconds=duration_seconds))

    def read_records(self) -> list[CanonicalReadRecord]:
        """A snapshot list of recorded read outcomes (oldest first)."""

        return list(self._reads)

    def read_count(self, outcome: ReadOutcome) -> int:
        return sum(1 for record in self._reads if record.outcome is outcome)

    def __repr__(self) -> str:
        return "InMemoryCanonicalReadMetrics(redacted)"
