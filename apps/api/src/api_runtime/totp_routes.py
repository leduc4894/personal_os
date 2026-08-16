"""TOTP/recovery endpoints: envelopes, one-time payloads and closed errors.

The six endpoints of the spec 16.2 route set are created per composed runtime:
each closure binds the runtime's TOTP service, session dependencies and cookie
contract, so the application factory only registers semantic operation ids
and response models. The challenge-bearing routes reuse the state-tolerant
CSRF dependency of spec 9.2 verbatim — the service layer decides which
session states each action accepts — while the proof-bearing routes keep the
strict variant. Every response carries the canonical envelope and
``Cache-Control: no-store``; provisioning and recovery responses also carry
``Pragma: no-cache`` (spec 16). Service outcomes map onto the closed registry
codes (spec 17) and the rate-limited exits add only their registered safe
``retry_after_seconds`` detail. Secrets render exactly once: the provisioning
offer and the recovery codes never appear in any other surface.
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
    create_session_route_dependencies,
)
from api_runtime.authentication_models import (
    RecoveryCodesData,
    RecoveryLimitedContext,
    SessionData,
    TotpCodeRequest,
    TotpEnrollmentData,
    TotpEnrollmentOfferData,
    TotpEnrollmentRequest,
    TotpProofRequest,
    TotpRecoveryPermittedAction,
    TotpRecoveryRequest,
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
from personal_os.authentication.totp import TotpEnrollmentAction
from personal_os.diagnostics.context import DiagnosticContext, current_diagnostic_context
from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import InternalApplicationError

#: Response headers every authentication response carries (spec 16).
_NO_STORE_HEADERS: Final[dict[str, str]] = {"cache-control": "no-store"}

#: Provisioning and recovery responses add the no-cache pragma (spec 16).
_NO_STORE_NO_CACHE_HEADERS: Final[dict[str, str]] = {
    "cache-control": "no-store",
    "pragma": "no-cache",
}

#: The closed permitted-action set of the recovery-limited context (10.3).
_RECOVERY_PERMITTED_ACTIONS: Final[tuple[TotpRecoveryPermittedAction, ...]] = (
    "totp_replacement",
    "logout",
)


@dataclass(frozen=True, slots=True)
class TotpRouteEndpoints:
    """The six endpoint callables of the closed TOTP/recovery route set."""

    verify_challenge: Callable[..., Awaitable[JSONResponse]]
    submit_enrollment_action: Callable[..., Awaitable[JSONResponse]]
    verify_enrollment: Callable[..., Awaitable[JSONResponse]]
    recover: Callable[..., Awaitable[JSONResponse]]
    regenerate_recovery_codes: Callable[..., Awaitable[JSONResponse]]
    disable: Callable[..., Awaitable[JSONResponse]]


def _bound_diagnostic_context() -> DiagnosticContext:
    """Return the diagnostic context owned by the request correlation middleware."""
    context = current_diagnostic_context()
    if context is None:
        raise RuntimeError("totp routes require a bound request correlation context")
    return context


def create_totp_route_endpoints(
    runtime: WebAuthenticationRuntime,
) -> TotpRouteEndpoints:
    """Build the six TOTP/recovery endpoints over one runtime."""
    dependencies = create_session_route_dependencies(runtime)
    cookie_contract = runtime.cookie_contract

    def _request_id() -> UUID:
        context = current_diagnostic_context()
        if context is None:
            raise RuntimeError("totp routes require a bound request correlation context")
        return context.request_id

    def _success_json(
        data: SessionData
        | TotpEnrollmentData
        | RecoveryCodesData
        | RecoveryLimitedContext,
        *,
        headers: dict[str, str] = _NO_STORE_HEADERS,
    ) -> JSONResponse:
        envelope = success_envelope(request_id=_request_id(), data=data)
        return JSONResponse(content=envelope.model_dump(mode="json"), headers=headers)

    def _error_json(
        error: AuthenticationError, *, headers: dict[str, str] = _NO_STORE_HEADERS
    ) -> JSONResponse:
        envelope = error_envelope(request_id=_request_id(), error=error)
        return JSONResponse(
            content=envelope.model_dump(mode="json"),
            status_code=HTTP_ERROR_STATUSES[error.error_code],
            headers=headers,
        )

    async def _rate_limited_json(
        locked_until: datetime | None, *, headers: dict[str, str] = _NO_STORE_HEADERS
    ) -> JSONResponse:
        """Render the throttled exit with its registered safe retry detail."""
        retry_after_seconds = 1
        if locked_until is not None:
            database_now = await runtime.totp_service.database_now()
            remaining_seconds = (locked_until - database_now).total_seconds()
            retry_after_seconds = max(1, math.ceil(remaining_seconds))
        return _error_json(
            AuthenticationError(
                ErrorCode.AUTHENTICATION_RATE_LIMITED,
                safe_details={"retry_after_seconds": retry_after_seconds},
            ),
            headers=headers,
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

    def _rotated_session_response(
        state: WebSessionState,
        *,
        idle_expires_at: datetime,
        absolute_expires_at: datetime,
        session_secret: str,
        csrf_secret: str,
    ) -> JSONResponse:
        response = _success_json(
            _session_data(
                state, idle_expires_at=idle_expires_at, absolute_expires_at=absolute_expires_at
            )
        )
        apply_session_cookies(
            response,
            cookie_contract,
            session_secret=session_secret,
            csrf_secret=csrf_secret,
        )
        return response

    async def verify_challenge(
        request: Request,
        credentials: TotpCodeRequest,
        authentication: AuthenticatedWebRequest = Depends(  # noqa: B008
            dependencies.require_csrf_protected_challenge_request
        ),
    ) -> JSONResponse:
        """Verify one login TOTP challenge and activate the pending binding."""
        request.scope["route_template"] = ApiRouteTemplate.AUTH_TOTP_VERIFY
        outcome = await runtime.totp_service.verify_session_totp(
            session_secret=authentication.session_secret,
            code=credentials.code,
            diagnostic_context=_bound_diagnostic_context(),
        )
        if outcome.public_error is not None:
            if outcome.public_error is ErrorCode.AUTHENTICATION_RATE_LIMITED:
                return await _rate_limited_json(outcome.locked_until)
            return _error_json(AuthenticationError(outcome.public_error))
        verified = outcome.verified
        if verified is None:
            raise InternalApplicationError(ErrorCode.INTERNAL_ERROR)
        return _rotated_session_response(
            WebSessionState.ACTIVE,
            idle_expires_at=verified.idle_expires_at,
            absolute_expires_at=verified.absolute_expires_at,
            session_secret=verified.rotated_session.session_secret,
            csrf_secret=verified.rotated_session.csrf_secret,
        )

    async def submit_enrollment_action(
        request: Request,
        enrollment_request: TotpEnrollmentRequest,
        authentication: AuthenticatedWebRequest = Depends(  # noqa: B008
            dependencies.require_csrf_protected_challenge_request
        ),
    ) -> JSONResponse:
        """Run one strict enrollment action (spec 10.1)."""
        request.scope["route_template"] = ApiRouteTemplate.AUTH_TOTP_ENROLLMENTS
        outcome = await runtime.totp_service.submit_enrollment_action(
            session_secret=authentication.session_secret,
            action=enrollment_request.action,
            diagnostic_context=_bound_diagnostic_context(),
        )
        if outcome.public_error is not None:
            return _error_json(AuthenticationError(outcome.public_error))
        started = outcome.started
        if started is not None:
            return _success_json(
                TotpEnrollmentData(
                    action=TotpEnrollmentAction.START,
                    enrollment=TotpEnrollmentOfferData(
                        enrollment_id=started.enrollment_id,
                        provisioning_uri=started.provisioning_uri,
                        secret=started.secret_base32,
                        expires_at=started.enrollment_expires_at,
                    ),
                    dismissed_at=None,
                ),
                headers=_NO_STORE_NO_CACHE_HEADERS,
            )
        dismissed_at = outcome.dismissed_at
        if dismissed_at is None:
            raise InternalApplicationError(ErrorCode.INTERNAL_ERROR)
        return _success_json(
            TotpEnrollmentData(
                action=TotpEnrollmentAction.DISMISS_INITIAL_OFFER,
                enrollment=None,
                dismissed_at=dismissed_at,
            )
        )

    async def verify_enrollment(
        request: Request,
        credentials: TotpCodeRequest,
        enrollment_id: UUID,
        authentication: AuthenticatedWebRequest = Depends(  # noqa: B008
            dependencies.require_csrf_protected_challenge_request
        ),
    ) -> JSONResponse:
        """Verify one enrollment code, activate and return the codes once."""
        request.scope["route_template"] = ApiRouteTemplate.AUTH_TOTP_ENROLLMENT_VERIFY
        outcome = await runtime.totp_service.verify_enrollment(
            session_secret=authentication.session_secret,
            enrollment_id=enrollment_id,
            code=credentials.code,
            diagnostic_context=_bound_diagnostic_context(),
        )
        if outcome.public_error is not None:
            if outcome.public_error is ErrorCode.AUTHENTICATION_RATE_LIMITED:
                return await _rate_limited_json(outcome.locked_until)
            return _error_json(AuthenticationError(outcome.public_error))
        verified = outcome.verified
        if verified is None:
            raise InternalApplicationError(ErrorCode.INTERNAL_ERROR)
        response = _success_json(
            RecoveryCodesData(
                codes=verified.issued_codes.codes, revision=verified.issued_codes.revision
            ),
            headers=_NO_STORE_NO_CACHE_HEADERS,
        )
        rotated = verified.rotated_session
        if rotated is not None:
            apply_session_cookies(
                response,
                cookie_contract,
                session_secret=rotated.session_secret,
                csrf_secret=rotated.csrf_secret,
            )
        return response

    async def recover(
        request: Request,
        credentials: TotpRecoveryRequest,
        authentication: AuthenticatedWebRequest = Depends(  # noqa: B008
            dependencies.require_csrf_protected_challenge_request
        ),
    ) -> JSONResponse:
        """Consume one recovery code and enter the recovery-limited state."""
        request.scope["route_template"] = ApiRouteTemplate.AUTH_TOTP_RECOVERY
        outcome = await runtime.totp_service.recover_with_code(
            session_secret=authentication.session_secret,
            password=credentials.password,
            recovery_code=credentials.recovery_code,
            diagnostic_context=_bound_diagnostic_context(),
        )
        if outcome.public_error is not None:
            if outcome.public_error is ErrorCode.AUTHENTICATION_RATE_LIMITED:
                return await _rate_limited_json(outcome.locked_until)
            return _error_json(AuthenticationError(outcome.public_error))
        entered = outcome.entered
        if entered is None:
            raise InternalApplicationError(ErrorCode.INTERNAL_ERROR)
        response = _success_json(
            RecoveryLimitedContext(
                state=WebSessionState.RECOVERY_LIMITED,
                permitted_actions=_RECOVERY_PERMITTED_ACTIONS,
                idle_expires_at=entered.idle_expires_at,
                absolute_expires_at=entered.absolute_expires_at,
            ),
            headers=_NO_STORE_NO_CACHE_HEADERS,
        )
        apply_session_cookies(
            response,
            cookie_contract,
            session_secret=entered.rotated_session.session_secret,
            csrf_secret=entered.rotated_session.csrf_secret,
        )
        return response

    async def regenerate_recovery_codes(
        request: Request,
        credentials: TotpProofRequest,
        authentication: AuthenticatedWebRequest = Depends(  # noqa: B008
            dependencies.require_csrf_protected_request
        ),
    ) -> JSONResponse:
        """Re-verify password plus current TOTP and issue a fresh code set."""
        request.scope["route_template"] = ApiRouteTemplate.AUTH_TOTP_RECOVERY_CODES_REGENERATE
        outcome = await runtime.totp_service.regenerate_recovery_codes(
            session_secret=authentication.session_secret,
            password=credentials.password,
            code=credentials.totp_code,
            diagnostic_context=_bound_diagnostic_context(),
        )
        if outcome.public_error is not None:
            if outcome.public_error is ErrorCode.AUTHENTICATION_RATE_LIMITED:
                return await _rate_limited_json(outcome.locked_until)
            return _error_json(AuthenticationError(outcome.public_error))
        issued = outcome.issued_codes
        if issued is None:
            raise InternalApplicationError(ErrorCode.INTERNAL_ERROR)
        return _success_json(
            RecoveryCodesData(codes=issued.codes, revision=issued.revision),
            headers=_NO_STORE_NO_CACHE_HEADERS,
        )

    async def disable(
        request: Request,
        credentials: TotpProofRequest,
        authentication: AuthenticatedWebRequest = Depends(  # noqa: B008
            dependencies.require_csrf_protected_request
        ),
    ) -> JSONResponse:
        """Close every TOTP surface and rotate to password-only (spec 10.3)."""
        request.scope["route_template"] = ApiRouteTemplate.AUTH_TOTP_DISABLE
        outcome = await runtime.totp_service.disable_totp(
            session_secret=authentication.session_secret,
            password=credentials.password,
            code=credentials.totp_code,
            diagnostic_context=_bound_diagnostic_context(),
        )
        if outcome.public_error is not None:
            if outcome.public_error is ErrorCode.AUTHENTICATION_RATE_LIMITED:
                return await _rate_limited_json(outcome.locked_until)
            return _error_json(AuthenticationError(outcome.public_error))
        disabled = outcome.disabled
        if disabled is None:
            raise InternalApplicationError(ErrorCode.INTERNAL_ERROR)
        return _rotated_session_response(
            WebSessionState.ACTIVE,
            idle_expires_at=disabled.idle_expires_at,
            absolute_expires_at=disabled.absolute_expires_at,
            session_secret=disabled.rotated_session.session_secret,
            csrf_secret=disabled.rotated_session.csrf_secret,
        )

    return TotpRouteEndpoints(
        verify_challenge=verify_challenge,
        submit_enrollment_action=submit_enrollment_action,
        verify_enrollment=verify_enrollment,
        recover=recover,
        regenerate_recovery_codes=regenerate_recovery_codes,
        disable=disable,
    )
