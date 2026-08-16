"""The cache-suppression header matrix of every authentication route (spec 16).

Every authentication response — success, typed rejection or validation
failure — carries ``Cache-Control: no-store``. The provisioning and recovery
surfaces, the ones that render one-time secret material or rotated session
bindings, additionally carry ``Pragma: no-cache``: grant creation, the grant
poll exchange, refresh rotation, TOTP enrollment start, enrollment
verification, recovery entry and recovery-code regeneration. One journey
walks every route of the closed set through the real composed application
over the offline deterministic composition — success and rejection paths
alike — and pins the exact header outcome per exchange.
"""

from __future__ import annotations

import base64
from collections.abc import Iterator
from datetime import timedelta
from typing import Final
from uuid import uuid4

import httpx
import pytest
from api_runtime.application import create_api_application
from api_runtime.authentication_composition import (
    OFFLINE_PASSWORD,
    OFFLINE_USERNAME,
    OFFLINE_WEB_ALLOWED_ORIGIN,
    OfflineAuthenticationClock,
    OfflineAuthenticationState,
    compose_offline_web_authentication,
)
from api_runtime.authentication_dependencies import (
    CSRF_COOKIE_NAME,
    SESSION_COOKIE_NAME,
)
from fastapi.testclient import TestClient

from personal_os.authentication.totp import totp_code
from personal_os.runtime_configuration.models import RuntimeEnvironment

#: The one origin and base URL the offline composition accepts.
_ORIGIN: Final[str] = OFFLINE_WEB_ALLOWED_ORIGIN

_VALID_LOGIN: Final[dict[str, str]] = {
    "username": OFFLINE_USERNAME,
    "password": OFFLINE_PASSWORD,
}


class _ReadyProbe:
    """Readiness probe stub: the authentication routes never consult it."""

    async def check(self) -> None: ...


