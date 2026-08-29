"""Device sync plugin endpoints (spec 7.1-7.4).

The eight endpoints are created per composed runtime: each closure binds the
device sync service, the verified content service, the device-token service
of the composed web authentication runtime and the request-bounded
diagnostic context, so the application factory only registers the semantic
operation ids and response models. The surface accepts exactly the
``obsidian_sync`` access Bearer credential — session cookies, refresh and
polling credentials close with the registered invalid-credential code — and
derives workspace, device and user from the resolved token context; no
request field ever selects one. JSON responses carry the canonical envelope
and ``Cache-Control: no-store``; the binary download streams the already
verified exact bytes with their exact content headers, keeps the
verified-reader context open until the streaming generator closes, and a
mid-stream failure terminates the transport without ever attempting a
second JSON body — pre-stream failures stay canonical JSON envelopes because
the verified context is entered (and fully verified) inside the endpoint,
before any response start exists.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Annotated, Final
from uuid import UUID

from fastapi import Depends, Path, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials

from api_runtime.authentication_composition import WebAuthenticationRuntime
from api_runtime.authentication_dependencies import (
    ACCESS_BEARER_SCHEME,
    extract_bearer_credential,
)
from api_runtime.device_sync_composition import DeviceSyncRuntime
from api_runtime.device_sync_content import VerifiedDeviceContent
from api_runtime.device_sync_models import (
    CursorAcknowledgementRequest,
    DeviceCursorReceiptData,
    DeviceEventPageData,
    ManifestActionPageData,
    ManifestCompleteRequest,
    ManifestFinalizeRequest,
    ManifestPageReceiptData,
    ManifestPageRequest,
    ManifestRunReceiptData,
    ManifestStartRequest,
    device_cursor_receipt_data,
    device_event_page_data,
    manifest_action_page_data,
    manifest_page_receipt_data,
    manifest_run_receipt_data,
    parse_final_digest,
    parse_page_digest,
    to_domain_entries,
)
from personal_os.api_contracts import ApiRouteTemplate, success_envelope
from personal_os.authentication.contracts import AuthenticatedDeviceContext, DeviceScope
from personal_os.authentication.errors import AuthenticationError
from personal_os.device_sync.contracts import (
    MAX_MANIFEST_PAGE_ENTRIES,
    AppendManifestPageCommand,
    CompleteManifestCommand,
    DeviceSyncContext,
    FinalizeManifestCommand,
    ManifestActionsQuery,
    StartManifestCommand,
)
from personal_os.diagnostics.context import DiagnosticContext, current_diagnostic_context
from personal_os.error_contracts.codes import ErrorCode

#: Response headers every device sync response carries.
_NO_STORE_HEADERS: Final[dict[str, str]] = {"cache-control": "no-store"}

#: The content digest response header of the verified binary download.
_CONTENT_SHA256_HEADER: Final[str] = "x-content-sha256"


@dataclass(frozen=True, slots=True)
class DeviceSyncRouteEndpoints:
    """The eight endpoint callables of the closed device sync route set."""

    pull_events: Callable[..., Awaitable[JSONResponse]]
    acknowledge_cursor: Callable[..., Awaitable[JSONResponse]]
    start_manifest: Callable[..., Awaitable[JSONResponse]]
    append_manifest_page: Callable[..., Awaitable[JSONResponse]]
    finalize_manifest: Callable[..., Awaitable[JSONResponse]]
    list_manifest_actions: Callable[..., Awaitable[JSONResponse]]
    complete_manifest: Callable[..., Awaitable[JSONResponse]]
    download_source_version: Callable[..., Awaitable[StreamingResponse]]


async def verified_chunks(
    opened: AbstractAsyncContextManager[VerifiedDeviceContent],
    *,
    entered: list[VerifiedDeviceContent] | None = None,
) -> AsyncGenerator[bytes]:
    """Stream one verified content context, closing it exactly once.

    The verified-reader context stays open for the whole iteration and is
    closed on normal completion, on any error and on generator close, so the
    spool-backed reader never outlives its response. The optional ``entered``
    sink receives the verified content the moment the context is entered —
    after full digest, size and media verification — so the caller can prime
    the stream inside the endpoint (before any response start exists) and
    build the exact content headers from the verified descriptor.
    """

    async with opened as content:
        if entered is not None:
            entered.append(content)
        async for chunk in content.reader:
            yield chunk


def _bound_diagnostic_context() -> DiagnosticContext:
    """Return the diagnostic context owned by the request correlation middleware."""

    context = current_diagnostic_context()
    if context is None:
        raise RuntimeError("device sync routes require a bound request correlation context")
    return context


def _request_id() -> UUID:
    context = current_diagnostic_context()
    if context is None:
        raise RuntimeError("device sync routes require a bound request correlation context")
    return context.request_id


def _device_sync_context(device: AuthenticatedDeviceContext) -> DeviceSyncContext:
    """Map the bearer-resolved device context to the closed sync context.

    Workspace, device and user identities all derive exclusively from the
    authenticated bearer credential; a request field never picks any of them.
    """

    return DeviceSyncContext(
        workspace_id=device.workspace_id,
        device_id=device.device_id,
        user_id=device.user_id,
    )


def _success_json(
    data: (
        DeviceEventPageData
        | DeviceCursorReceiptData
        | ManifestRunReceiptData
        | ManifestPageReceiptData
        | ManifestActionPageData
    ),
) -> JSONResponse:
    envelope = success_envelope(request_id=_request_id(), data=data)
    return JSONResponse(
        content=envelope.model_dump(mode="json", exclude_unset=True),
        status_code=200,
        headers=_NO_STORE_HEADERS,
    )


def create_device_sync_route_endpoints(
    *,
    web_authentication: WebAuthenticationRuntime,
    device_sync: DeviceSyncRuntime,
) -> DeviceSyncRouteEndpoints:
    """Build the eight device sync endpoints over the composed runtimes."""

    service = device_sync.service
    content_service = device_sync.content

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
        workspace, device and user identity — never a request input.
        """

        del authorization  # the closed registry answers bad presentations
        credential = extract_bearer_credential(request)
        token = await web_authentication.device_token_service.authenticate_access(
            access_credential=credential
        )
        if token.context.scope is not DeviceScope.OBSIDIAN_SYNC:
            raise AuthenticationError(ErrorCode.AUTHORIZATION_SCOPE_DENIED)
        return token.context

    async def pull_events(
        request: Request,
        device: AuthenticatedDeviceContext = Depends(  # noqa: B008
            require_sync_device
        ),
    ) -> JSONResponse:
        """Pull one bounded page of immutable events after the acknowledged cursor."""

        request.scope["route_template"] = ApiRouteTemplate.SYNC_EVENTS
        page = await service.pull_events(
            context=_device_sync_context(device),
            diagnostic_context=_bound_diagnostic_context(),
        )
        return _success_json(device_event_page_data(page))

    async def acknowledge_cursor(
        request: Request,
        body: CursorAcknowledgementRequest,
        device: AuthenticatedDeviceContext = Depends(  # noqa: B008
            require_sync_device
        ),
    ) -> JSONResponse:
        """Advance the durable cursor after local terminalization."""

        request.scope["route_template"] = ApiRouteTemplate.SYNC_CURSOR_ACKNOWLEDGEMENTS
        receipt = await service.acknowledge_cursor(
            context=_device_sync_context(device),
            expected_previous_sequence=body.expected_previous_sequence,
            applied_through_sequence=body.applied_through_sequence,
            diagnostic_context=_bound_diagnostic_context(),
        )
        return _success_json(device_cursor_receipt_data(receipt))

    async def start_manifest(
        request: Request,
        body: ManifestStartRequest,
        device: AuthenticatedDeviceContext = Depends(  # noqa: B008
            require_sync_device
        ),
    ) -> JSONResponse:
        """Start or exactly resume the device's manifest run."""

        request.scope["route_template"] = ApiRouteTemplate.SYNC_MANIFESTS
        receipt = await service.start_manifest(
            StartManifestCommand(
                context=_device_sync_context(device),
                client_observation_generation=body.client_observation_generation,
                diagnostic_context=_bound_diagnostic_context(),
            )
        )
        return _success_json(manifest_run_receipt_data(receipt))

    async def append_manifest_page(
        request: Request,
        body: ManifestPageRequest,
        manifest_run_id: UUID,
        page_number: Annotated[int, Path(ge=0)],
        device: AuthenticatedDeviceContext = Depends(  # noqa: B008
            require_sync_device
        ),
    ) -> JSONResponse:
        """Put the exact next ordered page of one manifest run."""

        request.scope["route_template"] = ApiRouteTemplate.SYNC_MANIFEST_PAGES
        command = AppendManifestPageCommand(
            context=_device_sync_context(device),
            manifest_run_id=manifest_run_id,
            page_number=page_number,
            entries=to_domain_entries(body.entries),
            page_digest=parse_page_digest(body.page_digest),
            diagnostic_context=_bound_diagnostic_context(),
        )
        receipt = await service.append_manifest_page(command)
        return _success_json(manifest_page_receipt_data(receipt))

    async def finalize_manifest(
        request: Request,
        body: ManifestFinalizeRequest,
        manifest_run_id: UUID,
        device: AuthenticatedDeviceContext = Depends(  # noqa: B008
            require_sync_device
        ),
    ) -> JSONResponse:
        """Finalize one run with its total count and final digest."""

        request.scope["route_template"] = ApiRouteTemplate.SYNC_MANIFEST_FINALIZE
        command = FinalizeManifestCommand(
            context=_device_sync_context(device),
            manifest_run_id=manifest_run_id,
            total_entry_count=body.total_entry_count,
            final_digest=parse_final_digest(body.final_digest),
            diagnostic_context=_bound_diagnostic_context(),
        )
        receipt = await service.finalize_manifest(command)
        return _success_json(manifest_run_receipt_data(receipt))

    async def list_manifest_actions(
        request: Request,
        manifest_run_id: UUID,
        after_action_index: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[
            int, Query(ge=1, le=MAX_MANIFEST_PAGE_ENTRIES)
        ] = MAX_MANIFEST_PAGE_ENTRIES,
        device: AuthenticatedDeviceContext = Depends(  # noqa: B008
            require_sync_device
        ),
    ) -> JSONResponse:
        """Read one deterministic action page of the planned run."""

        request.scope["route_template"] = ApiRouteTemplate.SYNC_MANIFEST_ACTIONS
        query = ManifestActionsQuery(
            context=_device_sync_context(device),
            manifest_run_id=manifest_run_id,
            after_action_index=after_action_index,
            limit=limit,
            diagnostic_context=_bound_diagnostic_context(),
        )
        page = await service.read_manifest_actions(query)
        return _success_json(manifest_action_page_data(page))

    async def complete_manifest(
        request: Request,
        body: ManifestCompleteRequest,
        manifest_run_id: UUID,
        device: AuthenticatedDeviceContext = Depends(  # noqa: B008
            require_sync_device
        ),
    ) -> JSONResponse:
        """Complete the exact planned run and advance the cursor."""

        request.scope["route_template"] = ApiRouteTemplate.SYNC_MANIFEST_COMPLETE
        command = CompleteManifestCommand(
            context=_device_sync_context(device),
            manifest_run_id=manifest_run_id,
            final_digest=parse_final_digest(body.final_digest),
            diagnostic_context=_bound_diagnostic_context(),
        )
        receipt = await service.complete_manifest(command)
        return _success_json(device_cursor_receipt_data(receipt))

    async def _continued(primed: bytes, stream: AsyncGenerator[bytes]) -> AsyncIterator[bytes]:
        """Yield the primed first chunk, then the remainder of the stream."""

        try:
            if primed:
                yield primed
            async for chunk in stream:
                yield chunk
        finally:
            # Client disconnect closes only this outer generator; the inner
            # verified-chunks generator owns the opened reader context and
            # must close deterministically instead of by GC.
            await stream.aclose()

    async def download_source_version(
        request: Request,
        source_id: UUID,
        source_version_id: UUID,
        device: AuthenticatedDeviceContext = Depends(  # noqa: B008
            require_sync_device
        ),
    ) -> StreamingResponse:
        """Stream the exact verified bytes of one exact source version.

        The verified content context is entered — and fully verified — by the
        primed first read below, still inside the endpoint, so membership,
        policy and integrity failures render the canonical JSON envelope
        instead of a broken stream. Only then does the response start carry
        the exact content headers; the context stays open until the streaming
        generator closes, and a mid-stream failure terminates the transport
        without ever attempting a second JSON body.
        """

        request.scope["route_template"] = ApiRouteTemplate.SYNC_SOURCE_VERSION_CONTENT
        entered: list[VerifiedDeviceContent] = []
        stream = verified_chunks(
            content_service.open_content(
                _device_sync_context(device),
                source_id=source_id,
                source_version_id=source_version_id,
                diagnostic_context=_bound_diagnostic_context(),
            ),
            entered=entered,
        )
        try:
            primed = await stream.__anext__()
        except StopAsyncIteration:
            primed = b""
        content = entered[0]
        descriptor = content.descriptor
        return StreamingResponse(
            _continued(primed, stream),
            status_code=200,
            # The wire contract carries the descriptor's EXACT canonical
            # media type: the client verifies the header equals the frozen
            # fingerprint's closed `type/subtype` value. Starlette's
            # `media_type` helper would append `; charset=utf-8` to every
            # text/* type — a parameterized value the client's closed check
            # (correctly) rejects, which the live Desktop gate proved as
            # `device_download_integrity_failed`. Setting the header
            # explicitly keeps it verbatim.
            headers={
                "content-type": descriptor.media_type.value,
                "content-length": str(descriptor.size_bytes),
                _CONTENT_SHA256_HEADER: descriptor.content_digest.hexadecimal,
                # The exact byte stream also forbids intermediary
                # re-encoding: the live Desktop gate proved a compressing
                # edge response drops the explicit Content-Length and fails
                # the client's size verification.
                "cache-control": "no-store, no-transform",
            },
        )

    return DeviceSyncRouteEndpoints(
        pull_events=pull_events,
        acknowledge_cursor=acknowledge_cursor,
        start_manifest=start_manifest,
        append_manifest_page=append_manifest_page,
        finalize_manifest=finalize_manifest,
        list_manifest_actions=list_manifest_actions,
        complete_manifest=complete_manifest,
        download_source_version=download_source_version,
    )


__all__ = [
    "DeviceSyncRouteEndpoints",
    "create_device_sync_route_endpoints",
    "verified_chunks",
]
