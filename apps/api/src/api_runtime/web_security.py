"""Pure-ASGI web security headers: fresh nonce CSP, referrer policy, nosniff.

The middleware owns the browser-facing security headers of every HTTP
response the API process emits (spec 20.2). Each response receives a fresh
unguessable CSP nonce rendered into the exact directive set the spec lists,
plus ``Referrer-Policy: no-referrer`` and ``X-Content-Type-Options:
nosniff``; any value the wrapped application set for one of the three owned
headers is replaced, never duplicated, so the emitted posture is exactly the
configured one. The API serves JSON only — the CSP still applies because it
protects against content-sniffing and markup-injection reinterpretation of
error bodies. HSTS stays absent on purpose: TLS termination belongs to the
reverse-proxy deployment contract. No ``BaseHTTPMiddleware`` and no framework
imports: the callable takes the raw ``scope``/``receive``/``send`` triple and
amends ``http.response.start`` only; non-HTTP ASGI scopes pass through
untouched.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable, Mapping
from typing import Any, Final

from api_runtime.request_context import ASGIApp, Receive, Scope, Send

#: Entropy of one CSP nonce: 128 bits of URL-safe base64 material. The nonce
#: is public response content, so only unpredictability per response matters.
_NONCE_ENTROPY_BYTES: Final[int] = 16

#: The three response headers this middleware owns exclusively. App-set values
#: for them are replaced so the emitted posture stays exact.
_OWNED_RESPONSE_HEADERS: Final[frozenset[bytes]] = frozenset(
    {
        b"content-security-policy",
        b"referrer-policy",
        b"x-content-type-options",
    }
)

#: Fixed response headers of spec 20.2 that carry no per-response material.
_REFERRER_POLICY_HEADER: Final[tuple[bytes, bytes]] = (b"referrer-policy", b"no-referrer")
_CONTENT_TYPE_OPTIONS_HEADER: Final[tuple[bytes, bytes]] = (
    b"x-content-type-options",
    b"nosniff",
)


def mint_csp_nonce() -> str:
    """Mint one fresh URL-safe CSP nonce (128 bits of entropy)."""
    return secrets.token_urlsafe(_NONCE_ENTROPY_BYTES)


def render_content_security_policy(nonce: str) -> str:
    """Render the exact CSP directive set of spec 20.2 for one nonce."""
    return "; ".join(
        (
            "default-src 'self'",
            f"script-src 'self' 'nonce-{nonce}'",
            "connect-src 'self'",
            "img-src 'self' data:",
            "object-src 'none'",
            "base-uri 'none'",
            "frame-ancestors 'none'",
            "form-action 'self'",
        )
    )


def _amend_response_start(message: Mapping[str, Any], nonce: str) -> dict[str, Any]:
    """Copy a ``http.response.start`` message with the owned headers replaced."""
    headers = [
        (name, value)
        for name, value in message.get("headers") or []
        if name.lower() not in _OWNED_RESPONSE_HEADERS
    ]
    headers.append((b"content-security-policy", render_content_security_policy(nonce).encode()))
    headers.append(_REFERRER_POLICY_HEADER)
    headers.append(_CONTENT_TYPE_OPTIONS_HEADER)
    amended = dict(message)
    amended["headers"] = headers
    return amended


class WebSecurityHeadersMiddleware:
    """Apply the nonce CSP and fixed web security headers to every response."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        nonce_factory: Callable[[], str] = mint_csp_nonce,
    ) -> None:
        self._app = app
        self._nonce_factory = nonce_factory

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        nonce = self._nonce_factory()

        async def send_with_security_headers(message: Mapping[str, Any]) -> None:
            if message["type"] == "http.response.start":
                await send(_amend_response_start(message, nonce))
                return
            await send(message)

        await self._app(scope, receive, send_with_security_headers)
