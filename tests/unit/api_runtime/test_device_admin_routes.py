"""Admin device list and revoke routes over the offline composition.

These tests drive the two closed Admin device routes through the FastAPI
``TestClient`` against the real application factory wired with the offline
deterministic authentication composition. The devices come from real grant
exchanges through the poll route, so the list answers with the validated
platform/plugin metadata of the exchanged grant while the system bootstrap
device stays invisible. The revoke route proves the whole guard chain of
spec 14.1: the strict active-session origin gate, the CSRF triple check, the
recent re-authentication window, the exact stored display-name confirmation
(the closed 409 mismatch code on anything else), the one-transaction
revocation of the device, its families, its tokens and the grants claiming
its identity, and the terminal codes the plugin's credentials answer with
afterwards. Every response carries ``Cache-Control: no-store``.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timedelta
from typing import Final
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

from personal_os.runtime_configuration.models import RuntimeEnvironment

#: The offline composition accepts exactly this origin (spec 9.3).
ORIGIN: Final[str] = OFFLINE_WEB_ALLOWED_ORIGIN

_SECURE_BASE_URL: Final[str] = OFFLINE_WEB_ALLOWED_ORIGIN

_VALID_LOGIN: Final[dict[str, str]] = {
    "username": "admin",
    "password": "correct-horse-battery-staple",
}


class _ReadyProbe:
    """Readiness probe stub: the admin routes never consult dependencies."""

    async def check(self) -> None: ...


@dataclass(frozen=True, slots=True)
class AdminRouteHarness:
    """One test client bound to its shared offline clock and runtime."""

    client: TestClient
    clock: OfflineAuthenticationClock
    runtime: WebAuthenticationRuntime


@pytest.fixture
def harness() -> Iterator[AdminRouteHarness]:
    clock = OfflineAuthenticationClock()
    state = OfflineAuthenticationState(totp_active=False)
    runtime = compose_offline_web_authentication(clock=clock, state=state)
    application = create_api_application(
        environment=RuntimeEnvironment.TEST,
        readiness_probe=_ReadyProbe(),
        web_authentication=runtime,
    )
    with TestClient(application, base_url=_SECURE_BASE_URL) as test_client:
        yield AdminRouteHarness(client=test_client, clock=clock, runtime=runtime)


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


@dataclass(frozen=True, slots=True)
class RegisteredDevice:
    """One exchanged device with the credentials the plugin holds."""

    device_id: str
    device_name: str
    access_credential: str
    refresh_credential: str


def register_device(
    harness: AdminRouteHarness, cookies: dict[str, str], *, device_name: str = "Personal desktop"
) -> RegisteredDevice:
    """Create, approve and exchange one grant through the HTTP routes."""
    created = harness.client.post(
        "/api/auth/device-authorizations",
        headers={"Origin": ORIGIN},
        json={
            "client_instance_id": str(uuid4()),
            "device_name": device_name,
            "platform_class": "obsidian_desktop",
            "platform_name": "windows",
            "plugin_version": "1.4.0",
            "requested_scope": "obsidian_sync",
        },
    )
    assert created.status_code == 200, created.text
    data = dict(created.json()["data"])
    approved = harness.client.post(
        f"/api/auth/device-authorizations/{data['grant_id']}/approve",
        headers=authenticated_headers(ORIGIN, cookies),
    )
    assert approved.status_code == 200, approved.text
    exchanged = harness.client.post(
        f"/api/auth/device-authorizations/{data['grant_id']}/poll",
        headers={"Authorization": f"Bearer {data['polling_secret']}"},
    )
    assert exchanged.status_code == 200, exchanged.text
    payload = dict(exchanged.json()["data"])
    return RegisteredDevice(
        device_id=str(payload["device_id"]),
        device_name=device_name,
        access_credential=str(payload["access_credential"]),
        refresh_credential=str(payload["refresh_credential"]),
    )


def list_devices(harness: AdminRouteHarness, cookies: dict[str, str]):
    """Fetch the Admin device list behind the authenticated session."""
    return harness.client.get("/api/admin/devices", headers=authenticated_headers(ORIGIN, cookies))


def revoke_device(
    harness: AdminRouteHarness,
    cookies: dict[str, str],
    device_id: str,
    *,
    device_name_confirmation: str,
):
    """Post one Admin device revocation with its name confirmation."""
    return harness.client.post(
        f"/api/admin/devices/{device_id}/revoke",
        headers=authenticated_headers(ORIGIN, cookies),
        json={"device_name_confirmation": device_name_confirmation},
    )


# --- the device list -------------------------------------------------------------------


def test_device_list_requires_an_authenticated_session(harness: AdminRouteHarness) -> None:
    # The exact-origin gate closes the request before any session material
    # is read, exactly like every other session-bound GET (spec 9.3).
    missing_origin = harness.client.get("/api/admin/devices")
    assert missing_origin.status_code == 403
    assert missing_origin.json()["error"]["code"] == "csrf_validation_failed"
    assert missing_origin.headers["cache-control"] == "no-store"

    missing_session = harness.client.get("/api/admin/devices", headers={"Origin": ORIGIN})
    assert missing_session.status_code == 401
    assert missing_session.json()["error"]["code"] == "authentication_required"
    assert missing_session.headers["cache-control"] == "no-store"

    wrong_origin = harness.client.get(
        "/api/admin/devices", headers={"Origin": "https://attacker.example"}
    )
    assert wrong_origin.status_code == 403
    assert wrong_origin.json()["error"]["code"] == "csrf_validation_failed"


def test_device_list_excludes_the_bootstrap_device_and_joins_the_grant_metadata(
    harness: AdminRouteHarness,
) -> None:
    cookies = login(harness.client)
    device = register_device(harness, cookies)
    response = list_devices(harness, cookies)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    devices = response.json()["data"]["devices"]
    assert [entry["device_id"] for entry in devices] == [device.device_id]
    entry = devices[0]
    assert set(entry) == {
        "device_id",
        "device_name",
        "platform_class",
        "platform_name",
        "plugin_version",
        "status",
        "registered_at",
        "last_seen_at",
        "revoked_at",
        "family_absolute_expires_at",
    }
    assert entry["device_name"] == "Personal desktop"
    assert entry["platform_class"] == "obsidian_desktop"
    assert entry["platform_name"] == "windows"
    assert entry["plugin_version"] == "1.4.0"
    assert entry["status"] == "active"
    assert entry["revoked_at"] is None
    assert entry["registered_at"]
    assert entry["family_absolute_expires_at"]


# --- admin revoke guards ---------------------------------------------------------------


def test_admin_revoke_requires_the_csrf_pair(harness: AdminRouteHarness) -> None:
    cookies = login(harness.client)
    device = register_device(harness, cookies)
    response = harness.client.post(
        f"/api/admin/devices/{device.device_id}/revoke",
        headers={
            "Origin": ORIGIN,
            "Cookie": f"{SESSION_COOKIE_NAME}={cookies['session']}",
        },
        json={"device_name_confirmation": device.device_name},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "csrf_validation_failed"
    assert response.headers["cache-control"] == "no-store"


def test_admin_revoke_requires_recent_reauthentication(harness: AdminRouteHarness) -> None:
    cookies = login(harness.client)
    device = register_device(harness, cookies)
    harness.clock.database_now_value += timedelta(minutes=6)
    stale = revoke_device(
        harness, cookies, device.device_id, device_name_confirmation=device.device_name
    )
    assert stale.status_code == 403
    assert stale.json()["error"]["code"] == "recent_authentication_required"

    rotated = reauthenticate(harness.client, cookies)
    approved = revoke_device(
        harness, rotated, device.device_id, device_name_confirmation=device.device_name
    )
    assert approved.status_code == 200, approved.text


def test_admin_revoke_requires_exact_device_name(harness: AdminRouteHarness) -> None:
    cookies = login(harness.client)
    device = register_device(harness, cookies)
    response = harness.client.post(
        f"/api/admin/devices/{device.device_id}/revoke",
        headers={
            "Origin": ORIGIN,
            "Cookie": (
                f"{SESSION_COOKIE_NAME}={cookies['session']}; {CSRF_COOKIE_NAME}={cookies['csrf']}"
            ),
            "X-CSRF-Token": cookies["csrf"],
        },
        json={"device_name_confirmation": "wrong"},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "device_revocation_confirmation_invalid"
    assert response.headers["cache-control"] == "no-store"

    # The mismatch revoked nothing: the device still lists as active.
    listed = list_devices(harness, cookies)
    assert listed.status_code == 200
    assert listed.json()["data"]["devices"][0]["status"] == "active"


def test_admin_revoke_of_an_unknown_device_fails_closed(harness: AdminRouteHarness) -> None:
    cookies = login(harness.client)
    response = revoke_device(harness, cookies, str(uuid4()), device_name_confirmation="whatever")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "device_credential_invalid"


def test_admin_revoke_rejects_invalid_confirmation_bodies(harness: AdminRouteHarness) -> None:
    cookies = login(harness.client)
    device = register_device(harness, cookies)
    for body in ({}, {"device_name_confirmation": ""}, {"device_name_confirmation": "x" * 81}):
        response = harness.client.post(
            f"/api/admin/devices/{device.device_id}/revoke",
            headers=authenticated_headers(ORIGIN, cookies),
            json=body,
        )
        assert response.status_code == 422, body
        assert response.json()["error"]["code"] == "api_request_validation_failed"


# --- admin revoke effects --------------------------------------------------------------


def test_admin_revoke_disables_the_device_and_its_credentials(
    harness: AdminRouteHarness,
) -> None:
    cookies = login(harness.client)
    device = register_device(harness, cookies, device_name="Family laptop")
    revoked = revoke_device(
        harness, cookies, device.device_id, device_name_confirmation="Family laptop"
    )
    assert revoked.status_code == 200, revoked.text
    assert revoked.headers["cache-control"] == "no-store"
    payload = revoked.json()["data"]
    assert set(payload) == {"device_id", "revoked_at"}
    assert payload["device_id"] == device.device_id

    listed = list_devices(harness, cookies)
    assert listed.status_code == 200
    entry = listed.json()["data"]["devices"][0]
    assert entry["status"] == "revoked"
    assert entry["revoked_at"] == payload["revoked_at"]

    # The plugin's credentials are terminal after the revocation.
    refresh_response = harness.client.post(
        "/api/auth/device-tokens/refresh",
        headers={"Authorization": f"Bearer {device.refresh_credential}"},
        json={"rotation_id": str(uuid4())},
    )
    assert refresh_response.status_code == 401
    assert refresh_response.json()["error"]["code"] == "device_token_reuse_detected"

    # A second revoke of the read-only revoked row stays idempotent.
    repeated = revoke_device(
        harness, cookies, device.device_id, device_name_confirmation="Family laptop"
    )
    assert repeated.status_code == 200
    assert repeated.json()["data"]["revoked_at"] == payload["revoked_at"]


def test_admin_routes_reject_device_credentials(harness: AdminRouteHarness) -> None:
    cookies = login(harness.client)
    device = register_device(harness, cookies)
    # A device credential is never a Web authority: without the session
    # cookie the request never passes the origin gate's session resolution.
    listed_with_bearer = harness.client.get(
        "/api/admin/devices",
        headers={"Authorization": f"Bearer {device.refresh_credential}"},
    )
    assert listed_with_bearer.status_code == 403
    assert listed_with_bearer.json()["error"]["code"] == "csrf_validation_failed"
