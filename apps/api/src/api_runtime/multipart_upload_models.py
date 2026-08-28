"""Strict multipart upload wire models and the domain boundary conversion.

Every model here is frozen and closed for extra fields and mirrors the wire
grammar of the Child 7 spec 5 exactly: the create body carries the stable
journal-event identity and declared fingerprint of the preflight it follows
— deliberately the same frozen grammar as the small-file preflight body,
because both calls bind the same frozen event — and never a workspace,
device, user, receipt, presigned URL or object-store selector. Conversion
to the frozen domain value happens only through this boundary: every field
grammar the small-file surface owns (idempotency key, locator, digest,
media type, policy revision, operation shape) surfaces as its identical
closed ``small_file_preflight_invalid`` reason token, a declared size
outside the server-owned multipart routing range — strictly above the
unchanged single-part routing constant and at or below the 100 MiB product
maximum — surfaces as the closed multipart part rejection, and one byte
above the product maximum keeps the closed size-limit rejection. The
response renderers project the domain results onto strict payloads: the
plan carries exactly the opaque session ID, geometry and expiry; the
status carries exactly the state, geometry, expiry, the ordered completed
part numbers and — only once committed — the frozen terminal receipt; the
completion carries the claimed state and its frozen result. The one
part-URL response is the sole model in the entire surface carrying a
``url`` member: no plan, status or completion model admits one, so a
signed URL cannot render on any other surface, and the byte-range members
beside it are the exact derived window the client must transmit.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from api_runtime.small_file_sync_models import (
    SmallFilePreflightRequest,
    SmallFileTerminalResultData,
    small_file_terminal_result_data,
    to_domain_preflight,
)
from personal_os.error_contracts.codes import ErrorCode
from personal_os.multipart_upload.contracts import (
    MultipartCompletionResult,
    MultipartPartUrl,
    MultipartSessionState,
    MultipartSessionStatus,
    MultipartUploadPlan,
)
from personal_os.multipart_upload.errors import MultipartUploadError
from personal_os.small_file_sync.contracts import (
    MAX_SINGLE_PART_FILE_SIZE_BYTES,
    MAX_UPLOAD_FILE_SIZE_BYTES,
    SmallFilePreflight,
)


class MultipartSessionCreateRequest(SmallFilePreflightRequest):
    """The strict multipart session create-or-resume body (spec 5).

    Inherits the journal-event preflight grammar exactly — the stable event
    identity, the idempotency key, the create/update operation shape, the
    plugin-local file identity and the declared fingerprint — because the
    create call binds the very same frozen operation the preflight decided.
    Never a workspace, device, user, receipt, presigned URL, provider or
    object-store selector: the credential alone derives the scope.
    """


class MultipartSessionPlanData(BaseModel):
    """The server-owned plan of one permitted multipart transfer (spec 4).

    Exactly the opaque public session ID, the frozen part geometry and the
    24-hour session expiry — and nothing else: no signed URL, staging key,
    provider identity, ETag, receipt or storage detail ever crosses with
    the plan. The client derives the status route from the fixed contract
    and the session ID alone.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str = Field(min_length=32, max_length=128)
    part_size_bytes: int = Field(gt=0)
    part_count: int = Field(gt=0)
    expires_at: datetime


class MultipartSessionStatusData(BaseModel):
    """The safe observable state of one multipart session (spec 4/5).

    The opaque session ID, the current state, the frozen geometry, the
    session expiry, the ordered provider-reconciled completed part numbers
    and — only once the session is committed — its frozen terminal
    source-event receipt. No digest, staging key, provider identity, URL or
    signed material is a member, so none can ever render.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str = Field(min_length=32, max_length=128)
    state: MultipartSessionState
    part_size_bytes: int = Field(gt=0)
    part_count: int = Field(gt=0)
    expires_at: datetime
    completed_part_numbers: tuple[int, ...]
    terminal_result: SmallFileTerminalResultData | None = None


class MultipartPartUrlData(BaseModel):
    """The one short-lived presigned part authorization (spec 4/5).

    The sole response model of the entire multipart surface that carries a
    ``url`` member: exactly one bearer URL, its own expiry, the numbered
    part and the exact derived byte window the single PUT must transmit.
    The response is never persisted by the client, never copied into any
    other model and never written to an application log; the URL value
    itself is the only field no other surface may render.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    part_number: int = Field(ge=1)
    offset_bytes: int = Field(ge=0)
    size_bytes: int = Field(gt=0)
    url: str = Field(min_length=1)
    expires_at: datetime


