"""Immutable source-publication commands and their closed value objects.

Commands are the transport-neutral write intent for publishing a source
version. Validation runs before any I/O: non-nil canonical UUIDs, a closed
command and actor vocabulary, an exact-trimmed create title of 1-500 Unicode
code points without control characters, and an aware client timestamp
normalized to UTC. An update can never mutate source type, title or locator;
those fields do not exist on :class:`UpdateSourceVersion`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final
from unicodedata import category
from uuid import UUID

from personal_os.object_storage import ExpectedObject
from personal_os.sources.actors import SourceActor, reject_nil_uuid

_IDEMPOTENCY_KEY_MIN_LENGTH: Final[int] = 1
_IDEMPOTENCY_KEY_MAX_LENGTH: Final[int] = 200
# Printable ASCII ``!`` through ``~``: no space, DEL or any control character.
_IDEMPOTENCY_KEY_CHARS: Final[frozenset[str]] = frozenset(
    chr(code_point) for code_point in range(0x21, 0x7F)
)
_TITLE_MAX_CODE_POINTS: Final[int] = 500


class SourceType(StrEnum):
    """Closed vocabulary of source types a create command may declare."""

    MARKDOWN = "markdown"
    TEXT = "text"
    PDF = "pdf"
    IMAGE = "image"
    AUDIO = "audio"
    WEB = "web"
    YOUTUBE = "youtube"


@dataclass(frozen=True, slots=True)
class IdempotencyKey:
    """Opaque workspace-scoped idempotency key of 1-200 printable ASCII characters.

    The grammar is printable non-whitespace ASCII ``!`` through ``~`` with no
    normalization. The key is opaque, workspace-scoped and never logged.
    """

    value: str

    def __post_init__(self) -> None:
        length = len(self.value)
        if length < _IDEMPOTENCY_KEY_MIN_LENGTH or length > _IDEMPOTENCY_KEY_MAX_LENGTH:
            raise ValueError(
                f"idempotency key must contain {_IDEMPOTENCY_KEY_MIN_LENGTH} to "
                f"{_IDEMPOTENCY_KEY_MAX_LENGTH} characters"
            )
        if any(char not in _IDEMPOTENCY_KEY_CHARS for char in self.value):
            raise ValueError(
                "idempotency key must be printable non-whitespace ASCII without normalization"
            )


@dataclass(frozen=True, slots=True)
class SourceTitle:
    """Exact-trimmed create title of 1-500 Unicode code points.

    The value is neither Unicode-normalized nor case-folded; the stored title
    and the fingerprint use the same exact code-point sequence after the trim
    check. Control characters are rejected.
    """

    value: str

    def __post_init__(self) -> None:
        if not self.value or self.value != self.value.strip():
            raise ValueError("title must be non-empty and exactly trimmed")
        if len(self.value) > _TITLE_MAX_CODE_POINTS:
            raise ValueError(f"title must be at most {_TITLE_MAX_CODE_POINTS} code points")
        if any(_is_control_character(char) for char in self.value):
            raise ValueError("title must not contain control characters")


def _is_control_character(char: str) -> bool:
    return category(char) == "Cc"


def normalize_utc_timestamp(field_name: str, timestamp: datetime | None) -> datetime | None:
    """Reject a naïve ``timestamp`` and normalize an aware one to UTC."""
    if timestamp is None:
        return None
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return timestamp.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class CreateSourceVersion:
    """Immutable command creating the first version of a source.

    ``source_id`` is backend-issued before the transaction and retained for
    retry. A create carries the source type and title; an update can never
    mutate them.
    """

    workspace_id: UUID
    source_id: UUID
    event_id: UUID
    idempotency_key: IdempotencyKey
    source_type: SourceType
    title: SourceTitle
    actor: SourceActor
    expected_object: ExpectedObject
    client_timestamp: datetime | None

    def __post_init__(self) -> None:
        reject_nil_uuid("workspace_id", self.workspace_id)
        reject_nil_uuid("source_id", self.source_id)
        reject_nil_uuid("event_id", self.event_id)
        object.__setattr__(
            self,
            "client_timestamp",
            normalize_utc_timestamp("client_timestamp", self.client_timestamp),
        )


@dataclass(frozen=True, slots=True)
class UpdateSourceVersion:
    """Immutable command publishing a new version over ``base_version_id``.

    Deliberately carries no source type or title field: an update cannot
    mutate source type, title or locator.
    """

    workspace_id: UUID
    source_id: UUID
    event_id: UUID
    idempotency_key: IdempotencyKey
    base_version_id: UUID
    actor: SourceActor
    expected_object: ExpectedObject
    client_timestamp: datetime | None

    def __post_init__(self) -> None:
        reject_nil_uuid("workspace_id", self.workspace_id)
        reject_nil_uuid("source_id", self.source_id)
        reject_nil_uuid("event_id", self.event_id)
        reject_nil_uuid("base_version_id", self.base_version_id)
        object.__setattr__(
            self,
            "client_timestamp",
            normalize_utc_timestamp("client_timestamp", self.client_timestamp),
        )
