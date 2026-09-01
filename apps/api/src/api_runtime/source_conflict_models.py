"""Strict source-conflict wire models and the domain boundary conversion.

Every model here is frozen and closed for extra fields and mirrors the wire
grammar of the Conflict Inbox API (Child 8 spec 6) exactly: the resolve
body carries only the new resolution event identity, its idempotency key,
the closed resolution kind, the reviewed remote version and — only under
``save_merged`` — the verified object reference of an already-uploaded
merged result. Raw merged bytes, digests, locators, workspace or device
selectors are refused by the closed schema, so no request can smuggle
content or cross-workspace identity past the boundary. Conversion to the
frozen domain command happens only through this boundary: each field
grammar the domain owns surfaces as the typed
``source_conflict_input_invalid`` with its single closed ``reason`` token.
The response renderers project domain read models onto strict payloads
carrying only opaque identifiers, closed labels and normalized UTC
timestamps — never a locator snapshot, object key, digest, receipt or any
provider detail — and the choices matrix offers exactly the resolution
kinds the store can still apply: a byteless candidate never offers
``keep_local``/``save_merged`` (the store rejects both with
``deletion_apply_unsupported``), a terminal conflict offers none, and the
merge choice appears only for the spec-named ``text/markdown`` candidate
whose bounded three-way merge the Inbox runs locally.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from personal_os.diagnostics.events import SafeToken
from personal_os.error_contracts.codes import ErrorCode
from personal_os.source_conflicts.commands import (
    ConflictResolutionResult,
    ResolveConflictCommand,
)
from personal_os.source_conflicts.contracts import (
    ConflictCandidateKind,
    ConflictIdempotencyKey,
    ConflictKind,
    ConflictResolutionKind,
    ConflictResolutionOutcome,
    ConflictStatus,
    SourceConflict,
)
from personal_os.source_conflicts.errors import (
    CANDIDATE_OBJECT_INVALID,
    IDEMPOTENCY_KEY_INVALID,
    RESOLUTION_EVENT_ID_INVALID,
    REVIEWED_REMOTE_INVALID,
    SourceConflictError,
)

#: Wire grammar of the idempotency key: exactly the canonical lowercase
#: hyphenated UUID text form the plugin mints with ``crypto.randomUUID``.
_IDEMPOTENCY_KEY_PATTERN: Final[str] = (
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)

#: The closed bound of one open-conflict listing page, mirroring the
#: durable store's own page bound.
MAX_CONFLICT_PAGE_LIMIT: Final[int] = 200

#: The default page size of the open-conflict listing.
DEFAULT_CONFLICT_PAGE_LIMIT: Final[int] = 50

#: The media type of the only bounded three-way merge the Inbox runs
#: locally (spec 5.2: "Text/Markdown"); every other candidate — including
#: every binary form — permits only the two whole-object choices.
_MERGEABLE_CANDIDATE_MEDIA_TYPE: Final[str] = "text/markdown"


def is_mergeable_conflict_media_type(media_type: str) -> bool:
    """Report whether a candidate media type admits the local merge choice."""

    return media_type == _MERGEABLE_CANDIDATE_MEDIA_TYPE


class SourceConflictResolveRequest(BaseModel):
    """The strict explicit-resolution body (spec 6).

    Carries exactly the new event identity, its fresh idempotency key, the
    closed resolution choice, the reviewed remote version and the optional
    verified object reference of an already-uploaded merged result — never
    raw bytes, a digest, a locator, a workspace or a device selector. The
    merged result itself travels only through the existing verified upload
    flow; this body references it, it never carries it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    resolution_event_id: UUID
    idempotency_key: str = Field(pattern=_IDEMPOTENCY_KEY_PATTERN)
    resolution_kind: ConflictResolutionKind
    reviewed_remote_version_id: UUID | None = None
    verified_candidate_object_id: UUID | None = None


