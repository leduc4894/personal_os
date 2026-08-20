"""Closed, framework-neutral source lifecycle command and result values."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from personal_os.source_locators import NormalizedLocator
from personal_os.sources.actors import reject_nil_uuid
from personal_os.sources.commands import normalize_utc_timestamp


class LifecycleOperation(StrEnum):
    """The only source lifecycle transitions this child permits."""

    RENAME = "rename"
    MOVE = "move"
    DELETE = "delete"
    RESTORE = "restore"


class LifecycleState(StrEnum):
    """Resulting canonical source lifecycle state."""

    ACTIVE = "active"
    DELETED = "deleted"


@dataclass(frozen=True, slots=True)
class SourceLifecycleCommand:
    """Validated source locator mutation with stable UUIDv7 event identity."""

    source_id: UUID
    event_id: UUID
    idempotency_key: str
    operation: LifecycleOperation
    expected_version_id: UUID
    expected_locator: NormalizedLocator | None
    target_locator: NormalizedLocator | None
    tombstone_id: UUID | None
    policy_revision: int
    client_timestamp: datetime | None

    def __post_init__(self) -> None:
        reject_nil_uuid("source_id", self.source_id)
        reject_nil_uuid("event_id", self.event_id)
        reject_nil_uuid("expected_version_id", self.expected_version_id)
        if self.event_id.version != 7:
            raise ValueError("event_id must be a UUIDv7")
        if not self.idempotency_key:
            raise ValueError("idempotency_key must be non-empty")
        if not isinstance(self.operation, LifecycleOperation):
            raise ValueError("operation must be a closed LifecycleOperation")
        if self.policy_revision < 1:
            raise ValueError("policy_revision must be a positive integer")
        if self.tombstone_id is not None:
            reject_nil_uuid("tombstone_id", self.tombstone_id)
        self._validate_operation_shape()
        object.__setattr__(
            self,
            "client_timestamp",
            normalize_utc_timestamp("client_timestamp", self.client_timestamp),
        )

    def _validate_operation_shape(self) -> None:
        if self.operation in {LifecycleOperation.RENAME, LifecycleOperation.MOVE}:
            if self.expected_locator is None:
                raise ValueError(f"{self.operation.value} requires expected_locator")
            if self.target_locator is None:
                raise ValueError(f"{self.operation.value} requires target_locator")
            if self.tombstone_id is not None:
                raise ValueError(f"{self.operation.value} must not carry tombstone_id")
        elif self.operation is LifecycleOperation.DELETE:
            if self.expected_locator is None:
                raise ValueError("delete requires expected_locator")
            if self.target_locator is not None:
                raise ValueError("delete must not carry target_locator")
            if self.tombstone_id is not None:
                raise ValueError("delete must not carry tombstone_id")
        else:
            if self.expected_locator is not None:
                raise ValueError("restore must not carry expected_locator")
            if self.target_locator is None:
                raise ValueError("restore requires target_locator")
            if self.tombstone_id is None:
                raise ValueError("restore requires tombstone_id")
        if (
            self.expected_locator is not None
            and self.target_locator is not None
            and self.expected_locator == self.target_locator
        ):
            raise ValueError("expected_locator and target_locator must differ")


@dataclass(frozen=True, slots=True)
class SourceLifecycleCommitResult:
    """Frozen result retained and returned unchanged by exact replay."""

    source_id: UUID
    source_version_id: UUID
    event_id: UUID
    event_sequence: int
    state: LifecycleState
    tombstone_id: UUID | None
    resulting_locator: NormalizedLocator | None
    committed_at: datetime

    def __post_init__(self) -> None:
        reject_nil_uuid("source_id", self.source_id)
        reject_nil_uuid("source_version_id", self.source_version_id)
        reject_nil_uuid("event_id", self.event_id)
        if self.event_sequence < 1:
            raise ValueError("event_sequence must be a positive integer")
        if not isinstance(self.state, LifecycleState):
            raise ValueError("state must be a closed LifecycleState")
        if self.state is LifecycleState.ACTIVE:
            if self.resulting_locator is None:
                raise ValueError("active lifecycle result requires resulting_locator")
            if self.tombstone_id is not None:
                raise ValueError("active lifecycle result must not carry tombstone_id")
        else:
            if self.resulting_locator is not None:
                raise ValueError("deleted lifecycle result must not carry resulting_locator")
            if self.tombstone_id is None:
                raise ValueError("deleted lifecycle result requires tombstone_id")
            reject_nil_uuid("tombstone_id", self.tombstone_id)
        object.__setattr__(
            self, "committed_at", normalize_utc_timestamp("committed_at", self.committed_at)
        )
