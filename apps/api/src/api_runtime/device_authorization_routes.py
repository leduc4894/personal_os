"""Browser device-authorization endpoints: envelopes and closed errors (spec 11).

The four endpoints of the spec 16.3 device-authorization route set this task
owns are created per composed runtime: each closure binds the runtime's
device-authorization service, session dependencies and cookie contract, so
the application factory only registers semantic operation ids and response
models. The unauthenticated plugin creation endpoint keeps the exact-origin
gate and throttles per source address; the browser lookup and decision
endpoints reuse the strict CSRF dependency of spec 9.3 verbatim. Every
response carries the canonical envelope and ``Cache-Control: no-store``; the
creation response also carries ``Pragma: no-cache`` because it is the one
provisioning surface that renders the user code and polling secret (spec 16).
Service rejections raise the typed authentication error and reach the shared
application handler, which maps them onto the closed registry codes (spec 17)
with the same cache-suppression posture.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final
from uuid import UUID

from fastapi import Depends, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from api_runtime.authentication_composition import WebAuthenticationRuntime
from api_runtime.authentication_dependencies import (
    AuthenticatedWebRequest,
    client_source_address,
    create_session_route_dependencies,
    require_polling_credential,
)
from api_runtime.authentication_models import (
    DeviceGrantContextData,
    DeviceGrantData,
    DeviceGrantDecisionData,
    DeviceGrantExchangeData,
    DeviceGrantLookupRequest,
    DeviceGrantRequest,
)
from personal_os.api_contracts import (
    ApiRouteTemplate,
    success_envelope,
)
from personal_os.diagnostics.context import DiagnosticContext, current_diagnostic_context

#: Response headers every authentication response carries (spec 16).
_NO_STORE_HEADERS: Final[dict[str, str]] = {"cache-control": "no-store"}

#: The provisioning creation response adds the no-cache pragma (spec 16).
_NO_STORE_NO_CACHE_HEADERS: Final[dict[str, str]] = {
    "cache-control": "no-store",
    "pragma": "no-cache",
}

#: The dedicated OpenAPI security scheme of the polling Bearer credential
#: (spec 16): the only authority the poll route accepts. The scheme never
#: auto-rejects so the closed registry code answers every bad presentation.
_POLLING_BEARER_SCHEME = HTTPBearer(
    scheme_name="devicePollingCredential",
    description="The pg1 polling credential of one device-authorization grant",
    auto_error=False,
)


@dataclass(frozen=True, slots=True)
class DeviceAuthorizationRouteEndpoints:
    """The five endpoint callables of the closed device-authorization set."""

    create_grant: Callable[..., Awaitable[JSONResponse]]
    lookup_grant: Callable[..., Awaitable[JSONResponse]]
    approve_grant: Callable[..., Awaitable[JSONResponse]]
    deny_grant: Callable[..., Awaitable[JSONResponse]]
    poll_grant: Callable[..., Awaitable[JSONResponse]]


def _bound_diagnostic_context() -> DiagnosticContext:
    """Return the diagnostic context owned by the request correlation middleware."""
    context = current_diagnostic_context()
    if context is None:
        raise RuntimeError(
            "device authorization routes require a bound request correlation context"
        )
    return context


def create_device_authorization_route_endpoints(
    runtime: WebAuthenticationRuntime,
) -> DeviceAuthorizationRouteEndpoints:
    """Build the four device-authorization endpoints over one runtime."""
    dependencies = create_session_route_dependencies(runtime)

    def _request_id() -> UUID:
        context = current_diagnostic_context()
        if context is None:
            raise RuntimeError(
                "device authorization routes require a bound request correlation context"
            )
        return context.request_id

    def _success_json(
        data: DeviceGrantData
        | DeviceGrantContextData
        | DeviceGrantDecisionData
        | DeviceGrantExchangeData,
        *,
        headers: dict[str, str] = _NO_STORE_HEADERS,
    ) -> JSONResponse:
        envelope = success_envelope(request_id=_request_id(), data=data)
        return JSONResponse(content=envelope.model_dump(mode="json"), headers=headers)

    async def create_grant(
        request: Request,
        grant_request: DeviceGrantRequest,
        origin_guard: None = Depends(dependencies.require_allowed_origin),
    ) -> JSONResponse:
        """Create one grant and render the one-time provisioning payload."""
        del origin_guard
        request.scope["route_template"] = ApiRouteTemplate.AUTH_DEVICE_AUTHORIZATIONS
        created = await runtime.device_authorization_service.create_grant(
            client_instance_id=grant_request.client_instance_id,
            device_name=grant_request.device_name,
            platform_class=grant_request.platform_class,
            platform_name=grant_request.platform_name,
            plugin_version=grant_request.plugin_version,
            requested_scope=grant_request.requested_scope,
            claimed_device_id=grant_request.claimed_device_id,
            source_bucket=client_source_address(request),
            diagnostic_context=_bound_diagnostic_context(),
        )
        return _success_json(
            DeviceGrantData(
                grant_id=created.grant_id,
                user_code=created.user_code,
                polling_secret=created.polling_secret,
                verification_uri=created.verification_uri,
                verification_uri_complete=created.verification_uri_complete,
                expires_in_seconds=created.expires_in_seconds,
                poll_interval_seconds=created.poll_interval_seconds,
            ),
            headers=_NO_STORE_NO_CACHE_HEADERS,
        )

    async def lookup_grant(
        request: Request,
        lookup_request: DeviceGrantLookupRequest,
        authentication: AuthenticatedWebRequest = Depends(  # noqa: B008
            dependencies.require_csrf_protected_request
        ),
    ) -> JSONResponse:
        """Resolve one user code to its approval-page display context."""
        request.scope["route_template"] = ApiRouteTemplate.AUTH_DEVICE_AUTHORIZATION_LOOKUP
        resolved = await runtime.device_authorization_service.lookup_grant(
            user_code=lookup_request.user_code,
            user_id=authentication.context.user_id,
            diagnostic_context=_bound_diagnostic_context(),
        )
        return _success_json(
            DeviceGrantContextData(
                grant_id=resolved.grant_id,
                user_code=resolved.user_code,
                device_name=resolved.device_name,
                platform_class=resolved.platform_class,
                platform_name=resolved.platform_name,
                plugin_version=resolved.plugin_version,
                requested_scope=resolved.requested_scope,
                expires_at=resolved.expires_at,
            )
        )

    async def approve_grant(
        request: Request,
        grant_id: UUID,
        authentication: AuthenticatedWebRequest = Depends(  # noqa: B008
            dependencies.require_csrf_protected_request
        ),
    ) -> JSONResponse:
        """Approve one grant behind the recent-authentication gate (11.3)."""
        request.scope["route_template"] = ApiRouteTemplate.AUTH_DEVICE_AUTHORIZATION_APPROVE
        approved = await runtime.device_authorization_service.approve_grant(
            grant_id=grant_id,
            session_secret=authentication.session_secret,
            diagnostic_context=_bound_diagnostic_context(),
        )
        return _success_json(
            DeviceGrantDecisionData(
                grant_id=approved.grant_id,
                state=approved.state,
                decided_at=approved.approved_at,
            )
        )

    async def deny_grant(
        request: Request,
        grant_id: UUID,
        authentication: AuthenticatedWebRequest = Depends(  # noqa: B008
            dependencies.require_csrf_protected_request
        ),
    ) -> JSONResponse:
        """Deny one grant: explicit, terminal, no recent window (spec 11.3)."""
        request.scope["route_template"] = ApiRouteTemplate.AUTH_DEVICE_AUTHORIZATION_DENY
        denied = await runtime.device_authorization_service.deny_grant(
            grant_id=grant_id,
            session_secret=authentication.session_secret,
            diagnostic_context=_bound_diagnostic_context(),
        )
        return _success_json(
            DeviceGrantDecisionData(
                grant_id=denied.grant_id,
                state=denied.state,
                decided_at=denied.denied_at,
            )
        )

    async def poll_grant(
        request: Request,
        grant_id: UUID,
        polling_credential: str = Depends(require_polling_credential),
        authorization: HTTPAuthorizationCredentials | None = Depends(  # noqa: B008
            _POLLING_BEARER_SCHEME
        ),
    ) -> JSONResponse:
        """Poll one grant with its polling credential (spec 11.4, 12).

        The polling Bearer credential in the dedicated scheme is the only
        authority: session cookies and every other credential are never
        read. A pending grant answers the closed pending outcome with the
        five-second hint, a too-fast poll the slow-down outcome, and an
        approved grant exchanges once — an exchanged grant replays the exact
        committed credentials while the initial generation stays current.
        """
        del authorization  # the closed registry answers bad presentations
        request.scope["route_template"] = ApiRouteTemplate.AUTH_DEVICE_AUTHORIZATION_POLL
        exchanged = await runtime.device_token_service.exchange_grant(
            grant_id=grant_id,
            polling_credential=polling_credential,
            diagnostic_context=_bound_diagnostic_context(),
        )
        return _success_json(
            DeviceGrantExchangeData(
                grant_id=exchanged.grant_id,
                device_id=exchanged.device_id,
                token_family_id=exchanged.token_family_id,
                refresh_generation=exchanged.refresh_generation,
                access_credential=exchanged.access_credential,
                refresh_credential=exchanged.refresh_credential,
                access_expires_at=exchanged.access_expires_at,
                refresh_expires_at=exchanged.refresh_expires_at,
            ),
            headers=_NO_STORE_NO_CACHE_HEADERS,
        )

    return DeviceAuthorizationRouteEndpoints(
        create_grant=create_grant,
        lookup_grant=lookup_grant,
        approve_grant=approve_grant,
        deny_grant=deny_grant,
        poll_grant=poll_grant,
    )
