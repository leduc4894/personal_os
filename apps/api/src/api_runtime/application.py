"""FastAPI application factory: closed route set, envelopes and error handlers.

The factory composes exactly two health routes plus the five session/password
routes, six TOTP/recovery routes, seven browser device-authorization and
device-token routes and two Admin device routes of the injected
web-authentication runtime and the local/test-only OpenAPI document route,
registers the four envelope exception handlers,
strips FastAPI's default validation-error response from the generated
document (the shared handler emits the canonical error envelope instead), and
wraps the finished middleware stack with :class:`RequestContextMiddleware`
from the outside so request correlation owns every exchange, including the
catch-all internal error response. No CORS, GZip, session or authentication
middleware is added — cookies, the exact-origin gate and the CSRF checks live
in the runtime dependencies of the authentication routes — and the request id
in every envelope is read from the bound diagnostic context rather than
minted here. Authentication-route failures carry ``Cache-Control: no-store``
exactly like the route's own success and rejection responses.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from types import MappingProxyType
from typing import Any, Final, cast
from uuid import UUID

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException
from starlette.routing import Route as StarletteRoute
from starlette.routing import request_response
from starlette.types import ASGIApp, ExceptionHandler, Lifespan

from api_runtime import health_routes
from api_runtime.authentication_composition import WebAuthenticationRuntime
from api_runtime.authentication_models import (
    AdminDeviceListData,
    AdminDeviceRevokeData,
    DeviceGrantContextData,
    DeviceGrantData,
    DeviceGrantDecisionData,
    DeviceGrantExchangeData,
    DeviceSelfRevokeData,
    RecoveryCodesData,
    RecoveryLimitedContext,
    RefreshedDeviceTokenData,
    SessionData,
    TotpEnrollmentData,
)
from api_runtime.device_admin_routes import create_device_admin_route_endpoints
from api_runtime.device_authorization_routes import create_device_authorization_route_endpoints
from api_runtime.request_context import ASGIApp as CorrelationApp
from api_runtime.request_context import RequestContextMiddleware
from api_runtime.session_routes import create_session_route_endpoints
from api_runtime.totp_routes import create_totp_route_endpoints
from personal_os.api_contracts import (
    AUTHENTICATION_ROUTE_TEMPLATE_VALUES,
    HTTP_ERROR_STATUSES,
    ApiEnvelope,
    ApiRouteTemplate,
    ApiTransportError,
    CanonicalDatabaseReadinessProbe,
    LivenessData,
    ReadinessData,
    error_envelope,
    is_authentication_route_template,
)
from personal_os.diagnostics.context import current_diagnostic_context
from personal_os.diagnostics.events import DiagnosticEventSink, SafeToken
from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import ApplicationError, InternalApplicationError
from personal_os.package_metadata import distribution_version
from personal_os.runtime_configuration.models import RuntimeEnvironment

LOCAL_ENVIRONMENTS: Final[frozenset[RuntimeEnvironment]] = frozenset(
    {RuntimeEnvironment.LOCAL, RuntimeEnvironment.TEST}
)

_MALFORMED_JSON_ERROR_TYPE: Final = "json_invalid"
_MAX_VALIDATION_FIELD_NAMES: Final = 8

#: The response status FastAPI documents on every body-bearing route with its
#: own ``HTTPValidationError`` schema, and the framework schema definitions
#: that exist only for that default entry.
_HTTP_VALIDATION_STATUS: Final = "422"
_FRAMEWORK_VALIDATION_SCHEMA_NAMES: Final[tuple[str, ...]] = (
    "HTTPValidationError",
    "ValidationError",
)

#: Header every authentication-route response carries, success or failure
#: alike (spec 16.1); the error handlers apply it through the same closed
#: route-template membership the correlation middleware uses.
_NO_STORE_HEADERS: Final[Mapping[str, str]] = MappingProxyType({"cache-control": "no-store"})

#: Closed status-to-code table for framework ``HTTPException`` responses. The
#: two dependency statuses (503) are absent on purpose: a bare status cannot
#: name which registry error the framework meant, so typed readiness failures
#: travel as ``ApplicationError`` instead of as an ``HTTPException``.
_HTTP_EXCEPTION_ERROR_CODES: Final[Mapping[int, ErrorCode]] = MappingProxyType(
    {
        HTTP_ERROR_STATUSES[ErrorCode.API_REQUEST_MALFORMED]: ErrorCode.API_REQUEST_MALFORMED,
        HTTP_ERROR_STATUSES[ErrorCode.API_ROUTE_NOT_FOUND]: ErrorCode.API_ROUTE_NOT_FOUND,
        HTTP_ERROR_STATUSES[ErrorCode.API_METHOD_NOT_ALLOWED]: ErrorCode.API_METHOD_NOT_ALLOWED,
        HTTP_ERROR_STATUSES[ErrorCode.API_REQUEST_VALIDATION_FAILED]: (
            ErrorCode.API_REQUEST_VALIDATION_FAILED
        ),
        HTTP_ERROR_STATUSES[ErrorCode.INTERNAL_ERROR]: ErrorCode.INTERNAL_ERROR,
    }
)


def create_api_application(
    *,
    environment: RuntimeEnvironment,
    readiness_probe: CanonicalDatabaseReadinessProbe,
    web_authentication: WebAuthenticationRuntime,
    event_sink: DiagnosticEventSink | None = None,
    lifespan: Lifespan[FastAPI] | None = None,
) -> FastAPI:
    """Compose the runnable API application for one runtime environment."""
    app = FastAPI(
        title="Personal Knowledge API",
        version=distribution_version(),
        openapi_version="3.1.0",
        openapi_url="/api/openapi.json" if environment in LOCAL_ENVIRONMENTS else None,
        docs_url=None,
        redoc_url=None,
        redirect_slashes=False,
        lifespan=lifespan,
    )
    register_api_exception_handlers(app)
    app.add_api_route(
        "/api/health/live",
        health_routes.liveness,
        methods=["GET"],
        operation_id="getApiLiveness",
        response_model=ApiEnvelope[LivenessData],
    )

    async def readiness_endpoint(request: Request) -> JSONResponse:
        return await health_routes.readiness(request, readiness_probe)

    app.add_api_route(
        "/api/health/ready",
        readiness_endpoint,
        methods=["GET"],
        operation_id="getApiReadiness",
        response_model=ApiEnvelope[ReadinessData],
    )
    _register_session_routes(app, web_authentication)
    _register_totp_routes(app, web_authentication)
    _register_device_authorization_routes(app, web_authentication)
    _register_device_admin_routes(app, web_authentication)
    _classify_openapi_route(app)
    _suppress_framework_validation_error_document(app)
    # The pure-ASGI correlation middleware declares read-only ``Mapping``
    # message aliases; every ASGI message mapping is mutable in practice, so
    # the static mismatch with Starlette's ``ASGIApp`` is bridged here once at
    # composition time. Assigning the built stack keeps the correlation
    # middleware outermost, ahead of the framework error middleware, so even
    # the catch-all internal response stays correlated.
    correlation_app = cast("CorrelationApp", app.build_middleware_stack())
    app.middleware_stack = cast(
        "ASGIApp", RequestContextMiddleware(correlation_app, event_sink=event_sink)
    )
    return app


def register_api_exception_handlers(app: FastAPI) -> None:
    """Bind the four envelope handlers shared by every composed application."""
    app.add_exception_handler(
        RequestValidationError, _as_http_handler(_handle_request_validation_error)
    )
    app.add_exception_handler(HTTPException, _as_http_handler(_handle_http_exception))
    app.add_exception_handler(ApplicationError, _as_http_handler(_handle_application_error))
    app.add_exception_handler(Exception, _handle_unexpected_exception)


def _register_session_routes(app: FastAPI, web_authentication: WebAuthenticationRuntime) -> None:
    """Register the closed session/password route set (spec 16.1).

    Each route carries its manually assigned semantic operation id and the
    envelope response model of its strict session payload; the cookie, origin
    and CSRF behavior lives entirely in the runtime dependencies bound inside
    the endpoint factory.
    """
    endpoints = create_session_route_endpoints(web_authentication)
    session_envelope_model = ApiEnvelope[SessionData]
    app.add_api_route(
        "/api/auth/login",
        endpoints.login,
        methods=["POST"],
        operation_id="login",
        response_model=session_envelope_model,
    )
    app.add_api_route(
        "/api/auth/session",
        endpoints.get_session,
        methods=["GET"],
        operation_id="getSession",
        response_model=session_envelope_model,
    )
    app.add_api_route(
        "/api/auth/logout",
        endpoints.logout,
        methods=["POST"],
        operation_id="logout",
        response_model=session_envelope_model,
    )
    app.add_api_route(
        "/api/auth/reauthenticate",
        endpoints.reauthenticate,
        methods=["POST"],
        operation_id="reauthenticate",
        response_model=session_envelope_model,
    )
    app.add_api_route(
        "/api/auth/password",
        endpoints.change_password,
        methods=["PUT"],
        operation_id="changePassword",
        response_model=session_envelope_model,
    )


def _register_totp_routes(app: FastAPI, web_authentication: WebAuthenticationRuntime) -> None:
    """Register the closed TOTP/recovery route set (spec 16.2).

    Each route carries its manually assigned semantic operation id and the
    envelope response model of its strict payload; the challenge-tolerant and
    strict CSRF dependencies, the one-time provisioning/recovery payloads and
    the rotation cookies live in the endpoint factory.
    """
    endpoints = create_totp_route_endpoints(web_authentication)
    app.add_api_route(
        "/api/auth/totp/verify",
        endpoints.verify_challenge,
        methods=["POST"],
        operation_id="verifyTotpChallenge",
        response_model=ApiEnvelope[SessionData],
    )
    app.add_api_route(
        "/api/auth/totp/enrollments",
        endpoints.submit_enrollment_action,
        methods=["POST"],
        operation_id="createTotpEnrollment",
        response_model=ApiEnvelope[TotpEnrollmentData],
    )
    app.add_api_route(
        "/api/auth/totp/enrollments/{enrollment_id}/verify",
        endpoints.verify_enrollment,
        methods=["POST"],
        operation_id="verifyTotpEnrollment",
        response_model=ApiEnvelope[RecoveryCodesData],
    )
    app.add_api_route(
        "/api/auth/totp/recovery",
        endpoints.recover,
        methods=["POST"],
        operation_id="startTotpRecovery",
        response_model=ApiEnvelope[RecoveryLimitedContext],
    )
    app.add_api_route(
        "/api/auth/totp/recovery-codes/regenerate",
        endpoints.regenerate_recovery_codes,
        methods=["POST"],
        operation_id="regenerateTotpRecoveryCodes",
        response_model=ApiEnvelope[RecoveryCodesData],
    )
    app.add_api_route(
        "/api/auth/totp",
        endpoints.disable,
        methods=["DELETE"],
        operation_id="disableTotp",
        response_model=ApiEnvelope[SessionData],
    )


def _register_device_authorization_routes(
    app: FastAPI, web_authentication: WebAuthenticationRuntime
) -> None:
    """Register the closed browser device-authorization route set (16.3).

    Each route carries its manually assigned semantic operation id and the
    envelope response model of its strict payload; the exact-origin gate of
    the unauthenticated creation endpoint and the strict CSRF dependency of
    the browser endpoints live in the endpoint factory.
    """
    endpoints = create_device_authorization_route_endpoints(web_authentication)
    app.add_api_route(
        "/api/auth/device-authorizations",
        endpoints.create_grant,
        methods=["POST"],
        operation_id="createDeviceAuthorization",
        response_model=ApiEnvelope[DeviceGrantData],
    )
    app.add_api_route(
        "/api/auth/device-authorizations/lookup",
        endpoints.lookup_grant,
        methods=["POST"],
        operation_id="lookupDeviceAuthorization",
        response_model=ApiEnvelope[DeviceGrantContextData],
    )
    app.add_api_route(
        "/api/auth/device-authorizations/{grant_id}/approve",
        endpoints.approve_grant,
        methods=["POST"],
        operation_id="approveDeviceAuthorization",
        response_model=ApiEnvelope[DeviceGrantDecisionData],
    )
    app.add_api_route(
        "/api/auth/device-authorizations/{grant_id}/deny",
        endpoints.deny_grant,
        methods=["POST"],
        operation_id="denyDeviceAuthorization",
        response_model=ApiEnvelope[DeviceGrantDecisionData],
    )
    app.add_api_route(
        "/api/auth/device-authorizations/{grant_id}/poll",
        endpoints.poll_grant,
        methods=["POST"],
        operation_id="pollDeviceAuthorization",
        response_model=ApiEnvelope[DeviceGrantExchangeData],
    )
    app.add_api_route(
        "/api/auth/device-tokens/refresh",
        endpoints.refresh_device_tokens,
        methods=["POST"],
        operation_id="refreshDeviceTokens",
        response_model=ApiEnvelope[RefreshedDeviceTokenData],
    )
    app.add_api_route(
        "/api/auth/device-tokens/revoke-current",
        endpoints.revoke_current_device_token,
        methods=["POST"],
        operation_id="revokeCurrentDeviceToken",
        response_model=ApiEnvelope[DeviceSelfRevokeData],
    )


def _register_device_admin_routes(
    app: FastAPI, web_authentication: WebAuthenticationRuntime
) -> None:
    """Register the closed Admin device route set (spec 16.4).

    Each route carries its manually assigned semantic operation id and the
    envelope response model of its strict payload; the active-session and
    CSRF dependencies and the exact display-name confirmation live in the
    endpoint factory and the administration service.
    """
    endpoints = create_device_admin_route_endpoints(web_authentication)
    app.add_api_route(
        "/api/admin/devices",
        endpoints.list_devices,
        methods=["GET"],
        operation_id="listAdminDevices",
        response_model=ApiEnvelope[AdminDeviceListData],
    )
    app.add_api_route(
        "/api/admin/devices/{device_id}/revoke",
        endpoints.revoke_device,
        methods=["POST"],
        operation_id="revokeAdminDevice",
        response_model=ApiEnvelope[AdminDeviceRevokeData],
    )


def _as_http_handler(
    handler: Callable[[Request, Any], Awaitable[JSONResponse]],
) -> ExceptionHandler:
    """Widen one precisely typed handler to Starlette's handler contract.

    Starlette's ``ExceptionHandler`` alias widens the exception parameter to
    ``Exception`` while dispatch happens by the registered exception class, so
    this widening is sound and only reconciles callable variance for the
    type checker.
    """
    return cast("ExceptionHandler", handler)


def _classify_openapi_route(app: FastAPI) -> None:
    """Publish the closed document route template on FastAPI's OpenAPI route.

    FastAPI registers the local/test document route as a plain Starlette route,
    which never exposes a matched ``scope["route"]`` path for the correlation
    middleware's closed mapping. The wrapper only assigns the template; the
    returned document stays the raw standard document.
    """
    if app.openapi_url is None:
        return
    for route in app.routes:
        if isinstance(route, StarletteRoute) and route.path == app.openapi_url:
            _bind_openapi_route_template(route)
            break


def _bind_openapi_route_template(route: StarletteRoute) -> None:
    """Wrap one route's endpoint so it publishes the document route template."""
    # FastAPI's document endpoint is an async callable by construction.
    document_endpoint = cast("Callable[[Request], Awaitable[Response]]", route.endpoint)

    async def classified_openapi(request: Request) -> Response:
        request.scope["route_template"] = ApiRouteTemplate.OPENAPI_DOCUMENT
        return await document_endpoint(request)

    route.endpoint = classified_openapi
    route.app = request_response(classified_openapi)


