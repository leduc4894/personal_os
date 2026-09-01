"""FastAPI application factory: closed route set, envelopes and error handlers.

The factory composes exactly two health routes plus the five session/password
routes, six TOTP/recovery routes, seven browser device-authorization and
device-token routes, two Admin device routes of the injected
web-authentication runtime, the optional runtime-gated exclusion-policy,
small-file sync, multipart upload, source-lifecycle and device-sync route
sets — including the read-only sync rejection diagnostics Admin route of the
small-file-sync runtime, the read-only policy diagnostics Admin route of the
exclusion-policy runtime, the read-only Prometheus text policy metrics
exposition route of the exclusion-policy runtime, the eight device sync
routes of the device reconciliation surface, the five multipart upload
session routes of the resumable multipart transfer and the local/test-only
OpenAPI document route —
registers the four envelope exception handlers,
strips FastAPI's default validation-error response from the generated
document (the shared handler emits the canonical error envelope instead),
declares the dedicated device Bearer schemes of spec 16 the routes have not
bound yet, and wraps the finished middleware stack with the nonce-CSP web
security headers middleware and then :class:`RequestContextMiddleware`
from the outside so request correlation owns every exchange, including
the catch-all internal error response. No CORS, GZip, session or authentication
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
from fastapi.security import HTTPBearer
from starlette.exceptions import HTTPException
from starlette.routing import Route as StarletteRoute
from starlette.routing import request_response
from starlette.types import ASGIApp, ExceptionHandler, Lifespan

from api_runtime import health_routes
from api_runtime.authentication_composition import WebAuthenticationRuntime
from api_runtime.authentication_dependencies import ACCESS_BEARER_SCHEME, REFRESH_BEARER_SCHEME
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
from api_runtime.device_authorization_routes import (
    POLLING_BEARER_SCHEME,
    create_device_authorization_route_endpoints,
)
from api_runtime.device_sync_composition import DeviceSyncRuntime
from api_runtime.device_sync_models import (
    DeviceCursorReceiptData,
    DeviceEventPageData,
    ManifestActionPageData,
    ManifestPageReceiptData,
    ManifestRunReceiptData,
)
from api_runtime.device_sync_routes import create_device_sync_route_endpoints
from api_runtime.exclusion_policy_composition import ExclusionPolicyRuntime
from api_runtime.exclusion_policy_diagnostics_models import ExclusionPolicyDiagnosticsData
from api_runtime.exclusion_policy_diagnostics_routes import (
    create_policy_diagnostics_admin_route_endpoints,
)
from api_runtime.exclusion_policy_models import (
    ExclusionPolicyStatusData,
    PolicyDraftData,
    PolicyKeysetPageData,
    PolicyPreviewData,
    PolicyPublicationData,
    SignedPolicySnapshotData,
)
from api_runtime.exclusion_policy_routes import create_exclusion_policy_route_endpoints
from api_runtime.metrics_exposition_routes import (
    PROMETHEUS_TEXT_CONTENT_TYPE,
    create_metrics_exposition_route_endpoints,
)
from api_runtime.multipart_upload_composition import MultipartUploadRuntime
from api_runtime.multipart_upload_models import (
    MultipartCompletionData,
    MultipartPartUrlData,
    MultipartSessionPlanData,
    MultipartSessionStatusData,
)
from api_runtime.multipart_upload_routes import create_multipart_upload_route_endpoints
from api_runtime.request_context import ASGIApp as CorrelationApp
from api_runtime.request_context import RequestContextMiddleware
from api_runtime.session_routes import create_session_route_endpoints
from api_runtime.small_file_sync_composition import SmallFileSyncRuntime
from api_runtime.small_file_sync_diagnostics_models import SmallFileRejectionDiagnosticsData
from api_runtime.small_file_sync_diagnostics_routes import (
    create_sync_diagnostics_admin_route_endpoints,
)
from api_runtime.small_file_sync_models import (
    SmallFilePreflightData,
    SmallFileTerminalResultData,
)
from api_runtime.small_file_sync_routes import create_small_file_sync_route_endpoints
from api_runtime.source_lifecycle_composition import SourceLifecycleRuntime
from api_runtime.source_lifecycle_diagnostics_models import SourceLifecycleDiagnosticsData
from api_runtime.source_lifecycle_diagnostics_routes import (
    create_source_lifecycle_diagnostics_admin_route_endpoints,
)
from api_runtime.source_lifecycle_models import SourceLifecycleCommitData
from api_runtime.source_lifecycle_routes import create_source_lifecycle_route_endpoints
from api_runtime.totp_routes import create_totp_route_endpoints
from api_runtime.web_security import WebSecurityHeadersMiddleware
from personal_os.api_contracts import (
    HTTP_ERROR_STATUSES,
    NO_STORE_ROUTE_TEMPLATE_VALUES,
    ApiEnvelope,
    ApiRouteTemplate,
    ApiTransportError,
    CanonicalDatabaseReadinessProbe,
    LivenessData,
    ReadinessData,
    error_envelope,
    is_no_store_route_template,
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

#: The dedicated device Bearer schemes of spec 16 the published document must
#: carry. Polling and refresh render through their routes; the access scheme
#: authenticates the device-scoped surface of the later sync children, so it
#: is declared on the document ahead of any route binding rather than waiting
#: for a consumer to appear. Route-driven rendering always wins over the
#: declared entry.
_DECLARED_DEVICE_BEARER_SCHEMES: Final[tuple[HTTPBearer, ...]] = (
    POLLING_BEARER_SCHEME,
    ACCESS_BEARER_SCHEME,
    REFRESH_BEARER_SCHEME,
)

#: Header every authentication-route response carries, success or failure
#: alike (spec 16.1); the error handlers apply it through the same closed
#: route-template membership the correlation middleware uses.
_NO_STORE_HEADERS: Final[Mapping[str, str]] = MappingProxyType({"cache-control": "no-store"})

#: The operations whose documented success is exactly one non-JSON payload:
#: the binary exception to the JSON envelope (spec 7.4) and the Prometheus
#: text exposition of the policy metrics sink. The document hook below keeps
#: each success content exactly the one declared media type, because FastAPI
#: unconditionally merges a default ``application/json`` schema into every
#: custom response entry.
_NON_JSON_SUCCESS_MEDIA_BY_OPERATION: Final[Mapping[str, str]] = MappingProxyType(
    {
        "downloadDeviceSourceVersion": "application/octet-stream",
        "getMetricsExposition": PROMETHEUS_TEXT_CONTENT_TYPE,
    }
)

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
    exclusion_policy: ExclusionPolicyRuntime | None = None,
    small_file_sync: SmallFileSyncRuntime | None = None,
    multipart_upload: MultipartUploadRuntime | None = None,
    source_lifecycle: SourceLifecycleRuntime | None = None,
    device_sync: DeviceSyncRuntime | None = None,
    event_sink: DiagnosticEventSink | None = None,
    lifespan: Lifespan[FastAPI] | None = None,
) -> FastAPI:
    """Compose the runnable API application for one runtime environment.

    The exclusion-policy, small-file-sync, multipart-upload, source-lifecycle
    and device-sync runtimes are optional only so the authentication-only
    contract compositions of the earlier children stay constructible; the
    serve graph and the full contract document always compose them, and the
    routes register only when the runtime is present — never through router
    auto-discovery.
    """
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
    if exclusion_policy is not None:
        _register_exclusion_policy_routes(app, web_authentication, exclusion_policy)
        _register_policy_diagnostics_admin_route(app, web_authentication, exclusion_policy)
        _register_metrics_exposition_route(app, web_authentication, exclusion_policy)
    if small_file_sync is not None:
        _register_small_file_sync_routes(app, web_authentication, small_file_sync)
        _register_sync_diagnostics_admin_route(app, web_authentication, small_file_sync)
    if multipart_upload is not None:
        _register_multipart_upload_routes(app, web_authentication, multipart_upload)
    if source_lifecycle is not None:
        _register_source_lifecycle_routes(app, web_authentication, source_lifecycle)
        _register_source_lifecycle_diagnostics_admin_route(
            app, web_authentication, source_lifecycle
        )
    if device_sync is not None:
        _register_device_sync_routes(app, web_authentication, device_sync)
    _classify_openapi_route(app)
    _suppress_framework_validation_error_document(app)
    # The pure-ASGI correlation middleware declares read-only ``Mapping``
    # message aliases; every ASGI message mapping is mutable in practice, so
    # the static mismatch with Starlette's ``ASGIApp`` is bridged here once at
    # composition time. The web security headers middleware wraps the built
    # stack from the inside — every framework response, error envelope
    # included, receives the fresh nonce CSP and the fixed security headers —
    # while assigning the finished stack keeps the correlation middleware
    # outermost, ahead of both, so even the catch-all internal response stays
    # correlated.
    correlation_app = cast("CorrelationApp", app.build_middleware_stack())
    secured_app = WebSecurityHeadersMiddleware(correlation_app)
    app.middleware_stack = cast(
        "ASGIApp", RequestContextMiddleware(secured_app, event_sink=event_sink)
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
        operation_id="refreshDeviceToken",
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


def _register_exclusion_policy_routes(
    app: FastAPI,
    web_authentication: WebAuthenticationRuntime,
    exclusion_policy: ExclusionPolicyRuntime,
) -> None:
    """Register the closed exclusion-policy route set (spec 16.1/16.2).

    Each route carries its manually assigned semantic operation id, its
    envelope response model and its primary success status; the session/
    CSRF/recent-auth dependencies of the Admin routes and the access Bearer
    dependency of the plugin routes live in the endpoint factory. The
    preview poll documents its ready ``200`` beside the pending ``202``, the
    publication its exact-replay ``200`` beside the committed ``201``, and
    the snapshot its bodyless ``304``.
    """
    endpoints = create_exclusion_policy_route_endpoints(
        web_authentication=web_authentication, exclusion_policy=exclusion_policy
    )
    app.add_api_route(
        "/api/admin/exclusion-policy",
        endpoints.get_policy_status,
        methods=["GET"],
        operation_id="getExclusionPolicyStatus",
        response_model=ApiEnvelope[ExclusionPolicyStatusData],
    )
    app.add_api_route(
        "/api/admin/exclusion-policy/draft",
        endpoints.replace_draft,
        methods=["PUT"],
        operation_id="replaceExclusionPolicyDraft",
        response_model=ApiEnvelope[PolicyDraftData],
    )
    app.add_api_route(
        "/api/admin/exclusion-policy/previews",
        endpoints.create_preview,
        methods=["POST"],
        operation_id="createExclusionPolicyPreview",
        response_model=ApiEnvelope[PolicyPreviewData],
        status_code=202,
    )
    app.add_api_route(
        "/api/admin/exclusion-policy/previews/{policy_preview_id}",
        endpoints.get_preview,
        methods=["GET"],
        operation_id="getExclusionPolicyPreview",
        response_model=ApiEnvelope[PolicyPreviewData],
        status_code=202,
        responses={
            "200": {
                "description": "The preview is ready; the payload carries the first result page",
                "content": {
                    "application/json": {
                        "schema": {"$ref": "#/components/schemas/ApiEnvelope_PolicyPreviewData_"}
                    }
                },
            }
        },
    )
    app.add_api_route(
        "/api/admin/exclusion-policy/publications",
        endpoints.publish,
        methods=["POST"],
        operation_id="publishExclusionPolicy",
        response_model=ApiEnvelope[PolicyPublicationData],
        status_code=201,
        responses={
            "200": {
                "description": "The exact replay of an already committed publication",
                "content": {
                    "application/json": {
                        "schema": {
                            "$ref": "#/components/schemas/ApiEnvelope_PolicyPublicationData_"
                        }
                    }
                },
            }
        },
    )
    app.add_api_route(
        "/api/sync/exclusion-policy/keysets",
        endpoints.list_keysets,
        methods=["GET"],
        operation_id="listExclusionPolicyKeysets",
        response_model=ApiEnvelope[PolicyKeysetPageData],
    )
    app.add_api_route(
        "/api/sync/exclusion-policy/snapshot",
        endpoints.get_snapshot,
        methods=["GET"],
        operation_id="getExclusionPolicySnapshot",
        response_model=ApiEnvelope[SignedPolicySnapshotData],
        responses={"304": {"description": "The presented entity tag is current"}},
    )


def _register_policy_diagnostics_admin_route(
    app: FastAPI,
    web_authentication: WebAuthenticationRuntime,
    exclusion_policy: ExclusionPolicyRuntime,
) -> None:
    """Register the closed policy diagnostics Admin route.

    The route carries its manually assigned semantic operation id and the
    envelope response model of its strict closed payload; the active-session
    dependency of the Web Admin surface lives in the endpoint factory, and
    the payload carries only closed tokens, counts and epoch timestamps.
    """
    endpoints = create_policy_diagnostics_admin_route_endpoints(
        web_authentication=web_authentication, exclusion_policy=exclusion_policy
    )
    app.add_api_route(
        "/api/admin/exclusion-policy/diagnostics",
        endpoints.get_policy_diagnostics,
        methods=["GET"],
        operation_id="getExclusionPolicyDiagnostics",
        response_model=ApiEnvelope[ExclusionPolicyDiagnosticsData],
    )


def _register_metrics_exposition_route(
    app: FastAPI,
    web_authentication: WebAuthenticationRuntime,
    exclusion_policy: ExclusionPolicyRuntime,
) -> None:
    """Register the read-only Prometheus text policy metrics route.

    The route carries its manually assigned semantic operation id and its
    single text/plain success entry — the binary exception to the JSON
    envelope shared with the verified download route; the active-session
    dependency of the Web Admin surface lives in the endpoint factory, and
    the exposition carries only closed tokens and counts.
    """
    endpoints = create_metrics_exposition_route_endpoints(
        web_authentication=web_authentication, exclusion_policy=exclusion_policy
    )
    app.add_api_route(
        "/api/admin/metrics",
        endpoints.get_metrics_exposition,
        methods=["GET"],
        operation_id="getMetricsExposition",
        responses={
            "200": {
                "description": (
                    "The closed policy evaluation and publication counters "
                    "rendered in the Prometheus text exposition format"
                ),
                "content": {PROMETHEUS_TEXT_CONTENT_TYPE: {"schema": {"type": "string"}}},
            }
        },
    )


def _register_source_lifecycle_routes(
    app: FastAPI,
    web_authentication: WebAuthenticationRuntime,
    source_lifecycle: SourceLifecycleRuntime,
) -> None:
    """Register the closed source lifecycle route set (spec 19.2).

    The route carries its manually assigned semantic operation id and the
    envelope response model; the access Bearer dependency and the closed
    workspace/device/user derivation live in the endpoint factory.
    """

    endpoints = create_source_lifecycle_route_endpoints(
        web_authentication=web_authentication, source_lifecycle=source_lifecycle
    )
    app.add_api_route(
        "/api/sources/lifecycle-events",
        endpoints.commit_source_lifecycle_event,
        methods=["POST"],
        operation_id="commitSourceLifecycleEvent",
        response_model=ApiEnvelope[SourceLifecycleCommitData],
    )


def _register_source_lifecycle_diagnostics_admin_route(
    app: FastAPI,
    web_authentication: WebAuthenticationRuntime,
    source_lifecycle: SourceLifecycleRuntime,
) -> None:
    """Register the closed source lifecycle diagnostics Admin route.

    The route carries its manually assigned semantic operation id and the
    envelope response model of its strict closed payload; the active-session
    dependency of the Web Admin surface lives in the endpoint factory, and
    the payload carries only closed tokens, counts and epoch timestamps.
    """
    endpoints = create_source_lifecycle_diagnostics_admin_route_endpoints(
        web_authentication=web_authentication, source_lifecycle=source_lifecycle
    )
    app.add_api_route(
        "/api/admin/source-lifecycle/rejections",
        endpoints.get_rejection_diagnostics,
        methods=["GET"],
        operation_id="getSourceLifecycleRejectionDiagnostics",
        response_model=ApiEnvelope[SourceLifecycleDiagnosticsData],
    )


def _register_small_file_sync_routes(
    app: FastAPI,
    web_authentication: WebAuthenticationRuntime,
    small_file_sync: SmallFileSyncRuntime,
) -> None:
    """Register the closed small-file sync route set (spec 10.1/10.2).

    Each route carries its manually assigned semantic operation id, its
    envelope response model and the access Bearer dependency of the plugin
    surface; the preflight converts its strict body through the boundary
    models and the content route documents its raw body as exactly one
    ``application/octet-stream`` binary payload — never a form, callback or
    presigned target.
    """
    endpoints = create_small_file_sync_route_endpoints(
        web_authentication=web_authentication, small_file_sync=small_file_sync
    )
    app.add_api_route(
        "/api/sync/journal-events/preflight",
        endpoints.preflight_journal_event,
        methods=["POST"],
        operation_id="preflightJournalEventUpload",
        response_model=ApiEnvelope[SmallFilePreflightData],
    )
    app.add_api_route(
        "/api/uploads/{operation_id}/content",
        endpoints.upload_content,
        methods=["PUT"],
        operation_id="uploadSmallFileContent",
        response_model=ApiEnvelope[SmallFileTerminalResultData],
        openapi_extra={
            "requestBody": {
                "required": True,
                "description": ("The exact raw content bytes of the preflight-bound file"),
                "content": {
                    "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
                },
            }
        },
    )


def _register_multipart_upload_routes(
    app: FastAPI,
    web_authentication: WebAuthenticationRuntime,
    multipart_upload: MultipartUploadRuntime,
) -> None:
    """Register the closed multipart upload session route set (Child 7 spec 5).

    Each route carries its manually assigned semantic operation id and the
    envelope response model of its strict payload; the access Bearer
    dependency, the credential-derived workspace/device scope and the
    boundary session-grammar validation live in the endpoint factory. The
    part-URL response is the sole signed-URL surface of the API.
    """
    endpoints = create_multipart_upload_route_endpoints(
        web_authentication=web_authentication, multipart_upload=multipart_upload
    )
    app.add_api_route(
        "/api/uploads/multipart-sessions",
        endpoints.create_session,
        methods=["POST"],
        operation_id="createMultipartUploadSession",
        response_model=ApiEnvelope[MultipartSessionPlanData],
    )
    app.add_api_route(
        "/api/uploads/multipart-sessions/{session_id}",
        endpoints.get_session,
        methods=["GET"],
        operation_id="getMultipartUploadSession",
        response_model=ApiEnvelope[MultipartSessionStatusData],
    )
    app.add_api_route(
        "/api/uploads/multipart-sessions/{session_id}/parts/{part_number}/url",
        endpoints.issue_part_url,
        methods=["POST"],
        operation_id="issueMultipartPartUrl",
        response_model=ApiEnvelope[MultipartPartUrlData],
    )
    app.add_api_route(
        "/api/uploads/multipart-sessions/{session_id}/complete",
        endpoints.complete_session,
        methods=["POST"],
        operation_id="completeMultipartUploadSession",
        response_model=ApiEnvelope[MultipartCompletionData],
    )
    app.add_api_route(
        "/api/uploads/multipart-sessions/{session_id}/abort",
        endpoints.abort_session,
        methods=["POST"],
        operation_id="abortMultipartUploadSession",
        response_model=ApiEnvelope[MultipartSessionStatusData],
    )


def _register_sync_diagnostics_admin_route(
    app: FastAPI,
    web_authentication: WebAuthenticationRuntime,
    small_file_sync: SmallFileSyncRuntime,
) -> None:
    """Register the closed sync diagnostics Admin route.

    The route carries its manually assigned semantic operation id and the
    envelope response model of its strict closed payload; the active-session
    dependency of the Web Admin surface lives in the endpoint factory, and
    the payload carries only closed tokens, counts and epoch timestamps.
    """
    endpoints = create_sync_diagnostics_admin_route_endpoints(
        web_authentication=web_authentication, small_file_sync=small_file_sync
    )
    app.add_api_route(
        "/api/admin/sync/rejections",
        endpoints.get_rejection_diagnostics,
        methods=["GET"],
        operation_id="getSyncRejectionDiagnostics",
        response_model=ApiEnvelope[SmallFileRejectionDiagnosticsData],
    )


def _register_device_sync_routes(
    app: FastAPI,
    web_authentication: WebAuthenticationRuntime,
    device_sync: DeviceSyncRuntime,
) -> None:
    """Register the closed device sync route set (spec 7.1-7.4).

    Each route carries its manually assigned semantic operation id and the
    envelope response model of its strict payload; the access Bearer
    dependency, the credential-derived device scope and the closed error
    mapping live in the endpoint factory. The binary download documents its
    success as exactly one ``application/octet-stream`` binary payload with
    its exact content headers — the one documented exception to the JSON
    envelope (spec 7.4) — and never a form, callback or presigned target.
    """
    endpoints = create_device_sync_route_endpoints(
        web_authentication=web_authentication, device_sync=device_sync
    )
    app.add_api_route(
        "/api/sync/events",
        endpoints.pull_events,
        methods=["GET"],
        operation_id="pullDeviceSyncEvents",
        response_model=ApiEnvelope[DeviceEventPageData],
    )
    app.add_api_route(
        "/api/sync/cursor-acknowledgements",
        endpoints.acknowledge_cursor,
        methods=["POST"],
        operation_id="acknowledgeDeviceSyncCursor",
        response_model=ApiEnvelope[DeviceCursorReceiptData],
    )
    app.add_api_route(
        "/api/sync/manifests",
        endpoints.start_manifest,
        methods=["POST"],
        operation_id="startDeviceManifest",
        response_model=ApiEnvelope[ManifestRunReceiptData],
    )
    app.add_api_route(
        "/api/sync/manifests/{manifest_run_id}/pages/{page_number}",
        endpoints.append_manifest_page,
        methods=["PUT"],
        operation_id="appendDeviceManifestPage",
        response_model=ApiEnvelope[ManifestPageReceiptData],
    )
    app.add_api_route(
        "/api/sync/manifests/{manifest_run_id}/finalize",
        endpoints.finalize_manifest,
        methods=["POST"],
        operation_id="finalizeDeviceManifest",
        response_model=ApiEnvelope[ManifestRunReceiptData],
    )
    app.add_api_route(
        "/api/sync/manifests/{manifest_run_id}/actions",
        endpoints.list_manifest_actions,
        methods=["GET"],
        operation_id="listDeviceManifestActions",
        response_model=ApiEnvelope[ManifestActionPageData],
    )
    app.add_api_route(
        "/api/sync/manifests/{manifest_run_id}/complete",
        endpoints.complete_manifest,
        methods=["POST"],
        operation_id="completeDeviceManifest",
        response_model=ApiEnvelope[DeviceCursorReceiptData],
    )
    app.add_api_route(
        "/api/sources/{source_id}/versions/{source_version_id}/content",
        endpoints.download_source_version,
        methods=["GET"],
        operation_id="downloadDeviceSourceVersion",
        responses={
            "200": {
                "description": (
                    "The exact verified bytes of the source version as one binary "
                    "payload with its exact Content-Length, Content-Type and "
                    "X-Content-SHA256 headers"
                ),
                "content": {
                    "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
                },
                "headers": {
                    "X-Content-SHA256": {
                        "description": "The exact lowercase SHA-256 of the streamed bytes",
                        "schema": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                    },
                },
            }
        },
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
    exported snapshot alike. The same pass declares the dedicated device
    Bearer schemes of spec 16 that no route binds yet, so the published
    contract carries every credential scheme of the web authentication
    surface while route-driven rendering still wins where a route exists.
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
        _declare_device_bearer_schemes(document)
        _strip_merged_default_json_success_media(document)
        app.openapi_schema = document
        return document

    app.openapi = openapi  # type: ignore[method-assign]


def _strip_merged_default_json_success_media(document: dict[str, Any]) -> None:
    """Keep each non-JSON success content exactly its one declared payload.

    FastAPI merges a default ``application/json`` schema into every custom
    response entry regardless of the route's response class, but the
    documented non-JSON successes — the binary payload of spec 7.4 and the
    Prometheus text exposition of the policy metrics sink — answer exactly
    one declared media type, never a JSON component schema, so the merged
    default is dropped here for the operations that declared them.
    """

    for path_item in document.get("paths", {}).values():
        if not isinstance(path_item, dict):
            continue
        for operation in path_item.values():
            if not isinstance(operation, dict):
                continue
            declared_media = _NON_JSON_SUCCESS_MEDIA_BY_OPERATION.get(
                str(operation.get("operationId"))
            )
            if declared_media is None:
                continue
            success = operation.get("responses", {}).get("200")
            if not isinstance(success, dict):
                continue
            content = success.get("content")
            if isinstance(content, dict) and declared_media in content:
                content.pop("application/json", None)


def _declare_device_bearer_schemes(document: dict[str, Any]) -> None:
    """Publish the declared device Bearer schemes on the document.

    Schemes a route already renders keep their framework-generated
    definition; a declared scheme with no route binding — the access
    credential until the sync children land — is rendered from its own model
    in the same shape the framework would emit.
    """
    components = document.setdefault("components", {})
    security_schemes = components.setdefault("securitySchemes", {})
    if not isinstance(security_schemes, dict):
        return
    for scheme in _DECLARED_DEVICE_BEARER_SCHEMES:
        security_schemes.setdefault(
            scheme.scheme_name,
            scheme.model.model_dump(by_alias=True, exclude_none=True, mode="json"),
        )


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
    """Return the no-store headers when the rejected route carries the posture.

    Every authentication-bound and exclusion-policy route answers failures
    with ``Cache-Control: no-store`` (spec 16); the route is classified
    through the assigned route template or, when a dependency rejected the
    request before the endpoint published it, through the matched route path
    mapped against the same closed template vocabulary.
    """
    template = request.scope.get("route_template")
    if not isinstance(template, ApiRouteTemplate):
        matched_path = getattr(request.scope.get("route"), "path", None)
        if isinstance(matched_path, str) and matched_path in NO_STORE_ROUTE_TEMPLATE_VALUES:
            template = ApiRouteTemplate(matched_path)
        else:
            template = None
    if isinstance(template, ApiRouteTemplate) and is_no_store_route_template(template):
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
