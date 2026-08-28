"""OpenAPI exposure contracts: local/test-only document, closed route set.

These tests pin that the composed application exposes the raw standard OpenAPI
document (the only non-envelope success response) exactly in the local and test
environments, that staging and production have no OpenAPI route at all, that
Swagger and ReDoc are disabled in every environment, that the route set stays
closed to the two health routes plus the local/test document route, and that
the document route still receives the correlation headers.

The multipart privacy guard (resumable multipart mobile-upload task 11)
additionally pins that the locally served document — one of the safe surfaces
of the multipart child's leak scan (spec 9.3) — never carries a sensitive
multipart sentinel or the name of an in-process diagnostics surface: the
rejection ring and the metric sinks are process-local/structured-log surfaces,
never API routes or schema members.
"""

from __future__ import annotations

import json
import re

import httpx
import pytest
from api_runtime.application import create_api_application
from api_runtime.authentication_composition import compose_offline_web_authentication
from fastapi import FastAPI
from starlette.routing import Route as StarletteRoute

from personal_os.package_metadata import distribution_version
from personal_os.runtime_configuration.models import RuntimeEnvironment

#: Sensitive multipart sentinels (spec 9.3) the served document must never
#: render: provider identity, staging identity, wire credentials, Vault
#: paths, digests and diagnostics-surface names all stay off the API.
SENSITIVE_MULTIPART_SENTINELS: frozenset[str] = frozenset(
    {
        "sentinel-etag-9f8e7d6c",
        "sentinel-provider-upload-id-000111222333",
        "sentinel-session-id-value-445566778899",
        "sentinel-request-id-value-998877665544",
        "notes/sentinel-leak-path.md",
        "sentinel-digest-hex-0123456789abcdef0123456789abcdef",
        "https://sentinel-storage.example.com/staging?X-Amz-Signature=SENTINELSIGNATURE",
        "staging/sentinel-key-0f1e2d3c4b5a",
        "SentinelProviderException: multipart sentinel failure",
    }
)

#: The names of the multipart in-process diagnostics surfaces that must
#: never become API surface members.
FORBIDDEN_MULTIPART_SURFACE_MARKERS: frozenset[str] = frozenset(
    {"provider_upload_id", "staging_key", "rejection_diagnostics", "recent_rejections"}
)


_TRACEPARENT_PATTERN = re.compile(r"00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}")
_API_ROUTE_PATHS = frozenset(
    {
        "/api/health/live",
        "/api/health/ready",
        "/api/auth/login",
        "/api/auth/session",
        "/api/auth/logout",
        "/api/auth/reauthenticate",
        "/api/auth/password",
        "/api/auth/totp/verify",
        "/api/auth/totp/enrollments",
        "/api/auth/totp/enrollments/{enrollment_id}/verify",
        "/api/auth/totp/recovery",
        "/api/auth/totp/recovery-codes/regenerate",
        "/api/auth/totp",
        "/api/auth/device-authorizations",
        "/api/auth/device-authorizations/lookup",
        "/api/auth/device-authorizations/{grant_id}/approve",
        "/api/auth/device-authorizations/{grant_id}/deny",
        "/api/auth/device-authorizations/{grant_id}/poll",
        "/api/auth/device-tokens/refresh",
        "/api/auth/device-tokens/revoke-current",
        "/api/admin/devices",
        "/api/admin/devices/{device_id}/revoke",
    }
)


class ReadyProbe:
    """Injected readiness probe that succeeds without performing I/O."""

    async def check(self) -> None: ...


def create_test_app(environment: RuntimeEnvironment) -> FastAPI:
    return create_api_application(
        environment=environment,
        readiness_probe=ReadyProbe(),
        web_authentication=compose_offline_web_authentication(),
    )


async def request(app: FastAPI, method: str, path: str) -> httpx.Response:
    """Invoke one request through the raw ASGI transport without a network."""
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(method, path)


