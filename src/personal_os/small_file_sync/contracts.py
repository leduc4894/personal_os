"""Framework-neutral small-file sync domain contracts (spec 10 and 12).

Immutable value objects and closed vocabularies for the authenticated
two-step small-file upload: the strict preflight shape of spec 10.1, the
opaque URL-safe operation token handed to the client, the operation record
binding device/event identity to one declared fingerprint, the terminal
preflight-outcome mapping and the frozen canonical result retained for exact
replay. The module imports no FastAPI, SQLAlchemy, R2 or request type; device
and workspace identity arrive only through the credential-derived
:class:`SmallFileDeviceContext`, never from preflight data (a request body
never chooses a workspace). Locators, digests, tokens and file bytes are
treated as private values: they never enter diagnostics or metric labels.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final
from uuid import UUID

from personal_os.object_storage import CanonicalMediaType, ContentDigest
from personal_os.source_locators.values import NormalizedLocator as NormalizedLocator
from personal_os.sources.actors import reject_nil_uuid
from personal_os.sources.commands import normalize_utc_timestamp

#: Server-owned single-part upload ceiling: exactly 16 MiB (spec 3.1, 10.1).
#: ``size_bytes == MAX_SINGLE_PART_FILE_SIZE_BYTES`` is accepted; one byte
#: more is the closed size-limit rejection. This constant is the single
#: Python source of the limit and is never duplicated as a literal.
MAX_SINGLE_PART_FILE_SIZE_BYTES: Final[int] = 16 * 1024 * 1024

#: Canonical idempotency-key grammar: exactly the canonical lowercase
#: hyphenated UUID text form the plugin mints with ``crypto.randomUUID``.
_IDEMPOTENCY_KEY_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)

#: Opaque operation-token grammar: printable URL-safe base64url text of 32
#: to 128 characters. The grammar deliberately excludes the hyphenated UUID
#: form; the token is an opaque handle, never a raw canonical identifier,
#: storage location or provider receipt (task 6/7 choose the representation).
_OPERATION_TOKEN_MIN_LENGTH: Final[int] = 32
_OPERATION_TOKEN_MAX_LENGTH: Final[int] = 128
_OPERATION_TOKEN_CHARS: Final[frozenset[str]] = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)
#: The raw canonical UUID form (either case) that the token grammar refuses,
#: so a token can never be a bare database UUID in disguise.
_RAW_UUID_TOKEN_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


class SmallFileOperation(StrEnum):
    """Closed vocabulary of preflight operations (spec 10.1: create/update)."""

    CREATE = "create"
    UPDATE = "update"


class SmallFilePreflightOutcome(StrEnum):
    """Closed typed outcomes one preflight returns (spec 10.1 table).

    The four members of :data:`TERMINAL_PREFLIGHT_OUTCOMES` finish the event
    without any upload; ``SINGLE_PART_UPLOAD`` is the only outcome that opens
    the content-stream step.
    """

    COMMITTED_REPLAY = "committed_replay"
    NO_CHANGE = "no_change"
    EXCLUDED = "excluded"
    CONFLICT = "conflict"
    SINGLE_PART_UPLOAD = "single_part_upload"


#: The preflight outcomes that end the event with no upload and no automatic
#: retry (spec 10.1 table and section 12): exactly the four terminal outcomes.
TERMINAL_PREFLIGHT_OUTCOMES: Final[frozenset[SmallFilePreflightOutcome]] = frozenset(
    {
        SmallFilePreflightOutcome.COMMITTED_REPLAY,
        SmallFilePreflightOutcome.NO_CHANGE,
        SmallFilePreflightOutcome.EXCLUDED,
        SmallFilePreflightOutcome.CONFLICT,
    }
)


class SmallFileTerminalResultKind(StrEnum):
    """Closed terminal result kinds retained for exact replay (spec 10.3)."""

    COMMITTED = "committed"
    NO_CHANGE = "no_change"


@dataclass(frozen=True, slots=True)
class SmallFileIdempotencyKey:
    """Stable event idempotency key in canonical UUID text form.

    The grammar accepts only the canonical lowercase hyphenated 8-4-4-4-12
    form; uppercase, braces, ``urn:uuid:`` prefixes, whitespace, wrong
    lengths, the nil UUID and any non-printable form are rejected without
    normalization. The key is opaque, device/event-scoped and never logged.
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
class UploadOperationToken:
    """Opaque URL-safe token identifying one preflight-bound upload operation.

    The abstract grammar is printable base64url text of 32 to 128 characters
    so the token is safe in the upload path and is never a raw canonical UUID
    or object-store detail. Only the grammar lives here; tasks 6 and 7 own
    the representation and storage behind it.
    """

    value: str

    def __repr__(self) -> str:
        return f"{type(self).__name__}(value=<redacted>)"

    def __post_init__(self) -> None:
        length = len(self.value)
        if length < _OPERATION_TOKEN_MIN_LENGTH or length > _OPERATION_TOKEN_MAX_LENGTH:
            raise ValueError(
                f"operation token must contain {_OPERATION_TOKEN_MIN_LENGTH} to "
                f"{_OPERATION_TOKEN_MAX_LENGTH} characters"
            )
        if any(char not in _OPERATION_TOKEN_CHARS for char in self.value):
            raise ValueError("operation token must be printable URL-safe base64url text")
        if _RAW_UUID_TOKEN_PATTERN.fullmatch(self.value) is not None:
            raise ValueError(
                "operation token must be printable URL-safe base64url text, "
                "not a raw canonical UUID"
            )


