"""Pure-ASGI request correlation middleware and safe HTTP access observations.

The middleware owns the request identity for every HTTP exchange: it mints a
fresh server request id (UUIDv7), resolves the client correlation headers into
a :class:`DiagnosticContext`, echoes exactly one ``X-Request-ID`` and one
formatted ``traceparent`` response header, and emits one registered access
observation whose fields are closed enum values, the status code and a
non-negative duration. Raw paths, query strings and header values never enter
diagnostics. Non-HTTP ASGI scopes (lifespan, websocket handshake extension
points) pass through untouched without binding a context.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Mapping, MutableMapping
from typing import Any, Final

from personal_os.api_contracts import ApiHttpMethod, ApiRouteTemplate
from personal_os.diagnostics.context import (
    DiagnosticContext,
    DiagnosticContextResolution,
    bind_diagnostic_context,
    create_diagnostic_context,
)
from personal_os.diagnostics.events import DiagnosticEventSink, EventName, SafeToken
from personal_os.diagnostics.trace_context import format_traceparent

type Scope = MutableMapping[str, Any]
type Receive = Callable[[], Awaitable[Mapping[str, Any]]]
type Send = Callable[[Mapping[str, Any]], Awaitable[None]]
type ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

_NANOSECONDS_PER_MILLISECOND: Final = 1_000_000
_CLIENT_REQUEST_ID_HEADER: Final = b"x-client-request-id"
_TRACEPARENT_HEADER: Final = b"traceparent"
_RESPONSE_REQUEST_ID_HEADER: Final = b"x-request-id"
_OWNED_RESPONSE_HEADERS: Final = frozenset({_RESPONSE_REQUEST_ID_HEADER, _TRACEPARENT_HEADER})
_INVALID_FORMAT_REASON: Final = SafeToken.parse("invalid_format")


def _header(scope: Scope, name: bytes) -> str | None:
    """Return the first value of one request header, or ``None`` when absent."""
    for header_name, value in scope.get("headers") or []:
        if header_name.lower() == name:
            decoded: str = value.decode("latin-1")
            return decoded
    return None


def _resolve_http_method(scope: Scope) -> ApiHttpMethod:
    """Bucket the request method into the closed two-value method vocabulary."""
    if scope.get("method") == ApiHttpMethod.GET.value:
        return ApiHttpMethod.GET
    return ApiHttpMethod.OTHER


def _resolve_route_template(scope: Scope) -> ApiRouteTemplate:
    """Map the matched route to a closed template without retaining raw paths.

    An already-assigned ``route_template`` wins. FastAPI's matched route path is
    accepted only when it equals one of the three closed route values; any other
    path, including attacker-controlled raw paths, collapses to ``UNMATCHED``.
    """
    assigned = scope.get("route_template")
    if isinstance(assigned, ApiRouteTemplate):
        return assigned
    matched_path = getattr(scope.get("route"), "path", None)
    if isinstance(matched_path, str):
        try:
            return ApiRouteTemplate(matched_path)
        except ValueError:
            return ApiRouteTemplate.UNMATCHED
    return ApiRouteTemplate.UNMATCHED


def _amend_response_start(message: Mapping[str, Any], context: DiagnosticContext) -> dict[str, Any]:
    """Copy a ``http.response.start`` message carrying exactly one header per key.

    The middleware owns the correlation headers: any app-set ``X-Request-ID`` or
    ``traceparent`` is replaced by the server-owned values, while every other
    header is preserved in order.
    """
    headers = [
        (name, value)
        for name, value in message.get("headers") or []
        if name.lower() not in _OWNED_RESPONSE_HEADERS
    ]
    headers.append((_RESPONSE_REQUEST_ID_HEADER, str(context.request_id).encode()))
    headers.append((_TRACEPARENT_HEADER, format_traceparent(context.trace).encode()))
    amended = dict(message)
    amended["headers"] = headers
    return amended


class RequestContextMiddleware:
    """Own request correlation for one HTTP exchange as a pure ASGI middleware.

    No ``BaseHTTPMiddleware`` and no framework imports: the callable takes the
    raw ``scope``/``receive``/``send`` triple. When the wrapped app raises
    before sending ``http.response.start``, no access observation is emitted;
    the server's error handling owns that response.
    """

    def __init__(
        self,
        app: ASGIApp,
        event_sink: DiagnosticEventSink | None = None,
        monotonic_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        self._app = app
        self._event_sink = event_sink
        self._monotonic_ns = monotonic_ns

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        started_ns = self._monotonic_ns()
        resolution = create_diagnostic_context(
            client_request_id=_header(scope, _CLIENT_REQUEST_ID_HEADER),
            traceparent=_header(scope, _TRACEPARENT_HEADER),
        )
        status_code: int | None = None

        async def send_with_correlation_headers(message: Mapping[str, Any]) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                await send(_amend_response_start(message, resolution.context))
                return
            await send(message)

        with bind_diagnostic_context(resolution.context):
            self._emit_correlation_rejections(resolution)
            try:
                await self._app(scope, receive, send_with_correlation_headers)
            finally:
                if status_code is not None:
                    self._emit_access_observation(scope, status_code, started_ns)

    def _emit_correlation_rejections(self, resolution: DiagnosticContextResolution) -> None:
        """Emit fixed-reason rejection events without the rejected values."""
        if resolution.was_client_request_id_rejected:
            self._emit(EventName.CLIENT_REQUEST_ID_REJECTED)
        if resolution.was_traceparent_replaced:
            self._emit(EventName.TRACE_CONTEXT_REPLACED)

    def _emit_access_observation(self, scope: Scope, status_code: int, started_ns: int) -> None:
        """Emit the one registered access observation for a completed exchange."""
        if self._event_sink is None:
            return
        event_name = (
            EventName.API_REQUEST_COMPLETED
            if status_code < 400
            else EventName.API_REQUEST_REJECTED
            if status_code < 500
            else EventName.API_REQUEST_FAILED
        )
        duration_ms = max(0, (self._monotonic_ns() - started_ns) // _NANOSECONDS_PER_MILLISECOND)
        self._event_sink.emit(
            event_name,
            {
                "http_method": _resolve_http_method(scope),
                "route": _resolve_route_template(scope),
                "status_code": status_code,
                "duration_ms": duration_ms,
            },
        )

    def _emit(self, event_name: EventName) -> None:
        if self._event_sink is not None:
            self._event_sink.emit(event_name, {"reason": _INVALID_FORMAT_REASON})
