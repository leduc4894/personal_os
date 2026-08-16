"""Closed authentication route set: exact membership, methods, no aliases.

These tests compose the real application (factory, envelope handlers, request
correlation and web security middleware) over the offline deterministic
authentication ports and pin the served route set of the whole web
authentication surface: exactly the two health routes, the local/test OpenAPI
document route and the spec 16.1-16.4 session, TOTP, device and Admin routes —
each with exactly its method — and nothing else. Trailing-slash aliases do not
exist (``redirect_slashes=False``): the canonical path serves, the slashed
spelling collapses to the safe route-not-found envelope.
"""

from __future__ import annotations

from typing import Final

import httpx
import pytest
from api_runtime.application import create_api_application
from api_runtime.authentication_composition import compose_offline_web_authentication
from fastapi import FastAPI

from personal_os.runtime_configuration.models import RuntimeEnvironment

#: The closed route set with exactly the method each path serves; only the
#: Starlette document route derives ``HEAD`` from ``GET``. No other verb
#: exists on any path.
EXPECTED_ROUTE_METHODS: Final[dict[str, frozenset[str]]] = {
    "/api/openapi.json": frozenset({"GET", "HEAD"}),
    "/api/health/live": frozenset({"GET"}),
    "/api/health/ready": frozenset({"GET"}),
    "/api/auth/login": frozenset({"POST"}),
    "/api/auth/session": frozenset({"GET"}),
    "/api/auth/logout": frozenset({"POST"}),
    "/api/auth/reauthenticate": frozenset({"POST"}),
    "/api/auth/password": frozenset({"PUT"}),
    "/api/auth/totp/verify": frozenset({"POST"}),
    "/api/auth/totp/enrollments": frozenset({"POST"}),
    "/api/auth/totp/enrollments/{enrollment_id}/verify": frozenset({"POST"}),
    "/api/auth/totp/recovery": frozenset({"POST"}),
    "/api/auth/totp/recovery-codes/regenerate": frozenset({"POST"}),
    "/api/auth/totp": frozenset({"DELETE"}),
    "/api/auth/device-authorizations": frozenset({"POST"}),
    "/api/auth/device-authorizations/lookup": frozenset({"POST"}),
    "/api/auth/device-authorizations/{grant_id}/approve": frozenset({"POST"}),
    "/api/auth/device-authorizations/{grant_id}/deny": frozenset({"POST"}),
    "/api/auth/device-authorizations/{grant_id}/poll": frozenset({"POST"}),
    "/api/auth/device-tokens/refresh": frozenset({"POST"}),
    "/api/auth/device-tokens/revoke-current": frozenset({"POST"}),
    "/api/admin/devices": frozenset({"GET"}),
    "/api/admin/devices/{device_id}/revoke": frozenset({"POST"}),
}


class _ReadyProbe:
    """Readiness probe stub: route-set inspection never consults it."""

    async def check(self) -> None: ...


@pytest.fixture
def application() -> FastAPI:
    return create_api_application(
        environment=RuntimeEnvironment.TEST,
        readiness_probe=_ReadyProbe(),
        web_authentication=compose_offline_web_authentication(),
    )


def test_served_route_set_is_exactly_health_openapi_and_spec_16(application: FastAPI) -> None:
    served: dict[str, frozenset[str]] = {}
    for route in application.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        assert isinstance(path, str), route
        assert isinstance(methods, set), path
        assert path not in served, f"duplicate route registration: {path}"
        served[path] = frozenset(methods)
    assert served == EXPECTED_ROUTE_METHODS


def test_no_route_registers_a_trailing_slash_alias(application: FastAPI) -> None:
    for route in application.routes:
        path = getattr(route, "path", None)
        assert isinstance(path, str), route
        assert not path.endswith("/"), path


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/api/health/live/",
        "/api/health/ready/",
        "/api/auth/login/",
        "/api/auth/session/",
        "/api/auth/totp/",
        "/api/auth/device-authorizations/",
        "/api/auth/device-tokens/refresh/",
        "/api/admin/devices/",
    ],
)
async def test_trailing_slash_spelling_is_not_an_alias(application: FastAPI, path: str) -> None:
    transport = httpx.ASGITransport(app=application, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(path)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "api_route_not_found"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("DELETE", "/api/auth/session"),
        ("GET", "/api/auth/login"),
        ("PUT", "/api/auth/totp/verify"),
        ("PATCH", "/api/admin/devices"),
    ],
)
async def test_unknown_method_answers_the_safe_method_not_allowed_envelope(
    application: FastAPI, method: str, path: str
) -> None:
    transport = httpx.ASGITransport(app=application, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.request(method, path)
    assert response.status_code == 405
    assert response.json()["error"]["code"] == "api_method_not_allowed"


@pytest.mark.asyncio
async def test_composed_application_emits_the_security_headers(application: FastAPI) -> None:
    # Pins the application-factory wiring: the web security headers middleware
    # wraps the built stack, so success and error envelopes alike carry the
    # nonce CSP, the referrer policy and nosniff, with a fresh nonce per
    # response (spec 20.2).
    transport = httpx.ASGITransport(app=application, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        success = await client.get("/api/health/live")
        rejection = await client.get("/not-a-route")
    for response in (success, rejection):
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["x-content-type-options"] == "nosniff"
        policy = response.headers["content-security-policy"]
        assert policy.startswith("default-src 'self'; script-src 'self' 'nonce-")
        assert policy.endswith(
            "connect-src 'self'; img-src 'self' data:; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        )
    success_nonce = success.headers["content-security-policy"].split("'nonce-")[1].split("'")[0]
    rejection_nonce = rejection.headers["content-security-policy"].split("'nonce-")[1].split("'")[0]
    assert success_nonce and rejection_nonce
    assert success_nonce != rejection_nonce
