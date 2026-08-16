"""Admin device list and revoke endpoints: envelopes and closed errors (14.1).

The two endpoints of the spec 16.4 Admin route set are created per composed
runtime: each closure binds the runtime's device-administration service and
the session dependencies, so the application factory only registers the
semantic operation ids and response models. The list resolves behind the
strict active-session origin gate and renders only the spec-approved fields
of grant-joined device rows — the system bootstrap device never appears. The
revoke route stacks the CSRF triple check and the spec 9.4 recent
re-authentication window before the service's exact display-name
confirmation, and its one locked transaction revokes the device, its token
families, its tokens and the grants claiming its identity. Every response
carries the canonical envelope and ``Cache-Control: no-store``; typed
rejections reach the shared application handler, which maps them onto the
closed registry codes (spec 17) with the same cache-suppression posture.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import cast
from uuid import UUID

from fastapi import Depends, Request
from fastapi.responses import JSONResponse

from api_runtime.authentication_composition import WebAuthenticationRuntime
from api_runtime.authentication_dependencies import (
    AuthenticatedWebRequest,
    create_session_route_dependencies,
)
from api_runtime.authentication_models import (
    AdminDeviceData,
    AdminDeviceListData,
    AdminDeviceRevokeData,
    AdminDeviceRevokeRequest,
    DeviceLifecycleStatus,
)
from personal_os.api_contracts import ApiRouteTemplate, success_envelope
from personal_os.authentication.device_authorization import DevicePlatformClass
from personal_os.diagnostics.context import DiagnosticContext, current_diagnostic_context

#: Response headers every authentication response carries (spec 16).
_NO_STORE_HEADERS: dict[str, str] = {"cache-control": "no-store"}


@dataclass(frozen=True, slots=True)
class DeviceAdminRouteEndpoints:
    """The two endpoint callables of the closed Admin device set."""

    list_devices: Callable[..., Awaitable[JSONResponse]]
    revoke_device: Callable[..., Awaitable[JSONResponse]]


def _bound_diagnostic_context() -> DiagnosticContext:
    """Return the diagnostic context owned by the request correlation middleware."""
    context = current_diagnostic_context()
    if context is None:
        raise RuntimeError("device admin routes require a bound request correlation context")
    return context


def create_device_admin_route_endpoints(
    runtime: WebAuthenticationRuntime,
) -> DeviceAdminRouteEndpoints:
    """Build the two Admin device endpoints over one runtime."""
    dependencies = create_session_route_dependencies(runtime)

    def _request_id() -> UUID:
        return _bound_diagnostic_context().request_id

    def _success_json(data: AdminDeviceListData | AdminDeviceRevokeData) -> JSONResponse:
        envelope = success_envelope(request_id=_request_id(), data=data)
        return JSONResponse(content=envelope.model_dump(mode="json"), headers=_NO_STORE_HEADERS)

    async def list_devices(
        request: Request,
        authentication: AuthenticatedWebRequest = Depends(  # noqa: B008
            dependencies.require_session_request
        ),
    ) -> JSONResponse:
        """List the workspace's plugin devices for the Admin page (18.3)."""
        request.scope["route_template"] = ApiRouteTemplate.ADMIN_DEVICES
        listed = await runtime.device_administration_service.list_devices(
            session_secret=authentication.session_secret
        )
        return _success_json(
            AdminDeviceListData(
                devices=tuple(
                    AdminDeviceData(
                        device_id=device.device_id,
                        device_name=device.device_name,
                        platform_class=DevicePlatformClass(device.platform_class),
                        platform_name=device.platform_name,
                        plugin_version=device.plugin_version,
                        status=cast(DeviceLifecycleStatus, device.status),
                        registered_at=device.registered_at,
                        last_seen_at=device.last_seen_at,
                        revoked_at=device.revoked_at,
                        family_absolute_expires_at=device.family_absolute_expires_at,
                    )
                    for device in listed
                )
            )
        )

    async def revoke_device(
        request: Request,
        revoke_request: AdminDeviceRevokeRequest,
        device_id: UUID,
        authentication: AuthenticatedWebRequest = Depends(  # noqa: B008
            dependencies.require_csrf_protected_request
        ),
    ) -> JSONResponse:
        """Revoke one device behind the full spec 14.1 guard chain."""
        request.scope["route_template"] = ApiRouteTemplate.ADMIN_DEVICE_REVOKE
        revoked = await runtime.device_administration_service.revoke_device(
            device_id=device_id,
            session_secret=authentication.session_secret,
            device_name_confirmation=revoke_request.device_name_confirmation,
            diagnostic_context=_bound_diagnostic_context(),
        )
        return _success_json(
            AdminDeviceRevokeData(device_id=revoked.device_id, revoked_at=revoked.revoked_at)
        )

    return DeviceAdminRouteEndpoints(list_devices=list_devices, revoke_device=revoke_device)
