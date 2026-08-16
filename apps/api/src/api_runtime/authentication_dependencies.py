"""FastAPI session dependencies, cookie contract and CSRF triple check.

This module is the only place the session and CSRF cookies are read or
written. The cookie contract picks the production ``__Host-`` Secure names
against a real allowed origin and the explicit loopback local-development
names without ``Secure`` only when the runtime environment is local or test
AND the allowed origin is a plain-HTTP loopback origin (spec 9.1). The origin
guard enforces exact string equality against the configured allowed origin on
every session/password request (spec 9.3); a missing or merely similar origin
closes the request with the registered CSRF failure code. The session
dependency resolves the opaque cookie secret through the session service and
attaches the typed authenticated request context to ``request.state``; the
CSRF dependency adds the three further checks spec 9.3 names: the CSRF cookie,
an exactly equal ``X-CSRF-Token`` header, and a hash match against the stored
session binding. A state-tolerant challenge variant of that CSRF dependency
resolves every unrevoked, unexpired binding, because spec 9.2 lets
``pending_totp`` and ``recovery_limited`` sessions call logout — and the TOTP
challenge verification of the next slice resolves the same states. Response
helpers render the approved cookie attributes for
issue, rotation and logout clearing — routes never build cookie headers.

The runtime is consumed structurally through :class:`WebAuthenticationRuntimePort`
so this adapter layer stays independent of the composition module that builds
real and offline runtimes.
"""

from __future__ import annotations

import hmac
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Final, Literal, Protocol
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import Response
from fastapi.security import HTTPBearer

from personal_os.authentication.contracts import AuthenticatedWebContext
from personal_os.authentication.errors import AuthenticationError
from personal_os.authentication.sessions import (
    AuthenticatedSession,
    SessionService,
    StoredWebSession,
)
from personal_os.error_contracts.codes import ErrorCode
from personal_os.runtime_configuration.models import RuntimeEnvironment

#: Production cookie names (spec 9.1): browser-session cookies, no Domain.
SESSION_COOKIE_NAME: Final[str] = "__Host-admin_session"
CSRF_COOKIE_NAME: Final[str] = "__Host-admin_csrf"

#: Explicit loopback local-development names without the Secure attribute.
LOCAL_SESSION_COOKIE_NAME: Final[str] = "admin_session_local"
LOCAL_CSRF_COOKIE_NAME: Final[str] = "admin_csrf_local"

#: The single header the CSRF double-submit check compares (spec 9.3).
CSRF_HEADER_NAME: Final[str] = "x-csrf-token"

#: The dedicated Bearer authentication scheme header (spec 16): the opaque
#: device-credential routes accept exactly one credential in the standard
#: Authorization header and nothing else — no cookie, no query, no body.
_AUTHORIZATION_HEADER_NAME: Final[str] = "authorization"
_BEARER_AUTHENTICATION_SCHEME: Final[str] = "bearer"

#: Cookie attributes shared by both bindings (spec 9.1, 9.3).
_COOKIE_PATH: Final[str] = "/"
_COOKIE_SAMESITE: Final[Literal["lax"]] = "lax"

#: The environments allowed to activate the loopback local cookie mode.
_LOCAL_COOKIE_ENVIRONMENTS: Final[frozenset[RuntimeEnvironment]] = frozenset(
    {RuntimeEnvironment.LOCAL, RuntimeEnvironment.TEST}
)

#: Loopback host spellings of the explicit local-development origin.
_LOOPBACK_HOSTS: Final[frozenset[str]] = frozenset({"localhost", "127.0.0.1", "::1"})

#: Closed fallback when the ASGI server reports no socket peer: it is only
#: HMACed throttle material and never rendered or logged.
_UNKNOWN_SOURCE_ADDRESS: Final[str] = "unknown-source"


