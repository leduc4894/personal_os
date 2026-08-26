"""Pure-ASGI request correlation middleware: ownership, headers and safe observations.

These tests drive :class:`RequestContextMiddleware` through a minimal raw ASGI
invoker (no Starlette test client, no ``BaseHTTPMiddleware``). They pin the
server-owned UUIDv7 request id, the single correlation header pair on the
response, the status-to-event selection with closed enum fields, the fixed
``invalid_format`` rejection reasons that never retain the rejected value, and
non-HTTP scope passthrough.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from api_runtime.request_context import (
    ASGIApp,
    Receive,
    RequestContextMiddleware,
    Scope,
    Send,
)

from personal_os.api_contracts import ApiHttpMethod, ApiRouteTemplate
from personal_os.diagnostics.context import current_diagnostic_context
from personal_os.diagnostics.events import (
    EVENT_DEFINITIONS,
    EventName,
    ResultCode,
    SafeToken,
)

_SENTINEL_CLIENT_REQUEST_ID = "do-not-emit-client-request-id"
_SENTINEL_TRACEPARENT = "do-not-emit-traceparent"
_SENTINEL_RAW_PATH = "/do-not-emit-raw-path"
_SENTINEL_QUERY = b"q=do-not-emit-query-value"
_SENTINEL_HEADER_VALUE = "do-not-emit-request-header"
_SENTINEL_EXCEPTION_TEXT = "do-not-emit-exception-text"


@dataclass(slots=True)
class CapturedResponse:
    """Raw ASGI response captured from ``http.response.start`` plus body chunks."""

    status: int
    raw_headers: list[tuple[bytes, bytes]]

    @property
    def headers(self) -> dict[str, str]:
        return {name.decode(): value.decode() for name, value in self.raw_headers}

    def count_header(self, name: str) -> int:
        lowered = name.encode()
        return sum(1 for header_name, _ in self.raw_headers if header_name.lower() == lowered)


@dataclass(slots=True)
class RecordingSink:
    """Structural ``DiagnosticEventSink`` capturing delivered events verbatim."""

    events: list[tuple[EventName, dict[str, Any]]] = field(default_factory=list)

    def emit(self, event_name: EventName, fields: Mapping[str, object] | None = None) -> None:
        self.events.append((event_name, dict(fields or {})))


@dataclass(slots=True)
class ObservedAccessEvent:
    """One delivered access observation with the context bound at emit time."""

    name: EventName
    fields: dict[str, Any]
    context: Any
    result_code: ResultCode


@dataclass(slots=True)
class ObservingSink:
    """Structural sink capturing each event with its bound diagnostic context."""

    events: list[ObservedAccessEvent] = field(default_factory=list)

    def emit(self, event_name: EventName, fields: Mapping[str, object] | None = None) -> None:
        self.events.append(
            ObservedAccessEvent(
                name=event_name,
                fields=dict(fields or {}),
                context=current_diagnostic_context(),
                result_code=EVENT_DEFINITIONS[event_name].result_code,
            )
        )

    def only(self, event_name: EventName) -> ObservedAccessEvent:
        matches = [event for event in self.events if event.name is event_name]
        assert len(matches) == 1, event_name
        return matches[0]


def _response_app(
    status: int,
    *,
    headers: list[tuple[bytes, bytes]] | None = None,
    observe: list[object] | None = None,
) -> ASGIApp:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        if observe is not None:
            observe.append(current_diagnostic_context())
        await send({"type": "http.response.start", "status": status, "headers": headers or []})
        await send({"type": "http.response.body", "body": b"{}"})

    return app


async def _invoke_asgi(
    app: ASGIApp,
    *,
    method: str = "GET",
    path: str = "/api/health/live",
    query_string: bytes = b"",
    headers: Mapping[str, str] | None = None,
) -> CapturedResponse:
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query_string,
        "root_path": "",
        "headers": [
            (name.lower().encode(), value.encode()) for name, value in (headers or {}).items()
        ],
        "client": ("127.0.0.1", 42000),
        "server": ("127.0.0.1", 80),
    }

    async def receive() -> Mapping[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    started: Mapping[str, Any] | None = None
    body_chunks: list[bytes] = []

    async def send(message: Mapping[str, Any]) -> None:
        nonlocal started
        if message["type"] == "http.response.start":
            assert started is None, "http.response.start sent more than once"
            started = message
        elif message["type"] == "http.response.body":
            body_chunks.append(message["body"])

    await app(scope, receive, send)
    assert started is not None
    return CapturedResponse(
        status=started["status"],
        raw_headers=[(name, value) for name, value in started["headers"]],
    )


async def _invoke_asgi_observing_failure(
    app: ASGIApp,
    **invocation: Any,
) -> tuple[CapturedResponse, list[bytes], BaseException]:
    """Invoke one app that raises, capturing its exchange and the failure.

    Mirrors a real server: the response start that already crossed the wire
    is returned alongside the exception the server itself still observes.
    """

    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": invocation.get("method", "GET"),
        "scheme": "http",
        "path": invocation.get("path", "/api/health/live"),
        "query_string": invocation.get("query_string", b""),
        "root_path": "",
        "headers": [
            (name.lower().encode(), value.encode())
            for name, value in (invocation.get("headers") or {}).items()
        ],
        "client": ("127.0.0.1", 42000),
        "server": ("127.0.0.1", 80),
    }
    if "route_template" in invocation:
        scope["route_template"] = invocation["route_template"]

    async def receive() -> Mapping[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    started: Mapping[str, Any] | None = None
    body_chunks: list[bytes] = []

    async def send(message: Mapping[str, Any]) -> None:
        nonlocal started
        if message["type"] == "http.response.start":
            assert started is None, "http.response.start sent more than once"
            started = message
        elif message["type"] == "http.response.body":
            body_chunks.append(message["body"])

    caught: BaseException | None = None
    try:
        await app(scope, receive, send)
    except BaseException as error:
        caught = error
    assert caught is not None, "the crashing app must still raise to the server"
    assert started is not None, "the exchange must own its exception-generated response start"
    return (
        CapturedResponse(
            status=started["status"],
            raw_headers=[(name, value) for name, value in started["headers"]],
        ),
        body_chunks,
        caught,
    )


def _access_fields(sink: RecordingSink, event_name: EventName) -> dict[str, Any] | None:
    for captured_name, fields in sink.events:
        if captured_name is event_name:
            return fields
    return None


@pytest.mark.asyncio
async def test_middleware_owns_request_id_and_returns_trace_headers() -> None:
    observed_contexts: list[Any] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        observed_contexts.append(current_diagnostic_context())
        scope["route_template"] = ApiRouteTemplate.HEALTH_LIVE
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    response = await _invoke_asgi(
        RequestContextMiddleware(app),
        headers={
            "x-client-request-id": str(uuid4()),
            "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
        },
    )
    context = observed_contexts[0]
    assert context is not None and context.request_id.version == 7
    assert response.headers["x-request-id"] == str(context.request_id)
    assert response.headers["traceparent"].startswith("00-4bf92f3577b34da6a3ce929d0e0e4736-")
    assert current_diagnostic_context() is None


@pytest.mark.asyncio
async def test_completed_access_event_carries_only_closed_fields() -> None:
    sink = RecordingSink()
    response = await _invoke_asgi(
        RequestContextMiddleware(_response_app(200), event_sink=sink, monotonic_ns=lambda: 0)
    )
    assert response.status == 200
    assert sink.events == [
        (
            EventName.API_REQUEST_COMPLETED,
            {
                "http_method": ApiHttpMethod.GET,
                "route": ApiRouteTemplate.UNMATCHED,
                "status_code": 200,
                "duration_ms": 0,
            },
        )
    ]


@pytest.mark.asyncio
async def test_rejected_status_between_400_and_499_buckets_non_get_methods() -> None:
    sink = RecordingSink()
    await _invoke_asgi(
        RequestContextMiddleware(_response_app(404), event_sink=sink),
        method="DELETE",
    )
    fields = _access_fields(sink, EventName.API_REQUEST_REJECTED)
    assert fields is not None
    assert fields["http_method"] is ApiHttpMethod.OTHER
    assert fields["route"] is ApiRouteTemplate.UNMATCHED
    assert fields["status_code"] == 404
    assert _access_fields(sink, EventName.API_REQUEST_COMPLETED) is None
    assert _access_fields(sink, EventName.API_REQUEST_FAILED) is None


@pytest.mark.asyncio
async def test_failed_status_at_500_and_above_emits_failed_event() -> None:
    sink = RecordingSink()
    await _invoke_asgi(RequestContextMiddleware(_response_app(503), event_sink=sink))
    assert _access_fields(sink, EventName.API_REQUEST_FAILED) is not None
    assert _access_fields(sink, EventName.API_REQUEST_COMPLETED) is None
    assert _access_fields(sink, EventName.API_REQUEST_REJECTED) is None


@pytest.mark.asyncio
async def test_route_template_uses_closed_values_and_never_raw_paths() -> None:
    sink = RecordingSink()

    async def app_with_matched_route(scope: Scope, receive: Receive, send: Send) -> None:
        scope["route"] = SimpleNamespace(path="/api/health/ready")
        await send({"type": "http.response.start", "status": 200, "headers": []})

    await _invoke_asgi(RequestContextMiddleware(app_with_matched_route, event_sink=sink))
    fields = _access_fields(sink, EventName.API_REQUEST_COMPLETED)
    assert fields is not None
    assert fields["route"] is ApiRouteTemplate.HEALTH_READY


@pytest.mark.asyncio
async def test_malformed_correlation_headers_emit_fixed_reason_without_value() -> None:
    sink = RecordingSink()
    await _invoke_asgi(
        RequestContextMiddleware(_response_app(200), event_sink=sink),
        headers={
            "x-client-request-id": _SENTINEL_CLIENT_REQUEST_ID,
            "traceparent": _SENTINEL_TRACEPARENT,
        },
    )
    for name in (EventName.CLIENT_REQUEST_ID_REJECTED, EventName.TRACE_CONTEXT_REPLACED):
        fields = _access_fields(sink, name)
        assert fields is not None, name
        assert set(fields) == {"reason"}
        assert fields["reason"] == SafeToken.parse("invalid_format")
    serialized = repr(sink.events)
    assert _SENTINEL_CLIENT_REQUEST_ID not in serialized
    assert _SENTINEL_TRACEPARENT not in serialized


@pytest.mark.asyncio
async def test_middleware_replaces_app_set_correlation_headers_exactly_once() -> None:
    observed_request_ids: list[Any] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        observed_request_ids.append(current_diagnostic_context())
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"x-request-id", b"app-owned-do-not-emit"),
                    (b"traceparent", b"00-app-owned-do-not-emit-00"),
                    (b"content-type", b"application/json"),
                ],
            }
        )

    response = await _invoke_asgi(RequestContextMiddleware(app))
    context = observed_request_ids[0]
    assert response.count_header("x-request-id") == 1
    assert response.count_header("traceparent") == 1
    assert response.count_header("content-type") == 1
    assert response.headers["x-request-id"] == str(context.request_id)
    assert response.headers["traceparent"].startswith("00-")
    assert response.headers["content-type"] == "application/json"
    assert "app-owned-do-not-emit" not in repr(response.raw_headers)


@pytest.mark.asyncio
async def test_duration_is_clamped_to_non_negative_integer_milliseconds() -> None:
    readings = iter([2_000_000_000, 1_000_000_000])
    sink = RecordingSink()
    await _invoke_asgi(
        RequestContextMiddleware(
            _response_app(200), event_sink=sink, monotonic_ns=lambda: next(readings)
        )
    )
    fields = _access_fields(sink, EventName.API_REQUEST_COMPLETED)
    assert fields is not None
    assert fields["duration_ms"] == 0

    increasing = iter([0, 1_500_000])
    sink_two = RecordingSink()
    await _invoke_asgi(
        RequestContextMiddleware(
            _response_app(200), event_sink=sink_two, monotonic_ns=lambda: next(increasing)
        )
    )
    fields = _access_fields(sink_two, EventName.API_REQUEST_COMPLETED)
    assert fields is not None
    assert fields["duration_ms"] == 1


@pytest.mark.asyncio
async def test_sentinel_laden_request_never_reaches_events() -> None:
    sink = RecordingSink()
    response = await _invoke_asgi(
        RequestContextMiddleware(_response_app(200), event_sink=sink),
        path=_SENTINEL_RAW_PATH,
        query_string=_SENTINEL_QUERY,
        headers={
            "x-client-request-id": _SENTINEL_CLIENT_REQUEST_ID,
            "traceparent": _SENTINEL_TRACEPARENT,
            "x-custom-header": _SENTINEL_HEADER_VALUE,
        },
    )
    assert response.status == 200
    serialized = repr(sink.events)
    for sentinel in (
        _SENTINEL_CLIENT_REQUEST_ID,
        _SENTINEL_TRACEPARENT,
        _SENTINEL_RAW_PATH,
        "do-not-emit-query-value",
        _SENTINEL_HEADER_VALUE,
    ):
        assert sentinel not in serialized
    for _, fields in sink.events:
        assert set(fields) <= {
            "http_method",
            "route",
            "status_code",
            "duration_ms",
            "reason",
        }


@pytest.mark.asyncio
async def test_without_sink_middleware_still_binds_context_and_headers() -> None:
    observed_contexts: list[Any] = []
    response = await _invoke_asgi(
        RequestContextMiddleware(_response_app(200, observe=observed_contexts))
    )
    assert observed_contexts[0] is not None
    assert "x-request-id" in response.headers
    assert "traceparent" in response.headers


@pytest.mark.asyncio
async def test_non_http_scope_passes_through_without_context() -> None:
    sink = RecordingSink()
    observed_contexts: list[Any] = []
    sent: list[Mapping[str, Any]] = []

    async def lifespan_app(scope: Scope, receive: Receive, send: Send) -> None:
        observed_contexts.append(current_diagnostic_context())
        await send({"type": "lifespan.startup.complete"})

    async def receive() -> Mapping[str, Any]:
        return {"type": "lifespan.startup"}

    async def send(message: Mapping[str, Any]) -> None:
        sent.append(message)

    scope: Scope = {"type": "lifespan", "asgi": {"version": "3.0", "spec_version": "1.0"}}
    middleware: Callable[[Scope, Receive, Send], Awaitable[None]] = RequestContextMiddleware(
        lifespan_app, event_sink=sink
    )
    await middleware(scope, receive, send)

    assert observed_contexts == [None]
    assert sent == [{"type": "lifespan.startup.complete"}]
    assert sink.events == []
    assert current_diagnostic_context() is None


# --- failed-request correlation for exception-generated 5xx responses ---------------------------


def _crashing_app() -> ASGIApp:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        scope["route_template"] = ApiRouteTemplate.HEALTH_READY
        raise RuntimeError(_SENTINEL_EXCEPTION_TEXT)

    return app


@pytest.mark.asyncio
async def test_failed_access_observation_keeps_bound_request_id() -> None:
    sink = ObservingSink()
    response, body_chunks, caught = await _invoke_asgi_observing_failure(
        RequestContextMiddleware(_crashing_app(), event_sink=sink),
        path="/api/health/ready",
        route_template=ApiRouteTemplate.HEALTH_READY,
    )
    assert isinstance(caught, RuntimeError)
    assert response.status == 500
    failed = sink.only(EventName.API_REQUEST_FAILED)
    assert failed.context is not None
    assert failed.context.request_id == UUID(response.headers["x-request-id"])
    assert failed.result_code is ResultCode.FAILED
    assert failed.fields["route"] is ApiRouteTemplate.HEALTH_READY
    assert failed.fields["status_code"] == 500

    envelope = json.loads(b"".join(body_chunks))
    assert envelope["request_id"] == response.headers["x-request-id"]
    assert envelope["error"]["code"] == "internal_error"
    assert response.count_header("x-request-id") == 1
    assert response.headers["traceparent"].startswith("00-")
    assert current_diagnostic_context() is None


@pytest.mark.asyncio
async def test_exceptional_5xx_emits_exactly_one_failed_observation() -> None:
    sink = ObservingSink()
    await _invoke_asgi_observing_failure(
        RequestContextMiddleware(_crashing_app(), event_sink=sink),
        path="/api/health/ready",
        route_template=ApiRouteTemplate.HEALTH_READY,
    )
    assert [event.name for event in sink.events] == [EventName.API_REQUEST_FAILED]


@pytest.mark.asyncio
async def test_failed_request_correlation_never_leaks_sentinels() -> None:
    sink = ObservingSink()
    response, body_chunks, _ = await _invoke_asgi_observing_failure(
        RequestContextMiddleware(_crashing_app(), event_sink=sink),
        path=_SENTINEL_RAW_PATH,
        query_string=_SENTINEL_QUERY,
        headers={
            "x-client-request-id": _SENTINEL_CLIENT_REQUEST_ID,
            "traceparent": _SENTINEL_TRACEPARENT,
            "x-custom-header": _SENTINEL_HEADER_VALUE,
        },
        route_template=ApiRouteTemplate.HEALTH_READY,
    )
    serialized = repr(sink.events)
    for sentinel in (
        _SENTINEL_EXCEPTION_TEXT,
        _SENTINEL_RAW_PATH,
        "do-not-emit-query-value",
        _SENTINEL_CLIENT_REQUEST_ID,
        _SENTINEL_TRACEPARENT,
        _SENTINEL_HEADER_VALUE,
    ):
        assert sentinel not in serialized
        assert sentinel not in repr(response.raw_headers)
        assert sentinel not in repr(body_chunks)
    for event in sink.events:
        assert set(event.fields) <= {
            "http_method",
            "route",
            "status_code",
            "duration_ms",
            "reason",
        }


@pytest.mark.asyncio
async def test_completed_and_rejected_observations_unchanged_for_exceptional_path() -> None:
    """The exceptional 5xx ownership changes nothing for app-sent responses."""

    completed_sink = ObservingSink()
    completed = await _invoke_asgi(
        RequestContextMiddleware(_response_app(200), event_sink=completed_sink)
    )
    assert completed.status == 200
    completed_event = completed_sink.only(EventName.API_REQUEST_COMPLETED)
    assert completed_event.result_code is ResultCode.SUCCEEDED
    assert completed_event.context is not None
    assert completed_event.context.request_id == UUID(completed.headers["x-request-id"])

    rejected_sink = ObservingSink()
    rejected = await _invoke_asgi(
        RequestContextMiddleware(_response_app(404), event_sink=rejected_sink), method="DELETE"
    )
    assert rejected.status == 404
    rejected_event = rejected_sink.only(EventName.API_REQUEST_REJECTED)
    assert rejected_event.result_code is ResultCode.REJECTED
    assert rejected_event.context is not None
    assert rejected_event.context.request_id == UUID(rejected.headers["x-request-id"])