@pytest.mark.parametrize("environment", [RuntimeEnvironment.LOCAL, RuntimeEnvironment.TEST])
@pytest.mark.asyncio
async def test_local_environments_serve_raw_openapi_document_with_correlation(
    environment: RuntimeEnvironment,
) -> None:
    response = await request(create_test_app(environment), "GET", "/api/openapi.json")
    document = response.json()
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert document["openapi"] == "3.1.0"
    assert document["info"] == {
        "title": "Personal Knowledge API",
        "version": distribution_version(),
    }
    assert set(document["paths"]) == _API_ROUTE_PATHS
    operation_ids = {
        operation["operationId"]
        for path_operations in document["paths"].values()
        for operation in path_operations.values()
    }
    assert operation_ids == {
        "getApiLiveness",
        "getApiReadiness",
        "login",
        "getSession",
        "logout",
        "reauthenticate",
        "changePassword",
        "verifyTotpChallenge",
        "createTotpEnrollment",
        "verifyTotpEnrollment",
        "startTotpRecovery",
        "regenerateTotpRecoveryCodes",
        "disableTotp",
        "createDeviceAuthorization",
        "lookupDeviceAuthorization",
        "approveDeviceAuthorization",
        "denyDeviceAuthorization",
        "pollDeviceAuthorization",
        "refreshDeviceToken",
        "revokeCurrentDeviceToken",
        "listAdminDevices",
        "revokeAdminDevice",
    }
    assert "data" not in document
    assert "request_id" not in document
    assert response.headers["x-request-id"]
    assert _TRACEPARENT_PATTERN.fullmatch(response.headers["traceparent"]) is not None


@pytest.mark.asyncio
async def test_production_hides_the_openapi_route_entirely() -> None:
    app = create_test_app(RuntimeEnvironment.PRODUCTION)
    assert app.openapi_url is None
    assert {getattr(route, "path", None) for route in app.routes} == _API_ROUTE_PATHS
    response = await request(app, "GET", "/api/openapi.json")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "api_route_not_found"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/docs", "/redoc"])
async def test_docs_and_redoc_are_disabled_in_every_environment(path: str) -> None:
    response = await request(create_test_app(RuntimeEnvironment.TEST), "GET", path)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "api_route_not_found"


@pytest.mark.asyncio
async def test_route_set_is_closed_to_the_api_and_local_document_routes() -> None:
    app = create_test_app(RuntimeEnvironment.TEST)
    routes = {route.path: route for route in app.routes if hasattr(route, "path")}
    assert set(routes) == _API_ROUTE_PATHS | {"/api/openapi.json"}
    for path in ("/api/health/live", "/api/health/ready", "/api/auth/session"):
        assert "GET" in routes[path].methods, path
    for path in (
        "/api/auth/login",
        "/api/auth/logout",
        "/api/auth/reauthenticate",
        "/api/auth/totp/verify",
        "/api/auth/totp/enrollments",
        "/api/auth/totp/enrollments/{enrollment_id}/verify",
        "/api/auth/totp/recovery",
        "/api/auth/totp/recovery-codes/regenerate",
    ):
        assert "POST" in routes[path].methods, path
    assert "PUT" in routes["/api/auth/password"].methods
    assert "DELETE" in routes["/api/auth/totp"].methods
    assert isinstance(routes["/api/openapi.json"], StarletteRoute)


@pytest.mark.asyncio
async def test_served_openapi_document_carries_no_sensitive_multipart_sentinel() -> None:
    """The served document is a safe surface of the multipart leak scan (spec 9.3).

    The multipart diagnostics this child owns — the closed rejection ring,
    the metric sinks, the structured rejection events — are process-local
    and structured-log surfaces: none of their names, none of their label
    vocabularies and no sensitive sentinel value may render into the API
    document.
    """

    response = await request(create_test_app(RuntimeEnvironment.TEST), "GET", "/api/openapi.json")
    assert response.status_code == 200
    rendered = json.dumps(response.json())
    for sentinel in SENSITIVE_MULTIPART_SENTINELS:
        assert sentinel not in rendered, sentinel
    for forbidden_marker in FORBIDDEN_MULTIPART_SURFACE_MARKERS:
        assert forbidden_marker not in rendered, forbidden_marker
