"""Multipart upload session endpoints (Child 7 spec 5).

The five endpoints are created per composed runtime: each closure binds the
multipart upload service and the device-token service of the composed web
authentication runtime, so the application factory only registers the
semantic operation ids and response models. The surface accepts exactly the
``obsidian_sync`` access Bearer credential — session cookies, refresh and
polling credentials close with the registered invalid-credential code — and
derives workspace and device from the resolved token context; no request
body or path parameter ever selects one. The create body converts to the
frozen domain value through the strict boundary models (the same closed
reason tokens the small-file preflight owns, plus the multipart routing
range), the opaque session ID grammar is validated at the boundary — the
path pattern plus the domain's raw-UUID exclusion — and the part number is
bounded by the maximum geometry before the service re-derives the exact
window. Responses carry the canonical envelope and ``Cache-Control:
no-store``; the one part-URL response is the sole surface a signed URL
renders on and is never copied into a status, completion or diagnostics
model, never logged and never persisted.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
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
from api_runtime.multipart_upload_composition import MultipartUploadRuntime
from api_runtime.multipart_upload_models import (
    MultipartCompletionData,
    MultipartPartUrlData,
    MultipartSessionCreateRequest,
    MultipartSessionPlanData,
    MultipartSessionStatusData,
    multipart_completion_data,
    multipart_part_url_data,
    multipart_session_plan_data,
    multipart_session_status_data,
    to_multipart_session_preflight,
)
from personal_os.api_contracts import ApiRouteTemplate, success_envelope
from personal_os.authentication.contracts import AuthenticatedDeviceContext, DeviceScope
from personal_os.authentication.errors import AuthenticationError
from personal_os.diagnostics.context import DiagnosticContext, current_diagnostic_context
from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import ApiTransportError
from personal_os.multipart_upload.contracts import (
    MAX_MULTIPART_PART_COUNT,
    MultipartUploadSessionId,
)
from personal_os.small_file_sync.contracts import SmallFileDeviceContext

#: Response headers every multipart upload response carries.
_NO_STORE_HEADERS: Final[dict[str, str]] = {"cache-control": "no-store"}

#: Wire grammar of the opaque session ID path parameter: printable URL-safe
#: base64url text of 32 to 128 characters. The boundary re-checks the domain
#: grammar — including the raw-canonical-UUID exclusion the pattern cannot
#: express — and closes a violation with the registered validation failure.
_SESSION_ID_PATTERN: Final[str] = r"^[A-Za-z0-9_-]{32,128}$"


@dataclass(frozen=True, slots=True)
class MultipartUploadRouteEndpoints:
    """The five endpoint callables of the closed multipart session route set."""

    create_session: Callable[..., Awaitable[JSONResponse]]
    get_session: Callable[..., Awaitable[JSONResponse]]
    issue_part_url: Callable[..., Awaitable[JSONResponse]]
    complete_session: Callable[..., Awaitable[JSONResponse]]
    abort_session: Callable[..., Awaitable[JSONResponse]]


def _bound_diagnostic_context() -> DiagnosticContext:
    """Return the diagnostic context owned by the request correlation middleware."""

    context = current_diagnostic_context()
    if context is None:
        raise RuntimeError("multipart upload routes require a bound request correlation context")
    return context


def _request_id() -> UUID:
    context = current_diagnostic_context()
    if context is None:
        raise RuntimeError("multipart upload routes require a bound request correlation context")
    return context.request_id


def _success_json(
    data: MultipartSessionPlanData
    | MultipartSessionStatusData
    | MultipartPartUrlData
    | MultipartCompletionData,
) -> JSONResponse:
    envelope = success_envelope(request_id=_request_id(), data=data)
    return JSONResponse(
        content=envelope.model_dump(mode="json", exclude_unset=True),
        status_code=200,
        headers=_NO_STORE_HEADERS,
    )


def _session_id_value(raw_session_id: str) -> MultipartUploadSessionId:
    """Validate the opaque session ID against the closed domain grammar."""

    try:
        return MultipartUploadSessionId(raw_session_id)
    except ValueError:
        raise ApiTransportError(ErrorCode.API_REQUEST_VALIDATION_FAILED) from None


def _device_context(device: AuthenticatedDeviceContext) -> SmallFileDeviceContext:
    return SmallFileDeviceContext(device_id=device.device_id, workspace_id=device.workspace_id)


def create_multipart_upload_route_endpoints(
    *,
    web_authentication: WebAuthenticationRuntime,
    multipart_upload: MultipartUploadRuntime,
) -> MultipartUploadRouteEndpoints:
    """Build the five multipart session endpoints over the composed runtimes."""

    service = multipart_upload.service

    async def require_sync_device(
        request: Request,
        authorization: HTTPAuthorizationCredentials | None = Depends(  # noqa: B008
            ACCESS_BEARER_SCHEME
        ),
    ) -> AuthenticatedDeviceContext:
        """Resolve the access Bearer credential and require the sync scope.

        The dedicated access scheme is the only authority these routes
        accept: cookies and every other credential are never read, so
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

    async def create_session(
        request: Request,
        body: MultipartSessionCreateRequest,
        device: AuthenticatedDeviceContext = Depends(  # noqa: B008
            require_sync_device
        ),
    ) -> JSONResponse:
        """Create or exactly replay the one session of a frozen operation."""
        request.scope["route_template"] = ApiRouteTemplate.UPLOAD_MULTIPART_SESSIONS
        preflight = to_multipart_session_preflight(body)
        plan = await service.create_or_resume(
            preflight=preflight,
            device_context=_device_context(device),
            diagnostic_context=_bound_diagnostic_context(),
        )
        return _success_json(multipart_session_plan_data(plan))

    async def get_session(
        request: Request,
        session_id: Annotated[str, Path(pattern=_SESSION_ID_PATTERN)],
        device: AuthenticatedDeviceContext = Depends(  # noqa: B008
            require_sync_device
        ),
    ) -> JSONResponse:
        """Return the safe observable state, reconciling forward sessions."""
        request.scope["route_template"] = ApiRouteTemplate.UPLOAD_MULTIPART_SESSION
        status = await service.status(
            session_id=_session_id_value(session_id),
            device_context=_device_context(device),
            diagnostic_context=_bound_diagnostic_context(),
        )
        return _success_json(multipart_session_status_data(status))

    async def issue_part_url(
        request: Request,
        session_id: Annotated[str, Path(pattern=_SESSION_ID_PATTERN)],
        part_number: Annotated[int, Path(ge=1, le=MAX_MULTIPART_PART_COUNT)],
        device: AuthenticatedDeviceContext = Depends(  # noqa: B008
            require_sync_device
        ),
    ) -> JSONResponse:
        """Recheck authority/policy/state and issue one short-lived part URL.

        The sole response of the surface carrying a signed URL: the value
        renders once on this uncacheable response and is never copied into
        any other model, log or persisted record.
        """
        request.scope["route_template"] = ApiRouteTemplate.UPLOAD_MULTIPART_SESSION_PART_URL
        part_url = await service.issue_part_url(
            session_id=_session_id_value(session_id),
            part_number=part_number,
            device_context=_device_context(device),
            diagnostic_context=_bound_diagnostic_context(),
        )
        return _success_json(multipart_part_url_data(part_url))

    async def complete_session(
        request: Request,
        session_id: Annotated[str, Path(pattern=_SESSION_ID_PATTERN)],
        device: AuthenticatedDeviceContext = Depends(  # noqa: B008
            require_sync_device
        ),
    ) -> JSONResponse:
        """Claim completion; provider-list parts, complete, verify and promote."""
        request.scope["route_template"] = ApiRouteTemplate.UPLOAD_MULTIPART_SESSION_COMPLETE
        result = await service.complete(
            session_id=_session_id_value(session_id),
            device_context=_device_context(device),
            diagnostic_context=_bound_diagnostic_context(),
        )
        return _success_json(multipart_completion_data(result))

    async def abort_session(
        request: Request,
        session_id: Annotated[str, Path(pattern=_SESSION_ID_PATTERN)],
        device: AuthenticatedDeviceContext = Depends(  # noqa: B008
            require_sync_device
        ),
    ) -> JSONResponse:
        """Terminalize user cancellation into the exact cleanup obligation."""
        request.scope["route_template"] = ApiRouteTemplate.UPLOAD_MULTIPART_SESSION_ABORT
        status = await service.abort(
            session_id=_session_id_value(session_id),
            device_context=_device_context(device),
            diagnostic_context=_bound_diagnostic_context(),
        )
        return _success_json(multipart_session_status_data(status))

    return MultipartUploadRouteEndpoints(
        create_session=create_session,
        get_session=get_session,
        issue_part_url=issue_part_url,
        complete_session=complete_session,
        abort_session=abort_session,
    )


__all__ = [
    "MultipartUploadRouteEndpoints",
    "create_multipart_upload_route_endpoints",
]
