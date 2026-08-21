"""Source lifecycle plugin endpoint (spec 19.2).

The single endpoint is created per composed runtime: the closure binds the
:class:`SourceLifecycleService`, the device-token service of the composed web
authentication runtime and the closed request-bounded diagnostic context,
so the application factory only registers the semantic operation id and
the envelope response model. The surface accepts exactly the
``obsidian_sync`` access Bearer credential — session cookies, refresh and
polling credentials close with the registered invalid-credential code —
and derives workspace, device and user from the resolved token context; the
request body never selects one. The wire model converts to the frozen
domain command through the strict boundary validator, the service
orchestrates the replay preflight, the policy evaluation and the atomic
store commit, and the response renderer projects the canonical commit
result onto the strict ``SourceLifecycleCommitData`` envelope. Every
response carries ``Cache-Control: no-store`` and never exposes a
fingerprint, locator text, signed payload, policy decision or canonical
envelope.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final
from uuid import UUID

from fastapi import Depends, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials

from api_runtime.authentication_composition import WebAuthenticationRuntime
from api_runtime.authentication_dependencies import (
    ACCESS_BEARER_SCHEME,
    extract_bearer_credential,
)
from api_runtime.source_lifecycle_composition import SourceLifecycleRuntime
from api_runtime.source_lifecycle_models import (
    SourceLifecycleCommitData,
    SourceLifecycleEventRequest,
    source_lifecycle_commit_data,
    to_domain_command,
)
from personal_os.api_contracts import ApiRouteTemplate, success_envelope
from personal_os.authentication.contracts import AuthenticatedDeviceContext, DeviceScope
from personal_os.authentication.errors import AuthenticationError
from personal_os.diagnostics.context import DiagnosticContext, current_diagnostic_context
from personal_os.error_contracts.codes import ErrorCode
from personal_os.source_lifecycle.commands import SourceLifecycleCommand
from personal_os.source_lifecycle.errors import (
    SourceLifecycleError,
    lifecycle_application_error_for,
)
from personal_os.source_lifecycle.ports import LifecycleDeviceContext

#: Response headers every source lifecycle response carries (spec 19.2).
_NO_STORE_HEADERS: Final[dict[str, str]] = {"cache-control": "no-store"}


@dataclass(frozen=True, slots=True)
class SourceLifecycleRouteEndpoints:
    """The single endpoint callable of the closed lifecycle route set."""

    commit_source_lifecycle_event: Callable[..., Awaitable[JSONResponse]]


def _bound_diagnostic_context() -> DiagnosticContext:
    """Return the diagnostic context owned by the request correlation middleware."""

    context = current_diagnostic_context()
    if context is None:
        raise RuntimeError("source lifecycle routes require a bound request correlation context")
    return context


def _request_id() -> UUID:
    context = current_diagnostic_context()
    if context is None:
        raise RuntimeError("source lifecycle routes require a bound request correlation context")
    return context.request_id


def _device_context(device: AuthenticatedDeviceContext) -> LifecycleDeviceContext:
    """Map the bearer-resolved device context to the closed lifecycle device context.

    Workspace, device and user identities all derive exclusively from the
    authenticated bearer credential; a request body never picks any of them.
    """

    return LifecycleDeviceContext(
        workspace_id=device.workspace_id,
        device_id=device.device_id,
        user_id=device.user_id,
        device_kind="obsidian",
    )


def _success_json(data: SourceLifecycleCommitData) -> JSONResponse:
    envelope = success_envelope(request_id=_request_id(), data=data)
    return JSONResponse(
        content=envelope.model_dump(mode="json", exclude_unset=True),
        status_code=200,
        headers=_NO_STORE_HEADERS,
    )


def create_source_lifecycle_route_endpoints(
    *,
    web_authentication: WebAuthenticationRuntime,
    source_lifecycle: SourceLifecycleRuntime,
) -> SourceLifecycleRouteEndpoints:
    """Build the closed lifecycle route over the two composed runtimes.

    The route is a thin adapter: it resolves the access Bearer to a device
    context, converts the validated wire body to a frozen domain command
    through the shared boundary validator, hands the command to the service
    and projects the canonical commit result onto the strict envelope.
    Typed domain rejections translate to the registered
    :class:`SourceLifecycleApplicationError`, the framework's request
    validation envelope, or the closed invalid-credential / scope-denied
    envelopes — never echoing a fingerprint, locator text or content.
    """

    service = source_lifecycle.service

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

    async def commit_source_lifecycle_event(
        request: Request,
        body: SourceLifecycleEventRequest,
        device: AuthenticatedDeviceContext = Depends(  # noqa: B008
            require_sync_device
        ),
    ) -> JSONResponse:
        """Commit one source lifecycle event behind the OBSIDIAN_SYNC scope."""

        request.scope["route_template"] = ApiRouteTemplate.SYNC_SOURCE_LIFECYCLE_EVENTS
        command: SourceLifecycleCommand = to_domain_command(body)
        device_context = _device_context(device)
        try:
            result = await service.commit(
                command=command,
                device_context=device_context,
                diagnostic_context=_bound_diagnostic_context(),
            )
        except SourceLifecycleError as error:
            raise lifecycle_application_error_for(error.code) from error
        return _success_json(source_lifecycle_commit_data(result))

    return SourceLifecycleRouteEndpoints(
        commit_source_lifecycle_event=commit_source_lifecycle_event,
    )


__all__ = [
    "SourceLifecycleRouteEndpoints",
    "create_source_lifecycle_route_endpoints",
]
