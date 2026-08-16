"""Session/security HTTP contract of the composed authentication routes.

These tests run against the real composed application (factory, envelope
handlers and request correlation middleware) over the offline deterministic
authentication ports, and pin the cross-cutting security contract of spec 9,
16 and 17: every authentication response — success, rejection or validation
failure — carries ``Cache-Control: no-store`` and the canonical envelope shape,
credentials and rejected values never echo into any response surface, the
cookie contract keeps the browser-session ``__Host-`` Secure HttpOnly session
binding and the readable CSRF binding, and the closed route set refuses
trailing-slash aliases and unknown methods with the safe framework envelopes.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Final

import pytest
from api_runtime.application import create_api_application
from api_runtime.authentication_composition import (
    OFFLINE_WEB_ALLOWED_ORIGIN,
    compose_offline_web_authentication,
)
from api_runtime.authentication_dependencies import (
    CSRF_COOKIE_NAME,
    SESSION_COOKIE_NAME,
)
from fastapi.testclient import TestClient

from personal_os.runtime_configuration.models import RuntimeEnvironment

_ORIGIN: Final[str] = OFFLINE_WEB_ALLOWED_ORIGIN
_VALID_LOGIN: Final[dict[str, str]] = {
    "username": "admin",
    "password": "correct-horse-battery-staple",
}
_PASSWORD_SENTINEL: Final[str] = "sentinel-password-do-not-emit"
_ENVELOPE_KEYS: Final[frozenset[str]] = frozenset(
    {"request_id", "data", "warnings", "error"}
)


class _ReadyProbe:
    """Readiness probe stub: the authentication routes never consult it."""

    async def check(self) -> None: ...


@pytest.fixture
def client() -> Iterator[TestClient]:
    application = create_api_application(
        environment=RuntimeEnvironment.TEST,
        readiness_probe=_ReadyProbe(),
        web_authentication=compose_offline_web_authentication(),
    )
    with TestClient(application, base_url=_ORIGIN) as test_client:
        yield test_client


@pytest.mark.parametrize(
    ("method", "path", "headers", "json_body", "expected_status", "expected_code"),
    [
        (
            "POST",
            "/api/auth/login",
            {"Origin": _ORIGIN},
            _VALID_LOGIN,
            200,
            None,
        ),
        (
            "POST",
            "/api/auth/login",
            {"Origin": "https://attacker.example"},
            _VALID_LOGIN,
            403,
            "csrf_validation_failed",
        ),
        (
            "POST",
            "/api/auth/login",
            {"Origin": _ORIGIN},
            {"username": "admin", "password": _PASSWORD_SENTINEL},
            401,
            "authentication_failed",
        ),
        (
            "GET",
            "/api/auth/session",
            {"Origin": _ORIGIN},
            None,
            401,
            "authentication_required",
        ),
        (
            "PUT",
            "/api/auth/password",
            {},
            {"new_password": "fresh-trebuchet-unlock-phrase"},
            403,
            "csrf_validation_failed",
        ),
        ("POST", "/api/auth/logout", {}, None, 403, "csrf_validation_failed"),
    ],
)
def test_every_authentication_response_is_never_stored(
    client: TestClient,
    method: str,
    path: str,
    headers: dict[str, str],
    json_body: dict[str, str] | None,
    expected_status: int,
    expected_code: str | None,
) -> None:
    response = client.request(method, path, headers=headers, json=json_body)
    assert response.status_code == expected_status
    assert response.headers["cache-control"] == "no-store"
    payload = response.json()
    assert set(payload) == _ENVELOPE_KEYS
    if expected_code is None:
        assert payload["error"] is None
    else:
        assert payload["error"]["code"] == expected_code
        assert payload["data"] is None


def test_validation_failures_on_authentication_routes_are_never_stored(
    client: TestClient,
) -> None:
    response = client.post(
        "/api/auth/login",
        headers={"Origin": _ORIGIN},
        json={"username": "admin", "password": "short"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "api_request_validation_failed"
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    ("method", "path", "json_body"),
    [
        ("POST", "/api/auth/login", _VALID_LOGIN),
        ("PUT", "/api/auth/password", {"new_password": "fresh-trebuchet-unlock-phrase"}),
    ],
)
def test_non_ascii_attacker_headers_close_with_documented_codes(
    client: TestClient, method: str, path: str, json_body: dict[str, str]
) -> None:
    # Starlette decodes headers latin-1, so raw non-ASCII bytes reach the
    # origin and CSRF comparisons as non-ASCII strings: both must close with
    # the documented 403 envelope, never an internal-error 500.
    non_ascii_pair = "f\xf6rged-equal-pair".encode("latin-1")
    login_response = client.post("/api/auth/login", headers={"Origin": _ORIGIN}, json=_VALID_LOGIN)
    assert login_response.status_code == 200
    session_cookie = login_response.cookies[SESSION_COOKIE_NAME]
    response = client.request(
        method,
        path,
        headers={
            "Origin": "https://attacker.example/\xfc".encode("latin-1")
            if method == "POST"
            else _ORIGIN,
            "Cookie": (
                f"{SESSION_COOKIE_NAME}={session_cookie}; "
                f"{CSRF_COOKIE_NAME}=".encode("ascii") + non_ascii_pair
            ),
            "X-CSRF-Token": non_ascii_pair,
        },
        json=json_body,
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "csrf_validation_failed"
    assert response.headers["cache-control"] == "no-store"


def test_rejected_credentials_never_reach_any_response_surface(client: TestClient) -> None:
    response = client.post(
        "/api/auth/login",
        headers={"Origin": _ORIGIN},
        json={"username": "admin", "password": _PASSWORD_SENTINEL},
    )
    assert response.status_code == 401
    rendered_headers = "\n".join(f"{name}: {value}" for name, value in response.headers.items())
    assert _PASSWORD_SENTINEL not in response.text
    assert _PASSWORD_SENTINEL not in rendered_headers


def test_login_sets_the_browser_session_cookie_contract(client: TestClient) -> None:
    response = client.post("/api/auth/login", headers={"Origin": _ORIGIN}, json=_VALID_LOGIN)
    assert response.status_code == 200
    session_cookie = next(
        cookie
        for cookie in response.headers.get_list("set-cookie")
        if cookie.startswith(f"{SESSION_COOKIE_NAME}=")
    )
    csrf_cookie = next(
        cookie
        for cookie in response.headers.get_list("set-cookie")
        if cookie.startswith(f"{CSRF_COOKIE_NAME}=")
    )
    assert "Secure" in session_cookie
    assert "HttpOnly" in session_cookie
    assert "SameSite=lax" in session_cookie
    assert "Path=/" in session_cookie
    assert "Domain" not in session_cookie
    assert "HttpOnly" not in csrf_cookie
    assert "Secure" in csrf_cookie


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/auth/login/"),
        ("GET", "/api/auth/session/"),
        ("POST", "/api/auth/logout/"),
        ("POST", "/api/auth/reauthenticate/"),
        ("PUT", "/api/auth/password/"),
        ("DELETE", "/api/auth/session"),
        ("GET", "/api/auth/password"),
    ],
)
def test_route_set_stays_closed(client: TestClient, method: str, path: str) -> None:
    response = client.request(method, path, headers={"Origin": _ORIGIN})
    assert response.status_code in (404, 405)
    assert response.json()["error"]["code"] in {
        "api_route_not_found",
        "api_method_not_allowed",
    }


def test_authentication_responses_carry_the_correlation_contract(client: TestClient) -> None:
    response = client.get("/api/auth/session", headers={"Origin": _ORIGIN})
    payload = response.json()
    assert payload["request_id"] == response.headers["x-request-id"]
    assert "traceparent" in response.headers