class WebAuthenticationRuntimePort(Protocol):
    """The structural slice of one composed authentication runtime.

    The composition module owns the concrete runtime dataclass; the
    dependencies here need exactly the allowed origin, the cookie contract,
    the session service and the stored-hash CSRF verifier.
    """

    @property
    def allowed_origin(self) -> str: ...

    @property
    def cookie_contract(self) -> SessionCookieContract: ...

    @property
    def session_service(self) -> SessionService: ...

    @property
    def verify_csrf_token(self) -> Callable[[str, str], bool]: ...


@dataclass(frozen=True, slots=True)
class SessionCookieContract:
    """The frozen cookie names and flags of one composed runtime."""

    session_cookie_name: str
    csrf_cookie_name: str
    is_secure: bool
    is_local_loopback: bool


def build_session_cookie_contract(
    allowed_origin: str, environment: RuntimeEnvironment
) -> SessionCookieContract:
    """Select the cookie contract for one allowed origin and environment.

    The loopback local-development mode requires both a plain-HTTP loopback
    origin and a local/test runtime environment; every other combination —
    including loopback origins in staging and production — keeps the
    production ``__Host-`` names with the Secure attribute.
    """
    parsed = urlsplit(allowed_origin)
    host = parsed.hostname or ""
    is_local_loopback = (
        parsed.scheme == "http"
        and host in _LOOPBACK_HOSTS
        and environment in _LOCAL_COOKIE_ENVIRONMENTS
    )
    if is_local_loopback:
        return SessionCookieContract(
            session_cookie_name=LOCAL_SESSION_COOKIE_NAME,
            csrf_cookie_name=LOCAL_CSRF_COOKIE_NAME,
            is_secure=False,
            is_local_loopback=True,
        )
    return SessionCookieContract(
        session_cookie_name=SESSION_COOKIE_NAME,
        csrf_cookie_name=CSRF_COOKIE_NAME,
        is_secure=True,
        is_local_loopback=False,
    )


@dataclass(frozen=True, slots=True)
class AuthenticatedWebRequest:
    """The typed authenticated context one route handler consumes.

    ``session_secret`` is the presented cookie value a handler needs to drive
    service calls (rotation, revocation, password change); its ``repr`` is
    suppressed so no diagnostic sink or error rendering can echo it.
    """

    context: AuthenticatedWebContext
    session: StoredWebSession
    session_secret: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class SessionRouteDependencies:
    """The four FastAPI dependency callables of the session/password routes."""

    require_allowed_origin: Callable[[Request], Awaitable[None]]
    require_session_request: Callable[[Request], Awaitable[AuthenticatedWebRequest]]
    require_csrf_protected_request: Callable[[Request], Awaitable[AuthenticatedWebRequest]]
    require_csrf_protected_challenge_request: Callable[
        [Request], Awaitable[AuthenticatedWebRequest]
    ]


def client_source_address(request: Request) -> str:
    """Return the immediate socket peer address of one request.

    Spec 20.3 makes the socket peer the default resolver output before any
    trusted-proxy handling; the value only ever feeds the HMACed throttle
    material and is never logged.
    """
    if request.client is None:
        return _UNKNOWN_SOURCE_ADDRESS
    return request.client.host


def extract_bearer_credential(request: Request) -> str:
    """Return the one Bearer credential of a device-credential request.

    The dedicated Bearer scheme of spec 16 is the only authority these
    routes accept: a missing header, a non-Bearer scheme or an empty
    credential closes with the registered invalid-credential code. The
    value never renders in logs or diagnostics; the presented credential
    string is only ever hashed by the service that verifies it.
    """
    authorization = request.headers.get(_AUTHORIZATION_HEADER_NAME)
    if authorization is None:
        raise AuthenticationError(ErrorCode.DEVICE_CREDENTIAL_INVALID)
    scheme, _, credential = authorization.partition(" ")
    if scheme.lower() != _BEARER_AUTHENTICATION_SCHEME or not credential.strip():
        raise AuthenticationError(ErrorCode.DEVICE_CREDENTIAL_INVALID)
    return credential.strip()


