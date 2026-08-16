"""Session/password endpoints: envelopes, cookies and closed error mapping.

The five endpoints of the spec 16.1 session/password route set are created per
composed runtime: each closure binds the runtime's services, dependencies and
cookie contract, so the application factory only registers semantic
operation ids and response models. Cookie handling never appears inline —
issue, rotation and clearing go through the dedicated response helpers of
:mod:`api_runtime.authentication_dependencies`. Every response — success or
service rejection — carries the canonical envelope and
``Cache-Control: no-store``; service outcomes map onto the closed registry
codes (spec 17) through the shared status table, and the rate-limited login
adds only its registered safe ``retry_after_seconds`` detail.
"""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Final
from uuid import UUID

from fastapi import Depends, Request
from fastapi.responses import JSONResponse

from api_runtime.authentication_composition import WebAuthenticationRuntime
from api_runtime.authentication_dependencies import (
    AuthenticatedWebRequest,
    apply_session_cookies,
    clear_session_cookies,
    client_source_address,
    create_session_route_dependencies,
)
from api_runtime.authentication_models import (
    LoginRequest,
    PasswordChangeRequest,
    ReauthenticateRequest,
    SessionData,
)
from personal_os.api_contracts import (
    HTTP_ERROR_STATUSES,
    ApiRouteTemplate,
    error_envelope,
    success_envelope,
)
from personal_os.authentication.contracts import (
    AUTHENTICATED_WEB_SCOPES,
    WebSessionState,
)
from personal_os.authentication.errors import AuthenticationError
from personal_os.authentication.sessions import (
    LoginOutcome,
    RotatedCurrentSession,
    StartedWebSession,
    StoredWebSession,
)
from personal_os.diagnostics.context import DiagnosticContext, current_diagnostic_context
from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import InternalApplicationError

#: Response headers every authentication response carries (spec 16.1).
_NO_STORE_HEADERS: Final[dict[str, str]] = {"cache-control": "no-store"}


@dataclass(frozen=True, slots=True)
class SessionRouteEndpoints:
    """The five endpoint callables of the closed session/password route set."""

    login: Callable[..., Awaitable[JSONResponse]]
    get_session: Callable[..., Awaitable[JSONResponse]]
    logout: Callable[..., Awaitable[JSONResponse]]
    reauthenticate: Callable[..., Awaitable[JSONResponse]]
    change_password: Callable[..., Awaitable[JSONResponse]]


def _bound_diagnostic_context() -> DiagnosticContext:
    """Return the diagnostic context owned by the request correlation middleware."""
    context = current_diagnostic_context()
    if context is None:
        raise RuntimeError("session routes require a bound request correlation context")
    return context