def _suppress_framework_validation_error_document(app: FastAPI) -> None:
    """Drop FastAPI's default validation-error responses from the document.

    Body-bearing routes would otherwise document a ``422`` response with the
    framework's ``HTTPValidationError`` schema, but the shared request
    validation handler answers with the canonical error envelope —
    ``api_request_malformed`` or ``api_request_validation_failed`` with bounded
    field names — exactly like every other failure, and no route documents
    error envelopes. Suppressing the default keeps the advertised response set
    closed over the shapes actually emitted, for the served document and the
    exported snapshot alike.
    """
    framework_openapi = app.openapi

    def openapi() -> dict[str, Any]:
        cached = app.openapi_schema
        if cached is not None:
            return cached
        document = framework_openapi()
        schemas = document.get("components", {}).get("schemas")
        for path_item in document.get("paths", {}).values():
            if not isinstance(path_item, dict):
                continue
            for operation in path_item.values():
                if isinstance(operation, dict):
                    operation.get("responses", {}).pop(_HTTP_VALIDATION_STATUS, None)
        if isinstance(schemas, dict):
            for schema_name in _FRAMEWORK_VALIDATION_SCHEMA_NAMES:
                schemas.pop(schema_name, None)
        app.openapi_schema = document
        return document

    app.openapi = openapi  # type: ignore[method-assign]


