"""Closed, framework-neutral source-conflict commands and resolution result.

The capture command is the only shape a syncing domain issues to retain
conflict evidence; it never carries raw bytes, digests or paths — only the
verified object reference, opaque identifiers, closed labels and the
locator snapshot that crosses into canonical state. The resolve command
binds a new event identity and the reviewed remote version, and carries at
most the verified reference of an already-uploaded merged result: raw
content never enters a command. The frozen result is retained and returned
unchanged by an exact replay of the resolution event identity.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from personal_os.source_conflicts.contracts import (
    VERSION_PUBLISHING_RESOLUTIONS,
    ConflictCandidate,
    ConflictIdempotencyKey,
    ConflictKind,
    ConflictResolutionKind,
    ConflictResolutionOutcome,
    SourceConflict,
    validate_candidate_for_kind,
)
from personal_os.source_locators import NormalizedLocator
from personal_os.sources.actors import reject_nil_uuid
from personal_os.sources.commands import normalize_utc_timestamp


@dataclass(frozen=True, slots=True)
class CaptureConflictCommand:
    """One immutable conflict-capture intent issued by a syncing domain.

    Binds the immutable evidence of spec 4.2: the credential-derived
    workspace and originating device, the originating sync event identity
    with its idempotency key, the base and observed remote versions at
    capture time, the candidate shape of the spec 4.1 table and the locator
    snapshot a locator collision requires. A ``source_id`` may be null only
    while a locator collision has not identified a canonical source; every
    other kind binds one source. Capture changes no source current pointer.
    """

    workspace_id: UUID
    source_id: UUID | None
    conflict_kind: ConflictKind
    originating_event_id: UUID
    originating_device_id: UUID
    idempotency_key: ConflictIdempotencyKey
    base_version_id: UUID | None
    observed_remote_version_id: UUID | None
    candidate: ConflictCandidate
    normalized_locator: NormalizedLocator | None

    def __post_init__(self) -> None:
        if not isinstance(self.conflict_kind, ConflictKind):
            raise ValueError("conflict_kind must be a closed ConflictKind")
        if not isinstance(self.candidate, ConflictCandidate):
            raise ValueError("candidate must be a ConflictCandidate value")
        reject_nil_uuid("workspace_id", self.workspace_id)
        reject_nil_uuid("originating_event_id", self.originating_event_id)
        reject_nil_uuid("originating_device_id", self.originating_device_id)
        if self.base_version_id is not None:
            reject_nil_uuid("base_version_id", self.base_version_id)
        if self.observed_remote_version_id is not None:
            reject_nil_uuid("observed_remote_version_id", self.observed_remote_version_id)
        if self.source_id is not None:
            reject_nil_uuid("source_id", self.source_id)
        validate_candidate_for_kind(self.conflict_kind, self.candidate)
        if self.conflict_kind is ConflictKind.LOCATOR_COLLISION:
            if self.source_id is None and self.normalized_locator is None:
                raise ValueError(
                    "a locator_collision capture without a source_id requires a "
                    "normalized_locator snapshot"
                )
            if self.normalized_locator is None:
                raise ValueError(
                    "a locator_collision capture requires a normalized_locator snapshot"
                )
        elif self.source_id is None:
            raise ValueError("source_id may be null only for a locator_collision capture")


@dataclass(frozen=True, slots=True)
class ResolveConflictCommand:
    """One immutable explicit resolution attempt with a new event identity.

    ``resolution_event_id`` with ``idempotency_key`` is fresh: a resolution
    is a new accepted operation, never a replay of the capture event. The
    command carries only verified references — ``keep_remote`` and
    ``keep_local`` act on the retained evidence and must not substitute a
    new candidate object, while ``save_merged`` requires the verified
    object of the already-uploaded merged result. The reviewed remote
    version is rechecked against current canonical state inside the
    resolution transaction.
    """

    conflict_id: UUID
    reviewed_remote_version_id: UUID | None
    resolution_kind: ConflictResolutionKind
    resolution_event_id: UUID
    idempotency_key: ConflictIdempotencyKey
    verified_candidate_object_id: UUID | None

    def __post_init__(self) -> None:
        if not isinstance(self.resolution_kind, ConflictResolutionKind):
            raise ValueError("resolution_kind must be a closed ConflictResolutionKind")
        reject_nil_uuid("conflict_id", self.conflict_id)
        reject_nil_uuid("resolution_event_id", self.resolution_event_id)
        if self.reviewed_remote_version_id is not None:
            reject_nil_uuid("reviewed_remote_version_id", self.reviewed_remote_version_id)
        if self.verified_candidate_object_id is not None:
            reject_nil_uuid("verified_candidate_object_id", self.verified_candidate_object_id)
        if self.resolution_kind is ConflictResolutionKind.SAVE_MERGED:
            if self.verified_candidate_object_id is None:
                raise ValueError("a save_merged resolution requires a verified_candidate_object_id")
            return
        if self.verified_candidate_object_id is not None:
            raise ValueError(
                f"a {self.resolution_kind.value} resolution must not carry a "
                "verified_candidate_object_id"
            )


@dataclass(frozen=True, slots=True)
class ConflictResolutionResult:
    """The frozen resolution outcome retained for exact replay (spec 5.2).

    ``RESOLVED`` commits the winner — with exactly one resulting version
    only under ``keep_local``/``save_merged`` — and carries no successor.
    ``STALE_SUCCESSOR`` records the stale attempt, carries no resulting
    version and binds the open successor conflict already created against
    the newer observed remote. A same-identity replay receives this value
    unchanged with no duplicate version or successor.
    """

    kind: ConflictResolutionOutcome
    conflict_id: UUID
    resolution_event_id: UUID
    resolution_kind: ConflictResolutionKind
    resulting_version_id: UUID | None
    successor: SourceConflict | None
    completed_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ConflictResolutionOutcome):
            raise ValueError("kind must be a closed ConflictResolutionOutcome")
        if not isinstance(self.resolution_kind, ConflictResolutionKind):
            raise ValueError("resolution_kind must be a closed ConflictResolutionKind")
        reject_nil_uuid("conflict_id", self.conflict_id)
        reject_nil_uuid("resolution_event_id", self.resolution_event_id)
        if self.resulting_version_id is not None:
            reject_nil_uuid("resulting_version_id", self.resulting_version_id)
        object.__setattr__(
            self,
            "completed_at",
            normalize_utc_timestamp("completed_at", self.completed_at),
        )
        if self.kind is ConflictResolutionOutcome.RESOLVED:
            if self.successor is not None:
                raise ValueError("a resolved result carries no successor")
            if self.resolution_kind in VERSION_PUBLISHING_RESOLUTIONS:
                if self.resulting_version_id is None:
                    raise ValueError(
                        f"a {self.resolution_kind.value} resolution requires a resulting_version_id"
                    )
            elif self.resulting_version_id is not None:
                raise ValueError("a keep_remote resolution creates no source version")
            return
        if self.successor is None:
            raise ValueError("a stale_successor result requires a successor conflict")
        if self.resulting_version_id is not None:
            raise ValueError("a stale_successor result carries no resulting_version_id")


__all__ = [
    "CaptureConflictCommand",
    "ConflictResolutionResult",
    "ResolveConflictCommand",
]
