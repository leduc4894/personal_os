"""Small-file sync plugin endpoints (spec 10.1-10.3).

The two endpoints are created per composed runtime: each closure binds the
small-file sync service, the device-token service of the composed web
authentication runtime and the request-bounded content-stream limiter, so the
application factory only registers the semantic operation ids and response
models. The surface accepts exactly the ``obsidian_sync`` access Bearer
credential — session cookies, refresh and polling credentials close with the
registered invalid-credential code — and derives workspace and device from
the resolved token context; no request body ever selects one. The preflight
body converts to the frozen domain value through the strict boundary models,
and the content route streams the raw request body through the limiter that
enforces the server-owned single-part ceiling and an explicit read deadline
before any byte can reach the spool/verification path, so an over-size or
stalled body can never publish. Responses carry the canonical envelope and
``Cache-Control: no-store`` and never expose a receipt, object key or
provider detail.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, Final
from uuid import UUID

from fastapi import Depends, Path, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials

from api_runtime.authentication_composition import WebAuthenticationRuntime
from api_runtime.authentication_dependencies import (
    ACCESS_BEARER_SCHEME,
    extract_bearer_credential,
)
from api_runtime.small_file_sync_composition import SmallFileSyncRuntime
from api_runtime.small_file_sync_models import (
    SmallFilePreflightData,
    SmallFilePreflightRequest,
    SmallFileTerminalResultData,
    small_file_preflight_data,
    small_file_terminal_result_data,
    to_domain_preflight,
)
from personal_os.api_contracts import ApiRouteTemplate, success_envelope
from personal_os.authentication.contracts import AuthenticatedDeviceContext, DeviceScope
from personal_os.authentication.errors import AuthenticationError
from personal_os.diagnostics.context import DiagnosticContext, current_diagnostic_context
from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import ApiTransportError
from personal_os.small_file_sync.contracts import (
    MAX_SINGLE_PART_FILE_SIZE_BYTES,
    SmallFileDeviceContext,
    UploadOperationToken,
)
from personal_os.small_file_sync.errors import SmallFileSyncError

#: Response headers every small-file sync response carries.
_NO_STORE_HEADERS: Final[dict[str, str]] = {"cache-control": "no-store"}

#: The bounded read window of one content stream: a body that cannot deliver
#: its bytes within this many seconds is a broken stream and closes with the
#: safe integrity failure without ever publishing.
CONTENT_READ_DEADLINE_SECONDS: Final[float] = 120.0

#: Wire grammar of the opaque operation token path parameter: printable
#: URL-safe base64url text of 32 to 128 characters. The boundary re-checks
#: the domain grammar — including the raw-canonical-UUID exclusion the
#: pattern cannot express — and closes a violation with the registered
#: validation failure.
_OPERATION_TOKEN_PATTERN: Final[str] = r"^[A-Za-z0-9_-]{32,128}$"


@dataclass(frozen=True, slots=True)
class SmallFileSyncRouteEndpoints:
    """The two endpoint callables of the closed small-file sync route set."""

    preflight_journal_event: Callable[..., Awaitable[JSONResponse]]
    upload_content: Callable[..., Awaitable[JSONResponse]]


async def bounded_content_stream(
    chunks: AsyncIterator[bytes],
    *,
    maximum_bytes: int,
    deadline_seconds: float,
    monotonic_clock: Callable[[], float],
) -> AsyncIterator[bytes]:
    """Yield request body chunks under the server-owned ceiling and deadline.

    The running byte count is checked against ``maximum_bytes`` — equality
    passes, one byte more closes with the registered size-limit rejection —
    and every per-chunk read is itself wrapped in a bounded wait for the
    remaining read-window time, so a client that streams partial bytes and
    then goes silent without closing the connection is cut off by the
    deadline rather than holding the handler forever: the stalled await
    closes with the safe integrity failure of the broken-stream contract
    (spec 10.2). Both failures abort the iteration before the consumer can
    hand any further byte to the spool path, so an over-size or stalled body
    can never publish.
    """

    deadline_at = monotonic_clock() + deadline_seconds
    total_bytes = 0
    while True:
        remaining_seconds = deadline_at - monotonic_clock()
        if remaining_seconds <= 0:
            raise SmallFileSyncError(ErrorCode.SMALL_FILE_CONTENT_INTEGRITY_FAILED)
        try:
            async with asyncio.timeout(remaining_seconds):
                chunk = await chunks.__anext__()
        except TimeoutError:
            raise SmallFileSyncError(ErrorCode.SMALL_FILE_CONTENT_INTEGRITY_FAILED) from None
        except StopAsyncIteration:
            return
        # Wire normalization (spec 10.2): a proxied chunked body may carry
        # zero-length data events; they hold no bytes and must never reach
        # the spool path, whose per-chunk contract rejects empty chunks.
        if not chunk:
            continue
        total_bytes += len(chunk)
        if total_bytes > maximum_bytes:
            raise SmallFileSyncError(ErrorCode.SMALL_FILE_SIZE_LIMIT_EXCEEDED)
        yield chunk


def _bound_diagnostic_context() -> DiagnosticContext:
    """Return the diagnostic context owned by the request correlation middleware."""

    context = current_diagnostic_context()
    if context is None:
        raise RuntimeError("small file sync routes require a bound request correlation context")
    return context


def _request_id() -> UUID:
    context = current_diagnostic_context()
    if context is None:
        raise RuntimeError("small file sync routes require a bound request correlation context")
    return context.request_id


def _success_json(data: SmallFilePreflightData | SmallFileTerminalResultData) -> JSONResponse:
    envelope = success_envelope(request_id=_request_id(), data=data)
    return JSONResponse(
        content=envelope.model_dump(mode="json", exclude_unset=True),
        status_code=200,
        headers=_NO_STORE_HEADERS,
    )


def create_small_file_sync_route_endpoints(
    *,
    web_authentication: WebAuthenticationRuntime,
    small_file_sync: SmallFileSyncRuntime,
    monotonic_clock: Callable[[], float] = time.monotonic,
) -> SmallFileSyncRouteEndpoints:
    """Build the two small-file sync endpoints over the composed runtimes."""

    service = small_file_sync.service

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
        workspace and device identity — never a request input.
        """
        del authorization  # the closed registry answers bad presentations
        credential = extract_bearer_credential(request)
        token = await web_authentication.device_token_service.authenticate_access(
            access_credential=credential
        )
        if token.context.scope is not DeviceScope.OBSIDIAN_SYNC:
            raise AuthenticationError(ErrorCode.AUTHORIZATION_SCOPE_DENIED)
        return token.context

    async def preflight_journal_event(
        request: Request,
        body: SmallFilePreflightRequest,
        device: AuthenticatedDeviceContext = Depends(  # noqa: B008
            require_sync_device
        ),
    ) -> JSONResponse:
        """Run one journal-event preflight and render its typed outcome."""
        request.scope["route_template"] = ApiRouteTemplate.SYNC_JOURNAL_EVENTS_PREFLIGHT
        preflight = to_domain_preflight(body)
        result = await service.preflight(
            preflight=preflight,
            device_context=SmallFileDeviceContext(
                device_id=device.device_id, workspace_id=device.workspace_id
            ),
            diagnostic_context=_bound_diagnostic_context(),
        )
        return _success_json(small_file_preflight_data(result))

    async def upload_content(
        request: Request,
        operation_id: Annotated[str, Path(pattern=_OPERATION_TOKEN_PATTERN)],
        device: AuthenticatedDeviceContext = Depends(  # noqa: B008
            require_sync_device
        ),
    ) -> JSONResponse:
        """Bind one raw content stream to its preflight-bound operation."""
        request.scope["route_template"] = ApiRouteTemplate.UPLOAD_CONTENT
        try:
            operation_token = UploadOperationToken(operation_id)
        except ValueError:
            raise ApiTransportError(ErrorCode.API_REQUEST_VALIDATION_FAILED) from None
        stream = bounded_content_stream(
            request.stream(),
            maximum_bytes=MAX_SINGLE_PART_FILE_SIZE_BYTES,
            deadline_seconds=CONTENT_READ_DEADLINE_SECONDS,
            monotonic_clock=monotonic_clock,
        )
        terminal = await service.receive(
            operation_token=operation_token,
            device_context=SmallFileDeviceContext(
                device_id=device.device_id, workspace_id=device.workspace_id
            ),
            stream=stream,
            diagnostic_context=_bound_diagnostic_context(),
        )
        return _success_json(small_file_terminal_result_data(terminal))

    return SmallFileSyncRouteEndpoints(
        preflight_journal_event=preflight_journal_event,
        upload_content=upload_content,
    )


__all__ = [
    "CONTENT_READ_DEADLINE_SECONDS",
    "SmallFileSyncRouteEndpoints",
    "bounded_content_stream",
    "create_small_file_sync_route_endpoints",
]
