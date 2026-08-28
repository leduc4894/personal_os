"""Strict small-file sync wire models and the domain boundary conversion.

Every model here is frozen and closed for extra fields and mirrors the wire
grammar of spec 10.1 exactly: the preflight body carries the journal event
identity, the declared fingerprint and the accepted ``policy_revision`` wire
name — deliberately mapped onto the domain's ``policy_revision_number`` — and
never a workspace, device, user, receipt or object-store selector. Conversion
to the frozen domain values happens only through this boundary: each field
grammar that the domain owns (idempotency key, locator, digest, media type,
policy revision, operation shape and the server-owned upload ceiling)
surfaces as the typed ``small_file_preflight_invalid`` with its single closed
``reason`` token, or as the closed size-limit rejection when the declared
size is already over the ceiling. The response renderers project domain
results onto strict payloads: the preflight data carries exactly the members
its one typed outcome admits — rendered with ``exclude_unset`` so no outcome
leaks another outcome's payload — and the terminal result carries only the
safe canonical receipt members of spec 10.3, never an object key, provider
detail or digest.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from personal_os.diagnostics.events import SafeToken
from personal_os.error_contracts.codes import ErrorCode
from personal_os.object_storage import CanonicalMediaType, ContentDigest
from personal_os.small_file_sync.contracts import (
    MAX_UPLOAD_FILE_SIZE_BYTES,
    NormalizedLocator,
    SmallFileIdempotencyKey,
    SmallFileOperation,
    SmallFilePreflight,
    SmallFilePreflightOutcome,
    SmallFileTerminalResult,
    SmallFileTerminalResultKind,
)
from personal_os.small_file_sync.errors import (
    CREATE_BASE_PRESENT,
    DIGEST_INVALID,
    EVENT_ID_INVALID,
    IDEMPOTENCY_KEY_INVALID,
    LOCAL_FILE_ID_INVALID,
    LOCATOR_INVALID,
    MEDIA_TYPE_INVALID,
    POLICY_REVISION_INVALID,
    SIZE_BYTES_INVALID,
    UPDATE_BASE_MISSING,
    SmallFileSyncError,
)
from personal_os.small_file_sync.service import SmallFilePreflightResult

#: Wire grammar of the idempotency key: exactly the canonical lowercase
#: hyphenated UUID text form the plugin mints with ``crypto.randomUUID``.
_IDEMPOTENCY_KEY_PATTERN: Final[str] = (
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)

#: Wire grammar of the exact lowercase content digest (64 hex characters).
_SHA256_PATTERN: Final[str] = r"^[0-9a-f]{64}$"

PreflightOperationValue = Literal["create", "update"]


class SmallFilePreflightRequest(BaseModel):
    """The strict journal-event preflight body (spec 10.1).

    Carries the stable journal event identity, the idempotency key, the
    create/update operation shape, the plugin-local file identity, the
    declared fingerprint (locator context, digest, exact size, media type)
    and the accepted signed policy revision under its wire name — never a
    workspace, device, user, receipt, object-store or provider selector.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: UUID
    idempotency_key: str = Field(pattern=_IDEMPOTENCY_KEY_PATTERN)
    operation: PreflightOperationValue
    local_file_id: UUID
    source_id: UUID | None = None
    base_version_id: UUID | None = None
    normalized_locator: str
    sha256: str = Field(pattern=_SHA256_PATTERN)
    size_bytes: int
    media_type: str
    policy_revision: int


class SmallFileTerminalResultData(BaseModel):
    """The safe canonical terminal result of spec 10.3.

    The exact receipt an exact replay returns unchanged: the result kind, the
    canonical source and version identity, the content version and the commit
    moment. No digest, object key, storage receipt or provider detail is a
    member, so none can ever render.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    result_kind: SmallFileTerminalResultKind
    source_id: UUID
    source_version_id: UUID
    content_version: int
    committed_at: datetime


class SmallFilePreflightData(BaseModel):
    """One completed preflight: exactly one typed outcome and its safe payload.

    ``single_part_upload`` carries only the opaque operation token and its
    expiry; ``committed_replay`` and ``no_change`` carry only the frozen
    terminal result; ``excluded`` and ``conflict`` carry no payload member at
    all. Responses render with ``exclude_unset`` so each outcome emits
    exactly its own members.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: SmallFilePreflightOutcome
    operation_id: str | None = None
    expires_at: datetime | None = None
    result: SmallFileTerminalResultData | None = None


