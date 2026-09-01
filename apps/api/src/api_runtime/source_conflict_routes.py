"""Source conflict Conflict Inbox endpoints (Child 8 spec 6).

The four endpoints are created per composed runtime: each closure binds the
source conflict service, the conflict store, the policy guard, the verified
evidence reader and its catalog, plus the device-token service of the
composed web authentication runtime, so the application factory only
registers the semantic operation ids and response models. The surface
accepts exactly the ``obsidian_sync`` access Bearer credential — session
cookies, refresh and polling credentials close with the registered
invalid-credential code — and derives the workspace from the resolved token
context; no request field ever selects one, so no call can reach a
cross-workspace conflict. The routes are thin adapters: they validate wire
data through the strict boundary models, map closed domain errors, and
carry no conflict business logic. The evidence stream re-reads the
conflict inside the credential workspace, re-evaluates the exclusion
policy over exactly that read, and only then resolves the exact expected
object and opens the verified reader — the first chunk is primed inside
the endpoint, so membership, policy and integrity failures render the
canonical JSON envelope instead of a broken stream, and the exact bytes
carry their exact canonical content headers. Responses carry the canonical
envelope and ``Cache-Control: no-store`` and never expose a locator, raw
object key, digest, provider receipt or any cross-workspace identity.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, Final, cast
from uuid import UUID

from fastapi import Depends, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials

from api_runtime.authentication_composition import WebAuthenticationRuntime
from api_runtime.authentication_dependencies import (
    ACCESS_BEARER_SCHEME,
    extract_bearer_credential,
)
from api_runtime.source_conflict_composition import SourceConflictRuntime
from api_runtime.source_conflict_models import (
    DEFAULT_CONFLICT_PAGE_LIMIT,
    MAX_CONFLICT_PAGE_LIMIT,
    SourceConflictDetailData,
    SourceConflictPageData,
    SourceConflictResolutionData,
    SourceConflictResolveRequest,
    allowed_resolution_choices,
    source_conflict_data,
    source_conflict_detail_data,
    source_conflict_resolution_data,
    to_domain_resolve_command,
)
from personal_os.api_contracts import ApiRouteTemplate, success_envelope
from personal_os.authentication.contracts import AuthenticatedDeviceContext, DeviceScope
from personal_os.authentication.errors import AuthenticationError
from personal_os.diagnostics.context import DiagnosticContext, current_diagnostic_context
from personal_os.error_contracts.codes import ErrorCode
from personal_os.source_conflicts.contracts import (
    ConflictCandidateKind,
    ConflictEvidenceRole,
    ConflictResolutionKind,
    ConflictStatus,
    SourceConflict,
)
from personal_os.source_conflicts.errors import SourceConflictError

#: Response headers every JSON source conflict response carries.
_NO_STORE_HEADERS: Final[dict[str, str]] = {"cache-control": "no-store"}


@dataclass(frozen=True, slots=True)
class SourceConflictRouteEndpoints:
    """The four endpoint callables of the closed source conflict route set."""

    list_conflicts: Callable[..., Awaitable[JSONResponse]]
    get_conflict: Callable[..., Awaitable[JSONResponse]]
    download_evidence: Callable[..., Awaitable[StreamingResponse]]
    resolve_conflict: Callable[..., Awaitable[JSONResponse]]


def _bound_diagnostic_context() -> DiagnosticContext:
    """Return the diagnostic context owned by the request correlation middleware."""

    context = current_diagnostic_context()
    if context is None:
        raise RuntimeError("source conflict routes require a bound request correlation context")
    return context


def _request_id() -> UUID:
    context = current_diagnostic_context()
    if context is None:
        raise RuntimeError("source conflict routes require a bound request correlation context")
    return context.request_id


def _success_json(
    data: SourceConflictPageData | SourceConflictDetailData | SourceConflictResolutionData,
) -> JSONResponse:
    envelope = success_envelope(request_id=_request_id(), data=data)
    return JSONResponse(
        content=envelope.model_dump(mode="json", exclude_unset=True),
        status_code=200,
        headers=_NO_STORE_HEADERS,
    )


def create_source_conflict_route_endpoints(
    *,
    web_authentication: WebAuthenticationRuntime,
    source_conflicts: SourceConflictRuntime,
) -> SourceConflictRouteEndpoints:
    """Build the four source conflict endpoints over the composed runtimes."""

    service = source_conflicts.service
    store = source_conflicts.store
    policy_guard = source_conflicts.policy_guard
    evidence_reader = source_conflicts.evidence
    evidence_catalog = source_conflicts.evidence_catalog

    async def require_sync_device(
        request: Request,
        authorization: HTTPAuthorizationCredentials | None = Depends(  # noqa: B008
            ACCESS_BEARER_SCHEME
        ),
    ) -> AuthenticatedDeviceContext:
        """Resolve the access Bearer credential and require the sync scope.

        The dedicated access scheme of spec 16 is the only authority these
        routes accept: cookies and every other credential are never read, so
        presenting them changes nothing. The resolved context carries the
        workspace identity — never a request input.
        """

        del authorization  # the closed registry answers bad presentations
        credential = extract_bearer_credential(request)
        token = await web_authentication.device_token_service.authenticate_access(
            access_credential=credential
        )
        if token.context.scope is not DeviceScope.OBSIDIAN_SYNC:
            raise AuthenticationError(ErrorCode.AUTHORIZATION_SCOPE_DENIED)
        return token.context

    async def list_conflicts(
        request: Request,
        limit: Annotated[
            int, Query(ge=1, le=MAX_CONFLICT_PAGE_LIMIT)
        ] = DEFAULT_CONFLICT_PAGE_LIMIT,
        exclusive_start_conflict_id: UUID | None = None,
        device: AuthenticatedDeviceContext = Depends(  # noqa: B008
            require_sync_device
        ),
    ) -> JSONResponse:
        """Page the credential workspace's open conflicts in stable order."""

        request.scope["route_template"] = ApiRouteTemplate.SYNC_CONFLICTS
        conflicts = await store.list_open(
            device.workspace_id,
            limit=limit,
            exclusive_start_conflict_id=exclusive_start_conflict_id,
            diagnostic_context=_bound_diagnostic_context(),
        )
        has_more = len(conflicts) == limit
        return _success_json(
            SourceConflictPageData(
                conflicts=tuple(source_conflict_data(conflict) for conflict in conflicts),
                has_more=has_more,
                next_exclusive_start_conflict_id=(
                    conflicts[-1].conflict_id if has_more and conflicts else None
                ),
            )
        )

    async def get_conflict(
        request: Request,
        conflict_id: UUID,
        device: AuthenticatedDeviceContext = Depends(  # noqa: B008
            require_sync_device
        ),
    ) -> JSONResponse:
        """Render one conflict's safe metadata, choices and evidence identity."""

        request.scope["route_template"] = ApiRouteTemplate.SYNC_CONFLICT
        diagnostic_context = _bound_diagnostic_context()
        conflict = await store.read(conflict_id, device.workspace_id, diagnostic_context)
        choices = await _allowed_choices(conflict, device.workspace_id, diagnostic_context)
        return _success_json(source_conflict_detail_data(conflict, choices=choices))

    async def _allowed_choices(
        conflict: SourceConflict, workspace_id: UUID, diagnostic_context: DiagnosticContext
    ) -> tuple[ConflictResolutionKind, ...]:
        """Derive the offered choices behind the candidate's resolved media type.

        A byteless candidate offers ``keep_remote`` without any catalog
        call, and a terminal conflict offers none; only an open content
        candidate resolves its canonical media type, failing closed to the
        two whole-object choices when the descriptor is unavailable.
        """

        if conflict.status is not ConflictStatus.OPEN:
            return ()
        if conflict.candidate.candidate_kind is not ConflictCandidateKind.CONTENT:
            return allowed_resolution_choices(conflict, candidate_media_type=None)
        try:
            descriptor = await evidence_catalog.describe_evidence(
                conflict.conflict_id,
                ConflictEvidenceRole.CANDIDATE,
                workspace_id,
                diagnostic_context,
            )
        except SourceConflictError as error:
            if error.error_code is ErrorCode.SOURCE_CONFLICT_EVIDENCE_UNAVAILABLE:
                return allowed_resolution_choices(conflict, candidate_media_type=None)
            raise
        return allowed_resolution_choices(conflict, candidate_media_type=descriptor.media_type)

    async def _continued(primed: bytes, stream: AsyncGenerator[bytes]) -> AsyncIterator[bytes]:
        """Yield the primed first chunk, then the remainder of the stream."""

        try:
            if primed:
                yield primed
            async for chunk in stream:
                yield chunk
        finally:
            # Client disconnect closes only this outer generator; the inner
            # evidence stream owns the opened reader context and must close
            # deterministically instead of by GC.
            await stream.aclose()

    async def download_evidence(
        request: Request,
        conflict_id: UUID,
        role: ConflictEvidenceRole,
        device: AuthenticatedDeviceContext = Depends(  # noqa: B008
            require_sync_device
        ),
    ) -> StreamingResponse:
        """Stream one role's exact verified evidence bytes.

        The conflict is re-read inside the credential workspace and the
        exclusion policy re-evaluated over exactly that read before the
        verified reader opens: a denial, an unknown conflict or an
        unappliable role answers the canonical JSON envelope with the
        reader still closed. Only then does the exact expected object
        resolve, the verified reader open and prime its first chunk inside
        the endpoint, so pre-stream failures never start a broken
        transport. The response carries the exact canonical media type and
        byte length of the verified bytes.
        """

        request.scope["route_template"] = ApiRouteTemplate.SYNC_CONFLICT_EVIDENCE
        diagnostic_context = _bound_diagnostic_context()
        conflict = await store.read(conflict_id, device.workspace_id, diagnostic_context)
        await policy_guard.authorize_resolution(conflict, diagnostic_context)
        descriptor = await evidence_catalog.describe_evidence(
            conflict_id, role, device.workspace_id, diagnostic_context
        )
        stream = evidence_reader.open_evidence_stream(
            conflict_id, role, device.workspace_id, diagnostic_context
        )
        # The reader port returns the structural ``AsyncIterator`` shape;
        # both concrete readers are async generators, so the deterministic
        # close of the streaming helper is sound over the widened type.
        generator_stream = cast("AsyncGenerator[bytes]", stream)
        try:
            primed = await generator_stream.__anext__()
        except StopAsyncIteration:
            primed = b""
        return StreamingResponse(
            _continued(primed, generator_stream),
            status_code=200,
            headers={
                # The wire contract carries the descriptor's EXACT canonical
                # media type and byte length: the plugin verifies the header
                # equals the closed `type/subtype` value, and Starlette's
                # `media_type` helper would append `; charset=utf-8` to
                # every text/* type.
                "content-type": descriptor.media_type,
                "content-length": str(descriptor.size_bytes),
                # The exact byte stream also forbids intermediary
                # re-encoding, mirroring the verified download contract.
                "cache-control": "no-store, no-transform",
            },
        )

    async def resolve_conflict(
        request: Request,
        conflict_id: UUID,
        body: SourceConflictResolveRequest,
        device: AuthenticatedDeviceContext = Depends(  # noqa: B008
            require_sync_device
        ),
    ) -> JSONResponse:
        """Resolve one conflict behind the policy recheck, atomically."""

        request.scope["route_template"] = ApiRouteTemplate.SYNC_CONFLICT_RESOLUTION
        command = to_domain_resolve_command(body, conflict_id=conflict_id)
        result = await service.resolve_conflict(
            command,
            workspace_id=device.workspace_id,
            diagnostic_context=_bound_diagnostic_context(),
        )
        return _success_json(source_conflict_resolution_data(result))

    return SourceConflictRouteEndpoints(
        list_conflicts=list_conflicts,
        get_conflict=get_conflict,
        download_evidence=download_evidence,
        resolve_conflict=resolve_conflict,
    )


__all__ = [
    "SourceConflictRouteEndpoints",
    "create_source_conflict_route_endpoints",
]