class HeaderJourney:
    """One offline composed application walked across every auth route."""

    def __init__(self) -> None:
        self.clock = OfflineAuthenticationClock()
        runtime = compose_offline_web_authentication(
            clock=self.clock, state=OfflineAuthenticationState(totp_active=False)
        )
        application = create_api_application(
            environment=RuntimeEnvironment.TEST,
            readiness_probe=_ReadyProbe(),
            web_authentication=runtime,
        )
        self.client = TestClient(application, base_url=_ORIGIN)
        self.cookies: dict[str, str] = {}
        self.password: str = OFFLINE_PASSWORD
        self.active_secret_base32: str = ""

    # -- helpers --------------------------------------------------------------------

    def authenticated_headers(self) -> dict[str, str]:
        return {
            "Origin": _ORIGIN,
            "Cookie": (
                f"{SESSION_COOKIE_NAME}={self.cookies['session']}; "
                f"{CSRF_COOKIE_NAME}={self.cookies['csrf']}"
            ),
            "X-CSRF-Token": self.cookies["csrf"],
        }

    def login(self) -> httpx.Response:
        response = self.client.post(
            "/api/auth/login",
            headers={"Origin": _ORIGIN},
            json={"username": OFFLINE_USERNAME, "password": self.password},
        )
        assert response.status_code == 200, response.text
        self.cookies = {
            "session": response.cookies[SESSION_COOKIE_NAME],
            "csrf": response.cookies[CSRF_COOKIE_NAME],
        }
        return response

    def code_for(self, secret_base32: str) -> str:
        secret = base64.b32decode(secret_base32)
        return totp_code(
            secret=secret, unix_time_seconds=int(self.clock.database_now_value.timestamp())
        )

    def code_for_active_secret(self) -> str:
        assert self.active_secret_base32, "the enrollment must run first"
        return self.code_for(self.active_secret_base32)

    def create_grant(self) -> tuple[dict[str, str], httpx.Response]:
        """Create one grant; return its payload and the response itself."""
        response = self.client.post(
            "/api/auth/device-authorizations",
            headers={"Origin": _ORIGIN},
            json={
                "client_instance_id": str(uuid4()),
                "device_name": "Header Matrix Desktop",
                "platform_class": "obsidian_desktop",
                "platform_name": "windows",
                "plugin_version": "1.4.0",
                "requested_scope": "obsidian_sync",
            },
        )
        assert response.status_code == 200, response.text
        data = dict(response.json()["data"])
        return (
            {
                "grant_id": str(data["grant_id"]),
                "polling_secret": str(data["polling_secret"]),
                "user_code": str(data["user_code"]),
            },
            response,
        )

    def create_unsupported_grant(self) -> httpx.Response:
        """Create one grant outside the approved plugin window (426)."""
        return self.client.post(
            "/api/auth/device-authorizations",
            headers={"Origin": _ORIGIN},
            json={
                "client_instance_id": str(uuid4()),
                "device_name": "Header Matrix Desktop",
                "platform_class": "obsidian_desktop",
                "platform_name": "windows",
                "plugin_version": "9.9.9",
                "requested_scope": "obsidian_sync",
            },
        )

    def poll(self, grant_id: str, polling_secret: str):
        return self.client.post(
            f"/api/auth/device-authorizations/{grant_id}/poll",
            headers={"Authorization": f"Bearer {polling_secret}"},
        )

    def approve(self, grant_id: str):
        return self.client.post(
            f"/api/auth/device-authorizations/{grant_id}/approve",
            headers=self.authenticated_headers(),
        )

    def run_matrix(self) -> list[tuple[str, object, bool]]:
        """Walk every route outcome; return (label, response, expects_pragma)."""
        steps: list[tuple[str, object, bool]] = []

        def step(label: str, response: object, *, pragma: bool = False) -> None:
            steps.append((label, response, pragma))

        # Session surface: success, typed rejection and validation failure.
        rejected_login = self.client.post(
            "/api/auth/login",
            headers={"Origin": _ORIGIN},
            json={"username": OFFLINE_USERNAME, "password": "wrong-password-sentinel"},
        )
        step("login-rejected", rejected_login)
        invalid_login = self.client.post(
            "/api/auth/login",
            headers={"Origin": _ORIGIN},
            json={"username": OFFLINE_USERNAME, "password": "short"},
        )
        step("login-validation-failed", invalid_login)
        step("login", self.login())
        session = self.client.get("/api/auth/session", headers={"Origin": _ORIGIN})
        step("session-get", session)
        csrf_rejected = self.client.post(
            "/api/auth/logout", headers={"Origin": "https://attacker.example"}
        )
        step("logout-csrf-rejected", csrf_rejected)
        reauthenticate = self.client.post(
            "/api/auth/reauthenticate",
            headers=self.authenticated_headers(),
            json={"password": OFFLINE_PASSWORD},
        )
        step("reauthenticate", reauthenticate)
        if reauthenticate.status_code == 200:
            self.cookies = {
                "session": reauthenticate.cookies[SESSION_COOKIE_NAME],
                "csrf": reauthenticate.cookies[CSRF_COOKIE_NAME],
            }

        # TOTP surface: enrollment start renders the secret, verify renders
        # the recovery codes, regeneration renders a fresh set, and each
        # carries the no-cache pragma alongside no-store.
        enrollment = self.client.post(
            "/api/auth/totp/enrollments",
            headers=self.authenticated_headers(),
            json={"action": "start"},
        )
        step("enroll-start", enrollment, pragma=True)
        enrollment_data = enrollment.json()["data"]["enrollment"]
        enrollment_id = str(enrollment_data["enrollment_id"])
        verify_rejected = self.client.post(
            f"/api/auth/totp/enrollments/{enrollment_id}/verify",
            headers=self.authenticated_headers(),
            json={"code": "135790"},
        )
        step("enroll-verify-rejected", verify_rejected)
        verified = self.client.post(
            f"/api/auth/totp/enrollments/{enrollment_id}/verify",
            headers=self.authenticated_headers(),
            json={"code": self.code_for(str(enrollment_data["secret"]))},
        )
        step("enroll-verify", verified, pragma=True)
        issued_codes = [str(code) for code in verified.json()["data"]["codes"]]
        self.active_secret_base32 = str(enrollment_data["secret"])
        # The enrollment verification consumed the current TOTP step: advance
        # the pinned clock one period so the regeneration proof presents a
        # fresh step rather than the replayed one.
        self.clock.database_now_value += timedelta(seconds=30)
        active_code = self.code_for(str(enrollment_data["secret"]))
        regenerated = self.client.post(
            "/api/auth/totp/recovery-codes/regenerate",
            headers=self.authenticated_headers(),
            json={"password": OFFLINE_PASSWORD, "totp_code": active_code},
        )
        step("recovery-regenerate", regenerated, pragma=True)
        issued_codes = [str(code) for code in regenerated.json()["data"]["codes"]]

        # Device surface: creation, lookup, decisions, exchange and refresh.
        # The creation response renders the one-time user code and polling
        # secret: the provisioning pragma belongs on its 200, and the
        # unsupported-plugin rejection keeps the plain no-store contract.
        created, created_response = self.create_grant()
        step("grant-create", created_response, pragma=True)
        unsupported = self.create_unsupported_grant()
        assert unsupported.status_code == 426, unsupported.text
        assert unsupported.json()["error"]["code"] == "plugin_version_unsupported"
        step("grant-create-plugin-unsupported", unsupported)
        grant = self.client.post(
            "/api/auth/device-authorizations/lookup",
            headers=self.authenticated_headers(),
            json={"user_code": created["user_code"]},
        )
        step("lookup", grant)
        pending_poll = self.poll(created["grant_id"], created["polling_secret"])
        step("poll-pending", pending_poll)
        approved = self.approve(created["grant_id"])
        step("approve", approved)
        exchanged = self.poll(created["grant_id"], created["polling_secret"])
        step("poll-exchange", exchanged, pragma=True)
        refresh_credential = str(exchanged.json()["data"]["refresh_credential"])
        refreshed = self.client.post(
            "/api/auth/device-tokens/refresh",
            headers={"Authorization": f"Bearer {refresh_credential}"},
            json={"rotation_id": str(uuid4())},
        )
        step("refresh", refreshed, pragma=True)
        successor_credential = str(refreshed.json()["data"]["refresh_credential"])
        invalid_refresh = self.client.post(
            "/api/auth/device-tokens/refresh",
            headers={"Authorization": "Bearer rt1.not-a-real-credential"},
            json={"rotation_id": str(uuid4())},
        )
        step("refresh-credential-invalid", invalid_refresh)
        revoked_current = self.client.post(
            "/api/auth/device-tokens/revoke-current",
            headers={"Authorization": f"Bearer {successor_credential}"},
        )
        step("revoke-current", revoked_current)

        denied_grant, _denied_created_response = self.create_grant()
        denied = self.client.post(
            f"/api/auth/device-authorizations/{denied_grant['grant_id']}/deny",
            headers=self.authenticated_headers(),
        )
        step("deny", denied)

        # Admin surface.
        listed = self.client.get("/api/admin/devices", headers=self.authenticated_headers())
        step("admin-list", listed)
        devices = listed.json()["data"]["devices"]
        active_device = str(devices[0]["device_id"])
        revoke_rejected = self.client.post(
            f"/api/admin/devices/{active_device}/revoke",
            headers=self.authenticated_headers(),
            json={"device_name_confirmation": "Not The Device Name"},
        )
        step("admin-revoke-confirmation-invalid", revoke_rejected)

        # Password change renders rotated cookies under plain no-store, and
        # signing out closes the session surface.
        changed = self.client.put(
            "/api/auth/password",
            headers=self.authenticated_headers(),
            json={"new_password": "fresh-trebuchet-unlock-phrase"},
        )
        step("password-change", changed)
        if changed.status_code == 200:
            self.cookies = {
                "session": changed.cookies[SESSION_COOKIE_NAME],
                "csrf": changed.cookies[CSRF_COOKIE_NAME],
            }
        logged_out = self.client.post("/api/auth/logout", headers=self.authenticated_headers())
        step("logout", logged_out)

        # The second journey starts from the changed password: login answers
        # the pending-TOTP challenge state, the challenge route rejects a
        # wrong code, one recovery code enters the limited binding under the
        # no-cache pragma, and the challenge then activates the session
        # before the disable route closes the TOTP credential.
        self.password = "fresh-trebuchet-unlock-phrase"
        self.login()
        totp_verify_rejected = self.client.post(
            "/api/auth/totp/verify",
            headers=self.authenticated_headers(),
            json={"code": "246810"},
        )
        step("totp-verify-rejected", totp_verify_rejected)
        recovery_rejected = self.client.post(
            "/api/auth/totp/recovery",
            headers=self.authenticated_headers(),
            json={"password": self.password, "recovery_code": "AAAA-BBBB-CCCC"},
        )
        step("recovery-rejected", recovery_rejected)
        if recovery_rejected.status_code == 401 and issued_codes:
            recovered = self.client.post(
                "/api/auth/totp/recovery",
                headers=self.authenticated_headers(),
                json={"password": self.password, "recovery_code": issued_codes[0]},
            )
            step("recovery-entry", recovered, pragma=True)
            if recovered.status_code == 200:
                self.cookies = {
                    "session": recovered.cookies[SESSION_COOKIE_NAME],
                    "csrf": recovered.cookies[CSRF_COOKIE_NAME],
                }
                limited_logout = self.client.post(
                    "/api/auth/logout", headers=self.authenticated_headers()
                )
                step("recovery-logout", limited_logout)

        self.login()
        self.clock.database_now_value += timedelta(seconds=30)
        totp_verified = self.client.post(
            "/api/auth/totp/verify",
            headers=self.authenticated_headers(),
            json={"code": self.code_for(str(self.active_secret_base32))},
        )
        step("totp-verify", totp_verified)
        if totp_verified.status_code == 200:
            self.cookies = {
                "session": totp_verified.cookies[SESSION_COOKIE_NAME],
                "csrf": totp_verified.cookies[CSRF_COOKIE_NAME],
            }
        self.clock.database_now_value += timedelta(seconds=30)
        disabled = self.client.request(
            "DELETE",
            "/api/auth/totp",
            headers=self.authenticated_headers(),
            json={"password": self.password, "totp_code": self.code_for_active_secret()},
        )
        step("totp-disable", disabled)
        return steps