class SourceConflictData(BaseModel):
    """One conflict's safe metadata: the frozen read model on the wire.

    Every member is an opaque identifier, a closed label or a normalized
    UTC timestamp. The credential-derived workspace is deliberately absent
    (it is the caller's own), and no locator snapshot, object key, digest
    or provider detail ever renders. The optional members follow the
    domain's exact status shapes: an open conflict carries no resolution
    evidence, a resolving one the attempt identity, a resolved one the
    winner and — only under a publishing choice — the resulting version,
    and a superseded one its successor.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    conflict_id: UUID
    source_id: UUID | None
    conflict_kind: ConflictKind
    status: ConflictStatus
    originating_event_id: UUID
    originating_device_id: UUID
    base_version_id: UUID | None
    observed_remote_version_id: UUID | None
    candidate_kind: ConflictCandidateKind
    verified_candidate_object_id: UUID | None
    captured_at: datetime
    resolution_kind: ConflictResolutionKind | None
    resolution_event_id: UUID | None
    resulting_version_id: UUID | None
    successor_conflict_id: UUID | None
    closed_at: datetime | None


class SourceConflictDetailData(SourceConflictData):
    """One conflict's detail: the safe metadata plus the offered choices.

    ``choices`` carries exactly the resolution kinds this conflict still
    admits (see :func:`allowed_resolution_choices`), so the Inbox can never
    offer an unappliable choice.
    """

    choices: tuple[ConflictResolutionKind, ...]


class SourceConflictPageData(BaseModel):
    """One bounded page of the workspace's open conflicts.

    ``next_exclusive_start_conflict_id`` is the stable continuation cursor
    (the last identity of this page) whenever the page filled its bound.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    conflicts: tuple[SourceConflictData, ...]
    has_more: bool
    next_exclusive_start_conflict_id: UUID | None


