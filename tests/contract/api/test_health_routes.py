"""Health-route ASGI contracts: probe isolation, deadline and safe envelopes.

These tests drive the composed application through ``httpx.ASGITransport`` with
an injected readiness probe. They pin that liveness never performs I/O, that
readiness runs exactly one probe call under the two-second overall deadline,
that readiness failures use the registry error envelopes, that framework
404/405 responses use safe envelopes without echoing request payloads, that
``redirect_slashes=False`` refuses trailing-slash aliases, and that the request
id in the envelope equals the ``X-Request-ID`` header with a valid
``traceparent``.
"""

from __future__ import annotations

import asyncio
import re
import time
from uuid import UUID

import httpx
import pytest
from api_runtime.application import create_api_application
from fastapi import FastAPI

from personal_os.api_contracts import CanonicalDatabaseReadinessProbe
from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import DatabaseMigrationError
from personal_os.runtime_configuration.models import RuntimeEnvironment

_TRACEPARENT_PATTERN = re.compile(r"00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}")
_ENVELOPE_KEYS = {"request_id", "data", "warnings", "error"}


class RecordingProbe:
    """Injected readiness probe that counts calls without performing I/O."""

    def __init__(self) -> None:
        self.call_count = 0

    async def check(self) -> None:
        self.call_count += 1


class ReadyProbe(RecordingProbe):
    """Probe whose canonical database is immediately ready."""


class BlockingProbe(RecordingProbe):
    """Probe that blocks far beyond the two-second readiness deadline."""

    async def check(self) -> None:
        self.call_count += 1
        await asyncio.sleep(30.0)


class SchemaInvalidProbe(RecordingProbe):
    """Probe reporting a violated canonical schema contract."""

    async def check(self) -> None:
        self.call_count += 1
        raise DatabaseMigrationError(ErrorCode.DATABASE_SCHEMA_CONTRACT_INVALID)


def create_test_app(probe: CanonicalDatabaseReadinessProbe) -> FastAPI:
    return create_api_application(
        environment=RuntimeEnvironment.TEST,
        readiness_probe=probe,
    )


async def request(app: FastAPI, method: str, path: str) -> httpx.Response:
    """Invoke one request through the raw ASGI transport without a network."""
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(method, path)


@pytest.mark.asyncio
async def test_liveness_never_calls_readiness_probe() -> None:
    probe = RecordingProbe()
    response = await request(create_test_app(probe), "GET", "/api/health/live")
    assert response.status_code == 200
    assert response.json()["data"] == {"status": "live", "service": "api"}
    assert probe.call_count == 0


@pytest.mark.asyncio
async def test_readiness_has_one_two_second_deadline_and_no_retry() -> None:
    probe = BlockingProbe()
    started = time.monotonic()
    response = await request(create_test_app(probe), "GET", "/api/health/ready")
    elapsed_seconds = time.monotonic() - started
    assert response.status_code == 503
    assert response.json()["error"] == {
        "code": "database_connection_unavailable",
        "message": "The canonical database is unavailable",
        "retryable": True,
        "details": {},
    }
    assert probe.call_count == 1
    assert 1.0 <= elapsed_seconds < 6.0


@pytest.mark.asyncio
async def test_readiness_success_envelope_reports_ready_checks() -> None:
    probe = ReadyProbe()
    response = await request(create_test_app(probe), "GET", "/api/health/ready")
    payload = response.json()
    assert response.status_code == 200
    assert set(payload) == _ENVELOPE_KEYS
    assert payload["data"] == {
        "status": "ready",
        "checks": {"postgresql": "ready", "schema": "ready"},
    }
    assert payload["warnings"] == []
    assert payload["error"] is None
    assert probe.call_count == 1


@pytest.mark.asyncio
async def test_readiness_maps_probe_schema_failure_to_registered_error() -> None:
    probe = SchemaInvalidProbe()
    response = await request(create_test_app(probe), "GET", "/api/health/ready")
    assert response.status_code == 503
    assert response.json()["error"] == {
        "code": "database_schema_contract_invalid",
        "message": "The canonical database schema contract is invalid",
        "retryable": False,
        "details": {},
    }
    assert probe.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "method", "status", "code"),
    [
        ("/api/not-real?sentinel=do-not-emit", "GET", 404, "api_route_not_found"),
        ("/api/health/live", "POST", 405, "api_method_not_allowed"),
    ],
)
async def test_framework_errors_use_safe_envelope(
    path: str, method: str, status: int, code: str
) -> None:
    response = await request(create_test_app(ReadyProbe()), method, path)
    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert "do-not-emit" not in response.text


@pytest.mark.asyncio
async def test_trailing_slash_is_not_redirected_to_canonical_path() -> None:
    response = await request(create_test_app(ReadyProbe()), "GET", "/api/health/live/")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "api_route_not_found"


@pytest.mark.asyncio
async def test_envelope_request_id_equals_header_and_traceparent_is_valid() -> None:
    response = await request(create_test_app(ReadyProbe()), "GET", "/api/health/live")
    payload = response.json()
    assert set(payload) == _ENVELOPE_KEYS
    assert payload["error"] is None
    header_request_id = response.headers["x-request-id"]
    assert payload["request_id"] == header_request_id
    assert UUID(header_request_id).version == 7
    assert _TRACEPARENT_PATTERN.fullmatch(response.headers["traceparent"]) is not None
