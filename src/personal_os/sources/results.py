"""Immutable canonical result of a committed source-version publication.

The result carries exactly the canonical replay fields; replay status itself
is diagnostic-only. The value reuses :class:`ContentDigest` from
``personal_os.object_storage`` as the sole content identity.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from personal_os.object_storage import ContentDigest
from personal_os.sources.actors import reject_nil_uuid
from personal_os.sources.commands import normalize_utc_timestamp


class PublicationOutcome(StrEnum):
    """Closed vocabulary of publication outcomes."""

    PUBLISHED = "published"
    NO_CHANGE = "no_change"


@dataclass(frozen=True, slots=True)
class SourceVersionPublicationResult:
    """Immutable replay-canonical result of a source-version publication.

    ``content_version`` and ``event_sequence`` are positive integers and
    ``committed_at`` is the sync event's database transaction time, normalized
    to aware UTC.
    """

    source_id: UUID
    source_version_id: UUID
    content_version: int
    event_id: UUID
    event_sequence: int
    content_digest: ContentDigest
    outcome: PublicationOutcome
    committed_at: datetime

    def __post_init__(self) -> None:
        reject_nil_uuid("source_id", self.source_id)
        reject_nil_uuid("source_version_id", self.source_version_id)
        reject_nil_uuid("event_id", self.event_id)
        if self.content_version < 1:
            raise ValueError("content_version must be a positive integer")
        if self.event_sequence < 1:
            raise ValueError("event_sequence must be a positive integer")
        object.__setattr__(
            self,
            "committed_at",
            normalize_utc_timestamp("committed_at", self.committed_at),
        )