class SourceConflictResolutionData(BaseModel):
    """The frozen outcome of one explicit resolution attempt.

    ``resolved`` commits the winner — with exactly one resulting version
    only under ``keep_local``/``save_merged`` — and ``stale_successor``
    binds the open successor created against the newer observed remote; a
    same-identity replay receives this value unchanged.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: ConflictResolutionOutcome
    conflict_id: UUID
    resolution_event_id: UUID
    resolution_kind: ConflictResolutionKind
    resulting_version_id: UUID | None
    successor_conflict_id: UUID | None
    completed_at: datetime


def _input_invalid(reason: SafeToken | None = None) -> SourceConflictError:
    if reason is None:
        return SourceConflictError(ErrorCode.SOURCE_CONFLICT_INPUT_INVALID)
    return SourceConflictError(
        ErrorCode.SOURCE_CONFLICT_INPUT_INVALID, safe_details={"reason": reason}
    )


def to_domain_resolve_command(
    body: SourceConflictResolveRequest, *, conflict_id: UUID
) -> ResolveConflictCommand:
    """Convert one strict wire body into the frozen resolve command.

    Each grammar the domain owns — the non-nil event identity, the
    non-nil canonical idempotency key, the non-nil reviewed remote and the
    verified-object shape per resolution kind — surfaces as the closed
    ``source_conflict_input_invalid`` reason token of its field. A nil
    conflict identity has no field of this body; it closes with the
    reason-less validation rejection. Raw content never enters: the wire
    schema refuses every non-member before this conversion runs.
    """

    if conflict_id == UUID(int=0):
        raise _input_invalid()
    if body.resolution_event_id == UUID(int=0):
        raise _input_invalid(RESOLUTION_EVENT_ID_INVALID)
    if body.reviewed_remote_version_id == UUID(int=0):
        raise _input_invalid(REVIEWED_REMOTE_INVALID)
    if body.verified_candidate_object_id == UUID(int=0):
        raise _input_invalid(CANDIDATE_OBJECT_INVALID)
    try:
        idempotency_key = ConflictIdempotencyKey(body.idempotency_key)
    except ValueError:
        raise _input_invalid(IDEMPOTENCY_KEY_INVALID) from None
    if body.resolution_kind is ConflictResolutionKind.SAVE_MERGED:
        if body.verified_candidate_object_id is None:
            raise _input_invalid(CANDIDATE_OBJECT_INVALID)
    elif body.verified_candidate_object_id is not None:
        raise _input_invalid(CANDIDATE_OBJECT_INVALID)
    return ResolveConflictCommand(
        conflict_id=conflict_id,
        reviewed_remote_version_id=body.reviewed_remote_version_id,
        resolution_kind=body.resolution_kind,
        resolution_event_id=body.resolution_event_id,
        idempotency_key=idempotency_key,
        verified_candidate_object_id=body.verified_candidate_object_id,
    )


def allowed_resolution_choices(
    conflict: SourceConflict, *, candidate_media_type: str | None
) -> tuple[ConflictResolutionKind, ...]:
    """Derive exactly the choices this conflict still admits (spec 5.2/6).

    A terminal conflict accepts no further action. A byteless candidate —
    a ``delete_remote_edit`` conflict or a locator collision without
    retained bytes — offers only ``keep_remote``: applying its deletion
    intent is lifecycle-domain work the resolver refuses under
    ``deletion_apply_unsupported``, and that guard covers every publishing
    choice, so neither ``keep_local`` nor ``save_merged`` may be offered. A
    content candidate always offers the two whole-object choices, and the
    merge choice joins them only when the candidate's resolved media type
    is the spec-named ``text/markdown``; an unresolvable media type fails
    closed to the two whole-object choices rather than promising a merge
    the Inbox cannot run.
    """

    if conflict.status is not ConflictStatus.OPEN:
        return ()
    if conflict.candidate.candidate_kind is not ConflictCandidateKind.CONTENT:
        return (ConflictResolutionKind.KEEP_REMOTE,)
    choices = (ConflictResolutionKind.KEEP_REMOTE, ConflictResolutionKind.KEEP_LOCAL)
    if candidate_media_type is not None and is_mergeable_conflict_media_type(candidate_media_type):
        return (*choices, ConflictResolutionKind.SAVE_MERGED)
    return choices


def source_conflict_data(conflict: SourceConflict) -> SourceConflictData:
    """Render the frozen read model onto its strict wire payload."""

    return SourceConflictData(
        conflict_id=conflict.conflict_id,
        source_id=conflict.source_id,
        conflict_kind=conflict.conflict_kind,
        status=conflict.status,
        originating_event_id=conflict.originating_event_id,
        originating_device_id=conflict.originating_device_id,
        base_version_id=conflict.base_version_id,
        observed_remote_version_id=conflict.observed_remote_version_id,
        candidate_kind=conflict.candidate.candidate_kind,
        verified_candidate_object_id=conflict.candidate.verified_candidate_object_id,
        captured_at=conflict.captured_at,
        resolution_kind=conflict.resolution_kind,
        resolution_event_id=conflict.resolution_event_id,
        resulting_version_id=conflict.resulting_version_id,
        successor_conflict_id=conflict.successor_conflict_id,
        closed_at=conflict.closed_at,
    )


def source_conflict_detail_data(
    conflict: SourceConflict, *, choices: tuple[ConflictResolutionKind, ...]
) -> SourceConflictDetailData:
    """Render the safe detail payload with exactly the offered choices."""

    base = source_conflict_data(conflict)
    return SourceConflictDetailData(**base.model_dump(), choices=choices)


def source_conflict_resolution_data(
    result: ConflictResolutionResult,
) -> SourceConflictResolutionData:
    """Render the frozen resolution outcome onto its strict wire payload."""

    successor_id = result.successor.conflict_id if result.successor is not None else None
    return SourceConflictResolutionData(
        outcome=result.kind,
        conflict_id=result.conflict_id,
        resolution_event_id=result.resolution_event_id,
        resolution_kind=result.resolution_kind,
        resulting_version_id=result.resulting_version_id,
        successor_conflict_id=successor_id,
        completed_at=result.completed_at,
    )


__all__ = [
    "DEFAULT_CONFLICT_PAGE_LIMIT",
    "MAX_CONFLICT_PAGE_LIMIT",
    "SourceConflictData",
    "SourceConflictDetailData",
    "SourceConflictPageData",
    "SourceConflictResolutionData",
    "SourceConflictResolveRequest",
    "allowed_resolution_choices",
    "is_mergeable_conflict_media_type",
    "source_conflict_data",
    "source_conflict_detail_data",
    "source_conflict_resolution_data",
    "to_domain_resolve_command",
]