def create_session_route_endpoints(
    runtime: WebAuthenticationRuntime,
) -> SessionRouteEndpoints:
    """Build the five session/password endpoints over one runtime."""
    dependencies = create_session_route_dependencies(runtime)
    cookie_contract = runtime.cookie_contract

    def _request_id() -> UUID:
        context = current_diagnostic_context()
        if context is None:
            raise RuntimeError("session routes require a bound request correlation context")
        return context.request_id

    def _success_json(data: SessionData) -> JSONResponse:
        envelope = success_envelope(request_id=_request_id(), data=data)
        return JSONResponse(
            content=envelope.model_dump(mode="json"), headers=_NO_STORE_HEADERS
        )

    def _error_json(error: AuthenticationError) -> JSONResponse:
        envelope = error_envelope(request_id=_request_id(), error=error)
        return JSONResponse(
            content=envelope.model_dump(mode="json"),
            status_code=HTTP_ERROR_STATUSES[error.error_code],
            headers=_NO_STORE_HEADERS,
        )

    def _session_data(
        state: WebSessionState, *, idle_expires_at: datetime, absolute_expires_at: datetime
    ) -> SessionData:
        is_active = state is WebSessionState.ACTIVE
        return SessionData(
            state=state,
            authenticated=is_active,
            scopes=tuple(AUTHENTICATED_WEB_SCOPES) if is_active else (),
            idle_expires_at=idle_expires_at,
            absolute_expires_at=absolute_expires_at,
        )

    def _started_session_data(started: StartedWebSession) -> SessionData:
        return _session_data(
            started.state,
            idle_expires_at=started.idle_expires_at,
            absolute_expires_at=started.absolute_expires_at,
        )

    def _stored_session_data(session: StoredWebSession) -> SessionData:
        return _session_data(
            session.state,
            idle_expires_at=session.idle_expires_at,
            absolute_expires_at=session.absolute_expires_at,
        )

    def _rotated_response(
        session: StoredWebSession, rotated: RotatedCurrentSession
    ) -> JSONResponse:
        response = _success_json(
            _session_data(
                WebSessionState.ACTIVE,
                idle_expires_at=session.idle_expires_at,
                absolute_expires_at=session.absolute_expires_at,
            )
        )
        apply_session_cookies(
            response,
            cookie_contract,
            session_secret=rotated.session_secret,
            csrf_secret=rotated.csrf_secret,
        )
        return response

    async def _rate_limited_login(outcome: LoginOutcome) -> JSONResponse:
        """Render the throttled login with its registered safe retry detail."""
        locked_until = outcome.locked_until
        retry_after_seconds = 1
        if locked_until is not None:
            database_now = await runtime.session_service.database_now()
            remaining = (locked_until - database_now).total_seconds()
            retry_after_seconds = max(1, math.ceil(remaining))
        return _error_json(
            AuthenticationError(
                ErrorCode.AUTHENTICATION_RATE_LIMITED,
                safe_details={"retry_after_seconds": retry_after_seconds},
            )
        )

    async def login(
        request: Request,
        credentials: LoginRequest,
        origin_guard: None = Depends(dependencies.require_allowed_origin),
    ) -> JSONResponse:
        """Run one password login and issue the session and CSRF bindings."""
        del origin_guard
        request.scope["route_template"] = ApiRouteTemplate.AUTH_LOGIN
        outcome = await runtime.login_service.login(
            username=credentials.username,
            password=credentials.password,
            source_bucket=client_source_address(request),
            diagnostic_context=_bound_diagnostic_context(),
        )
        if outcome.public_error is not None:
            if outcome.public_error is ErrorCode.AUTHENTICATION_RATE_LIMITED:
                return await _rate_limited_login(outcome)
            return _error_json(AuthenticationError(outcome.public_error))
        started = outcome.started_session
        if started is None:
            raise InternalApplicationError(ErrorCode.INTERNAL_ERROR)
        response = _success_json(_started_session_data(started))
        apply_session_cookies(
            response,
            cookie_contract,
            session_secret=started.session_secret,
            csrf_secret=started.csrf_secret,
        )
        return response

    async def get_session(
        request: Request,
        authentication: AuthenticatedWebRequest = Depends(  # noqa: B008
            dependencies.require_session_request
        ),
    ) -> JSONResponse:
        """Return the authenticated session view of the presented binding."""
        request.scope["route_template"] = ApiRouteTemplate.AUTH_SESSION
        return _success_json(_stored_session_data(authentication.session))

    async def logout(
        request: Request,
        authentication: AuthenticatedWebRequest = Depends(  # noqa: B008
            dependencies.require_csrf_protected_request
        ),
    ) -> JSONResponse:
        """Revoke the session row, then clear both browser cookies."""
        request.scope["route_template"] = ApiRouteTemplate.AUTH_LOGOUT
        revoked_session = authentication.session
        await runtime.session_service.revoke(
            session_secret=authentication.session_secret
        )
        response = _success_json(
            _session_data(
                WebSessionState.REVOKED,
                idle_expires_at=revoked_session.idle_expires_at,
                absolute_expires_at=revoked_session.absolute_expires_at,
            )
        )
        clear_session_cookies(response, cookie_contract)
        return response

    async def reauthenticate(
        request: Request,
        credentials: ReauthenticateRequest,
        authentication: AuthenticatedWebRequest = Depends(  # noqa: B008
            dependencies.require_csrf_protected_request
        ),
    ) -> JSONResponse:
        """Verify the password again and rotate the session binding (spec 9.4)."""
        request.scope["route_template"] = ApiRouteTemplate.AUTH_REAUTHENTICATE
        outcome = await runtime.session_service.reauthenticate(
            session_secret=authentication.session_secret,
            password=credentials.password,
        )
        if outcome.public_error is not None:
            return _error_json(AuthenticationError(outcome.public_error))
        rotated = outcome.rotated_session
        if rotated is None:
            raise InternalApplicationError(ErrorCode.INTERNAL_ERROR)
        return _rotated_response(authentication.session, rotated)

    async def change_password(
        request: Request,
        credentials: PasswordChangeRequest,
        authentication: AuthenticatedWebRequest = Depends(  # noqa: B008
            dependencies.require_csrf_protected_request
        ),
    ) -> JSONResponse:
        """Change the password, revoke other sessions and rotate this one."""
        request.scope["route_template"] = ApiRouteTemplate.AUTH_PASSWORD
        outcome = await runtime.password_change_service.change_password(
            session_secret=authentication.session_secret,
            new_password=credentials.new_password,
            diagnostic_context=_bound_diagnostic_context(),
        )
        if outcome.public_error is not None:
            return _error_json(AuthenticationError(outcome.public_error))
        rotated = outcome.rotated_session
        if rotated is None:
            raise InternalApplicationError(ErrorCode.INTERNAL_ERROR)
        return _rotated_response(authentication.session, rotated)

    return SessionRouteEndpoints(
        login=login,
        get_session=get_session,
        logout=logout,
        reauthenticate=reauthenticate,
        change_password=change_password,
    )
