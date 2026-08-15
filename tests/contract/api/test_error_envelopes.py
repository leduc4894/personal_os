"""Envelope error-contract tests: validation, HTTP and application failures.

These tests build a test-only FastAPI application, register the production
exception handlers on it and add body-accepting routes under ``/test``. That
application never enters ``create_api_application`` or its OpenAPI snapshot:
the composition under test is only ``register_api_exception_handlers`` plus
the request correlation middleware. They pin the malformed-JSON versus
schema-failure split, the safe ``field_names`` detail without rejected values,
registered statuses for application errors, the internal fallback for unknown
status/code combinations, and the sentinel-free 500 envelope.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from uuid import UUID

import httpx
import pytest
from api_runtime.application import register_api_exception_handlers
from api_runtime.request_context import RequestContextMiddleware
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from starlette.exceptions import HTTPException

from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import ApplicationError, DatabaseMigrationError

_TRACEPARENT_PATTERN = re.compile(r"00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}")
_ENVELOPE_KEYS = {"request_id", "data", "warnings", "error"}


class AcceptedBody(BaseModel):
    """Body contract whose rejected values must never reach an envelope."""

    model_config = ConfigDict(extra="forbid")
    name: str
    count: int


def create_body_test_app() -> FastAPI:
    """Compose a test-only app with the production handlers and correlation."""
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None, redirect_slashes=False)
    register_api_exception_handlers(app)

    @app.post("/test/body")
    async def accept_body(body: AcceptedBody) -> JSONResponse:
        return JSONResponse({"accepted": True})

    @app.get("/test/dependency-error")
    async def dependency_error() -> JSONResponse:
        raise DatabaseMigrationError(ErrorCode.DATABASE_CONNECTION_UNAVAILABLE)

    @app.get("/test/unrouted-code-error")
    async def unrouted_code_error() -> JSONResponse:
        raise ApplicationError(ErrorCode.SOURCE_NOT_FOUND)

    @app.get("/test/http-unknown-status")
    async def http_unknown_status() -> JSONResponse:
        raise HTTPException(status_code=418)

    @app.get("/test/crash")
    async def crash() -> JSONResponse:
        raise RuntimeError("sentinel-secret-do-not-emit")

    app.middleware_stack = RequestContextMiddleware(app.build_middleware_stack())
    return app


async def request(
    app: FastAPI,
    method: str,
    path: str,
    *,
    content: bytes | None = None,
    headers: Mapping[str, str] | None = None,
) -> httpx.Response:
    """Invoke one request through the raw ASGI transport without a network."""
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(method, path, content=content, headers=headers)


@pytest.mark.asyncio
async def test_malformed_json_is_rejected_as_malformed_not_schema_failure() -> None:
    response = await request(
        create_body_test_app(),
        "POST",
        "/test/body",
        content=b'{"name": ',
        headers={"content-type": "application/json"},
    )
    payload = response.json()
    assert response.status_code == 400
    assert set(payload) == _ENVELOPE_KEYS
    assert payload["error"] == {
        "code": "api_request_malformed",
        "message": "The API request is malformed",
        "retryable": False,
        "details": {},
    }
    assert payload["request_id"] == response.headers["x-request-id"]
    assert "column" not in response.text


@pytest.mark.asyncio
async def test_schema_failure_lists_only_unique_safe_field_names() -> None:
    response = await request(
        create_body_test_app(),
        "POST",
        "/test/body",
        content=b'{"name": 5, "count": "sentinel-rejected-value"}',
        headers={"content-type": "application/json"},
    )
    payload = response.json()
    assert response.status_code == 422
    assert set(payload) == _ENVELOPE_KEYS
    assert payload["error"]["code"] == "api_request_validation_failed"
    assert payload["error"]["details"] == {"field_names": ["name", "count"]}
    assert "sentinel-rejected-value" not in response.text


@pytest.mark.asyncio
async def test_unsafe_field_name_is_never_exposed_in_details() -> None:
    response = await request(
        create_body_test_app(),
        "POST",
        "/test/body",
        content=b'{"nAme!": 1}',
        headers={"content-type": "application/json"},
    )
    payload = response.json()
    assert response.status_code == 422
    assert payload["error"]["details"] == {"field_names": ["name", "count"]}
    assert "nAme!" not in response.text


@pytest.mark.asyncio
async def test_application_error_uses_its_registered_status_and_body() -> None:
    response = await request(create_body_test_app(), "GET", "/test/dependency-error")
    payload = response.json()
    assert response.status_code == 503
    assert set(payload) == _ENVELOPE_KEYS
    assert payload["error"] == {
        "code": "database_connection_unavailable",
        "message": "The canonical database is unavailable",
        "retryable": True,
        "details": {},
    }
    assert payload["request_id"] == response.headers["x-request-id"]


@pytest.mark.asyncio
async def test_application_error_without_http_status_falls_back_to_internal() -> None:
    response = await request(create_body_test_app(), "GET", "/test/unrouted-code-error")
    payload = response.json()
    assert response.status_code == 500
    assert payload["error"] == {
        "code": "internal_error",
        "message": "An unexpected internal error occurred",
        "retryable": False,
        "details": {},
    }


@pytest.mark.asyncio
async def test_http_exception_with_unknown_status_falls_back_to_internal() -> None:
    response = await request(create_body_test_app(), "GET", "/test/http-unknown-status")
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"


@pytest.mark.asyncio
async def test_unexpected_exception_returns_internal_envelope_without_sentinel() -> None:
    response = await request(create_body_test_app(), "GET", "/test/crash")
    payload = response.json()
    assert response.status_code == 500
    assert set(payload) == _ENVELOPE_KEYS
    assert payload["error"] == {
        "code": "internal_error",
        "message": "An unexpected internal error occurred",
        "retryable": False,
        "details": {},
    }
    assert "sentinel-secret-do-not-emit" not in response.text
    assert payload["request_id"] == response.headers["x-request-id"]
    assert UUID(payload["request_id"]).version == 7
    assert _TRACEPARENT_PATTERN.fullmatch(response.headers["traceparent"]) is not None