def _preflight_invalid(reason: SafeToken) -> SmallFileSyncError:
    return SmallFileSyncError(
        ErrorCode.SMALL_FILE_PREFLIGHT_INVALID, safe_details={"reason": reason}
    )


def to_domain_preflight(body: SmallFilePreflightRequest) -> SmallFilePreflight:
    """Convert one strict wire body into the frozen domain preflight.

    Every semantic grammar stays owned by the domain; this boundary maps each
    violation onto the closed ``small_file_preflight_invalid`` reason token
    of its field, and a declared size over the server-owned upload ceiling —
    the 100 MiB product maximum, above the unchanged single-part routing
    constant whose larger sizes now route to the multipart session endpoints
    (Child 7 spec 4) — onto the closed size-limit rejection. The converted
    value never carries a workspace, device or user — those derive from the
    credential.
    """

    if body.event_id == UUID(int=0):
        raise _preflight_invalid(EVENT_ID_INVALID)
    if body.local_file_id == UUID(int=0):
        raise _preflight_invalid(LOCAL_FILE_ID_INVALID)
    try:
        idempotency_key = SmallFileIdempotencyKey(body.idempotency_key)
    except ValueError:
        raise _preflight_invalid(IDEMPOTENCY_KEY_INVALID) from None
    try:
        locator = NormalizedLocator(body.normalized_locator)
    except ValueError:
        raise _preflight_invalid(LOCATOR_INVALID) from None
    try:
        digest = ContentDigest.parse(body.sha256)
    except ValueError:
        raise _preflight_invalid(DIGEST_INVALID) from None
    try:
        media_type = CanonicalMediaType.parse(body.media_type)
    except ValueError:
        raise _preflight_invalid(MEDIA_TYPE_INVALID) from None
    if body.policy_revision < 1:
        raise _preflight_invalid(POLICY_REVISION_INVALID)
    if body.size_bytes < 0:
        raise _preflight_invalid(SIZE_BYTES_INVALID)
    if body.size_bytes > MAX_UPLOAD_FILE_SIZE_BYTES:
        raise SmallFileSyncError(ErrorCode.SMALL_FILE_SIZE_LIMIT_EXCEEDED)
    operation = SmallFileOperation(body.operation)
    if operation is SmallFileOperation.CREATE:
        if body.source_id is not None or body.base_version_id is not None:
            raise _preflight_invalid(CREATE_BASE_PRESENT)
    else:
        if body.source_id is None or body.base_version_id is None:
            raise _preflight_invalid(UPDATE_BASE_MISSING)
        if body.source_id == UUID(int=0) or body.base_version_id == UUID(int=0):
            raise _preflight_invalid(UPDATE_BASE_MISSING)
    return SmallFilePreflight(
        event_id=body.event_id,
        idempotency_key=idempotency_key,
        operation=operation,
        local_file_id=body.local_file_id,
        source_id=body.source_id,
        base_version_id=body.base_version_id,
        normalized_locator=locator,
        sha256=digest,
        size_bytes=body.size_bytes,
        media_type=media_type,
        policy_revision_number=body.policy_revision,
    )


def small_file_terminal_result_data(
    terminal: SmallFileTerminalResult,
) -> SmallFileTerminalResultData:
    """Render the frozen terminal receipt onto its strict wire payload."""

    return SmallFileTerminalResultData(
        result_kind=terminal.result_kind,
        source_id=terminal.source_id,
        source_version_id=terminal.source_version_id,
        content_version=terminal.content_version,
        committed_at=terminal.committed_at,
    )


def small_file_preflight_data(result: SmallFilePreflightResult) -> SmallFilePreflightData:
    """Render exactly one typed preflight outcome onto its strict payload."""

    if result.outcome is SmallFilePreflightOutcome.SINGLE_PART_UPLOAD:
        token = result.operation_token
        expires_at = result.expires_at
        if token is None or expires_at is None:
            raise SmallFileSyncError(ErrorCode.SMALL_FILE_UPLOAD_STATE_INVALID)
        return SmallFilePreflightData(
            outcome=result.outcome,
            operation_id=token.value,
            expires_at=expires_at,
        )
    if result.terminal_result is not None:
        return SmallFilePreflightData(
            outcome=result.outcome,
            result=small_file_terminal_result_data(result.terminal_result),
        )
    return SmallFilePreflightData(outcome=result.outcome)


__all__ = [
    "SmallFilePreflightData",
    "SmallFilePreflightRequest",
    "SmallFileTerminalResultData",
    "small_file_preflight_data",
    "small_file_terminal_result_data",
    "to_domain_preflight",
]
