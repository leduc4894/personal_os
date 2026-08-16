"""Session/CSRF dependencies and cookie response helpers (spec 9.1, 9.3).

These tests pin the framework adapter layer in isolation: the cookie contract
picks the ``__Host-`` Secure names for real origins and keeps the explicit
loopback local-development names without ``Secure`` restricted to the local and
test environments; the origin guard enforces exact string equality against the
configured allowed origin; the session dependency resolves the cookie through
the service and attaches the typed authenticated context to the request state;
the CSRF dependency requires the cookie, an equal header and the stored-hash
match; and the response helpers render exactly the approved cookie attributes.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import timedelta
from typing import Any, Final

import pytest
from api_runtime.authentication_composition import (
    OFFLINE_WEB_ALLOWED_ORIGIN,
    OfflineAuthenticationState,
    WebAuthenticationRuntime,
    assert_keyring_covers_required_key_ids,
    compose_offline_web_authentication,
)
from api_runtime.authentication_dependencies import (
    CSRF_COOKIE_NAME,
    CSRF_HEADER_NAME,
    LOCAL_CSRF_COOKIE_NAME,
    LOCAL_SESSION_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    AuthenticatedWebRequest,
    apply_session_cookies,
    build_session_cookie_contract,
    clear_session_cookies,
    create_session_route_dependencies,
)
from fastapi import Request
from fastapi.responses import JSONResponse

from personal_os.authentication.contracts import WebSessionState
from personal_os.authentication.errors import AuthenticationError
from personal_os.authentication.sessions import StartedWebSession
from personal_os.diagnostics.context import create_diagnostic_context
from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import ConfigurationError
from personal_os.runtime_configuration.models import RuntimeEnvironment

_LOOPBACK_ORIGIN: Final[str] = "http://127.0.0.1:3000"
_SECURE_LOOPBACK_ORIGIN: Final[str] = "https://127.0.0.1"


def build_request(*, headers: Mapping[str, str], path: str = "/api/auth/password") -> Request:
    """Build one HTTP request scope carrying exactly the given headers."""
    scope: dict[str, Any] = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "root_path": "",
        "headers": [
            (name.lower().encode("latin-1"), value.encode("latin-1"))
            for name, value in headers.items()
        ],
        "client": ("192.0.2.10", 41000),
        "server": ("web-admin.example", 443),
    }
    return Request(scope)


# --- cookie contract -----------------------------------------------------------------


def test_real_origins_use_the_host_prefixed_secure_contract() -> None:
    for environment in (RuntimeEnvironment.LOCAL, RuntimeEnvironment.STAGING):
        contract = build_session_cookie_contract(OFFLINE_WEB_ALLOWED_ORIGIN, environment)
        assert contract.session_cookie_name == SESSION_COOKIE_NAME
        assert contract.csrf_cookie_name == CSRF_COOKIE_NAME
        assert contract.is_secure is True
        assert contract.is_local_loopback is False


def test_loopback_local_mode_uses_local_names_without_secure() -> None:
    contract = build_session_cookie_contract(_LOOPBACK_ORIGIN, RuntimeEnvironment.LOCAL)
    assert contract.session_cookie_name == LOCAL_SESSION_COOKIE_NAME
    assert contract.csrf_cookie_name == LOCAL_CSRF_COOKIE_NAME
    assert contract.is_secure is False
    assert contract.is_local_loopback is True


def test_loopback_local_mode_cannot_activate_in_staging_or_production() -> None:
    for environment in (RuntimeEnvironment.STAGING, RuntimeEnvironment.PRODUCTION):
        contract = build_session_cookie_contract(_LOOPBACK_ORIGIN, environment)
        assert contract.session_cookie_name == SESSION_COOKIE_NAME
        assert contract.csrf_cookie_name == CSRF_COOKIE_NAME
        assert contract.is_secure is True
        assert contract.is_local_loopback is False


def test_secure_loopback_origin_still_uses_the_secure_contract() -> None:
    contract = build_session_cookie_contract(_SECURE_LOOPBACK_ORIGIN, RuntimeEnvironment.TEST)
    assert contract.session_cookie_name == SESSION_COOKIE_NAME
    assert contract.is_secure is True


# --- response helpers ----------------------------------------------------------------


def test_apply_session_cookies_renders_the_approved_attributes() -> None:
    contract = build_session_cookie_contract(
        OFFLINE_WEB_ALLOWED_ORIGIN, RuntimeEnvironment.TEST
    )
    response = JSONResponse(content={})
    apply_session_cookies(
        response,
        contract,
        session_secret="session-secret-value",
        csrf_secret="csrf-secret-value",
    )
    session_cookie = next(
        cookie
        for cookie in response.headers.getlist("set-cookie")
        if cookie.startswith(f"{SESSION_COOKIE_NAME}=")
    )
    csrf_cookie = next(
        cookie
        for cookie in response.headers.getlist("set-cookie")
        if cookie.startswith(f"{CSRF_COOKIE_NAME}=")
    )
    assert session_cookie.startswith(f"{SESSION_COOKIE_NAME}=session-secret-value;")
    assert "HttpOnly" in session_cookie
    assert "Secure" in session_cookie
    assert "SameSite=lax" in session_cookie
    assert "Path=/" in session_cookie
    assert "Domain" not in session_cookie
    assert csrf_cookie.startswith(f"{CSRF_COOKIE_NAME}=csrf-secret-value;")
    assert "HttpOnly" not in csrf_cookie
    assert "Secure" in csrf_cookie
    assert "SameSite=lax" in csrf_cookie


def test_clear_session_cookies_expires_both_bindings() -> None:
    contract = build_session_cookie_contract(_LOOPBACK_ORIGIN, RuntimeEnvironment.LOCAL)
    response = JSONResponse(content={})
    clear_session_cookies(response, contract)
    clearing = response.headers.getlist("set-cookie")
    assert any(
        cookie.startswith(f"{LOCAL_SESSION_COOKIE_NAME}=") and "Max-Age=0" in cookie
        for cookie in clearing
    )
    assert any(
        cookie.startswith(f"{LOCAL_CSRF_COOKIE_NAME}=") and "Max-Age=0" in cookie
        for cookie in clearing
    )


# --- origin guard --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_origin_guard_accepts_the_exact_allowed_origin() -> None:
    dependencies = create_session_route_dependencies(compose_offline_web_authentication())
    result = await dependencies.require_allowed_origin(
        build_request(headers={"origin": OFFLINE_WEB_ALLOWED_ORIGIN})
    )
    assert result is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "origin_value",
    [
        None,
        "https://attacker.example",
        OFFLINE_WEB_ALLOWED_ORIGIN + "/",
        "null",
        # Starlette decodes headers latin-1, so a non-ASCII byte arrives as a
        # non-ASCII str: the guard must close with the CSRF code, not crash.
        "https://attacker.example/\xfc",
        "https://attacker.example/\xfc\ber",
    ],
)
async def test_origin_guard_rejects_every_non_exact_origin(origin_value: str | None) -> None:
    headers: dict[str, str] = {}
    if origin_value is not None:
        headers["origin"] = origin_value
    dependencies = create_session_route_dependencies(compose_offline_web_authentication())
    with pytest.raises(AuthenticationError) as rejection:
        await dependencies.require_allowed_origin(build_request(headers=headers))
    assert rejection.value.error_code is ErrorCode.CSRF_VALIDATION_FAILED


# --- session resolution --------------------------------------------------------------


async def start_offline_session() -> tuple[StartedWebSession, WebAuthenticationRuntime]:
    """Start one offline session and return it with its runtime."""
    runtime = compose_offline_web_authentication()
    outcome = await runtime.login_service.login(
        username="admin",
        password="correct-horse-battery-staple",
        source_bucket="192.0.2.10",
        diagnostic_context=create_diagnostic_context().context,
    )
    assert outcome.started_session is not None
    return outcome.started_session, runtime


@pytest.mark.asyncio
async def test_session_dependency_attaches_the_typed_context_to_request_state() -> None:
    started_session, runtime = await start_offline_session()
    dependencies = create_session_route_dependencies(runtime)
    request = build_request(
        headers={
            "origin": OFFLINE_WEB_ALLOWED_ORIGIN,
            "cookie": f"{SESSION_COOKIE_NAME}={started_session.session_secret}",
        },
        path="/api/auth/session",
    )
    resolved = await dependencies.require_session_request(request)
    assert isinstance(resolved, AuthenticatedWebRequest)
    assert resolved.context.web_session_id == started_session.web_session_id
    assert resolved.context.scopes
    assert resolved.session.state.value == "active"
    assert request.state.authentication is resolved


@pytest.mark.asyncio
@pytest.mark.parametrize("session_cookie_value", [None, "forged-session-secret"])
async def test_session_dependency_rejects_missing_or_unknown_cookies(
    session_cookie_value: str | None,
) -> None:
    runtime = compose_offline_web_authentication()
    dependencies = create_session_route_dependencies(runtime)
    headers = {"origin": OFFLINE_WEB_ALLOWED_ORIGIN}
    if session_cookie_value is not None:
        headers["cookie"] = f"{SESSION_COOKIE_NAME}={session_cookie_value}"
    with pytest.raises(AuthenticationError) as rejection:
        await dependencies.require_session_request(
            build_request(headers=headers, path="/api/auth/session")
        )
    assert rejection.value.error_code is ErrorCode.AUTHENTICATION_REQUIRED


# --- CSRF triple check ---------------------------------------------------------------


def csrf_request_headers(
    started_session: StartedWebSession, *, csrf_value: str | None
) -> dict[str, str]:
    """Origin and session cookie plus an optional CSRF cookie/header pair."""
    cookie_value = f"{SESSION_COOKIE_NAME}={started_session.session_secret}"
    headers = {"origin": OFFLINE_WEB_ALLOWED_ORIGIN}
    if csrf_value is not None:
        cookie_value += f"; {CSRF_COOKIE_NAME}={csrf_value}"
        headers[CSRF_HEADER_NAME] = csrf_value
    headers["cookie"] = cookie_value
    return headers


@pytest.mark.asyncio
async def test_csrf_dependency_accepts_the_matching_triple() -> None:
    started_session, runtime = await start_offline_session()
    dependencies = create_session_route_dependencies(runtime)
    request = build_request(
        headers=csrf_request_headers(started_session, csrf_value=started_session.csrf_secret)
    )
    resolved = await dependencies.require_csrf_protected_request(request)
    assert isinstance(resolved, AuthenticatedWebRequest)
    assert request.state.authentication is resolved


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("csrf_cookie_value", "csrf_header_value"),
    [(None, None), ("matching-value", None), ("matching-value", "unequal-value")],
)
async def test_csrf_dependency_requires_the_cookie_and_header_pair(
    csrf_cookie_value: str | None, csrf_header_value: str | None
) -> None:
    started_session, runtime = await start_offline_session()
    dependencies = create_session_route_dependencies(runtime)
    headers = csrf_request_headers(started_session, csrf_value=csrf_cookie_value)
    if csrf_header_value is None:
        headers.pop(CSRF_HEADER_NAME, None)
    else:
        headers[CSRF_HEADER_NAME] = csrf_header_value
    with pytest.raises(AuthenticationError) as rejection:
        await dependencies.require_csrf_protected_request(build_request(headers=headers))
    assert rejection.value.error_code is ErrorCode.CSRF_VALIDATION_FAILED


@pytest.mark.asyncio
async def test_csrf_dependency_requires_the_stored_hash_match() -> None:
    started_session, runtime = await start_offline_session()
    dependencies = create_session_route_dependencies(runtime)
    forged_pair = "forged-but-equal-csrf-pair"
    with pytest.raises(AuthenticationError) as rejection:
        await dependencies.require_csrf_protected_request(
            build_request(
                headers=csrf_request_headers(started_session, csrf_value=forged_pair)
            )
        )
    assert rejection.value.error_code is ErrorCode.CSRF_VALIDATION_FAILED


@pytest.mark.asyncio
async def test_csrf_dependency_rejects_a_non_ascii_but_equal_pair_closed() -> None:
    started_session, runtime = await start_offline_session()
    dependencies = create_session_route_dependencies(runtime)
    non_ascii_pair = "f\xf6rged-but-equal-pair"
    with pytest.raises(AuthenticationError) as rejection:
        await dependencies.require_csrf_protected_request(
            build_request(
                headers=csrf_request_headers(started_session, csrf_value=non_ascii_pair)
            )
        )
    assert rejection.value.error_code is ErrorCode.CSRF_VALIDATION_FAILED


# --- state-tolerant challenge resolution (spec 9.2) ------------------------------------


async def start_offline_session_in_state(
    state: WebSessionState,
) -> tuple[StartedWebSession, WebAuthenticationRuntime, OfflineAuthenticationState]:
    """Start one offline session, then restamp its stored row's state.

    The offline composition exposes its in-memory state so tests can produce
    the ``pending_totp``/``recovery_limited``/``revoked`` rows the recovery
    flows of later tasks will create, while every secret stays genuine.
    """
    offline_state = OfflineAuthenticationState(totp_active=False)
    runtime = compose_offline_web_authentication(state=offline_state)
    outcome = await runtime.login_service.login(
        username="admin",
        password="correct-horse-battery-staple",
        source_bucket="192.0.2.10",
        diagnostic_context=create_diagnostic_context().context,
    )
    started = outcome.started_session
    assert started is not None
    stored = offline_state.sessions_by_secret_hash[started.session_secret_hash]
    offline_state.sessions_by_secret_hash[started.session_secret_hash] = replace(
        stored, state=state
    )
    return started, runtime, offline_state


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    [WebSessionState.PENDING_TOTP, WebSessionState.RECOVERY_LIMITED],
)
async def test_challenge_csrf_dependency_accepts_unrevoked_non_active_states(
    state: WebSessionState,
) -> None:
    started_session, runtime, _offline_state = await start_offline_session_in_state(state)
    dependencies = create_session_route_dependencies(runtime)
    request = build_request(
        headers=csrf_request_headers(started_session, csrf_value=started_session.csrf_secret)
    )
    resolved = await dependencies.require_csrf_protected_challenge_request(request)
    assert isinstance(resolved, AuthenticatedWebRequest)
    assert resolved.session.state is state
    assert request.state.authentication is resolved


@pytest.mark.asyncio
async def test_challenge_csrf_dependency_rejects_revoked_and_expired_bindings() -> None:
    started_session, runtime, _offline_state = await start_offline_session_in_state(
        WebSessionState.REVOKED
    )
    dependencies = create_session_route_dependencies(runtime)
    with pytest.raises(AuthenticationError) as revoked_rejection:
        await dependencies.require_csrf_protected_challenge_request(
            build_request(
                headers=csrf_request_headers(
                    started_session, csrf_value=started_session.csrf_secret
                )
            )
        )
    assert revoked_rejection.value.error_code is ErrorCode.AUTHENTICATION_REQUIRED

    pending_session, pending_runtime, pending_state = await start_offline_session_in_state(
        WebSessionState.PENDING_TOTP
    )
    stored = pending_state.sessions_by_secret_hash[pending_session.session_secret_hash]
    # The restamped row keeps its twelve-hour active idle window; moving it
    # past the fixed offline clock makes the binding expired.
    pending_state.sessions_by_secret_hash[stored.session_secret_hash] = replace(
        stored, idle_expires_at=stored.idle_expires_at - timedelta(hours=13)
    )
    with pytest.raises(AuthenticationError) as expired_rejection:
        await create_session_route_dependencies(
            pending_runtime
        ).require_csrf_protected_challenge_request(
            build_request(
                headers=csrf_request_headers(
                    pending_session, csrf_value=pending_session.csrf_secret
                )
            )
        )
    assert expired_rejection.value.error_code is ErrorCode.AUTHENTICATION_REQUIRED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    [WebSessionState.PENDING_TOTP, WebSessionState.RECOVERY_LIMITED],
)
async def test_strict_csrf_dependency_still_rejects_non_active_states(
    state: WebSessionState,
) -> None:
    started_session, runtime, _offline_state = await start_offline_session_in_state(state)
    dependencies = create_session_route_dependencies(runtime)
    with pytest.raises(AuthenticationError) as rejection:
        await dependencies.require_csrf_protected_request(
            build_request(
                headers=csrf_request_headers(
                    started_session, csrf_value=started_session.csrf_secret
                )
            )
        )
    assert rejection.value.error_code is ErrorCode.AUTHENTICATION_REQUIRED


# --- keyring coverage refusal (spec 20.1) ---------------------------------------------


def _keyring_of(key_ids: tuple[str, ...]) -> Any:
    from types import MappingProxyType

    from api_runtime.authentication_crypto import AuthenticationKeyring

    return AuthenticationKeyring(
        current_key_id=key_ids[0],
        keys_by_id=MappingProxyType({key_id: bytes(32) for key_id in key_ids}),
    )


def test_covering_keyring_passes_the_startup_refusal() -> None:
    keyring = _keyring_of(("auth-key-v1", "auth-key-v0"))
    assert_keyring_covers_required_key_ids(frozenset({"auth-key-v1"}), keyring)
    assert_keyring_covers_required_key_ids(frozenset(), keyring)


def test_missing_referenced_key_id_refuses_startup_safely() -> None:
    keyring = _keyring_of(("auth-key-v1",))
    with pytest.raises(ConfigurationError) as rejection:
        assert_keyring_covers_required_key_ids(
            frozenset({"auth-key-v0", "auth-key-v1"}), keyring
        )
    assert rejection.value.error_code is ErrorCode.CONFIGURATION_SECRET_INVALID
    rendered = str(rejection.value)
    assert "auth-key-v0" not in rendered
