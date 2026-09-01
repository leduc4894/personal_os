"""Framework-neutral source-conflict domain contracts (Child 8 spec 4).

Closed vocabularies and immutable value objects for the
server-authoritative conflict aggregate: the four conflict kinds, the four
aggregate states of the capture/resolve state machine, the three explicit
resolution choices, the strict event idempotency key, the content-versus-
delete candidate shape of the spec 4.1 table, and the safe aggregate read
model capture, read and replay return. The module imports no FastAPI,
SQLAlchemy, R2 or request type; device and workspace identity arrive only
through the issuing domain's credential-derived context, never from capture
data. Locators, digests, tokens, candidate bytes and merged drafts are
private values: they never enter diagnostics, error details or metric
labels, and the locator snapshot exists only to cross the store boundary
into canonical state.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final
from uuid import UUID

from personal_os.sources.actors import reject_nil_uuid
from personal_os.sources.commands import normalize_utc_timestamp

#: Canonical idempotency-key grammar: exactly the canonical lowercase
#: hyphenated UUID text form the plugin mints with ``crypto.randomUUID``.
#: Capture and resolution each carry one fresh key bound to one event
#: identity; an exact replay of that identity returns the stored outcome.
_IDEMPOTENCY_KEY_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


class ConflictKind(StrEnum):
    """Closed vocabulary of conflict kinds (spec 4.1 table).

    ``STALE_CONTENT`` and ``EDIT_REMOTE_DELETE`` retain verified content
    bytes; ``DELETE_REMOTE_EDIT`` carries only a deletion intent; a
    ``LOCATOR_COLLISION`` carries a locator snapshot and retains content
    bytes only when the local bytes changed.
    """

    STALE_CONTENT = "stale_content"
    EDIT_REMOTE_DELETE = "edit_remote_delete"
    DELETE_REMOTE_EDIT = "delete_remote_edit"
    LOCATOR_COLLISION = "locator_collision"


class ConflictStatus(StrEnum):
    """Closed aggregate states of the conflict state machine (spec 4.3)."""

    OPEN = "open"
    RESOLVING = "resolving"
    RESOLVED = "resolved"
    SUPERSEDED = "superseded"


#: The two terminal states: they accept no further mutation and exact replay
#: returns them unchanged, never a duplicate winner.
TERMINAL_CONFLICT_STATUSES: Final[frozenset[ConflictStatus]] = frozenset(
    {ConflictStatus.RESOLVED, ConflictStatus.SUPERSEDED}
)


class ConflictResolutionKind(StrEnum):
    """The only explicit user choices that close a conflict (spec 3.8)."""

    KEEP_REMOTE = "keep_remote"
    KEEP_LOCAL = "keep_local"
    SAVE_MERGED = "save_merged"


#: The resolution choices that publish exactly one immutable source version
#: against the reviewed remote version; ``keep_remote`` publishes none.
VERSION_PUBLISHING_RESOLUTIONS: Final[frozenset[ConflictResolutionKind]] = frozenset(
    {ConflictResolutionKind.KEEP_LOCAL, ConflictResolutionKind.SAVE_MERGED}
)


class ConflictResolutionOutcome(StrEnum):
    """Closed terminal outcomes of one resolution attempt.

    ``RESOLVED`` commits the winner; ``STALE_SUCCESSOR`` records the attempt
    as stale against the reviewed remote, supersedes the conflict and opens
    a successor bound to the newer observed remote. Both are frozen and
    returned unchanged by an exact replay of the resolution event identity.
    """

    RESOLVED = "resolved"
    STALE_SUCCESSOR = "stale_successor"


class ConflictCandidateKind(StrEnum):
    """Whether a conflict retains verified content bytes or a deletion intent."""

    CONTENT = "content"
    DELETE = "delete"


class ConflictEvidenceRole(StrEnum):
    """The three immutable evidence roles the verified-read boundary streams.

    ``CANDIDATE`` exists only while the conflict retains a content candidate;
    a delete conflict has no candidate bytes and the reader fails closed.
    """

    BASE = "base"
    REMOTE = "remote"
    CANDIDATE = "candidate"


@dataclass(frozen=True, slots=True)
class ConflictIdempotencyKey:
    """Stable event idempotency key in canonical UUID text form.

    The grammar accepts only the canonical lowercase hyphenated 8-4-4-4-12
    form; uppercase, braces, ``urn:uuid:`` prefixes, whitespace, wrong
    lengths, the nil UUID and any non-printable form are rejected without
    normalization. The key is opaque, event-scoped and never logged.
    """

    value: str

    def __repr__(self) -> str:
        return f"{type(self).__name__}(value=<redacted>)"

    def __post_init__(self) -> None:
        if _IDEMPOTENCY_KEY_PATTERN.fullmatch(self.value) is None:
            raise ValueError("idempotency key must be a canonical lowercase hyphenated UUID")
        if self.value == "00000000-0000-0000-0000-000000000000":
            raise ValueError("idempotency key must be a non-nil UUID")


@dataclass(frozen=True, slots=True)
class ConflictCandidate:
    """One conflict's retained local-side evidence shape (spec 4.1 table).

    A content candidate is the immutable verified object the existing
    single-part or multipart flow already proved by digest, size and media
    type — never raw bytes and never a source version. A delete candidate
    is a no-byte deletion intent; it must not carry a content object, so a
    conflict can never be partly a deletion and partly a content candidate.
    """

    candidate_kind: ConflictCandidateKind
    verified_candidate_object_id: UUID | None

    def __post_init__(self) -> None:
        if self.candidate_kind is ConflictCandidateKind.CONTENT:
            if self.verified_candidate_object_id is None:
                raise ValueError("a content candidate requires a verified_candidate_object_id")
            reject_nil_uuid("verified_candidate_object_id", self.verified_candidate_object_id)
            return
        if self.verified_candidate_object_id is not None:
            raise ValueError("a delete candidate must not carry a verified_candidate_object_id")

    @classmethod
    def content(cls, verified_candidate_object_id: UUID | None) -> ConflictCandidate:
        """Build the content variant; the verified object reference is required."""
        return cls(
            candidate_kind=ConflictCandidateKind.CONTENT,
            verified_candidate_object_id=verified_candidate_object_id,
        )

    @classmethod
    def delete(cls, verified_candidate_object_id: UUID | None = None) -> ConflictCandidate:
        """Build the no-byte deletion variant; a content object is refused."""
        return cls(
            candidate_kind=ConflictCandidateKind.DELETE,
            verified_candidate_object_id=verified_candidate_object_id,
        )


def validate_candidate_for_kind(conflict_kind: ConflictKind, candidate: ConflictCandidate) -> None:
    """Enforce the candidate requirement of the spec 4.1 table.

    ``stale_content`` and ``edit_remote_delete`` conflicts require a content
    candidate; a ``delete_remote_edit`` conflict requires a delete candidate;
    a ``locator_collision`` accepts either shape because its content object
    is required only when the local bytes changed.
    """

    if (
        conflict_kind in {ConflictKind.STALE_CONTENT, ConflictKind.EDIT_REMOTE_DELETE}
        and candidate.candidate_kind is not ConflictCandidateKind.CONTENT
    ):
        raise ValueError(f"a {conflict_kind.value} conflict requires a content candidate")
    if (
        conflict_kind is ConflictKind.DELETE_REMOTE_EDIT
        and candidate.candidate_kind is not ConflictCandidateKind.DELETE
    ):
        raise ValueError(f"a {conflict_kind.value} conflict requires a delete candidate")


@dataclass(frozen=True, slots=True)
class SourceConflict:
    """The frozen aggregate read model capture, read and replay return.

    Carries only opaque identifiers, closed labels and normalized UTC
    timestamps: the locator snapshot lives in canonical state and the raw
    locator, candidate bytes, digests and object keys never cross into this
    value. Status shapes are exact: an open conflict carries no resolution
    evidence; a resolving conflict carries the attempt identity but no
    outcome; a resolved conflict binds the winner and — only under
    ``keep_local``/``save_merged`` — exactly one resulting version; a
    superseded conflict binds the stale attempt and its successor and never
    a resulting version. Evidence is immutable: no field describes later
    remote state.
    """

    conflict_id: UUID
    workspace_id: UUID
    source_id: UUID | None
    conflict_kind: ConflictKind
    status: ConflictStatus
    originating_event_id: UUID
    originating_device_id: UUID
    base_version_id: UUID | None
    observed_remote_version_id: UUID | None
    candidate: ConflictCandidate
    captured_at: datetime
    resolution_kind: ConflictResolutionKind | None
    resolution_event_id: UUID | None
    resulting_version_id: UUID | None
    successor_conflict_id: UUID | None
    closed_at: datetime | None

    def __post_init__(self) -> None:
        if not isinstance(self.conflict_kind, ConflictKind):
            raise ValueError("conflict_kind must be a closed ConflictKind")
        if not isinstance(self.status, ConflictStatus):
            raise ValueError("status must be a closed ConflictStatus")
        if not isinstance(self.candidate, ConflictCandidate):
            raise ValueError("candidate must be a ConflictCandidate value")
        reject_nil_uuid("conflict_id", self.conflict_id)
        reject_nil_uuid("workspace_id", self.workspace_id)
        reject_nil_uuid("originating_event_id", self.originating_event_id)
        reject_nil_uuid("originating_device_id", self.originating_device_id)
        if self.base_version_id is not None:
            reject_nil_uuid("base_version_id", self.base_version_id)
        if self.observed_remote_version_id is not None:
            reject_nil_uuid("observed_remote_version_id", self.observed_remote_version_id)
        if self.source_id is not None:
            reject_nil_uuid("source_id", self.source_id)
        elif self.conflict_kind is not ConflictKind.LOCATOR_COLLISION:
            raise ValueError("source_id may be null only for a locator_collision conflict")
        if self.resolution_event_id is not None:
            reject_nil_uuid("resolution_event_id", self.resolution_event_id)
        if self.resulting_version_id is not None:
            reject_nil_uuid("resulting_version_id", self.resulting_version_id)
        if self.successor_conflict_id is not None:
            reject_nil_uuid("successor_conflict_id", self.successor_conflict_id)
        validate_candidate_for_kind(self.conflict_kind, self.candidate)
        object.__setattr__(
            self, "captured_at", normalize_utc_timestamp("captured_at", self.captured_at)
        )
        object.__setattr__(self, "closed_at", normalize_utc_timestamp("closed_at", self.closed_at))
        self._validate_status_shape()

    def _validate_status_shape(self) -> None:
        if self.status is ConflictStatus.OPEN:
            if (
                self.resolution_kind is not None
                or self.resolution_event_id is not None
                or self.resulting_version_id is not None
                or self.successor_conflict_id is not None
                or self.closed_at is not None
            ):
                raise ValueError("an open conflict carries no resolution evidence")
            return
        if self.status is ConflictStatus.RESOLVING:
            if self.resolution_kind is None or self.resolution_event_id is None:
                raise ValueError(
                    "a resolving conflict requires resolution_kind and resolution_event_id"
                )
            if (
                self.resulting_version_id is not None
                or self.successor_conflict_id is not None
                or self.closed_at is not None
            ):
                raise ValueError(
                    "a resolving conflict carries no resulting version, successor or closed_at"
                )
            return
        if self.status is ConflictStatus.RESOLVED:
            if (
                self.resolution_kind is None
                or self.resolution_event_id is None
                or self.closed_at is None
            ):
                raise ValueError(
                    "a resolved conflict requires resolution_kind, resolution_event_id "
                    "and closed_at"
                )
            if self.successor_conflict_id is not None:
                raise ValueError("a resolved conflict carries no successor")
            if self.resolution_kind in VERSION_PUBLISHING_RESOLUTIONS:
                if self.resulting_version_id is None:
                    raise ValueError(
                        f"a {self.resolution_kind.value} resolution requires a resulting_version_id"
                    )
            elif self.resulting_version_id is not None:
                raise ValueError("a keep_remote resolution creates no source version")
            return
        if (
            self.resolution_event_id is None
            or self.successor_conflict_id is None
            or self.closed_at is None
        ):
            raise ValueError(
                "a superseded conflict requires resolution_event_id, successor_conflict_id "
                "and closed_at"
            )
        if self.resulting_version_id is not None:
            raise ValueError("a superseded conflict carries no resulting_version_id")
        if self.successor_conflict_id == self.conflict_id:
            raise ValueError("successor_conflict_id must differ from conflict_id")


__all__ = [
    "TERMINAL_CONFLICT_STATUSES",
    "VERSION_PUBLISHING_RESOLUTIONS",
    "ConflictCandidate",
    "ConflictCandidateKind",
    "ConflictEvidenceRole",
    "ConflictIdempotencyKey",
    "ConflictKind",
    "ConflictResolutionKind",
    "ConflictResolutionOutcome",
    "ConflictStatus",
    "SourceConflict",
    "validate_candidate_for_kind",
]
