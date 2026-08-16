"""TOTP/recovery HTTP routes over the offline deterministic composition.

These tests drive the six closed TOTP/recovery routes of spec 16.2 through the
FastAPI ``TestClient`` against the real application factory wired with the
offline deterministic authentication composition: no database, no key file and
no environment read. They pin the login-challenge verification that activates
a ``pending_totp`` binding with rotated cookies, the enrollment offer and its
one-time provisioning URI, the ten one-time recovery codes returned exactly
once, the recovery-limited choreography (enter with password plus one code,
replace TOTP, return to active), regeneration and disable with their
password-plus-current-TOTP proofs, the re-authentication TOTP leg of spec 9.4,
the closed error envelopes with ``Cache-Control: no-store`` (plus
``Pragma: no-cache`` on provisioning and recovery responses) and the
serialized same-code race that accepts one attempt only.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Iterator
from dataclasses import replace
from datetime import timedelta
from typing import Final
from uuid import UUID

import pytest
from api_runtime.application import create_api_application
from api_runtime.authentication_composition import (
    OFFLINE_TOTP_SECRET,
    OFFLINE_WEB_ALLOWED_ORIGIN,
    OfflineAuthenticationClock,
    OfflineAuthenticationState,
    compose_offline_web_authentication,
)
from api_runtime.authentication_dependencies import (
    CSRF_COOKIE_NAME,
    SESSION_COOKIE_NAME,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient

from personal_os.authentication.contracts import WebSessionState
from personal_os.authentication.sessions import session_secret_hash_of
from personal_os.authentication.totp import RECOVERY_CODE_COUNT, totp_code
from personal_os.diagnostics.context import create_diagnostic_context
from personal_os.runtime_configuration.models import RuntimeEnvironment

ORIGIN: Final[str] = OFFLINE_WEB_ALLOWED_ORIGIN
_SECURE_BASE_URL: Final[str] = OFFLINE_WEB_ALLOWED_ORIGIN

_VALID_LOGIN: Final[dict[str, str]] = {
    "username": "admin",
    "password": "correct-horse-battery-staple",
}
_WRONG_PASSWORD: Final[str] = "sentinel-wrong-password-value"

_PERMITTED_RECOVERY_ACTIONS: Final[frozenset[str]] = frozenset(
    {"totp_replacement", "logout"}
)


class _ReadyProbe:
    """Readiness probe stub: the TOTP routes never consult dependencies."""

    async def check(self) -> None: ...


def create_totp_test_app(
    *,
    totp_active: bool = False,
    clock: OfflineAuthenticationClock | None = None,
    state: OfflineAuthenticationState | None = None,
) -> FastAPI:
    """Compose the real application over the offline deterministic ports."""
    return create_api_application(
        environment=RuntimeEnvironment.TEST,
        readiness_probe=_ReadyProbe(),
        web_authentication=compose_offline_web_authentication(
            totp_active=totp_active, clock=clock, state=state
        ),
    )


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(create_totp_test_app(), base_url=_SECURE_BASE_URL) as test_client:
        yield test_client


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


def authenticated_headers(cookies: dict[str, str]) -> dict[str, str]:
    """Origin plus the session and CSRF cookie/header pair of one session."""
    return {
        "Origin": ORIGIN,
        "Cookie": (
            f"{SESSION_COOKIE_NAME}={cookies['session']}; "
            f"{CSRF_COOKIE_NAME}={cookies['csrf']}"
        ),
        "X-CSRF-Token": cookies["csrf"],
    }


def code_at(clock: OfflineAuthenticationClock, secret: bytes) -> str:
    """The valid TOTP code for one secret at the pinned offline clock moment."""
    return totp_code(
        secret=secret, unix_time_seconds=int(clock.database_now_value.timestamp())
    )


def start_enrollment(
    test_client: TestClient, cookies: dict[str, str]
) -> tuple[UUID, bytes]:
    """Start one enrollment and return its id and raw secret bytes."""
    response = test_client.post(
        "/api/auth/totp/enrollments",
        headers=authenticated_headers(cookies),
        json={"action": "start"},
    )
    assert response.status_code == 200, response.text
    enrollment = response.json()["data"]["enrollment"]
    assert enrollment is not None
    return (
        UUID(enrollment["enrollment_id"]),
        base64.b32decode(enrollment["secret"]),
    )


def complete_enrollment(
    test_client: TestClient,
    cookies: dict[str, str],
    clock: OfflineAuthenticationClock,
) -> tuple[UUID, bytes, list[str]]:
    """Run the full start-plus-verify enrollment and return secret and codes."""
    enrollment_id, secret = start_enrollment(test_client, cookies)
    response = test_client.post(
        f"/api/auth/totp/enrollments/{enrollment_id}/verify",
        headers=authenticated_headers(cookies),
        json={"code": code_at(clock, secret)},
    )
    assert response.status_code == 200, response.text
    codes = list(response.json()["data"]["codes"])
    assert len(codes) == RECOVERY_CODE_COUNT
    return enrollment_id, secret, codes


# --- login-challenge verification ----------------------------------------------------


def test_totp_verify_activates_a_pending_totp_session_with_rotated_cookies() -> None:
    clock = OfflineAuthenticationClock()
    with TestClient(
        create_totp_test_app(totp_active=True, clock=clock), base_url=_SECURE_BASE_URL
    ) as totp_client:
        cookies = login(totp_client)
        response = totp_client.post(
            "/api/auth/totp/verify",
            headers=authenticated_headers(cookies),
            json={"code": code_at(clock, OFFLINE_TOTP_SECRET)},
        )
        assert response.status_code == 200, response.text
        assert response.json()["data"]["state"] == "active"
        assert response.json()["data"]["authenticated"] is True
        assert response.cookies[SESSION_COOKIE_NAME] != cookies["session"]
        assert response.headers["cache-control"] == "no-store"
        session_view = totp_client.get("/api/auth/session", headers={"Origin": ORIGIN})
        assert session_view.status_code == 200
        assert session_view.json()["data"]["state"] == "active"


def test_totp_verify_rejects_a_wrong_code_then_locks_the_verification_bucket() -> None:
    clock = OfflineAuthenticationClock()
    with TestClient(
        create_totp_test_app(totp_active=True, clock=clock), base_url=_SECURE_BASE_URL
    ) as totp_client:
        cookies = login(totp_client)
        for _ in range(5):
            rejected = totp_client.post(
                "/api/auth/totp/verify",
                headers=authenticated_headers(cookies),
                json={"code": "000000"},
            )
            assert rejected.status_code == 401
            assert rejected.json()["error"]["code"] == "authentication_failed"
            assert "000000" not in rejected.text
        locked = totp_client.post(
            "/api/auth/totp/verify",
            headers=authenticated_headers(cookies),
            json={"code": code_at(clock, OFFLINE_TOTP_SECRET)},
        )
        assert locked.status_code == 429
        error = locked.json()["error"]
        assert error["code"] == "authentication_rate_limited"
        assert error["details"]["retry_after_seconds"] > 0
        assert locked.headers["cache-control"] == "no-store"


def test_totp_verify_replays_the_same_code_only_once() -> None:
    clock = OfflineAuthenticationClock()
    with TestClient(
        create_totp_test_app(totp_active=True, clock=clock), base_url=_SECURE_BASE_URL
    ) as totp_client:
        cookies = login(totp_client)
        code = code_at(clock, OFFLINE_TOTP_SECRET)
        accepted = totp_client.post(
            "/api/auth/totp/verify",
            headers=authenticated_headers(cookies),
            json={"code": code},
        )
        assert accepted.status_code == 200
        # A second login restarts the challenge; the same step is now a replay.
        second_cookies = login(totp_client)
        replayed = totp_client.post(
            "/api/auth/totp/verify",
            headers=authenticated_headers(second_cookies),
            json={"code": code},
        )
        assert replayed.status_code == 401
        assert replayed.json()["error"]["code"] == "authentication_failed"


def test_totp_verify_rejects_active_and_recovery_limited_bindings() -> None:
    # Only a pending_totp binding may complete the login challenge: an active
    # session and a recovery_limited binding both fail closed.
    with TestClient(create_totp_test_app(), base_url=_SECURE_BASE_URL) as plain_client:
        cookies = login(plain_client)
        active_response = plain_client.post(
            "/api/auth/totp/verify",
            headers=authenticated_headers(cookies),
            json={"code": "123456"},
        )
        assert active_response.status_code == 401
        assert active_response.json()["error"]["code"] == "authentication_required"
    state = OfflineAuthenticationState(totp_active=False)
    with TestClient(
        create_totp_test_app(state=state), base_url=_SECURE_BASE_URL
    ) as limited_client:
        cookies = login(limited_client)
        stored = state.sessions_by_secret_hash[session_secret_hash_of(cookies["session"])]
        state.sessions_by_secret_hash[stored.session_secret_hash] = replace(
            stored,
            state=WebSessionState.RECOVERY_LIMITED,
            authentication_method="recovery_code",
            authenticated_at=stored.created_at,
        )
        limited_response = limited_client.post(
            "/api/auth/totp/verify",
            headers=authenticated_headers(cookies),
            json={"code": "123456"},
        )
        assert limited_response.status_code == 401
        assert limited_response.json()["error"]["code"] == "authentication_required"


def test_totp_verify_requires_origin_and_csrf_proof(client: TestClient) -> None:
    missing_origin = client.post("/api/auth/totp/verify", json={"code": "123456"})
    assert missing_origin.status_code == 403
    assert missing_origin.json()["error"]["code"] == "csrf_validation_failed"
    # Dependency rejections happen before the endpoint publishes its template,
    # so the no-store posture must come from the closed route-template set.
    assert missing_origin.headers["cache-control"] == "no-store"
    cookies = login(client)
    missing_csrf = client.post(
        "/api/auth/totp/verify",
        headers={"Origin": ORIGIN, "Cookie": f"{SESSION_COOKIE_NAME}={cookies['session']}"},
        json={"code": "123456"},
    )
    assert missing_csrf.status_code == 403
    assert missing_csrf.json()["error"]["code"] == "csrf_validation_failed"
    assert missing_csrf.headers["cache-control"] == "no-store"


# --- enrollment ------------------------------------------------------------------------


def test_enrollment_start_returns_the_one_time_provisioning_uri(client: TestClient) -> None:
    cookies = login(client)
    response = client.post(
        "/api/auth/totp/enrollments",
        headers=authenticated_headers(cookies),
        json={"action": "start"},
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["action"] == "start"
    assert data["dismissed_at"] is None
    enrollment = data["enrollment"]
    assert enrollment["provisioning_uri"].startswith("otpauth://totp/")
    assert "secret=" in enrollment["provisioning_uri"]
    secret = base64.b32decode(enrollment["secret"])
    assert len(secret) == 20
    assert enrollment["expires_at"]
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"


def test_enrollment_start_requires_recent_reauthentication(client: TestClient) -> None:
    clock = OfflineAuthenticationClock()
    with TestClient(
        create_totp_test_app(clock=clock), base_url=_SECURE_BASE_URL
    ) as timed_client:
        cookies = login(timed_client)
        clock.database_now_value += timedelta(minutes=6)
        response = timed_client.post(
            "/api/auth/totp/enrollments",
            headers=authenticated_headers(cookies),
            json={"action": "start"},
        )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "recent_authentication_required"
    assert response.headers["cache-control"] == "no-store"


def test_dismiss_initial_offer_records_no_secret_and_no_pending_row(
    client: TestClient,
) -> None:
    state = OfflineAuthenticationState(totp_active=False)
    with TestClient(
        create_totp_test_app(state=state), base_url=_SECURE_BASE_URL
    ) as offer_client:
        cookies = login(offer_client)
        response = offer_client.post(
            "/api/auth/totp/enrollments",
            headers=authenticated_headers(cookies),
            json={"action": "dismiss_initial_offer"},
        )
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert data["action"] == "dismiss_initial_offer"
        assert data["enrollment"] is None
        assert data["dismissed_at"]
        assert state.totp_prompt_dismissed_at is not None
        assert state.totp_credential_rows == []


def test_enrollment_verification_returns_ten_one_time_recovery_codes(
    client: TestClient,
) -> None:
    clock = OfflineAuthenticationClock()
    with TestClient(
        create_totp_test_app(clock=clock), base_url=_SECURE_BASE_URL
    ) as enroll_client:
        cookies = login(enroll_client)
        enrollment_id, secret = start_enrollment(enroll_client, cookies)
        response = enroll_client.post(
            f"/api/auth/totp/enrollments/{enrollment_id}/verify",
            headers=authenticated_headers(cookies),
            json={"code": code_at(clock, secret)},
        )
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert len(data["codes"]) == RECOVERY_CODE_COUNT == 10
        assert data["revision"] == 1
        assert all(len(code.replace("-", "")) == 12 for code in data["codes"])
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["pragma"] == "no-cache"
        # The completing session stays active on its existing binding.
        session_view = enroll_client.get("/api/auth/session", headers={"Origin": ORIGIN})
        assert session_view.status_code == 200
        assert session_view.json()["data"]["state"] == "active"


def test_expired_enrollment_is_rejected_with_the_enrollment_state_error(
    client: TestClient,
) -> None:
    clock = OfflineAuthenticationClock()
    with TestClient(
        create_totp_test_app(clock=clock), base_url=_SECURE_BASE_URL
    ) as enroll_client:
        cookies = login(enroll_client)
        enrollment_id, secret = start_enrollment(enroll_client, cookies)
        clock.database_now_value += timedelta(minutes=11)
        response = enroll_client.post(
            f"/api/auth/totp/enrollments/{enrollment_id}/verify",
            headers=authenticated_headers(cookies),
            json={"code": code_at(clock, secret)},
        )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "totp_enrollment_state_invalid"
    assert response.headers["cache-control"] == "no-store"


def test_second_start_while_totp_is_active_is_rejected(client: TestClient) -> None:
    clock = OfflineAuthenticationClock()
    with TestClient(
        create_totp_test_app(clock=clock), base_url=_SECURE_BASE_URL
    ) as enroll_client:
        cookies = login(enroll_client)
        complete_enrollment(enroll_client, cookies, clock)
        response = enroll_client.post(
            "/api/auth/totp/enrollments",
            headers=authenticated_headers(cookies),
            json={"action": "start"},
        )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "totp_enrollment_state_invalid"


# --- recovery ----------------------------------------------------------------------------


def test_recovery_enters_recovery_limited_then_replacement_returns_to_active() -> None:
    clock = OfflineAuthenticationClock()
    with TestClient(
        create_totp_test_app(clock=clock), base_url=_SECURE_BASE_URL
    ) as recovery_client:
        cookies = login(recovery_client)
        _enrollment_id, _secret, codes = complete_enrollment(recovery_client, cookies, clock)
        # Re-login: the now-active TOTP restarts the challenge.
        challenge_login = recovery_client.post(
            "/api/auth/login", headers={"Origin": ORIGIN}, json=_VALID_LOGIN
        )
        assert challenge_login.json()["data"]["state"] == "pending_totp"
        assert challenge_login.json()["data"]["authenticated"] is False
        challenge_cookies = {
            "session": challenge_login.cookies[SESSION_COOKIE_NAME],
            "csrf": challenge_login.cookies[CSRF_COOKIE_NAME],
        }
        recovery = recovery_client.post(
            "/api/auth/totp/recovery",
            headers=authenticated_headers(challenge_cookies),
            json={"password": _VALID_LOGIN["password"], "recovery_code": codes[0]},
        )
        assert recovery.status_code == 200, recovery.text
        context = recovery.json()["data"]
        assert context["state"] == "recovery_limited"
        assert frozenset(context["permitted_actions"]) == _PERMITTED_RECOVERY_ACTIONS
        assert recovery.cookies[SESSION_COOKIE_NAME] != challenge_cookies["session"]
        assert recovery.headers["cache-control"] == "no-store"
        assert recovery.headers["pragma"] == "no-cache"
        # The recovery-limited binding authenticates no ordinary route.
        denied = recovery_client.get("/api/auth/session", headers={"Origin": ORIGIN})
        assert denied.status_code == 401
        # Replacement: start a fresh enrollment from the limited session.
        replacement_enrollment, replacement_secret = start_enrollment(
            recovery_client,
            {
                "session": recovery.cookies[SESSION_COOKIE_NAME],
                "csrf": recovery.cookies[CSRF_COOKIE_NAME],
            },
        )
        replaced = recovery_client.post(
            f"/api/auth/totp/enrollments/{replacement_enrollment}/verify",
            headers={
                "Origin": ORIGIN,
                "Cookie": (
                    f"{SESSION_COOKIE_NAME}={recovery.cookies[SESSION_COOKIE_NAME]}; "
                    f"{CSRF_COOKIE_NAME}={recovery.cookies[CSRF_COOKIE_NAME]}"
                ),
                "X-CSRF-Token": recovery.cookies[CSRF_COOKIE_NAME],
            },
            json={"code": code_at(clock, replacement_secret)},
        )
        assert replaced.status_code == 200, replaced.text
        assert len(replaced.json()["data"]["codes"]) == RECOVERY_CODE_COUNT
        restored = recovery_client.get("/api/auth/session", headers={"Origin": ORIGIN})
        assert restored.status_code == 200
        assert restored.json()["data"]["state"] == "active"


def test_recovery_consumes_each_code_once(client: TestClient) -> None:
    clock = OfflineAuthenticationClock()
    with TestClient(
        create_totp_test_app(clock=clock), base_url=_SECURE_BASE_URL
    ) as recovery_client:
        cookies = login(recovery_client)
        _, _, codes = complete_enrollment(recovery_client, cookies, clock)
        challenge_cookies = login(recovery_client)
        first = recovery_client.post(
            "/api/auth/totp/recovery",
            headers=authenticated_headers(challenge_cookies),
            json={"password": _VALID_LOGIN["password"], "recovery_code": codes[0]},
        )
        assert first.status_code == 200
        # A fresh challenge cannot reuse the consumed code.
        second_challenge = login(recovery_client)
        second = recovery_client.post(
            "/api/auth/totp/recovery",
            headers=authenticated_headers(second_challenge),
            json={"password": _VALID_LOGIN["password"], "recovery_code": codes[0]},
        )
        assert second.status_code == 401
        assert second.json()["error"]["code"] == "authentication_failed"


def test_recovery_requires_the_correct_password(client: TestClient) -> None:
    clock = OfflineAuthenticationClock()
    with TestClient(
        create_totp_test_app(clock=clock), base_url=_SECURE_BASE_URL
    ) as recovery_client:
        cookies = login(recovery_client)
        _, _, codes = complete_enrollment(recovery_client, cookies, clock)
        challenge_cookies = login(recovery_client)
        response = recovery_client.post(
            "/api/auth/totp/recovery",
            headers=authenticated_headers(challenge_cookies),
            json={"password": _WRONG_PASSWORD, "recovery_code": codes[0]},
        )
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "authentication_failed"
        assert _WRONG_PASSWORD not in response.text


# --- regeneration and disable --------------------------------------------------------------


def test_regenerate_requires_password_and_current_totp_and_rotates_codes(
    client: TestClient,
) -> None:
    clock = OfflineAuthenticationClock()
    with TestClient(
        create_totp_test_app(clock=clock), base_url=_SECURE_BASE_URL
    ) as security_client:
        cookies = login(security_client)
        _, secret, prior_codes = complete_enrollment(security_client, cookies, clock)
        # The enrollment consumed the current step; the proofs move one step on.
        clock.database_now_value += timedelta(seconds=30)
        wrong_proof = security_client.post(
            "/api/auth/totp/recovery-codes/regenerate",
            headers=authenticated_headers(cookies),
            json={"password": _VALID_LOGIN["password"], "totp_code": "000000"},
        )
        assert wrong_proof.status_code == 401
        wrong_password = security_client.post(
            "/api/auth/totp/recovery-codes/regenerate",
            headers=authenticated_headers(cookies),
            json={"password": _WRONG_PASSWORD, "totp_code": code_at(clock, secret)},
        )
        assert wrong_password.status_code == 401
        regenerated = security_client.post(
            "/api/auth/totp/recovery-codes/regenerate",
            headers=authenticated_headers(cookies),
            json={"password": _VALID_LOGIN["password"], "totp_code": code_at(clock, secret)},
        )
        assert regenerated.status_code == 200, regenerated.text
        data = regenerated.json()["data"]
        assert data["revision"] == 2
        assert len(data["codes"]) == RECOVERY_CODE_COUNT
        assert set(data["codes"]).isdisjoint(prior_codes)
        assert regenerated.headers["pragma"] == "no-cache"
        # The prior revision is invalid: none of the old codes recovers.
        challenge_cookies = login(security_client)
        denied = security_client.post(
            "/api/auth/totp/recovery",
            headers=authenticated_headers(challenge_cookies),
            json={"password": _VALID_LOGIN["password"], "recovery_code": prior_codes[1]},
        )
        assert denied.status_code == 401


def test_disable_rotates_to_password_only_and_revokes_other_sessions() -> None:
    clock = OfflineAuthenticationClock()
    application = create_totp_test_app(clock=clock)
    with (
        TestClient(application, base_url=_SECURE_BASE_URL) as first_client,
        TestClient(application, base_url=_SECURE_BASE_URL) as second_client,
    ):
        first_cookies = login(first_client)
        _second_cookies = login(second_client)
        _, secret, _ = complete_enrollment(first_client, first_cookies, clock)
        clock.database_now_value += timedelta(seconds=30)
        disabled = first_client.request("DELETE", 
            "/api/auth/totp",
            headers=authenticated_headers(first_cookies),
            json={"password": _VALID_LOGIN["password"], "totp_code": code_at(clock, secret)},
        )
        assert disabled.status_code == 200, disabled.text
        assert disabled.json()["data"]["state"] == "active"
        assert disabled.cookies[SESSION_COOKIE_NAME] != first_cookies["session"]
        # Every other Web session is revoked; the current one stays password-only.
        revoked = second_client.get("/api/auth/session", headers={"Origin": ORIGIN})
        assert revoked.status_code == 401
        rotated_view = first_client.get("/api/auth/session", headers={"Origin": ORIGIN})
        assert rotated_view.status_code == 200
        assert rotated_view.json()["data"]["state"] == "active"
        # A fresh password login lands directly in the active state.
        login(first_client)
        fresh_view = first_client.get("/api/auth/session", headers={"Origin": ORIGIN})
        assert fresh_view.json()["data"]["state"] == "active"


def test_disable_requires_both_proofs(client: TestClient) -> None:
    clock = OfflineAuthenticationClock()
    with TestClient(
        create_totp_test_app(clock=clock), base_url=_SECURE_BASE_URL
    ) as security_client:
        cookies = login(security_client)
        _, secret, _ = complete_enrollment(security_client, cookies, clock)
        clock.database_now_value += timedelta(seconds=30)
        wrong_totp = security_client.request("DELETE", 
            "/api/auth/totp",
            headers=authenticated_headers(cookies),
            json={"password": _VALID_LOGIN["password"], "totp_code": "000000"},
        )
        assert wrong_totp.status_code == 401
        wrong_password = security_client.request("DELETE", 
            "/api/auth/totp",
            headers=authenticated_headers(cookies),
            json={"password": _WRONG_PASSWORD, "totp_code": code_at(clock, secret)},
        )
        assert wrong_password.status_code == 401


# --- re-authentication TOTP leg (spec 9.4) ---------------------------------------------------


def test_reauthenticate_demands_a_totp_code_when_totp_is_active() -> None:
    clock = OfflineAuthenticationClock()
    with TestClient(
        create_totp_test_app(clock=clock), base_url=_SECURE_BASE_URL
    ) as reauth_client:
        cookies = login(reauth_client)
        _, secret, _ = complete_enrollment(reauth_client, cookies, clock)
        clock.database_now_value += timedelta(seconds=30)
        without_code = reauth_client.post(
            "/api/auth/reauthenticate",
            headers=authenticated_headers(cookies),
            json={"password": _VALID_LOGIN["password"]},
        )
        assert without_code.status_code == 401
        assert without_code.json()["error"]["code"] == "authentication_failed"
        wrong_code = reauth_client.post(
            "/api/auth/reauthenticate",
            headers=authenticated_headers(cookies),
            json={"password": _VALID_LOGIN["password"], "totp_code": "000000"},
        )
        assert wrong_code.status_code == 401
        accepted = reauth_client.post(
            "/api/auth/reauthenticate",
            headers=authenticated_headers(cookies),
            json={
                "password": _VALID_LOGIN["password"],
                "totp_code": code_at(clock, secret),
            },
        )
        assert accepted.status_code == 200, accepted.text
        assert accepted.cookies[SESSION_COOKIE_NAME] != cookies["session"]


def test_reauthenticate_without_totp_keeps_the_password_only_contract(
    client: TestClient,
) -> None:
    cookies = login(client)
    response = client.post(
        "/api/auth/reauthenticate",
        headers=authenticated_headers(cookies),
        json={"password": _VALID_LOGIN["password"]},
    )
    assert response.status_code == 200
    assert response.json()["data"]["state"] == "active"


# --- serialized same-code race ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_same_totp_step_is_accepted_once_under_serialized_race() -> None:
    clock = OfflineAuthenticationClock()
    runtime = compose_offline_web_authentication(totp_active=True, clock=clock)
    started = await runtime.login_service.login(
        username="admin",
        password=_VALID_LOGIN["password"],
        source_bucket="race-source",
        diagnostic_context=create_diagnostic_context().context,
    )
    assert started.started_session is not None
    code = code_at(clock, OFFLINE_TOTP_SECRET)
    results = await asyncio.gather(
        runtime.totp_service.verify_session_totp(
            session_secret=started.started_session.session_secret,
            code=code,
            diagnostic_context=create_diagnostic_context().context,
        ),
        runtime.totp_service.verify_session_totp(
            session_secret=started.started_session.session_secret,
            code=code,
            diagnostic_context=create_diagnostic_context().context,
        ),
        return_exceptions=True,
    )
    successes = sum(
        (not isinstance(result, BaseException)) and result.public_error is None
        for result in results
    )
    assert successes == 1


# --- route closure ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/auth/totp/verify/"),
        ("POST", "/api/auth/totp/enrollments/"),
        ("GET", "/api/auth/totp"),
        ("POST", "/api/auth/totp/recovery-codes/regenerate/"),
        ("PUT", "/api/auth/totp/verify"),
    ],
)
def test_totp_route_set_stays_closed(client: TestClient, method: str, path: str) -> None:
    response = client.request(
        method, path, headers={"Origin": ORIGIN}, json={"code": "123456"}
    )
    assert response.status_code in (404, 405)
    assert response.json()["error"]["code"] in {
        "api_route_not_found",
        "api_method_not_allowed",
    }


def test_totp_error_responses_carry_no_secret_material(client: TestClient) -> None:
    cookies = login(client)
    response = client.post(
        "/api/auth/totp/verify",
        headers=authenticated_headers(cookies),
        json={"code": "999999"},
    )
    assert response.status_code == 401
    assert "999999" not in response.text
    assert response.headers["cache-control"] == "no-store"