async def require_polling_credential(request: Request) -> str:
    """Resolve the polling Bearer credential of one grant poll (spec 11.4).

    The polling credential is the only authority this route accepts: Web
    session cookies, CSRF material and every other credential are simply
    never read here, so presenting them changes nothing.
    """
    return extract_bearer_credential(request)


async def require_refresh_credential(request: Request) -> str:
    """Resolve the refresh Bearer credential of one rotation or self-revoke.

    The dedicated refresh scheme of spec 16 is the only authority these
    routes accept: a missing header, a non-Bearer scheme or an empty
    credential closes with the registered invalid-credential code, and the
    credential kind itself is verified where the secret is verified — the
    wrong-kind presentation answers the same closed code.
    """
    return extract_bearer_credential(request)


async def require_access_credential(request: Request) -> str:
    """Resolve the access Bearer credential of one device-scoped request.

    The dedicated access scheme of spec 16 is the authority of the
    access-authenticated device surface; the sync routes of the later
    children bind it, and the wrong-kind presentation answers the same
    closed invalid-credential code as a missing one.
    """
    return extract_bearer_credential(request)


#: The dedicated OpenAPI security scheme of the access Bearer credential
#: (spec 16). It never auto-rejects so the closed registry code answers every
#: bad presentation; the Task 11 sync routes bind it.
ACCESS_BEARER_SCHEME = HTTPBearer(
    scheme_name="AccessCredential",
    description="The at1 access credential of one approved device",
    auto_error=False,
)

#: The dedicated OpenAPI security scheme of the refresh Bearer credential
#: (spec 16): the only authority the refresh and self-revoke routes accept.
REFRESH_BEARER_SCHEME = HTTPBearer(
    scheme_name="RefreshCredential",
    description="The rt1 refresh credential of one device token family",
    auto_error=False,
)


