"""Pure-ASGI web security headers: fresh nonce CSP, referrer and nosniff.

These tests drive :class:`WebSecurityHeadersMiddleware` through a minimal raw
ASGI invoker (no Starlette test client, no ``BaseHTTPMiddleware``). They pin
the exact CSP directive set of spec 20.2 with a fresh unguessable nonce minted
per response, the ``Referrer-Policy: no-referrer`` and
``X-Content-Type-Options: nosniff`` companions, ownership of the three headers
(any app-set value is replaced, never duplicated), and untouched passthrough
for non-HTTP scopes. HSTS stays absent: TLS termination belongs to the reverse
proxy.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final

import pytest
from api_runtime.request_context import ASGIApp, Receive, Scope, Send
from api_runtime.web_security import WebSecurityHeadersMiddleware

_NONCE_ALPHABET: Final[frozenset[str]] = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)
_CSP_NONCE_DIRECTIVE_PREFIX: Final[str] = "script-src 'self' 'nonce-"


@dataclass(slots=True)
class CapturedResponse:
    """Raw ASGI response captured from ``http.response.start``."""

    status: int
    raw_headers: list[tuple[bytes, bytes]]

    @property
    def headers(self) -> dict[str, str]:
        return {name.decode(): value.decode() for name, value in self.raw_headers}

    def count_header(self, name: str) -> int:
        lowered = name.encode()
        return sum(1 for header_name, _ in self.raw_headers if header_name.lower() == lowered)


def _response_app(
    status: int,
    *,
    headers: list[tuple[bytes, bytes]] | None = None,
) -> ASGIApp:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": status, "headers": headers or []})
        await send({"type": "http.response.body", "body": b"{}"})

    return app


@dataclass(slots=True)
class NonceSequence:
    """Deterministic nonce factory recording every minted value in order."""

    values: list[str] = field(default_factory=list)

    def __call__(self) -> str:
        nonce = f"nonce-{len(self.values)}-abcdefghijklmnopqrstuvwxyz"
        self.values.append(nonce)
        return nonce


async def _invoke_http(app: ASGIApp) -> CapturedResponse:
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/api/health/live",
        "raw_path": b"/api/health/live",
        "query_string": b"",
        "root_path": "",
        "headers": [],
        "client": ("127.0.0.1", 42000),
        "server": ("127.0.0.1", 80),
    }

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    started: Mapping[str, Any] | None = None

    async def send(message: Mapping[str, Any]) -> None:
        nonlocal started
        if message["type"] == "http.response.start":
            assert started is None, "http.response.start sent more than once"
            started = dict(message)

    await app(scope, receive, send)
    assert started is not None
    return CapturedResponse(
        status=started["status"],
        raw_headers=[(name, value) for name, value in started["headers"]],
    )


def _extract_nonce(content_security_policy: str) -> str:
    directives = content_security_policy.split("; ")
    script_directive = next(d for d in directives if d.startswith(_CSP_NONCE_DIRECTIVE_PREFIX))
    nonce = script_directive.removeprefix(_CSP_NONCE_DIRECTIVE_PREFIX).removesuffix("'")
    assert nonce, "script-src directive carries no nonce"
    return nonce


@pytest.mark.asyncio
async def test_every_response_carries_the_exact_csp_directive_set() -> None:
    middleware = WebSecurityHeadersMiddleware(_response_app(200))
    response = await _invoke_http(middleware)
    policy = response.headers["content-security-policy"]
    nonce = _extract_nonce(policy)
    assert policy == (
        "default-src 'self'; "
        f"{_CSP_NONCE_DIRECTIVE_PREFIX}{nonce}'; "
        "connect-src 'self'; "
        "img-src 'self' data:; "
        "object-src 'none'; "
        "base-uri 'none'; "
        "frame-ancestors 'none'; "
        "form-action 'self'"
    )


@pytest.mark.asyncio
async def test_every_response_carries_referrer_policy_and_nosniff() -> None:
    middleware = WebSecurityHeadersMiddleware(_response_app(404))
    response = await _invoke_http(middleware)
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-content-type-options"] == "nosniff"


@pytest.mark.asyncio
async def test_each_response_receives_a_fresh_nonce() -> None:
    nonces = NonceSequence()
    middleware = WebSecurityHeadersMiddleware(_response_app(200), nonce_factory=nonces)
    first = await _invoke_http(middleware)
    second = await _invoke_http(middleware)
    first_nonce = _extract_nonce(first.headers["content-security-policy"])
    second_nonce = _extract_nonce(second.headers["content-security-policy"])
    assert first_nonce != second_nonce
    assert nonces.values == [first_nonce, second_nonce]


@pytest.mark.asyncio
async def test_default_nonce_factory_mints_unguessable_values() -> None:
    middleware = WebSecurityHeadersMiddleware(_response_app(200))
    first = await _invoke_http(middleware)
    second = await _invoke_http(middleware)
    for response in (first, second):
        nonce = _extract_nonce(response.headers["content-security-policy"])
        assert len(nonce) >= 22
        assert set(nonce) <= _NONCE_ALPHABET
    assert _extract_nonce(first.headers["content-security-policy"]) != _extract_nonce(
        second.headers["content-security-policy"]
    )


@pytest.mark.asyncio
async def test_owned_headers_are_replaced_never_duplicated() -> None:
    spoofed: list[tuple[bytes, bytes]] = [
        (b"content-security-policy", b"default-src *"),
        (b"Referrer-Policy", b"unsafe-url"),
        (b"x-content-type-options", b"nosniff"),
    ]
    middleware = WebSecurityHeadersMiddleware(_response_app(200, headers=spoofed))
    response = await _invoke_http(middleware)
    for header_name in ("content-security-policy", "referrer-policy", "x-content-type-options"):
        assert response.count_header(header_name) == 1
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "default-src *" not in response.headers["content-security-policy"]


@pytest.mark.asyncio
async def test_non_http_scopes_pass_through_untouched() -> None:
    observed: list[bool] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        observed.append(True)
        await send({"type": "lifespan.startup.complete"})

    async def receive() -> dict[str, Any]:
        return {"type": "lifespan.startup"}

    sent: list[dict[str, Any]] = []

    async def send(message: Mapping[str, Any]) -> None:
        sent.append(dict(message))

    middleware = WebSecurityHeadersMiddleware(app)
    await middleware({"type": "lifespan"}, receive, send)
    assert observed == [True]
    assert sent == [{"type": "lifespan.startup.complete"}]


@pytest.mark.asyncio
async def test_hsts_stays_with_the_reverse_proxy() -> None:
    middleware = WebSecurityHeadersMiddleware(_response_app(200))
    response = await _invoke_http(middleware)
    assert "strict-transport-security" not in response.headers