@pytest.fixture
def journey() -> Iterator[HeaderJourney]:
    harness = HeaderJourney()
    with harness.client:
        yield harness


def test_every_authentication_route_pins_its_cache_suppression_headers(
    journey: HeaderJourney,
) -> None:
    steps = journey.run_matrix()
    assert len(steps) >= 20, "the matrix must cross every closed route at least once"
    for label, response, expects_pragma in steps:
        assert response.headers["cache-control"] == "no-store", (
            f"'{label}' lost Cache-Control: no-store"
        )
        if expects_pragma:
            assert response.headers["pragma"] == "no-cache", (
                f"provisioning surface '{label}' lost Pragma: no-cache"
            )


def test_provisioning_failures_keep_the_no_store_contract(journey: HeaderJourney) -> None:
    login_response = journey.login()
    assert login_response.status_code == 200
    assert login_response.headers["cache-control"] == "no-store"

    unknown_decision = journey.client.post(
        f"/api/auth/device-authorizations/{uuid4()}/approve",
        headers=journey.authenticated_headers(),
    )
    assert unknown_decision.status_code in (401, 404)
    assert unknown_decision.headers["cache-control"] == "no-store"

    # The provisioning surface itself: its 200 carries both suppression
    # headers, and its unsupported-plugin rejection keeps plain no-store.
    slow_poll_grant, created_response = journey.create_grant()
    assert created_response.headers["cache-control"] == "no-store"
    assert created_response.headers["pragma"] == "no-cache"
    unsupported = journey.create_unsupported_grant()
    assert unsupported.status_code == 426
    assert unsupported.headers["cache-control"] == "no-store"

    first_poll = journey.poll(slow_poll_grant["grant_id"], slow_poll_grant["polling_secret"])
    second_poll = journey.poll(slow_poll_grant["grant_id"], slow_poll_grant["polling_secret"])
    assert second_poll.status_code in (400, 429)
    for response in (first_poll, second_poll):
        assert response.headers["cache-control"] == "no-store"
