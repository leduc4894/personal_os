"""Browser device-authorization HTTP routes over the offline composition.

These tests drive the four closed device-authorization routes through the
FastAPI ``TestClient`` against the real application factory wired with the
offline deterministic authentication composition: no database, no key file
and no environment read. They pin the exact-origin gate and per-source
creation throttle of the unauthenticated plugin endpoint, the provisioning
headers around the one-time user code and polling secret, the
fragment-only complete verification URL, the strict model rejections, the
session/CSRF requirements of the browser lookup and decision endpoints, the
recent re-authentication gate that guards approval but not denial, the closed
expired/state transitions, and ``Cache-Control: no-store`` on every response.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timedelta
from typing import Final
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
from api_runtime.application import create_api_application
from api_runtime.authentication_composition import (
    OFFLINE_WEB_ALLOWED_ORIGIN,
    OfflineAuthenticationClock,
    OfflineAuthenticationState,
    WebAuthenticationRuntime,
    compose_offline_web_authentication,
)
from api_runtime.authentication_dependencies import (
    CSRF_COOKIE_NAME,
    SESSION_COOKIE_NAME,
)
from fastapi.testclient import TestClient

from personal_os.authentication.contracts import DeviceScope
from personal_os.authentication.device_authorization import DevicePlatformClass
from personal_os.authentication.errors import AuthenticationError
from personal_os.diagnostics.context import create_diagnostic_context
from personal_os.error_contracts.codes import ErrorCode
from personal_os.runtime_configuration.models import RuntimeEnvironment

#: The offline composition accepts exactly this origin (spec 9.3).
ORIGIN: Final[str] = OFFLINE_WEB_ALLOWED_ORIGIN

_SECURE_BASE_URL: Final[str] = OFFLINE_WEB_ALLOWED_ORIGIN

_VALID_LOGIN: Final[dict[str, str]] = {
    "username": "admin",
    "password": "correct-horse-battery-staple",
}

#: The offline composition pins these approved plugin version bounds.
_OFFLINE_PLUGIN_BOUNDS: Final[tuple[str, str]] = ("1.0.0", "2.0.0")


class _ReadyProbe:
    """Readiness probe stub: the device routes never consult dependencies."""

    async def check(self) -> None: ...


@dataclass(frozen=True, slots=True)
class DeviceRouteHarness:
    """One test client bound to its shared offline clock and runtime."""

    client: TestClient
    clock: OfflineAuthenticationClock
    runtime: WebAuthenticationRuntime


@pytest.fixture
def harness() -> Iterator[DeviceRouteHarness]:
    clock = OfflineAuthenticationClock()
    state = OfflineAuthenticationState(totp_active=False)
    runtime = compose_offline_web_authentication(clock=clock, state=state)
    application = create_api_application(
        environment=RuntimeEnvironment.TEST,
        readiness_probe=_ReadyProbe(),
        web_authentication=runtime,
    )
    with TestClient(application, base_url=_SECURE_BASE_URL) as test_client:
        yield DeviceRouteHarness(client=test_client, clock=clock, runtime=runtime)


def login(
    test_client: TestClient, *, origin: str = ORIGIN, body: dict[str, str] | None = None
) -> dict[str, str]:
    """Log in and return the session and CSRF cookie values the server set."""
    response = test_client.post(
        "/api/auth/login", headers={"Origin": origin}, json=body or _VALID_LOGIN
    )
    assert response.status_code == 200, response.text
    return {
        "session": response.cookies[SESSION_COOKIE_NAME],
        "csrf": response.cookies[CSRF_COOKIE_NAME],
    }


def authenticated_headers(origin: str, cookies: dict[str, str]) -> dict[str, str]:
    """Origin plus the session and CSRF cookie/header pair of one session."""
    return {
        "Origin": origin,
        "Cookie": (
            f"{SESSION_COOKIE_NAME}={cookies['session']}; {CSRF_COOKIE_NAME}={cookies['csrf']}"
        ),
        "X-CSRF-Token": cookies["csrf"],
    }


def grant_request_body(**overrides: str) -> dict[str, str]:
    """One valid plugin grant-creation body with optional field overrides."""
    body: dict[str, str] = {
        "client_instance_id": str(uuid4()),
        "device_name": "Personal desktop",
        "platform_class": "obsidian_desktop",
        "platform_name": "windows",
        "plugin_version": "1.4.0",
        "requested_scope": "obsidian_sync",
    }
    body.update(overrides)
    return body


def create_grant(
    test_client: TestClient, *, body: dict[str, str] | None = None, origin: str = ORIGIN
) -> dict[str, object]:
    """Create one grant and return the response data payload."""
    response = test_client.post(
        "/api/auth/device-authorizations",
        headers={"Origin": origin},
        json=body if body is not None else grant_request_body(),
    )
    assert response.status_code == 200, response.text
    return dict(response.json()["data"])


def reauthenticate(test_client: TestClient, cookies: dict[str, str]) -> dict[str, str]:
    """Run one recent re-authentication and return the rotated cookies."""
    response = test_client.post(
        "/api/auth/reauthenticate",
        headers=authenticated_headers(ORIGIN, cookies),
        json={"password": _VALID_LOGIN["password"]},
    )
    assert response.status_code == 200, response.text
    return {
        "session": response.cookies[SESSION_COOKIE_NAME],
        "csrf": response.cookies[CSRF_COOKIE_NAME],
    }


# --- grant creation -------------------------------------------------------------------


def test_create_grant_returns_the_exact_provisioning_payload(
    harness: DeviceRouteHarness,
) -> None:
    data = create_grant(harness.client)
    assert set(data) == {
        "grant_id",
        "user_code",
        "polling_secret",
        "verification_uri",
        "verification_uri_complete",
        "expires_in_seconds",
        "poll_interval_seconds",
    }
    assert re.fullmatch(r"[A-Z2-9]{4}-[A-Z2-9]{4}", str(data["user_code"])) is not None
    assert str(data["polling_secret"]).startswith("pg1.")
    assert data["expires_in_seconds"] == 600
    assert data["poll_interval_seconds"] == 5


def test_create_grant_places_only_the_user_code_in_the_complete_url_fragment(
    harness: DeviceRouteHarness,
) -> None:
    data = create_grant(harness.client)
    parsed = urlsplit(str(data["verification_uri_complete"]))
    assert parsed.query == ""
    assert parsed.fragment == data["user_code"]
    assert data["polling_secret"] not in str(data["verification_uri_complete"])
    assert data["polling_secret"] not in str(data["verification_uri"])
    assert urlsplit(str(data["verification_uri"])).fragment == ""


def test_create_grant_response_carries_the_provisioning_headers(
    harness: DeviceRouteHarness,
) -> None:
    response = harness.client.post(
        "/api/auth/device-authorizations",
        headers={"Origin": ORIGIN},
        json=grant_request_body(),
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    assert response.json()["error"] is None


def test_create_grant_requires_the_exact_configured_origin(
    harness: DeviceRouteHarness,
) -> None:
    response = harness.client.post(
        "/api/auth/device-authorizations",
        headers={"Origin": "https://attacker.example"},
        json=grant_request_body(),
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "csrf_validation_failed"
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "overrides",
    [
        {"device_name": ""},  # below the one-character display bound
        {"device_name": "x" * 81},  # above the 80-character display bound
        {"platform_class": "obsidian_tv"},  # outside the closed platform classes
        {"platform_name": "Not A Platform"},  # outside the closed token grammar
        {"plugin_version": "1.4"},  # not a semantic dotted triple
        {"requested_scope": "vault_read"},  # outside the fixed scope
        {"client_instance_id": "not-a-uuid"},  # not a client instance id
    ],
)
def test_create_grant_rejects_invalid_request_fields(
    harness: DeviceRouteHarness, overrides: dict[str, str]
) -> None:
    response = harness.client.post(
        "/api/auth/device-authorizations",
        headers={"Origin": ORIGIN},
        json=grant_request_body(**overrides),
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "api_request_validation_failed"
    assert response.headers["cache-control"] == "no-store"


def test_create_grant_rejects_unsupported_plugin_versions_with_safe_bounds(
    harness: DeviceRouteHarness,
) -> None:
    for unsupported_version in ("0.9.9", "2.0.1"):
        response = harness.client.post(
            "/api/auth/device-authorizations",
            headers={"Origin": ORIGIN},
            json=grant_request_body(plugin_version=unsupported_version),
        )
        assert response.status_code == 426, response.text
        error = response.json()["error"]
        assert error["code"] == "plugin_version_unsupported"
        assert error["details"]["approved_version_bounds"] == list(_OFFLINE_PLUGIN_BOUNDS)


def test_create_grant_locks_the_source_bucket_after_five_creations(
    harness: DeviceRouteHarness,
) -> None:
    for _ in range(5):
        assert (
            harness.client.post(
                "/api/auth/device-authorizations",
                headers={"Origin": ORIGIN},
                json=grant_request_body(),
            ).status_code
            == 200
        )
    locked = harness.client.post(
        "/api/auth/device-authorizations",
        headers={"Origin": ORIGIN},
        json=grant_request_body(),
    )
    assert locked.status_code == 429
    error = locked.json()["error"]
    assert error["code"] == "authentication_rate_limited"
    assert error["retryable"] is True
    assert error["details"]["retry_after_seconds"] > 0


@pytest.mark.asyncio
async def test_create_grant_caps_live_grants_per_client_instance(
    harness: DeviceRouteHarness,
) -> None:
    client_instance_id = uuid4()

    async def create_with_source(source_bucket: str):
        return await harness.runtime.device_authorization_service.create_grant(
            client_instance_id=client_instance_id,
            device_name="Personal desktop",
            platform_class=DevicePlatformClass.OBSIDIAN_DESKTOP,
            platform_name="windows",
            plugin_version="1.4.0",
            requested_scope=DeviceScope.OBSIDIAN_SYNC,
            source_bucket=source_bucket,
            diagnostic_context=create_diagnostic_context().context,
        )

    for source_index in range(5):
        created = await create_with_source(f"cap-source-{source_index}")
        assert created.user_code
    with pytest.raises(AuthenticationError) as raised:
        await create_with_source("cap-source-final")
    assert raised.value.error_code is ErrorCode.AUTHENTICATION_RATE_LIMITED


# --- browser lookup -------------------------------------------------------------------


def test_lookup_requires_an_authenticated_session(
    harness: DeviceRouteHarness,
) -> None:
    response = harness.client.post(
        "/api/auth/device-authorizations/lookup",
        headers={"Origin": ORIGIN},
        json={"user_code": "ABCDEFG-W"},
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_required"
    assert response.headers["cache-control"] == "no-store"


def test_lookup_resolves_the_pending_grant_display_context(
    harness: DeviceRouteHarness,
) -> None:
    cookies = login(harness.client)
    data = create_grant(harness.client)
    response = harness.client.post(
        "/api/auth/device-authorizations/lookup",
        headers=authenticated_headers(ORIGIN, cookies),
        json={"user_code": data["user_code"]},
    )
    assert response.status_code == 200, response.text
    resolved = response.json()["data"]
    assert set(resolved) == {
        "grant_id",
        "user_code",
        "device_name",
        "platform_class",
        "platform_name",
        "plugin_version",
        "requested_scope",
        "expires_at",
    }
    assert resolved["grant_id"] == data["grant_id"]
    assert resolved["user_code"] == data["user_code"]
    assert resolved["device_name"] == "Personal desktop"
    assert resolved["platform_class"] == "obsidian_desktop"
    assert resolved["platform_name"] == "windows"
    assert resolved["plugin_version"] == "1.4.0"
    assert resolved["requested_scope"] == "obsidian_sync"
    assert resolved["expires_at"]
    assert response.headers["cache-control"] == "no-store"


def test_lookup_rejects_unknown_user_codes_closed(
    harness: DeviceRouteHarness,
) -> None:
    cookies = login(harness.client)
    # Grammar-shaped but checksum-invalid: it passes the request model and
    # fails the domain checksum, closing as an invalid device credential.
    response = harness.client.post(
        "/api/auth/device-authorizations/lookup",
        headers=authenticated_headers(ORIGIN, cookies),
        json={"user_code": "ZZZZ-ZZZZ"},
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "device_credential_invalid"


def test_lookup_rejects_grammar_invalid_user_codes(
    harness: DeviceRouteHarness,
) -> None:
    cookies = login(harness.client)
    response = harness.client.post(
        "/api/auth/device-authorizations/lookup",
        headers=authenticated_headers(ORIGIN, cookies),
        json={"user_code": "abcdefgh"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "api_request_validation_failed"


def test_lookup_reports_denied_and_expired_grants_through_closed_codes(
    harness: DeviceRouteHarness,
) -> None:
    cookies = login(harness.client)
    denied_grant = create_grant(harness.client)
    denial = harness.client.post(
        f"/api/auth/device-authorizations/{denied_grant['grant_id']}/deny",
        headers=authenticated_headers(ORIGIN, cookies),
    )
    assert denial.status_code == 200, denial.text
    denied_lookup = harness.client.post(
        "/api/auth/device-authorizations/lookup",
        headers=authenticated_headers(ORIGIN, cookies),
        json={"user_code": denied_grant["user_code"]},
    )
    assert denied_lookup.status_code == 403
    assert denied_lookup.json()["error"]["code"] == "device_authorization_denied"

    expired_grant = create_grant(harness.client)
    harness.clock.database_now_value += timedelta(minutes=11)
    expired_lookup = harness.client.post(
        "/api/auth/device-authorizations/lookup",
        headers=authenticated_headers(ORIGIN, cookies),
        json={"user_code": expired_grant["user_code"]},
    )
    assert expired_lookup.status_code == 410
    assert expired_lookup.json()["error"]["code"] == "device_authorization_expired"


# --- approval -------------------------------------------------------------------------


def test_approve_requires_recent_reauthentication(
    harness: DeviceRouteHarness,
) -> None:
    cookies = login(harness.client)
    data = create_grant(harness.client)
    harness.clock.database_now_value += timedelta(minutes=6)
    stale = harness.client.post(
        f"/api/auth/device-authorizations/{data['grant_id']}/approve",
        headers=authenticated_headers(ORIGIN, cookies),
    )
    assert stale.status_code == 403
    assert stale.json()["error"]["code"] == "recent_authentication_required"
    assert stale.headers["cache-control"] == "no-store"


def test_approve_after_reauthentication_approves_the_grant(
    harness: DeviceRouteHarness,
) -> None:
    cookies = login(harness.client)
    data = create_grant(harness.client)
    harness.clock.database_now_value += timedelta(minutes=6)
    rotated_cookies = reauthenticate(harness.client, cookies)
    approved = harness.client.post(
        f"/api/auth/device-authorizations/{data['grant_id']}/approve",
        headers=authenticated_headers(ORIGIN, rotated_cookies),
    )
    assert approved.status_code == 200, approved.text
    payload = approved.json()["data"]
    assert payload["grant_id"] == data["grant_id"]
    assert payload["state"] == "approved"
    assert approved.headers["cache-control"] == "no-store"


def test_approve_twice_reports_the_closed_state_conflict(
    harness: DeviceRouteHarness,
) -> None:
    cookies = login(harness.client)
    data = create_grant(harness.client)
    first = harness.client.post(
        f"/api/auth/device-authorizations/{data['grant_id']}/approve",
        headers=authenticated_headers(ORIGIN, cookies),
    )
    assert first.status_code == 200, first.text
    second = harness.client.post(
        f"/api/auth/device-authorizations/{data['grant_id']}/approve",
        headers=authenticated_headers(ORIGIN, cookies),
    )
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "device_authorization_state_invalid"


def test_approve_after_deny_reports_the_closed_state_conflict(
    harness: DeviceRouteHarness,
) -> None:
    cookies = login(harness.client)
    data = create_grant(harness.client)
    denied = harness.client.post(
        f"/api/auth/device-authorizations/{data['grant_id']}/deny",
        headers=authenticated_headers(ORIGIN, cookies),
    )
    assert denied.status_code == 200, denied.text
    approved = harness.client.post(
        f"/api/auth/device-authorizations/{data['grant_id']}/approve",
        headers=authenticated_headers(ORIGIN, cookies),
    )
    assert approved.status_code == 409
    assert approved.json()["error"]["code"] == "device_authorization_state_invalid"


def test_approve_of_an_expired_grant_reports_the_closed_expired_code(
    harness: DeviceRouteHarness,
) -> None:
    cookies = login(harness.client)
    data = create_grant(harness.client)
    harness.clock.database_now_value += timedelta(minutes=6)
    rotated_cookies = reauthenticate(harness.client, cookies)
    harness.clock.database_now_value += timedelta(minutes=4, seconds=30)
    expired = harness.client.post(
        f"/api/auth/device-authorizations/{data['grant_id']}/approve",
        headers=authenticated_headers(ORIGIN, rotated_cookies),
    )
    assert expired.status_code == 410
    assert expired.json()["error"]["code"] == "device_authorization_expired"
    assert expired.headers["cache-control"] == "no-store"


# --- denial ---------------------------------------------------------------------------


def test_deny_works_without_recent_reauthentication(
    harness: DeviceRouteHarness,
) -> None:
    cookies = login(harness.client)
    data = create_grant(harness.client)
    harness.clock.database_now_value += timedelta(minutes=6)
    denied = harness.client.post(
        f"/api/auth/device-authorizations/{data['grant_id']}/deny",
        headers=authenticated_headers(ORIGIN, cookies),
    )
    assert denied.status_code == 200, denied.text
    payload = denied.json()["data"]
    assert payload["grant_id"] == data["grant_id"]
    assert payload["state"] == "denied"
    assert denied.headers["cache-control"] == "no-store"


def test_deny_of_an_expired_grant_reports_the_closed_expired_code(
    harness: DeviceRouteHarness,
) -> None:
    cookies = login(harness.client)
    data = create_grant(harness.client)
    harness.clock.database_now_value += timedelta(minutes=11)
    denied = harness.client.post(
        f"/api/auth/device-authorizations/{data['grant_id']}/deny",
        headers=authenticated_headers(ORIGIN, cookies),
    )
    assert denied.status_code == 410
    assert denied.json()["error"]["code"] == "device_authorization_expired"


# --- shared route guards --------------------------------------------------------------


def test_decision_routes_reject_a_missing_csrf_pair(
    harness: DeviceRouteHarness,
) -> None:
    cookies = login(harness.client)
    data = create_grant(harness.client)
    response = harness.client.post(
        f"/api/auth/device-authorizations/{data['grant_id']}/deny",
        headers={"Origin": ORIGIN, "Cookie": f"{SESSION_COOKIE_NAME}={cookies['session']}"},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "csrf_validation_failed"
    assert response.headers["cache-control"] == "no-store"


def test_unknown_grant_decisions_fail_closed(
    harness: DeviceRouteHarness,
) -> None:
    cookies = login(harness.client)
    response = harness.client.post(
        f"/api/auth/device-authorizations/{uuid4()}/deny",
        headers=authenticated_headers(ORIGIN, cookies),
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "device_credential_invalid"