def create_session_route_dependencies(
    runtime: WebAuthenticationRuntimePort,
) -> SessionRouteDependencies:
    """Build the origin, session and CSRF dependencies over one runtime."""

    async def require_allowed_origin(request: Request) -> None:
        """Reject any request whose origin is not exactly the allowed one.

        The comparison is plain string equality: the allowed origin is public
        routing material rather than a secret, so constant time buys nothing —
        and header values are latin-1-decoded strings that may carry
        non-ASCII bytes, which a digest comparison would reject with a
        ``TypeError`` instead of the closed CSRF failure.
        """
        origin = request.headers.get("origin")
        if origin is None or origin != runtime.allowed_origin:
            raise AuthenticationError(ErrorCode.CSRF_VALIDATION_FAILED)

    async def _resolve_session(
        request: Request,
        *,
        resolve: Callable[..., Awaitable[AuthenticatedSession]],
    ) -> AuthenticatedWebRequest:
        """Resolve the session cookie through the given service resolution."""
        session_secret = request.cookies.get(runtime.cookie_contract.session_cookie_name)
        if session_secret is None:
            raise AuthenticationError(ErrorCode.AUTHENTICATION_REQUIRED)
        resolved = await resolve(session_secret=session_secret)
        authenticated_request = AuthenticatedWebRequest(
            context=resolved.context,
            session=resolved.session,
            session_secret=session_secret,
        )
        request.state.authentication = authenticated_request
        return authenticated_request

    async def _resolve_authenticated_session(request: Request) -> AuthenticatedWebRequest:
        """Resolve the session cookie to the typed authenticated request."""
        return await _resolve_session(request, resolve=runtime.session_service.authenticate)

    async def _resolve_challenge_session(request: Request) -> AuthenticatedWebRequest:
        """Resolve the session cookie tolerating the pending/recovery states."""
        return await _resolve_session(
            request, resolve=runtime.session_service.resolve_challenge_eligible
        )

    def _require_csrf_pair(
        request: Request, authenticated_request: AuthenticatedWebRequest
    ) -> AuthenticatedWebRequest:
        """Apply the CSRF cookie/header pair and stored-hash match (spec 9.3)."""
        csrf_cookie = request.cookies.get(runtime.cookie_contract.csrf_cookie_name)
        csrf_header = request.headers.get(CSRF_HEADER_NAME)
        if csrf_cookie is None or csrf_header is None:
            raise AuthenticationError(ErrorCode.CSRF_VALIDATION_FAILED)
        # Cookie and header values are latin-1-decoded strings that may carry
        # non-ASCII bytes, so the pair compares as encoded bytes: a digest
        # comparison of the raw strings would raise on non-ASCII input instead
        # of closing with the CSRF failure, and equal genuine tokens encode to
        # equal bytes under one consistent encoding.
        if not hmac.compare_digest(csrf_cookie.encode("utf-8"), csrf_header.encode("utf-8")):
            raise AuthenticationError(ErrorCode.CSRF_VALIDATION_FAILED)
        stored_hash = authenticated_request.session.csrf_secret_hash
        if not runtime.verify_csrf_token(csrf_cookie, stored_hash):
            raise AuthenticationError(ErrorCode.CSRF_VALIDATION_FAILED)
        return authenticated_request

    async def require_session_request(request: Request) -> AuthenticatedWebRequest:
        """Exact-origin gate plus the authenticated session resolution."""
        await require_allowed_origin(request)
        return await _resolve_authenticated_session(request)

    async def require_csrf_protected_request(
        request: Request,
    ) -> AuthenticatedWebRequest:
        """Origin, session cookie, CSRF pair and stored-hash match (spec 9.3)."""
        await require_allowed_origin(request)
        authenticated_request = await _resolve_authenticated_session(request)
        return _require_csrf_pair(request, authenticated_request)

    async def require_csrf_protected_challenge_request(
        request: Request,
    ) -> AuthenticatedWebRequest:
        """Origin, tolerant session resolution and the same CSRF triple check.

        Spec 9.2 lets ``pending_totp`` and ``recovery_limited`` call logout —
        and, for the challenge-verification routes of the TOTP slice, their
        own challenge — so this resolves every unrevoked, unexpired binding
        while revoked, expired and unknown secrets still fail closed. The CSRF
        checks are identical to the strict variant; only the accepted session
        states widen, which is why the strict dependency keeps guarding
        re-authentication and password change.
        """
        await require_allowed_origin(request)
        authenticated_request = await _resolve_challenge_session(request)
        return _require_csrf_pair(request, authenticated_request)

    return SessionRouteDependencies(
        require_allowed_origin=require_allowed_origin,
        require_session_request=require_session_request,
        require_csrf_protected_request=require_csrf_protected_request,
        require_csrf_protected_challenge_request=require_csrf_protected_challenge_request,
    )


def apply_session_cookies(
    response: Response,
    contract: SessionCookieContract,
    *,
    session_secret: str,
    csrf_secret: str,
) -> None:
    """Issue one session binding: HttpOnly session cookie plus readable CSRF."""
    response.set_cookie(
        contract.session_cookie_name,
        session_secret,
        path=_COOKIE_PATH,
        secure=contract.is_secure,
        httponly=True,
        samesite=_COOKIE_SAMESITE,
    )
    response.set_cookie(
        contract.csrf_cookie_name,
        csrf_secret,
        path=_COOKIE_PATH,
        secure=contract.is_secure,
        httponly=False,
        samesite=_COOKIE_SAMESITE,
    )


def clear_session_cookies(response: Response, contract: SessionCookieContract) -> None:
    """Clear both bindings of one logout with expiring empty cookies."""
    response.delete_cookie(
        contract.session_cookie_name,
        path=_COOKIE_PATH,
        secure=contract.is_secure,
        httponly=True,
        samesite=_COOKIE_SAMESITE,
    )
    response.delete_cookie(
        contract.csrf_cookie_name,
        path=_COOKIE_PATH,
        secure=contract.is_secure,
        httponly=False,
        samesite=_COOKIE_SAMESITE,
    )