class MultipartCompletionData(BaseModel):
    """The safe result of one completion claim (spec 4.2/5).

    Either the session is still completing under its durable claimant and
    carries only its persisted state, or the claim finished and the frozen
    terminal source-event result returns unchanged on every exact replay.
    No URL, provider identity or digest is ever a member.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: MultipartSessionState
    terminal_result: SmallFileTerminalResultData | None = None


def to_multipart_session_preflight(body: MultipartSessionCreateRequest) -> SmallFilePreflight:
    """Convert one strict wire body into the frozen multipart preflight.

    Every field grammar stays owned by the shared small-file boundary — the
    create call binds the same frozen event the preflight decided, so the
    identical closed reason tokens apply — and this boundary adds the one
    multipart-owned rule: the declared size must sit strictly above the
    unchanged single-part routing constant and at or below the 100 MiB
    product maximum, because this endpoint mints only multipart sessions.
    The converted value never carries a workspace, device or user — those
    derive from the credential alone.
    """

    preflight = to_domain_preflight(body)
    if not MAX_SINGLE_PART_FILE_SIZE_BYTES < preflight.size_bytes <= MAX_UPLOAD_FILE_SIZE_BYTES:
        raise MultipartUploadError(ErrorCode.MULTIPART_PART_INVALID)
    return preflight


def multipart_session_plan_data(plan: MultipartUploadPlan) -> MultipartSessionPlanData:
    """Render the frozen session plan onto its strict wire payload."""

    return MultipartSessionPlanData(
        session_id=plan.session_id.value,
        part_size_bytes=plan.part_size_bytes,
        part_count=plan.part_count,
        expires_at=plan.expires_at,
    )


def multipart_session_status_data(status: MultipartSessionStatus) -> MultipartSessionStatusData:
    """Render the safe session status onto its strict wire payload.

    The completed part numbers render in ascending order — a stable,
    geometry-bounded set — and the frozen terminal receipt renders only
    when the committed session carries one, so no other state ever emits
    the member.
    """

    if status.terminal_result is None:
        return MultipartSessionStatusData(
            session_id=status.session_id.value,
            state=status.state,
            part_size_bytes=status.part_size_bytes,
            part_count=status.part_count,
            expires_at=status.expires_at,
            completed_part_numbers=tuple(sorted(status.completed_part_numbers)),
        )
    return MultipartSessionStatusData(
        session_id=status.session_id.value,
        state=status.state,
        part_size_bytes=status.part_size_bytes,
        part_count=status.part_count,
        expires_at=status.expires_at,
        completed_part_numbers=tuple(sorted(status.completed_part_numbers)),
        terminal_result=small_file_terminal_result_data(status.terminal_result),
    )


def multipart_part_url_data(part_url: MultipartPartUrl) -> MultipartPartUrlData:
    """Render the one presigned part authorization onto its strict payload.

    The sole renderer that touches a URL value; the derived byte window
    travels beside it so the client transmits exactly the authorized range.
    """

    return MultipartPartUrlData(
        part_number=part_url.part_number,
        offset_bytes=part_url.byte_range.offset_bytes,
        size_bytes=part_url.byte_range.size_bytes,
        url=part_url.url,
        expires_at=part_url.expires_at,
    )


def multipart_completion_data(result: MultipartCompletionResult) -> MultipartCompletionData:
    """Render one completion claim result onto its strict wire payload.

    The frozen terminal receipt renders only in the committed state, so an
    in-progress claim answers with its persisted state alone.
    """

    if result.terminal_result is None:
        return MultipartCompletionData(state=result.state)
    return MultipartCompletionData(
        state=result.state,
        terminal_result=small_file_terminal_result_data(result.terminal_result),
    )


__all__ = [
    "MultipartCompletionData",
    "MultipartPartUrlData",
    "MultipartSessionCreateRequest",
    "MultipartSessionPlanData",
    "MultipartSessionStatusData",
    "multipart_completion_data",
    "multipart_part_url_data",
    "multipart_session_plan_data",
    "multipart_session_status_data",
    "to_multipart_session_preflight",
]
