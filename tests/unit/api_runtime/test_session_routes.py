"""Session/password HTTP routes over the offline deterministic composition.

These tests drive the five closed session/password routes through the FastAPI
``TestClient`` against the real application factory wired with the offline
deterministic authentication composition: no database, no key file and no
environment read. They pin the cookie contract (browser-session
``__Host-admin_session`` Secure HttpOnly SameSite=Lax Path-slash cookie plus
the readable CSRF cookie), the exact-origin gate, the CSRF cookie/header/hash
triple check, the closed error envelopes with ``Cache-Control: no-store`` on
success and failure alike, the session-state payload a client needs to decide
whether a TOTP challenge remains, logout revocation with cookie clearing,
re-authentication and password-change rotation, and the login throttle's
rate-limited exit with its safe retry detail.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Final

import httpx
import pytest
from api_runtime.application import create_api_application
from api_runtime.authentication_composition import (
    _OFFLINE_MASTER_KEY,
    OFFLINE_WEB_ALLOWED_ORIGIN,
    OfflineAuthenticationClock,
    OfflineAuthenticationCrypto,
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
from personal_os.authentication.sessions import (
    ThrottleBucketKind,
    derive_throttle_hmac_key,
    session_secret_hash_of,
    throttle_bucket_hash,
)
from personal_os.runtime_configuration.models import RuntimeEnvironment

#: The offline composition accepts exactly this origin (spec 9.3).
ORIGIN: Final[str] = OFFLINE_WEB_ALLOWED_ORIGIN

#: A secure base URL so the browser-style Secure cookies are replayed by the
#: cookie jar exactly like a real deployment origin would.
_SECURE_BASE_URL: Final[str] = OFFLINE_WEB_ALLOWED_ORIGIN

_VALID_LOGIN: Final[dict[str, str]] = {
    "username": "admin",
    "password": "correct-horse-battery-staple",
}
_WRONG_LOGIN: Final[dict[str, str]] = {
    "username": "admin",
    "password": "sentinel-wrong-password-value",
}
_VALID_CHANGE: Final[dict[str, str]] = {"new_password": "fresh-trebuchet-unlock-phrase"}

_ENVELOPE_KEYS: Final[frozenset[str]] = frozenset({"request_id", "data", "warnings", "error"})
_WEB_SCOPES: Final[tuple[str, ...]] = (
    "device_administration_manage",
    "device_authorization_approve",
    "web_security_manage",
)


class _ReadyProbe:
    """Readiness probe stub: the session routes never consult dependencies."""

    async def check(self) -> None: ...


def create_session_test_app(
    *,
    totp_active: bool = False,
    clock: OfflineAuthenticationClock | None = None,
    state: OfflineAuthenticationState | None = None,
    trusted_proxy_cidrs: tuple[str, ...] = (),
) -> FastAPI:
    """Compose the real application over the offline deterministic ports."""
    return create_api_application(
        environment=RuntimeEnvironment.TEST,
        readiness_probe=_ReadyProbe(),
        web_authentication=compose_offline_web_authentication(
            totp_active=totp_active,
            clock=clock,
            state=state,
            trusted_proxy_cidrs=trusted_proxy_cidrs,
        ),
    )


def login_source_bucket_hash(source: str) -> str:
    """Hash one login source bucket exactly like the offline LoginService."""
    hmac_key = derive_throttle_hmac_key(OfflineAuthenticationCrypto(), _OFFLINE_MASTER_KEY)
    return throttle_bucket_hash(
        hmac_key=hmac_key, bucket_kind=ThrottleBucketKind.LOGIN_SOURCE, bucket_material=source
    )


async def post_failed_login_behind_transport(
    app: FastAPI, *, client_address: tuple[str, int], forwarded_for: str
) -> None:
    """Send one wrong-password login from the given socket peer."""
    transport = httpx.ASGITransport(app=app, client=client_address, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url=_SECURE_BASE_URL) as client:
        response = await client.post(
            "/api/auth/login",
            headers={"Origin": ORIGIN, "x-forwarded-for": forwarded_for},
            json=_WRONG_LOGIN,
        )
    assert response.status_code == 401, response.text
    assert response.json()["error"]["code"] == "authentication_failed"


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(create_session_test_app(), base_url=_SECURE_BASE_URL) as test_client:
        yield test_client


def login(
    test_client: TestClient, *, origin: str = ORIGIN, body: dict[str, str] | None = None
) -> dict[str, str]:
    """Log in and return the session and CSRF cookie values the server set."""
    response = test_client.post(
        "/api/auth/login", headers={"Origin": origin}, json=body or _VALID_LOGIN
    )
    assert response.status_code == 200, response.text
    session_cookie = response.cookies[SESSION_COOKIE_NAME]
    csrf_cookie = response.cookies[CSRF_COOKIE_NAME]
    assert session_cookie and csrf_cookie
    return {"session": session_cookie, "csrf": csrf_cookie}


def authenticated_headers(origin: str, cookies: dict[str, str]) -> dict[str, str]:
    """Origin plus the session and CSRF cookie/header pair of one session."""
    return {
        "Origin": origin,
        "Cookie": (
            f"{SESSION_COOKIE_NAME}={cookies['session']}; {CSRF_COOKIE_NAME}={cookies['csrf']}"
        ),
        "X-CSRF-Token": cookies["csrf"],
    }


# --- login ---------------------------------------------------------------------------


def test_login_sets_host_session_and_csrf_cookies(client: TestClient) -> None:
    response = client.post("/api/auth/login", headers={"Origin": ORIGIN}, json=_VALID_LOGIN)
    assert response.status_code == 200
    set_cookies = response.headers.get_list("set-cookie")
    assert any(
        cookie.startswith(f"{SESSION_COOKIE_NAME}=")
        and "HttpOnly" in cookie
        and "Secure" in cookie
        and "SameSite=lax" in cookie
        and "Path=/" in cookie
        and "Domain" not in cookie
        and "Max-Age" not in cookie
        and "Expires" not in cookie
        for cookie in set_cookies
    )
    assert response.headers["cache-control"] == "no-store"


def test_login_csrf_cookie_is_secure_but_readable_by_the_browser(client: TestClient) -> None:
    response = client.post("/api/auth/login", headers={"Origin": ORIGIN}, json=_VALID_LOGIN)
    csrf_cookie = next(
        cookie
        for cookie in response.headers.get_list("set-cookie")
        if cookie.startswith(f"{CSRF_COOKIE_NAME}=")
    )
    assert "HttpOnly" not in csrf_cookie
    assert "Secure" in csrf_cookie
    assert "SameSite=lax" in csrf_cookie
    assert "Path=/" in csrf_cookie
    assert "Domain" not in csrf_cookie


def test_login_returns_active_session_data_with_scopes(client: TestClient) -> None:
    response = client.post("/api/auth/login", headers={"Origin": ORIGIN}, json=_VALID_LOGIN)
    payload = response.json()
    assert set(payload) == _ENVELOPE_KEYS
    assert payload["error"] is None
    assert payload["warnings"] == []
    assert payload["data"]["state"] == "active"
    assert payload["data"]["authenticated"] is True
    assert frozenset(payload["data"]["scopes"]) == frozenset(_WEB_SCOPES)
    assert payload["data"]["idle_expires_at"]
    assert payload["data"]["absolute_expires_at"]
    assert payload["request_id"] == response.headers["x-request-id"]


def test_login_with_active_totp_reports_pending_totp_state() -> None:
    with TestClient(
        create_session_test_app(totp_active=True), base_url=_SECURE_BASE_URL
    ) as totp_client:
        response = totp_client.post(
            "/api/auth/login", headers={"Origin": ORIGIN}, json=_VALID_LOGIN
        )
    assert response.status_code == 200
    assert response.json()["data"]["state"] == "pending_totp"
    assert response.json()["data"]["authenticated"] is False
    assert response.json()["data"]["scopes"] == []


def test_login_with_wrong_password_fails_closed_without_cookies(client: TestClient) -> None:
    response = client.post("/api/auth/login", headers={"Origin": ORIGIN}, json=_WRONG_LOGIN)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_failed"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers.get_list("set-cookie") == []
    assert _WRONG_LOGIN["password"] not in response.text


@pytest.mark.parametrize("origin", ["https://attacker.example", "null"])
def test_login_requires_the_exact_configured_origin(client: TestClient, origin: str) -> None:
    response = client.post("/api/auth/login", headers={"Origin": origin}, json=_VALID_LOGIN)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "csrf_validation_failed"
    assert response.headers["cache-control"] == "no-store"


def test_login_without_origin_header_is_rejected(client: TestClient) -> None:
    response = client.post("/api/auth/login", json=_VALID_LOGIN)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "csrf_validation_failed"


def test_login_with_non_ascii_origin_bytes_is_rejected_closed(client: TestClient) -> None:
    # Starlette decodes headers latin-1: a non-ASCII byte must close with the
    # documented 403, never escalate to an internal error.
    response = client.post(
        "/api/auth/login",
        headers={"Origin": "https://attacker.example/\xfc".encode("latin-1")},
        json=_VALID_LOGIN,
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "csrf_validation_failed"
    assert response.headers["cache-control"] == "no-store"


def test_login_locks_after_five_failures_with_safe_retry_detail(client: TestClient) -> None:
    for _ in range(5):
        rejected = client.post("/api/auth/login", headers={"Origin": ORIGIN}, json=_WRONG_LOGIN)
        assert rejected.status_code == 401
    locked = client.post("/api/auth/login", headers={"Origin": ORIGIN}, json=_VALID_LOGIN)
    assert locked.status_code == 429
    error = locked.json()["error"]
    assert error["code"] == "authentication_rate_limited"
    assert error["retryable"] is True
    assert error["details"]["retry_after_seconds"] > 0
    assert locked.headers["cache-control"] == "no-store"


class CountingOfflineClock(OfflineAuthenticationClock):
    """Offline clock double counting every decision-clock read."""

    def __init__(self) -> None:
        super().__init__()
        self.database_now_call_count = 0

    async def database_now(self) -> datetime:
        self.database_now_call_count += 1
        return await super().database_now()


def test_rate_limited_login_retry_after_reads_no_second_decision_clock() -> None:
    # The throttled exit computes its retry detail from the outcome's carried
    # decision clock: one rate-limited request reads the clock exactly once —
    # the login decision's own read — never a second read for the detail.
    clock = CountingOfflineClock()
    with TestClient(
        create_session_test_app(clock=clock), base_url=_SECURE_BASE_URL
    ) as rate_limited_client:
        for _ in range(5):
            rejected = rate_limited_client.post(
                "/api/auth/login", headers={"Origin": ORIGIN}, json=_WRONG_LOGIN
            )
            assert rejected.status_code == 401
        calls_before_locked_request = clock.database_now_call_count
        locked = rate_limited_client.post(
            "/api/auth/login", headers={"Origin": ORIGIN}, json=_VALID_LOGIN
        )
        assert locked.status_code == 429
        assert locked.json()["error"]["details"]["retry_after_seconds"] > 0
        assert clock.database_now_call_count - calls_before_locked_request == 1


def test_malformed_login_body_is_rejected_without_cookie_changes(client: TestClient) -> None:
    response = client.post(
        "/api/auth/login",
        headers={"Origin": ORIGIN, "Content-Type": "application/json"},
        content=b'{"username": "admin", "password": "sentinel-',
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "api_request_malformed"
    assert response.headers.get_list("set-cookie") == []


# --- session -------------------------------------------------------------------------


def test_session_requires_the_session_cookie(client: TestClient) -> None:
    response = client.get("/api/auth/session", headers={"Origin": ORIGIN})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_required"
    assert response.headers["cache-control"] == "no-store"


def test_session_returns_the_authenticated_view(client: TestClient) -> None:
    login(client)
    response = client.get("/api/auth/session", headers={"Origin": ORIGIN})
    assert response.status_code == 200
    assert response.json()["data"]["state"] == "active"
    assert response.json()["data"]["authenticated"] is True
    assert frozenset(response.json()["data"]["scopes"]) == frozenset(_WEB_SCOPES)
    assert response.headers["cache-control"] == "no-store"


def test_session_with_unknown_cookie_value_is_rejected(client: TestClient) -> None:
    client.post("/api/auth/login", headers={"Origin": ORIGIN}, json=_VALID_LOGIN)
    response = client.get(
        "/api/auth/session",
        headers={"Origin": ORIGIN, "Cookie": f"{SESSION_COOKIE_NAME}=forged-secret-value"},
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_required"


def test_session_requires_the_exact_origin(client: TestClient) -> None:
    login(client)
    response = client.get("/api/auth/session", headers={"Origin": "https://attacker.example"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "csrf_validation_failed"


# --- logout --------------------------------------------------------------------------


def test_logout_revokes_the_session_and_clears_cookies(client: TestClient) -> None:
    cookies = login(client)
    response = client.post("/api/auth/logout", headers=authenticated_headers(ORIGIN, cookies))
    assert response.status_code == 200
    assert response.json()["data"]["state"] == "revoked"
    assert response.json()["data"]["authenticated"] is False
    clearing = response.headers.get_list("set-cookie")
    assert any(
        cookie.startswith(f"{SESSION_COOKIE_NAME}=") and "Max-Age=0" in cookie
        for cookie in clearing
    )
    assert any(
        cookie.startswith(f"{CSRF_COOKIE_NAME}=") and "Max-Age=0" in cookie for cookie in clearing
    )
    assert response.headers["cache-control"] == "no-store"
    revoked = client.get("/api/auth/session", headers={"Origin": ORIGIN})
    assert revoked.status_code == 401


def test_logout_rejects_a_missing_csrf_pair(client: TestClient) -> None:
    cookies = login(client)
    response = client.post(
        "/api/auth/logout",
        headers={"Origin": ORIGIN, "Cookie": f"{SESSION_COOKIE_NAME}={cookies['session']}"},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "csrf_validation_failed"


def test_logout_succeeds_from_a_pending_totp_session() -> None:
    # Spec 9.2: a pending_totp session may call logout even though it never
    # authenticates any other route.
    with TestClient(
        create_session_test_app(totp_active=True), base_url=_SECURE_BASE_URL
    ) as totp_client:
        cookies = login(totp_client)
        response = totp_client.post(
            "/api/auth/logout", headers=authenticated_headers(ORIGIN, cookies)
        )
        assert response.status_code == 200, response.text
        assert response.json()["data"]["state"] == "revoked"
        assert response.json()["data"]["authenticated"] is False
        assert any(
            cookie.startswith(f"{SESSION_COOKIE_NAME}=") and "Max-Age=0" in cookie
            for cookie in response.headers.get_list("set-cookie")
        )
        assert any(
            cookie.startswith(f"{CSRF_COOKIE_NAME}=") and "Max-Age=0" in cookie
            for cookie in response.headers.get_list("set-cookie")
        )
        afterwards = totp_client.get("/api/auth/session", headers={"Origin": ORIGIN})
        assert afterwards.status_code == 401


def test_logout_succeeds_from_a_recovery_limited_session() -> None:
    offline_state = OfflineAuthenticationState(totp_active=False)
    with TestClient(
        create_session_test_app(state=offline_state), base_url=_SECURE_BASE_URL
    ) as recovery_client:
        cookies = login(recovery_client)
        stored = offline_state.sessions_by_secret_hash[session_secret_hash_of(cookies["session"])]
        offline_state.sessions_by_secret_hash[stored.session_secret_hash] = replace(
            stored, state=WebSessionState.RECOVERY_LIMITED
        )
        response = recovery_client.post(
            "/api/auth/logout", headers=authenticated_headers(ORIGIN, cookies)
        )
        assert response.status_code == 200, response.text
        assert response.json()["data"]["state"] == "revoked"
        assert response.json()["data"]["authenticated"] is False
        assert response.headers.get_list("set-cookie") != []


def test_logout_from_an_expired_pending_totp_session_is_rejected() -> None:
    clock = OfflineAuthenticationClock()
    with TestClient(
        create_session_test_app(totp_active=True, clock=clock), base_url=_SECURE_BASE_URL
    ) as totp_client:
        cookies = login(totp_client)
        clock.database_now_value += timedelta(minutes=6)
        response = totp_client.post(
            "/api/auth/logout", headers=authenticated_headers(ORIGIN, cookies)
        )
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "authentication_required"


# --- reauthentication ----------------------------------------------------------------


def test_reauthenticate_rotates_the_session_binding(client: TestClient) -> None:
    cookies = login(client)
    response = client.post(
        "/api/auth/reauthenticate",
        headers=authenticated_headers(ORIGIN, cookies),
        json={"password": _VALID_LOGIN["password"]},
    )
    assert response.status_code == 200
    assert response.json()["data"]["state"] == "active"
    assert response.json()["data"]["authenticated"] is True
    rotated = response.cookies[SESSION_COOKIE_NAME]
    assert rotated != cookies["session"]
    assert response.headers["cache-control"] == "no-store"


def test_reauthenticate_with_wrong_password_fails_closed(client: TestClient) -> None:
    cookies = login(client)
    response = client.post(
        "/api/auth/reauthenticate",
        headers=authenticated_headers(ORIGIN, cookies),
        json={"password": _WRONG_LOGIN["password"]},
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_failed"
    assert response.headers.get_list("set-cookie") == []


def test_reauthenticate_from_a_pending_totp_session_still_requires_active() -> None:
    # Only logout tolerates the pending state; re-authentication keeps the
    # strict active-only resolution (spec 9.2, 9.4).
    with TestClient(
        create_session_test_app(totp_active=True), base_url=_SECURE_BASE_URL
    ) as totp_client:
        cookies = login(totp_client)
        response = totp_client.post(
            "/api/auth/reauthenticate",
            headers=authenticated_headers(ORIGIN, cookies),
            json={"password": _VALID_LOGIN["password"]},
        )
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "authentication_required"


# --- password change -----------------------------------------------------------------


def test_password_change_rejects_missing_csrf(client: TestClient) -> None:
    assert (
        client.put("/api/auth/password", json=_VALID_CHANGE).json()["error"]["code"]
        == "csrf_validation_failed"
    )


def test_password_change_rejects_an_unequal_csrf_header(client: TestClient) -> None:
    cookies = login(client)
    response = client.put(
        "/api/auth/password",
        headers={
            "Origin": ORIGIN,
            "Cookie": (
                f"{SESSION_COOKIE_NAME}={cookies['session']}; {CSRF_COOKIE_NAME}={cookies['csrf']}"
            ),
            "X-CSRF-Token": "forged-csrf-header-value",
        },
        json=_VALID_CHANGE,
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "csrf_validation_failed"


def test_password_change_with_a_non_ascii_csrf_pair_is_rejected_closed(
    client: TestClient,
) -> None:
    cookies = login(client)
    non_ascii_pair = "f\xf6rged-equal-pair".encode("latin-1")
    response = client.put(
        "/api/auth/password",
        headers={
            "Origin": ORIGIN,
            "Cookie": (
                f"{SESSION_COOKIE_NAME}={cookies['session']}; {CSRF_COOKIE_NAME}=".encode("ascii")
                + non_ascii_pair
            ),
            "X-CSRF-Token": non_ascii_pair,
        },
        json=_VALID_CHANGE,
    )
    # The equal-but-non-ASCII pair must close with the documented 403 instead
    # of escalating to an internal error inside the comparison.
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "csrf_validation_failed"


def test_password_change_rotates_and_revokes_other_sessions() -> None:
    application = create_session_test_app()
    with (
        TestClient(application, base_url=_SECURE_BASE_URL) as first_client,
        TestClient(application, base_url=_SECURE_BASE_URL) as second_client,
    ):
        first_cookies = login(first_client)
        second_cookies = login(second_client)
        response = first_client.put(
            "/api/auth/password",
            headers=authenticated_headers(ORIGIN, first_cookies),
            json=_VALID_CHANGE,
        )
        assert response.status_code == 200, response.text
        assert response.json()["data"]["state"] == "active"
        assert response.cookies[SESSION_COOKIE_NAME] != first_cookies["session"]
        revoked = second_client.get(
            "/api/auth/session",
            headers={
                "Origin": ORIGIN,
                "Cookie": f"{SESSION_COOKIE_NAME}={second_cookies['session']}",
            },
        )
        assert revoked.status_code == 401
        rotated = first_client.get("/api/auth/session", headers={"Origin": ORIGIN})
        assert rotated.status_code == 200


def test_password_change_requires_recent_authentication() -> None:
    clock = OfflineAuthenticationClock()
    with TestClient(
        create_session_test_app(clock=clock), base_url=_SECURE_BASE_URL
    ) as timed_client:
        cookies = login(timed_client)
        clock.database_now_value += timedelta(minutes=6)
        response = timed_client.put(
            "/api/auth/password",
            headers=authenticated_headers(ORIGIN, cookies),
            json=_VALID_CHANGE,
        )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "recent_authentication_required"
    assert response.headers["cache-control"] == "no-store"


# --- route closure -------------------------------------------------------------------


def test_trailing_slash_is_never_redirected(client: TestClient) -> None:
    response = client.post("/api/auth/login/", headers={"Origin": ORIGIN}, json=_VALID_LOGIN)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "api_route_not_found"


def test_disallowed_method_uses_the_closed_error_envelope(client: TestClient) -> None:
    response = client.put("/api/auth/session", headers={"Origin": ORIGIN})
    assert response.status_code == 405
    assert response.json()["error"]["code"] == "api_method_not_allowed"


# --- trusted-proxy source buckets -------------------------------------------------------


@pytest.mark.asyncio
async def test_login_source_bucket_uses_the_forwarded_client_behind_configured_trust() -> None:
    state = OfflineAuthenticationState(totp_active=False)
    app = create_session_test_app(state=state, trusted_proxy_cidrs=("192.0.2.0/24",))
    await post_failed_login_behind_transport(
        app, client_address=("192.0.2.10", 443), forwarded_for="198.51.100.7"
    )
    assert login_source_bucket_hash("198.51.100.7") in state.source_buckets
    assert login_source_bucket_hash("192.0.2.10") not in state.source_buckets


@pytest.mark.asyncio
async def test_login_source_bucket_ignores_forwarded_headers_without_configured_trust() -> None:
    state = OfflineAuthenticationState(totp_active=False)
    app = create_session_test_app(state=state)
    await post_failed_login_behind_transport(
        app, client_address=("192.0.2.10", 443), forwarded_for="198.51.100.7"
    )
    assert login_source_bucket_hash("192.0.2.10") in state.source_buckets
    assert login_source_bucket_hash("198.51.100.7") not in state.source_buckets
