"""Sensitive-data leak corpus for the HTTP boundary: sentinels never survive.

Every case injects one unique sentinel through one entry point of the composed
application — the raw path, the query string, request headers, cookies,
malformed JSON, rejected validation values, a database failure's exception
text and an unexpected exception — then scans two capture surfaces: the
complete HTTP exchange (status line inputs aside: body plus every response
header) and the structured diagnostics the request correlation middleware
emits. The corpus also pins the exposure posture: no ``server`` version
header, no CORS header, no Swagger/ReDoc HTML and no production OpenAPI
document body.

Body-accepting routes cannot exist on the closed composed route set, so the
malformed-JSON and validation cases follow the ``test_error_envelopes``
convention: a test-only application carrying the production exception
handlers and the production request correlation middleware with a recording
event sink. Every other case runs against the real composed application from
``create_api_application``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from api_runtime.application import create_api_application, register_api_exception_handlers
from api_runtime.request_context import RequestContextMiddleware
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from personal_os.api_contracts import CanonicalDatabaseReadinessProbe
from personal_os.diagnostics.events import EventName
from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import DatabaseMigrationError
from personal_os.runtime_configuration.models import RuntimeEnvironment

_RAW_PATH_SENTINEL = "sensitive-raw-path-do-not-emit"
_QUERY_SENTINEL = "sensitive-query-value-do-not-emit"
_HEADER_SENTINEL = "sensitive-header-value-do-not-emit"
_CORRELATION_SENTINEL = "sensitive-client-request-id-do-not-emit"
_COOKIE_SENTINEL = "sensitive-cookie-value-do-not-emit"
_MALFORMED_JSON_SENTINEL = "sensitive-malformed-json-do-not-emit"
_VALIDATION_SENTINEL = "sensitive-validation-value-do-not-emit"
_DATABASE_EXCEPTION_SENTINEL = "sensitive-database-exception-do-not-emit"
_UNEXPECTED_EXCEPTION_SENTINEL = "sensitive-unexpected-exception-do-not-emit"

_ALL_SENTINELS = (
    _RAW_PATH_SENTINEL,
    _QUERY_SENTINEL,
    _HEADER_SENTINEL,
    _CORRELATION_SENTINEL,
    _COOKIE_SENTINEL,
    _MALFORMED_JSON_SENTINEL,
    _VALIDATION_SENTINEL,
    _DATABASE_EXCEPTION_SENTINEL,
    _UNEXPECTED_EXCEPTION_SENTINEL,
)

_ENVELOPE_KEYS = {"request_id", "data", "warnings", "error"}


class ReadyProbe:
    """Injected readiness probe that succeeds without performing I/O."""

    async def check(self) -> None: ...


class SentinelDatabaseProbe:
    """Readiness probe failing with a sentinel-bearing driver exception cause."""

    async def check(self) -> None:
        try:
            raise RuntimeError(_DATABASE_EXCEPTION_SENTINEL)
        except RuntimeError as cause:
            raise DatabaseMigrationError(ErrorCode.DATABASE_CONNECTION_UNAVAILABLE) from cause


class CrashingProbe:
    """Readiness probe raising an unexpected sentinel-bearing exception."""

    async def check(self) -> None:
        raise RuntimeError(_UNEXPECTED_EXCEPTION_SENTINEL)


@dataclass
class RecordingEventSink:
    """Structured-diagnostics capture retaining every emitted event verbatim."""

    events: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def emit(self, event_name: EventName, fields: Mapping[str, object] | None = None) -> None:
        self.events.append((event_name.value, dict(fields or {})))

    def rendered(self) -> str:
        return json.dumps(self.events, default=str)

    def event_names(self) -> list[str]:
        return [name for name, _ in self.events]


class AcceptedBody(BaseModel):
    """Body contract whose rejected values must never reach any surface."""

    model_config = ConfigDict(extra="forbid")

    name: str
    count: int


def create_composed_app(
    probe: CanonicalDatabaseReadinessProbe,
    sink: RecordingEventSink,
    environment: RuntimeEnvironment = RuntimeEnvironment.TEST,
) -> FastAPI:
    return create_api_application(environment=environment, readiness_probe=probe, event_sink=sink)


def create_body_test_app(sink: RecordingEventSink) -> FastAPI:
    """Test-only app carrying the production handlers and correlation middleware."""
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None, redirect_slashes=False)
    register_api_exception_handlers(app)

    @app.post("/test/body")
    async def accept_body(body: AcceptedBody) -> JSONResponse:
        return JSONResponse({"accepted": True})

    app.middleware_stack = RequestContextMiddleware(app.build_middleware_stack(), event_sink=sink)
    return app


def http_surface(response: httpx.Response) -> str:
    """Every byte of the exchange the client can observe: body plus headers."""
    headers = "\n".join(f"{name}: {value}" for name, value in response.headers.items())
    return f"{response.text}\n{headers}"


def assert_no_sentinel(*blobs: str, sentinels: tuple[str, ...] = _ALL_SENTINELS) -> None:
    combined = "\n".join(blobs)
    for sentinel in sentinels:
        assert sentinel not in combined, f"sentinel leaked: {sentinel}"


async def request(
    app: FastAPI,
    method: str,
    path: str,
    *,
    content: bytes | None = None,
    headers: Mapping[str, str] | None = None,
    cookies: dict[str, str] | None = None,
) -> httpx.Response:
    """Invoke one request through the raw ASGI transport without a network."""
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(method, path, content=content, headers=headers, cookies=cookies)


@pytest.mark.asyncio
async def test_raw_path_query_header_cookie_and_correlation_sentinels_never_leak() -> None:
    sink = RecordingEventSink()
    app = create_composed_app(ReadyProbe(), sink)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        cookies={"session": _COOKIE_SENTINEL},
    ) as client:
        unmatched = await client.get(
            f"/{_RAW_PATH_SENTINEL}/nested",
            headers={"x-sensitive-header": _HEADER_SENTINEL},
        )
        liveness = await client.get(
            f"/api/health/live?token={_QUERY_SENTINEL}",
            headers={"x-client-request-id": _CORRELATION_SENTINEL},
        )

    assert unmatched.status_code == 404
    assert unmatched.json()["error"]["code"] == "api_route_not_found"
    assert liveness.status_code == 200
    assert set(liveness.json()) == _ENVELOPE_KEYS
    for response in (unmatched, liveness):
        assert_no_sentinel(http_surface(response))
    assert_no_sentinel(sink.rendered())

    # The diagnostics capture is real: both exchanges were observed and the
    # rejected client correlation id produced the fixed-reason rejection event.
    assert "api_request_rejected" in sink.event_names()
    assert "client_request_id_rejected" in sink.event_names()


@pytest.mark.asyncio
async def test_malformed_json_sentinel_never_reaches_http_or_diagnostics() -> None:
    sink = RecordingEventSink()
    app = create_body_test_app(sink)
    response = await request(
        app,
        "POST",
        "/test/body",
        content=b'{"name": "sensitive-malformed-json-do-not-emit',
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "api_request_malformed"
    assert_no_sentinel(http_surface(response), sink.rendered())
    assert "api_request_rejected" in sink.event_names()


@pytest.mark.asyncio
async def test_validation_value_sentinel_never_reaches_http_or_diagnostics() -> None:
    sink = RecordingEventSink()
    app = create_body_test_app(sink)
    body = json.dumps({"name": 5, "count": _VALIDATION_SENTINEL})
    response = await request(
        app,
        "POST",
        "/test/body",
        content=body.encode(),
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 422
    payload = response.json()
    assert payload["error"]["code"] == "api_request_validation_failed"
    assert payload["error"]["details"] == {"field_names": ["name", "count"]}
    assert_no_sentinel(http_surface(response), sink.rendered())
    assert "api_request_rejected" in sink.event_names()


@pytest.mark.asyncio
async def test_database_exception_text_sentinel_never_reaches_http_or_diagnostics() -> None:
    sink = RecordingEventSink()
    app = create_composed_app(SentinelDatabaseProbe(), sink)
    response = await request(app, "GET", "/api/health/ready")

    assert response.status_code == 503
    payload = response.json()
    assert payload["error"] == {
        "code": "database_connection_unavailable",
        "message": "The canonical database is unavailable",
        "retryable": True,
        "details": {},
    }
    assert_no_sentinel(http_surface(response), sink.rendered())
    assert "api_request_failed" in sink.event_names()


@pytest.mark.asyncio
async def test_unexpected_exception_sentinel_never_reaches_http_or_diagnostics() -> None:
    sink = RecordingEventSink()
    app = create_composed_app(CrashingProbe(), sink)
    response = await request(app, "GET", "/api/health/ready")

    assert response.status_code == 500
    payload = response.json()
    assert set(payload) == _ENVELOPE_KEYS
    assert payload["error"] == {
        "code": "internal_error",
        "message": "An unexpected internal error occurred",
        "retryable": False,
        "details": {},
    }
    assert payload["request_id"] == response.headers["x-request-id"]
    assert_no_sentinel(http_surface(response), sink.rendered())
    assert "api_request_failed" in sink.event_names()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("probe", "path", "expected_status"),
    [
        (ReadyProbe(), "/api/health/live", 200),
        (ReadyProbe(), "/api/health/ready", 200),
        (ReadyProbe(), "/not-a-route", 404),
        (SentinelDatabaseProbe(), "/api/health/ready", 503),
        (CrashingProbe(), "/api/health/ready", 500),
    ],
)
async def test_responses_carry_no_server_version_or_cors_headers(
    probe: CanonicalDatabaseReadinessProbe, path: str, expected_status: int
) -> None:
    sink = RecordingEventSink()
    app = create_composed_app(probe, sink)
    response = await request(app, "GET", path)

    assert response.status_code == expected_status
    assert "server" not in response.headers
    assert not [name for name in response.headers if name.startswith("access-control-")]
    assert_no_sentinel(http_surface(response), sink.rendered())


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/docs", "/redoc"])
async def test_docs_and_redoc_never_serve_html(path: str) -> None:
    sink = RecordingEventSink()
    app = create_composed_app(ReadyProbe(), sink)
    response = await request(app, "GET", path)

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["error"]["code"] == "api_route_not_found"
    lowered = response.text.lower()
    assert "<html" not in lowered
    assert "<script" not in lowered
    assert "swagger" not in lowered
    assert "redoc" not in lowered


@pytest.mark.asyncio
async def test_production_application_serves_no_openapi_document_body() -> None:
    sink = RecordingEventSink()
    app = create_composed_app(ReadyProbe(), sink, environment=RuntimeEnvironment.PRODUCTION)
    response = await request(app, "GET", "/api/openapi.json")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "api_route_not_found"
    assert '"openapi"' not in response.text
    assert '"paths"' not in response.text
    assert '"operationId"' not in response.text
    assert_no_sentinel(http_surface(response), sink.rendered())