@dataclass(frozen=True, slots=True)
class SmallFilePreflight:
    """Immutable validated preflight intent of spec 10.1.

    Field requirements are exact: a create carries neither ``source_id`` nor
    ``base_version_id`` (the client never mints a canonical source); an
    update requires both as non-nil UUIDs. ``size_bytes`` is a non-negative
    exact byte size at or below the frozen single-part limit — equality is
    allowed, one byte over is rejected. Device, user and workspace identity
    are never part of this value; they derive from the bearer credential.
    """

    event_id: UUID
    idempotency_key: SmallFileIdempotencyKey
    operation: SmallFileOperation
    local_file_id: UUID
    source_id: UUID | None
    base_version_id: UUID | None
    normalized_locator: NormalizedLocator
    sha256: ContentDigest
    size_bytes: int
    media_type: CanonicalMediaType
    policy_revision_number: int

    def __post_init__(self) -> None:
        reject_nil_uuid("event_id", self.event_id)
        reject_nil_uuid("local_file_id", self.local_file_id)
        if self.policy_revision_number < 1:
            raise ValueError("policy_revision_number must be a positive integer")
        if self.size_bytes < 0:
            raise ValueError("size_bytes must be a non-negative byte size")
        if self.size_bytes > MAX_SINGLE_PART_FILE_SIZE_BYTES:
            raise ValueError(
                "size_bytes exceeds the server single-part size limit of "
                f"{MAX_SINGLE_PART_FILE_SIZE_BYTES} bytes"
            )
        if self.operation is SmallFileOperation.CREATE:
            if self.source_id is not None:
                raise ValueError("create preflight must not carry a source_id")
            if self.base_version_id is not None:
                raise ValueError("create preflight must not carry a base_version_id")
            return
        if self.source_id is None:
            raise ValueError("update preflight requires a source_id")
        if self.base_version_id is None:
            raise ValueError("update preflight requires a base_version_id")
        reject_nil_uuid("source_id", self.source_id)
        reject_nil_uuid("base_version_id", self.base_version_id)


@dataclass(frozen=True, slots=True)
class SmallFileDeviceContext:
    """Credential-derived identity of the authenticated device (spec 10).

    Composed by the API adapter from the opaque bearer token and its fixed
    ``obsidian_sync`` scope; no preflight or request field can select any of
    these identities.
    """

    device_id: UUID
    workspace_id: UUID

    def __post_init__(self) -> None:
        reject_nil_uuid("device_id", self.device_id)
        reject_nil_uuid("workspace_id", self.workspace_id)


