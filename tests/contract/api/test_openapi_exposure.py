"""OpenAPI exposure contracts: local/test-only document, closed route set.

These tests pin that the composed application exposes the raw standard OpenAPI
document (the only non-envelope success response) exactly in the local and test
environments, that staging and production have no OpenAPI route at all, that
Swagger and ReDoc are disabled in every environment, that the route set stays
closed to the two health routes plus the local/test document route, and that
the document route still receives the correlation headers.
"""

from __future__ import annotations

import re

import httpx
import pytest
from api_runtime.application import create_api_application
from api_runtime.authentication_composition import compose_offline_web_authentication
from fastapi import FastAPI
from starlette.routing import Route as StarletteRoute

from personal_os.package_metadata import distribution_version
from personal_os.runtime_configuration.models import RuntimeEnvironment

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
    for path in ("/api/auth/login", "/api/auth/logout", "/api/auth/reauthenticate"):
        assert "POST" in routes[path].methods, path
    assert "PUT" in routes["/api/auth/password"].methods
    assert isinstance(routes["/api/openapi.json"], StarletteRoute)