async def _handle_request_validation_error(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Split request-shape failures into malformed JSON versus schema errors.

    Only the ``json_invalid`` type means the request body was never parsable
    JSON; every other failure is a schema violation whose envelope detail
    carries bounded unique top-level field names and never a rejected value.
    """
    errors = exc.errors()
    if any(error.get("type") == _MALFORMED_JSON_ERROR_TYPE for error in errors):
        return _error_response(request, ApiTransportError(ErrorCode.API_REQUEST_MALFORMED))
    field_names = _validation_field_names(errors)
    return _error_response(
        request,
        ApiTransportError(
            ErrorCode.API_REQUEST_VALIDATION_FAILED,
            safe_details={"field_names": field_names},
        ),
    )


async def _handle_http_exception(request: Request, exc: HTTPException) -> JSONResponse:
    """Map a framework ``HTTPException`` status through the closed table."""
    error_code = _HTTP_EXCEPTION_ERROR_CODES.get(exc.status_code)
    if error_code is None:
        return _internal_fallback_response(request)
    return _error_response(request, ApiTransportError(error_code))


async def _handle_application_error(request: Request, exc: ApplicationError) -> JSONResponse:
    """Use a typed error's registered status, or the internal fallback."""
    if exc.error_code not in HTTP_ERROR_STATUSES:
        return _internal_fallback_response(request)
    return _error_response(request, exc)


async def _handle_unexpected_exception(request: Request, exc: Exception) -> JSONResponse:
    """Collapse any unclassified failure into the sentinel-free internal envelope."""
    del exc
    return _internal_fallback_response(request)


def _internal_fallback_response(request: Request) -> JSONResponse:
    """Render the internal envelope for unknown status/code combinations."""
    return _error_response(request, InternalApplicationError(ErrorCode.INTERNAL_ERROR))


def _error_response(request: Request, error: ApplicationError) -> JSONResponse:
    """Render one error envelope with its closed-table status.

    Authentication-route failures carry ``Cache-Control: no-store`` like every
    other authentication response (spec 16.1); the route is classified through
    the assigned route template or, when a dependency rejected the request
    before the endpoint published it, through the matched route path mapped
    against the same closed template vocabulary.
    """
    envelope = error_envelope(request_id=_request_id(), error=error)
    status_code = HTTP_ERROR_STATUSES[error.error_code]
    return JSONResponse(
        content=envelope.model_dump(mode="json"),
        status_code=status_code,
        headers=_authentication_error_headers(request),
    )


def _authentication_error_headers(request: Request) -> Mapping[str, str]:
    """Return the no-store headers when the rejected route is an auth route."""
    template = request.scope.get("route_template")
    if not isinstance(template, ApiRouteTemplate):
        matched_path = getattr(request.scope.get("route"), "path", None)
        if isinstance(matched_path, str) and matched_path in AUTHENTICATION_ROUTE_TEMPLATE_VALUES:
            template = ApiRouteTemplate(matched_path)
        else:
            template = None
    if isinstance(template, ApiRouteTemplate) and is_authentication_route_template(template):
        return _NO_STORE_HEADERS
    return {}


def _request_id() -> UUID:
    """Return the request id owned by the bound request correlation context."""
    context = current_diagnostic_context()
    if context is None:
        raise RuntimeError("api exception handlers require a bound request correlation context")
    return context.request_id


def _validation_field_names(
    errors: Sequence[Mapping[str, Any]],
) -> tuple[SafeToken, ...]:
    """Collect bounded unique top-level field names, never rejected values.

    Only names satisfying the safe token grammar survive; positional locations
    (malformed-JSON offsets) and attacker-shaped names are dropped. Order is
    preserved and the list is capped before it reaches the registry limit.
    """
    names: list[str] = []
    for error in errors:
        location = error.get("loc")
        if not isinstance(location, tuple | list) or len(location) < 2:
            continue
        name = location[1]
        if isinstance(name, str) and name not in names:
            names.append(name)
    field_names: list[SafeToken] = []
    for name in names:
        if len(field_names) >= _MAX_VALIDATION_FIELD_NAMES:
            break
        try:
            field_names.append(SafeToken.parse(name))
        except ValueError:
            continue
    return tuple(field_names)
