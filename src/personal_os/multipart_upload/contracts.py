"""Framework-neutral multipart upload domain contracts (Child 7 spec 4).

Immutable value objects and closed vocabularies for the resumable multipart
staging transfer of one frozen outbound journal event whose content is larger
than the server-owned single-part limit: the opaque public session ID, the
exact part geometry (an 8 MiB ordinary part, a positive final part of at most
8 MiB, at most 13 parts for 16 MiB < size <= 100 MiB), the server session
state machine of spec 4.2 with its closed transition table, the session-bound
upload plan, the signed part-URL envelope, the safe status and completion
results, and the 24-hour session expiry derivation. The module imports no
FastAPI, SQLAlchemy, R2 SDK or request type; the session ID and the presigned
URL are private values that never render outside a redacted ``repr``, and no
provider identity (upload ID, ETag, staging key) appears here at all — those
stay inside the ports module, private to the store and provider adapters.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from personal_os.small_file_sync.contracts import (
    MAX_SINGLE_PART_FILE_SIZE_BYTES,
    MAX_UPLOAD_FILE_SIZE_BYTES,
    SmallFileTerminalResult,
)
from personal_os.sources.commands import normalize_utc_timestamp

#: Ordinary multipart part size: exactly 8 MiB (spec 4). Every part except
#: the final one carries exactly this many bytes; it is never negotiated.
MULTIPART_PART_SIZE_BYTES: Final[int] = 8 * 1024 * 1024

#: Maximum number of parts one multipart session's geometry may declare
#: (spec 4): the 100 MiB product maximum over the 8 MiB ordinary part.
MAX_MULTIPART_PART_COUNT: Final[int] = 13

#: One multipart session expires exactly 24 hours after its creation
#: (spec 4), across mobile suspend; expiry is terminal for that session.
MULTIPART_SESSION_LIFETIME: Final[timedelta] = timedelta(hours=24)

#: One presigned part URL authorizes its single PUT for at most 10 minutes
#: (spec 4); an expired URL is a normal retry through status plus a fresh
#: URL for that one unfinished part.
MULTIPART_PART_URL_LIFETIME: Final[timedelta] = timedelta(minutes=10)

#: Opaque public session-ID grammar: printable URL-safe base64url text of 32
#: to 128 characters. The grammar deliberately excludes the hyphenated UUID
#: form; the ID is an opaque handle, never a raw canonical identifier,
#: staging key, provider upload ID or presigned URL fragment.
_SESSION_ID_MIN_LENGTH: Final[int] = 32
_SESSION_ID_MAX_LENGTH: Final[int] = 128
_SESSION_ID_CHARS: Final[frozenset[str]] = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)
#: The raw canonical UUID form (either case) that the session-ID grammar
#: refuses, so a session ID can never be a bare database UUID in disguise.
_RAW_UUID_SESSION_ID_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

#: Bounded length of one presigned part URL value.
_MAX_PART_URL_LENGTH: Final[int] = 8192


class MultipartSessionState(StrEnum):
    """Closed server session states of the multipart lifecycle (spec 4.2).

    ``committed`` is the frozen successful source-event outcome, not merely a
    completed provider multipart object. ``integrity_failed`` and
    ``policy_denied`` are terminal for that frozen event and never publish.
    ``cleanup_pending`` is a cleanup obligation, not permission to reuse the
    session. Transitions are validated against the closed
    :data:`MULTIPART_SESSION_TRANSITIONS` table; the two terminal outcomes
    (``committed`` and ``cleaned``) accept no further transition.
    """

    CREATED = "created"
    UPLOADING = "uploading"
    COMPLETING = "completing"
    VERIFYING = "verifying"
    PROMOTING = "promoting"
    COMMITTED = "committed"
    CANCELLING = "cancelling"
    EXPIRED = "expired"
    INTEGRITY_FAILED = "integrity_failed"
    POLICY_DENIED = "policy_denied"
    CLEANUP_PENDING = "cleanup_pending"
    CLEANED = "cleaned"

    def allows_transition_to(self, target: MultipartSessionState) -> bool:
        """Return whether the closed table permits ``self -> target``."""

        return target in MULTIPART_SESSION_TRANSITIONS[self]

    def require_transition_to(self, target: MultipartSessionState) -> None:
        """Validate ``self -> target`` against the closed transition table.

        Raises ``ValueError`` naming both states when the table refuses the
        transition, so a terminal session can never be reopened and the
        forward chain can never skip a verification step.
        """

        if target not in MULTIPART_SESSION_TRANSITIONS[self]:
            raise ValueError(
                f"{self.value} session state does not allow transitioning to {target.value}"
            )


#: The exact closed transition table of the spec 4.2 server state machine.
#: The forward chain advances one step at a time; every active state may exit
#: to each terminal failure obligation (user cancellation, 24-hour expiry,
#: integrity failure, policy denial) because expiry strikes on the clock,
#: integrity is decided during completion and policy is rechecked at session
#: creation, URL issuance, completion and publication (spec 3/6). Each
#: failure obligation resolves into ``cleanup_pending`` and then ``cleaned``;
#: ``committed`` and ``cleaned`` are terminal with no outgoing transition.
MULTIPART_SESSION_TRANSITIONS: Final[
    Mapping[MultipartSessionState, frozenset[MultipartSessionState]]
] = MappingProxyType(
    {
        MultipartSessionState.CREATED: frozenset(
            {
                MultipartSessionState.UPLOADING,
                MultipartSessionState.CANCELLING,
                MultipartSessionState.EXPIRED,
                MultipartSessionState.INTEGRITY_FAILED,
                MultipartSessionState.POLICY_DENIED,
            }
        ),
        MultipartSessionState.UPLOADING: frozenset(
            {
                MultipartSessionState.COMPLETING,
                MultipartSessionState.CANCELLING,
                MultipartSessionState.EXPIRED,
                MultipartSessionState.INTEGRITY_FAILED,
                MultipartSessionState.POLICY_DENIED,
            }
        ),
        MultipartSessionState.COMPLETING: frozenset(
            {
                MultipartSessionState.VERIFYING,
                MultipartSessionState.CANCELLING,
                MultipartSessionState.EXPIRED,
                MultipartSessionState.INTEGRITY_FAILED,
                MultipartSessionState.POLICY_DENIED,
            }
        ),
        MultipartSessionState.VERIFYING: frozenset(
            {
                MultipartSessionState.PROMOTING,
                MultipartSessionState.CANCELLING,
                MultipartSessionState.EXPIRED,
                MultipartSessionState.INTEGRITY_FAILED,
                MultipartSessionState.POLICY_DENIED,
            }
        ),
        MultipartSessionState.PROMOTING: frozenset(
            {
                MultipartSessionState.COMMITTED,
                MultipartSessionState.CANCELLING,
                MultipartSessionState.EXPIRED,
                MultipartSessionState.INTEGRITY_FAILED,
                MultipartSessionState.POLICY_DENIED,
            }
        ),
        MultipartSessionState.CANCELLING: frozenset({MultipartSessionState.CLEANUP_PENDING}),
        MultipartSessionState.EXPIRED: frozenset({MultipartSessionState.CLEANUP_PENDING}),
        MultipartSessionState.INTEGRITY_FAILED: frozenset({MultipartSessionState.CLEANUP_PENDING}),
        MultipartSessionState.POLICY_DENIED: frozenset({MultipartSessionState.CLEANUP_PENDING}),
        MultipartSessionState.CLEANUP_PENDING: frozenset({MultipartSessionState.CLEANED}),
        MultipartSessionState.COMMITTED: frozenset(),
        MultipartSessionState.CLEANED: frozenset(),
    }
)


@dataclass(frozen=True, slots=True)
class MultipartUploadSessionId:
    """Opaque URL-safe public identifier of one multipart upload session.

    The abstract grammar is printable base64url text of 32 to 128 characters
    so the ID is safe in the session path and is never a raw canonical UUID,
    staging key, provider upload ID or object-store detail. The value is a
    private credential-scoped handle: it never renders outside a redacted
    ``repr`` and never enters a metric label.
    """

    value: str

    def __repr__(self) -> str:
        return f"{type(self).__name__}(value=<redacted>)"

    def __post_init__(self) -> None:
        length = len(self.value)
        if length < _SESSION_ID_MIN_LENGTH or length > _SESSION_ID_MAX_LENGTH:
            raise ValueError(
                f"session ID must contain {_SESSION_ID_MIN_LENGTH} to "
                f"{_SESSION_ID_MAX_LENGTH} characters"
            )
        if any(char not in _SESSION_ID_CHARS for char in self.value):
            raise ValueError("session ID must be printable URL-safe base64url text")
        if _RAW_UUID_SESSION_ID_PATTERN.fullmatch(self.value) is not None:
            raise ValueError(
                "session ID must be printable URL-safe base64url text, not a raw canonical UUID"
            )


@dataclass(frozen=True, slots=True)
class MultipartPartRange:
    """The exact byte range of one numbered part (spec 4).

    A part window is a positive byte size at most the ordinary part size at a
    non-negative byte offset: ordinary parts cover exactly
    :data:`MULTIPART_PART_SIZE_BYTES` bytes and only the final part may be
    smaller. Range arithmetic and the final-part rule are validated together
    on :class:`MultipartPartGeometry`, which is the single constructor of
    consistent ranges.
    """

    part_number: int
    offset_bytes: int
    size_bytes: int

    def __post_init__(self) -> None:
        if self.part_number < 1:
            raise ValueError("part_number must be a positive part number")
        if self.offset_bytes < 0:
            raise ValueError("offset_bytes must be a non-negative byte offset")
        if not 1 <= self.size_bytes <= MULTIPART_PART_SIZE_BYTES:
            raise ValueError(
                f"size_bytes must be 1 to {MULTIPART_PART_SIZE_BYTES} bytes for one part"
            )


@dataclass(frozen=True, slots=True)
class MultipartPartGeometry:
    """The immutable exact part geometry of one multipart transfer (spec 4).

    Derivable only for a declared size strictly above the single-part routing
    constant and at or below the 100 MiB product maximum: the ordinary part
    is exactly :data:`MULTIPART_PART_SIZE_BYTES`, the final part carries the
    remaining positive bytes (at most one ordinary part) and the part count
    never exceeds :data:`MAX_MULTIPART_PART_COUNT`. ``part_range`` derives
    the exact numbered window the server presigns and the client transmits.
    """

    total_size_bytes: int
    part_size_bytes: int
    part_count: int

    @classmethod
    def from_size_bytes(cls, size_bytes: int) -> MultipartPartGeometry:
        """Derive the exact geometry one transfer of ``size_bytes`` uses."""

        _require_multipart_routing_range(size_bytes)
        part_count = -(-size_bytes // MULTIPART_PART_SIZE_BYTES)
        return cls(
            total_size_bytes=size_bytes,
            part_size_bytes=MULTIPART_PART_SIZE_BYTES,
            part_count=part_count,
        )

    def part_range(self, part_number: int) -> MultipartPartRange:
        """Return the exact byte window of ``part_number`` for this geometry."""

        if not 1 <= part_number <= self.part_count:
            raise ValueError(
                f"part number must be 1 to {self.part_count} for this session geometry"
            )
        offset_bytes = (part_number - 1) * self.part_size_bytes
        if part_number < self.part_count:
            size_bytes = self.part_size_bytes
        else:
            size_bytes = self.total_size_bytes - (self.part_count - 1) * self.part_size_bytes
        return MultipartPartRange(
            part_number=part_number, offset_bytes=offset_bytes, size_bytes=size_bytes
        )

    def __post_init__(self) -> None:
        if self.part_size_bytes != MULTIPART_PART_SIZE_BYTES:
            raise ValueError(f"part_size_bytes must be exactly {MULTIPART_PART_SIZE_BYTES} bytes")
        _require_multipart_routing_range(self.total_size_bytes)
        expected_part_count = -(-self.total_size_bytes // self.part_size_bytes)
        if self.part_count != expected_part_count:
            raise ValueError(
                f"part_count must be exactly {expected_part_count} for this total size"
            )


def _require_multipart_routing_range(size_bytes: int) -> None:
    """Reject any declared size outside the closed multipart routing range."""

    if not MAX_SINGLE_PART_FILE_SIZE_BYTES < size_bytes <= MAX_UPLOAD_FILE_SIZE_BYTES:
        raise ValueError(
            "size_bytes must be inside the multipart routing range of "
            f"{MAX_SINGLE_PART_FILE_SIZE_BYTES} < size <= {MAX_UPLOAD_FILE_SIZE_BYTES} bytes"
        )


@dataclass(frozen=True, slots=True)
class MultipartUploadPlan:
    """The server-owned plan one permitted create/update receives (spec 4).

    Carries the opaque public session ID, the exact part geometry and the
    24-hour session expiry — and nothing else: no signed URL, staging key,
    provider ID, ETag, receipt or storage identity ever crosses with the
    plan. :meth:`from_size_bytes` derives only the size-bound geometry; the
    session identity and expiry attach when the durable session is created
    or exactly replayed.
    """

    session_id: MultipartUploadSessionId
    part_size_bytes: int
    part_count: int
    expires_at: datetime

    @classmethod
    def from_size_bytes(cls, size_bytes: int) -> MultipartPartGeometry:
        """Derive the exact geometry a plan for ``size_bytes`` must carry."""

        return MultipartPartGeometry.from_size_bytes(size_bytes)

    def __post_init__(self) -> None:
        if self.part_size_bytes != MULTIPART_PART_SIZE_BYTES:
            raise ValueError(f"part_size_bytes must be exactly {MULTIPART_PART_SIZE_BYTES} bytes")
        if not 1 <= self.part_count <= MAX_MULTIPART_PART_COUNT:
            raise ValueError(f"part_count must be 1 to {MAX_MULTIPART_PART_COUNT} parts")
        object.__setattr__(
            self,
            "expires_at",
            normalize_utc_timestamp("expires_at", self.expires_at),
        )


@dataclass(frozen=True, slots=True)
class MultipartPartUrl:
    """One short-lived presigned PUT authorization for a single part (spec 4).

    Binds exactly one numbered part, its exact byte range, the absolute
    https URL and that URL's own expiry. The URL (with its query signature)
    is a private value: it never renders outside a redacted ``repr``, is
    never persisted by the client and never enters an application log.
    """

    part_number: int
    byte_range: MultipartPartRange
    url: str
    expires_at: datetime

    def __repr__(self) -> str:
        return f"{type(self).__name__}(<redacted>)"

    def __post_init__(self) -> None:
        if self.part_number < 1:
            raise ValueError("part_number must be a positive part number")
        if self.byte_range.part_number != self.part_number:
            raise ValueError("byte_range must describe exactly the requested part number")
        if not self.url.startswith("https://") or len(self.url) > _MAX_PART_URL_LENGTH:
            raise ValueError("url must be an absolute https URL within the bounded length")
        object.__setattr__(
            self,
            "expires_at",
            normalize_utc_timestamp("expires_at", self.expires_at),
        )


@dataclass(frozen=True, slots=True)
class MultipartSessionStatus:
    """The safe observable state of one multipart session (spec 4/5).

    Carries the opaque session ID, current state, exact geometry, session
    expiry, the reconciled completed part-number set and — only once the
    session is committed — its frozen terminal source-event result. No
    digest, staging key, provider identity or URL is ever part of a status.
    """

    session_id: MultipartUploadSessionId
    state: MultipartSessionState
    part_size_bytes: int
    part_count: int
    expires_at: datetime
    completed_part_numbers: frozenset[int]
    terminal_result: SmallFileTerminalResult | None

    def __post_init__(self) -> None:
        if self.part_size_bytes != MULTIPART_PART_SIZE_BYTES:
            raise ValueError(f"part_size_bytes must be exactly {MULTIPART_PART_SIZE_BYTES} bytes")
        if not 1 <= self.part_count <= MAX_MULTIPART_PART_COUNT:
            raise ValueError(f"part_count must be 1 to {MAX_MULTIPART_PART_COUNT} parts")
        for part_number in self.completed_part_numbers:
            if not 1 <= part_number <= self.part_count:
                raise ValueError(
                    f"completed part numbers must be 1 to {self.part_count} "
                    "for this session geometry"
                )
        if self.state is MultipartSessionState.COMMITTED:
            if self.terminal_result is None:
                raise ValueError("committed session status requires its frozen terminal result")
        elif self.terminal_result is not None:
            raise ValueError("only a committed session status carries a terminal result")
        object.__setattr__(
            self,
            "expires_at",
            normalize_utc_timestamp("expires_at", self.expires_at),
        )


@dataclass(frozen=True, slots=True)
class MultipartCompletionResult:
    """The safe result of one completion claim (spec 4.2/5).

    Either the session is still completing under its durable claimant and
    carries its persisted state with no result, or the claim finished and the
    frozen terminal source-event result returns unchanged on every exact
    replay. A result exists only in the committed state.
    """

    state: MultipartSessionState
    terminal_result: SmallFileTerminalResult | None

    def __post_init__(self) -> None:
        if self.state is MultipartSessionState.COMMITTED:
            if self.terminal_result is None:
                raise ValueError("committed completion requires its frozen terminal result")
        elif self.terminal_result is not None:
            raise ValueError("only a committed completion carries a terminal result")


def compute_multipart_session_expiry(created_at: datetime) -> datetime:
    """Return the 24-hour multipart session expiry deadline (spec 4).

    The deadline normalizes to UTC and adds exactly
    :data:`MULTIPART_SESSION_LIFETIME`; database-time enforcement is the
    store's own authority.
    """

    normalized = normalize_utc_timestamp("created_at", created_at)
    if normalized is None:
        # Unreachable for the non-optional parameter; keeps the path total.
        raise ValueError("created_at must be timezone-aware")
    return normalized + MULTIPART_SESSION_LIFETIME


__all__ = [
    "MAX_MULTIPART_PART_COUNT",
    "MULTIPART_PART_SIZE_BYTES",
    "MULTIPART_PART_URL_LIFETIME",
    "MULTIPART_SESSION_LIFETIME",
    "MULTIPART_SESSION_TRANSITIONS",
    "MultipartCompletionResult",
    "MultipartPartGeometry",
    "MultipartPartRange",
    "MultipartPartUrl",
    "MultipartSessionState",
    "MultipartSessionStatus",
    "MultipartUploadPlan",
    "MultipartUploadSessionId",
    "compute_multipart_session_expiry",
]
