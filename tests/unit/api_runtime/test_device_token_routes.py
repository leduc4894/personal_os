"""Device refresh and self-revoke HTTP routes over the offline composition.

These tests drive the two closed device-token routes through the FastAPI
``TestClient`` against the real application factory wired with the offline
deterministic authentication composition: no database, no key file and no
environment read. Every exchange happens through the real poll route, so the
rotation surface answers under the same exact-replay and confirmed-reuse
contracts as the serve graph: the dedicated refresh Bearer scheme is the only
authority the refresh route accepts (session cookies, polling and access
credentials close with the registered invalid-credential code), one new
rotation identity commits the successor pair, the same identity replays the
byte-identical payload, and a different identity on a rotated predecessor
surfaces the closed reuse rejection after its revocation committed. The
self-revoke route authenticates the current refresh credential, revokes its
family and every usable token, and afterwards the credentials answer with the
terminal revoked code. Every response carries ``Cache-Control: no-store`` and
the credential-rendering responses also carry ``Pragma: no-cache``.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Final
from uuid import UUID, uuid4

import httpx
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

ROTATION_A: Final[UUID] = UUID("00000000-0000-0000-0000-0000000000aa")
ROTATION_B: Final[UUID] = UUID("00000000-0000-0000-0000-0000000000bb")


class _ReadyProbe:
    """Readiness probe stub: the device routes never consult dependencies."""

    async def check(self) -> None: ...


@dataclass(frozen=True, slots=True)
class TokenRouteHarness:
    """One test client bound to its shared offline clock and runtime."""

    client: TestClient
    clock: OfflineAuthenticationClock
    runtime: WebAuthenticationRuntime


@pytest.fixture
def harness() -> Iterator[TokenRouteHarness]:
    clock = OfflineAuthenticationClock()
    state = OfflineAuthenticationState(totp_active=False)
    runtime = compose_offline_web_authentication(clock=clock, state=state)
    application = create_api_application(
        environment=RuntimeEnvironment.TEST,
        readiness_probe=_ReadyProbe(),
        web_authentication=runtime,
    )
    with TestClient(application, base_url=_SECURE_BASE_URL) as test_client:
        yield TokenRouteHarness(client=test_client, clock=clock, runtime=runtime)


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


def poll_headers(polling_secret: str) -> dict[str, str]:
    """The dedicated polling Bearer header of one grant poll (spec 11.4)."""
    return {"Authorization": f"Bearer {polling_secret}"}


def refresh_headers(refresh_credential: str) -> dict[str, str]:
    """The dedicated refresh Bearer header of one rotation (spec 13.4)."""
    return {"Authorization": f"Bearer {refresh_credential}"}


@dataclass(frozen=True, slots=True)
class ExchangedGrant:
    """The response payload of one committed grant exchange."""

    grant_id: str
    polling_secret: str
    access_credential: str
    refresh_credential: str
    device_id: str
    token_family_id: str


def exchange_one_device(
    harness: TokenRouteHarness, *, device_name: str = "Personal desktop"
) -> ExchangedGrant:
    """Create, approve and poll one grant through the HTTP routes."""
    cookies = login(harness.client)
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
        headers=poll_headers(str(data["polling_secret"])),
    )
    assert exchanged.status_code == 200, exchanged.text
    payload = dict(exchanged.json()["data"])
    return ExchangedGrant(
        grant_id=str(data["grant_id"]),
        polling_secret=str(data["polling_secret"]),
        access_credential=str(payload["access_credential"]),
        refresh_credential=str(payload["refresh_credential"]),
        device_id=str(payload["device_id"]),
        token_family_id=str(payload["token_family_id"]),
    )


def refresh(
    harness: TokenRouteHarness,
    refresh_credential: str,
    rotation_id: UUID,
) -> httpx.Response:
    """Present one refresh rotation through the dedicated route."""
    return harness.client.post(
        "/api/auth/device-tokens/refresh",
        headers=refresh_headers(refresh_credential),
        json={"rotation_id": str(rotation_id)},
    )


def revoke_current(harness: TokenRouteHarness, refresh_credential: str) -> httpx.Response:
    """Present one self-revoke through the dedicated route."""
    return harness.client.post(
        "/api/auth/device-tokens/revoke-current",
        headers=refresh_headers(refresh_credential),
    )


# --- refresh route guards ---------------------------------------------------------------


def test_refresh_accepts_only_the_refresh_bearer_credential(harness: TokenRouteHarness) -> None:
    exchanged = exchange_one_device(harness)

    missing = harness.client.post(
        "/api/auth/device-tokens/refresh", json={"rotation_id": str(ROTATION_A)}
    )
    assert missing.status_code == 401
    assert missing.json()["error"]["code"] == "device_credential_invalid"

    # A fully valid Web session with its CSRF pair is not this route's
    # authority: only the dedicated refresh Bearer credential is accepted.
    cookies = login(harness.client)
    session_only = harness.client.post(
        "/api/auth/device-tokens/refresh",
        headers=authenticated_headers(ORIGIN, cookies),
        json={"rotation_id": str(ROTATION_A)},
    )
    assert session_only.status_code == 401
    assert session_only.json()["error"]["code"] == "device_credential_invalid"

    # The polling credential of the exchanged grant is the wrong kind.
    polling_as_refresh = harness.client.post(
        "/api/auth/device-tokens/refresh",
        headers=poll_headers(exchanged.polling_secret),
        json={"rotation_id": str(ROTATION_A)},
    )
    assert polling_as_refresh.status_code == 401
    assert polling_as_refresh.json()["error"]["code"] == "device_credential_invalid"

    # The access credential is the wrong kind too.
    access_as_refresh = harness.client.post(
        "/api/auth/device-tokens/refresh",
        headers=refresh_headers(exchanged.access_credential),
        json={"rotation_id": str(ROTATION_A)},
    )
    assert access_as_refresh.status_code == 401
    assert access_as_refresh.json()["error"]["code"] == "device_credential_invalid"

    unknown = harness.client.post(
        "/api/auth/device-tokens/refresh",
        headers=refresh_headers(f"rt1.{uuid4()}.{bytes(range(32)).hex()}"),
        json={"rotation_id": str(ROTATION_A)},
    )
    assert unknown.status_code == 401
    assert unknown.json()["error"]["code"] == "device_credential_invalid"
    assert unknown.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "body",
    [
        {},  # the strict body requires the rotation identity
        {"rotation_id": "not-a-uuid"},
        {"rotation_id": str(ROTATION_A), "extra": "field"},
    ],
)
def test_refresh_rejects_invalid_request_bodies(
    harness: TokenRouteHarness, body: dict[str, str]
) -> None:
    exchanged = exchange_one_device(harness)
    response = harness.client.post(
        "/api/auth/device-tokens/refresh",
        headers=refresh_headers(exchanged.refresh_credential),
        json=body,
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "api_request_validation_failed"
    assert response.headers["cache-control"] == "no-store"


# --- refresh rotation ------------------------------------------------------------------


def test_refresh_rotates_once_then_replays_the_identical_payload(
    harness: TokenRouteHarness,
) -> None:
    exchanged = exchange_one_device(harness)

    rotated = refresh(harness, exchanged.refresh_credential, ROTATION_A)
    assert rotated.status_code == 200, rotated.text
    assert rotated.headers["cache-control"] == "no-store"
    assert rotated.headers["pragma"] == "no-cache"
    payload = rotated.json()["data"]
    assert set(payload) == {
        "token_family_id",
        "refresh_generation",
        "access_credential",
        "refresh_credential",
        "access_expires_at",
        "refresh_expires_at",
        "family_absolute_expires_at",
    }
    assert payload["token_family_id"] == exchanged.token_family_id
    assert payload["refresh_generation"] == 2
    assert str(payload["access_credential"]).startswith("at1.")
    assert str(payload["refresh_credential"]).startswith("rt1.")
    assert payload["refresh_credential"] != exchanged.refresh_credential

    # A lost acknowledgement replays the byte-identical successor payload.
    replayed = refresh(harness, exchanged.refresh_credential, ROTATION_A)
    assert replayed.status_code == 200, replayed.text
    assert replayed.json()["data"] == payload


def test_refresh_with_a_new_identity_on_a_rotated_predecessor_is_reuse(
    harness: TokenRouteHarness,
) -> None:
    exchanged = exchange_one_device(harness)
    assert refresh(harness, exchanged.refresh_credential, ROTATION_A).status_code == 200

    reused = refresh(harness, exchanged.refresh_credential, ROTATION_B)
    assert reused.status_code == 401
    assert reused.json()["error"]["code"] == "device_token_reuse_detected"
    assert reused.headers["cache-control"] == "no-store"

    # The confirmed reuse revoked the family: even the committed successor
    # refresh credential answers with the terminal revoked code now.
    first_rotation = refresh(harness, exchanged.refresh_credential, ROTATION_A)
    assert first_rotation.status_code == 401
    assert first_rotation.json()["error"]["code"] == "device_token_reuse_detected"


# --- self-revoke -----------------------------------------------------------------------


def test_revoke_current_revokes_the_presented_family(harness: TokenRouteHarness) -> None:
    exchanged = exchange_one_device(harness)
    revoked = revoke_current(harness, exchanged.refresh_credential)
    assert revoked.status_code == 200, revoked.text
    assert revoked.headers["cache-control"] == "no-store"
    payload = revoked.json()["data"]
    assert set(payload) == {"device_id", "token_family_id", "revoked_at"}
    assert payload["device_id"] == exchanged.device_id
    assert payload["token_family_id"] == exchanged.token_family_id

    # Every credential of the revoked family is terminal now.
    refresh_after = refresh(harness, exchanged.refresh_credential, ROTATION_A)
    assert refresh_after.status_code == 401
    revoked_again = revoke_current(harness, exchanged.refresh_credential)
    assert revoked_again.status_code == 401
    assert revoked_again.json()["error"]["code"] == "device_revoked"


def test_revoke_current_accepts_only_the_refresh_bearer_credential(
    harness: TokenRouteHarness,
) -> None:
    exchanged = exchange_one_device(harness)

    missing = revoke_current(harness, "")
    assert missing.status_code == 401
    assert missing.json()["error"]["code"] == "device_credential_invalid"

    access_as_refresh = harness.client.post(
        "/api/auth/device-tokens/revoke-current",
        headers=refresh_headers(exchanged.access_credential),
    )
    assert access_as_refresh.status_code == 401
    assert access_as_refresh.json()["error"]["code"] == "device_credential_invalid"

    polling_as_refresh = harness.client.post(
        "/api/auth/device-tokens/revoke-current",
        headers=poll_headers(exchanged.polling_secret),
    )
    assert polling_as_refresh.status_code == 401
    assert polling_as_refresh.json()["error"]["code"] == "device_credential_invalid"

    # A Web session never authorizes the plugin self-revoke route.
    cookies = login(harness.client)
    session_only = harness.client.post(
        "/api/auth/device-tokens/revoke-current",
        headers=authenticated_headers(ORIGIN, cookies),
    )
    assert session_only.status_code == 401
    assert session_only.json()["error"]["code"] == "device_credential_invalid"


def test_revoke_current_of_a_rotated_predecessor_confirms_reuse(
    harness: TokenRouteHarness,
) -> None:
    exchanged = exchange_one_device(harness)
    rotated = refresh(harness, exchanged.refresh_credential, ROTATION_A)
    assert rotated.status_code == 200, rotated.text
    successor_credential = str(rotated.json()["data"]["refresh_credential"])

    # The stale predecessor is not the current credential: the same closed
    # reuse vocabulary answers, and the family revocation commits first.
    stale = revoke_current(harness, exchanged.refresh_credential)
    assert stale.status_code == 401
    assert stale.json()["error"]["code"] == "device_token_reuse_detected"

    successor = revoke_current(harness, successor_credential)
    assert successor.status_code == 401
    assert successor.json()["error"]["code"] == "device_revoked"