@dataclass(frozen=True, slots=True)
class SmallFileUploadOperation:
    """One preflight-bound pending upload operation (spec 10.1).

    Binds the credential-derived device context, the full validated preflight
    (event identity, idempotency key and declared fingerprint) and the
    expiry deadline under the opaque token. It permits no payload
    substitution: the declared fingerprint is immutable for the operation's
    life. A create may carry the internal UUID the server reserved for a
    future publication; that reservation never inserts a ``sources`` row and
    is never disclosed to the client. An update must not reserve one — its
    target source and base live on the preflight itself.
    """

    operation_token: UploadOperationToken
    preflight: SmallFilePreflight
    device_context: SmallFileDeviceContext
    reserved_source_id: UUID | None
    expires_at: datetime

    def __post_init__(self) -> None:
        if self.reserved_source_id is not None:
            reject_nil_uuid("reserved_source_id", self.reserved_source_id)
            if self.preflight.operation is SmallFileOperation.UPDATE:
                raise ValueError("update operation must not reserve a source_id")
        object.__setattr__(
            self,
            "expires_at",
            normalize_utc_timestamp("expires_at", self.expires_at),
        )


@dataclass(frozen=True, slots=True)
class SmallFileTerminalResult:
    """The safe canonical terminal result retained for exact replay (spec 10.3).

    Frozen when the publication transaction commits and returned unchanged by
    a same-identity replay: for ``committed`` it is the original
    source/version receipt the plugin persists; for ``no_change`` it is the
    confirmed current base. It carries no receipt, object key, provider
    detail or digest — those never leave the server.
    """

    result_kind: SmallFileTerminalResultKind
    source_id: UUID
    source_version_id: UUID
    content_version: int
    committed_at: datetime

    def __post_init__(self) -> None:
        reject_nil_uuid("source_id", self.source_id)
        reject_nil_uuid("source_version_id", self.source_version_id)
        if self.content_version < 1:
            raise ValueError("content_version must be a positive integer")
        object.__setattr__(
            self,
            "committed_at",
            normalize_utc_timestamp("committed_at", self.committed_at),
        )


def compute_locator_fingerprint(locator: NormalizedLocator) -> str:
    """Return the lowercase SHA-256 digest of one canonical normalized locator.

    The digest is the retained identifier the durable operation row keeps
    after the raw locator is cleared, so an exact replay can compare the
    locator without ever re-reading a sensitive path.
    """

    return hashlib.sha256(locator.value.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class BoundSmallFileOperation:
    """The durable receive-side view of one upload operation, locator-bound.

    Carries every immutable operation field the receive binding already
    held plus the bound initial locator evidence: the transient
    :class:`NormalizedLocator` (cleared on terminal transition) and the
    retained ``locator_fingerprint`` digest (kept for exact replay). An
    update preflight, a pre-migration row or a terminal operation may carry
    a null locator and a null digest; the digest alone (without the raw
    locator) is the canonical post-terminal shape.
    """

    operation_id: UUID
    operation_token: UploadOperationToken
    workspace_id: UUID
    device_id: UUID
    event_id: UUID
    idempotency_key: SmallFileIdempotencyKey
    operation: SmallFileOperation
    declared_sha256: ContentDigest
    declared_size_bytes: int
    declared_media_type: CanonicalMediaType
    policy_revision_number: int
    reserved_source_id: UUID | None
    update_source_id: UUID | None
    update_base_version_id: UUID | None
    normalized_locator: NormalizedLocator | None
    locator_fingerprint: str | None
    expires_at: datetime
    terminal_result: SmallFileTerminalResult | None

    def __post_init__(self) -> None:
        reject_nil_uuid("operation_id", self.operation_id)
        reject_nil_uuid("workspace_id", self.workspace_id)
        reject_nil_uuid("device_id", self.device_id)
        reject_nil_uuid("event_id", self.event_id)
        if self.policy_revision_number < 1:
            raise ValueError("policy_revision_number must be a positive integer")
        if self.reserved_source_id is not None:
            reject_nil_uuid("reserved_source_id", self.reserved_source_id)
            if self.operation is SmallFileOperation.UPDATE:
                raise ValueError("update operation must not reserve a source_id")
        if self.normalized_locator is not None:
            if self.operation is SmallFileOperation.UPDATE:
                raise ValueError("update operation must not carry a normalized locator")
            if self.locator_fingerprint is None:
                raise ValueError("normalized_locator requires a matching locator_fingerprint")
            expected = compute_locator_fingerprint(self.normalized_locator)
            if self.locator_fingerprint != expected:
                raise ValueError("locator_fingerprint does not match the normalized locator")
        object.__setattr__(
            self,
            "expires_at",
            normalize_utc_timestamp("expires_at", self.expires_at),
        )
