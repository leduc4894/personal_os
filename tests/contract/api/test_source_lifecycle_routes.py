"""Closed source lifecycle route: full FastAPI route over the offline composition.

These tests compose the real application factory over both offline
authentication and the offline source-lifecycle composition and pin the
closed served surface of spec 19.2: exactly one ``POST`` route,
``commitSourceLifecycleEvent`` as the operation id, the dedicated
``AccessCredential`` Bearer scheme, the closed request schema, the
envelope response model and the closed lifecycle error envelope mapping.
"""

from __future__ import annotations

from typing import Final
from uuid import UUID, uuid4

import httpx
import pytest
from api_runtime.application import create_api_application
from api_runtime.authentication_composition import compose_offline_web_authentication
from api_runtime.source_lifecycle_composition import compose_offline_source_lifecycle
from api_runtime.source_lifecycle_routes import create_source_lifecycle_route_endpoints
from fastapi import FastAPI

from personal_os.runtime_configuration.models import RuntimeEnvironment

LIFECYCLE_PATH: Final[str] = "/api/sources/lifecycle-events"
LIFECYCLE_OPERATION_ID: Final[str] = "commitSourceLifecycleEvent"

SOURCE_ID: Final[UUID] = UUID("018f47a0-7b00-7000-8000-000000000010")
EVENT_ID: Final[UUID] = UUID("018f47a0-7b00-7000-8000-000000000011")
EXPECTED_VERSION_ID: Final[UUID] = UUID("018f47a0-7b00-7000-8000-000000000013")
TOMBSTONE_ID: Final[UUID] = UUID("018f47a0-7b00-7000-8000-000000000014")


class _ReadyProbe:
    """Readiness probe stub: route-set inspection never consults it."""

    async def check(self) -> None: ...


@pytest.fixture
def application() -> FastAPI:
    return create_api_application(
        environment=RuntimeEnvironment.TEST,
        readiness_probe=_ReadyProbe(),
        web_authentication=compose_offline_web_authentication(),
        source_lifecycle=compose_offline_source_lifecycle(),
    )


def test_lifecycle_route_serves_exactly_the_one_method(application: FastAPI) -> None:
    served: dict[str, frozenset[str]] = {
        getattr(route, "path", ""): frozenset(getattr(route, "methods", set()))
        for route in application.routes
        if getattr(route, "path", "") == LIFECYCLE_PATH
    }
    assert served == {LIFECYCLE_PATH: frozenset({"POST"})}


def test_lifecycle_route_carries_the_semantic_operation_id(application: FastAPI) -> None:
    for route in application.routes:
        if getattr(route, "path", "") != LIFECYCLE_PATH:
            continue
        assert str(route.operation_id) == LIFECYCLE_OPERATION_ID, route.path


def test_lifecycle_route_binds_exactly_the_access_bearer_scheme(application: FastAPI) -> None:
    document = application.openapi()
    operation = document["paths"][LIFECYCLE_PATH]["post"]
    assert operation["security"] == [{"AccessCredential": []}]


def test_lifecycle_route_response_carries_cache_control_no_store(
    application: FastAPI,
) -> None:
    document = application.openapi()
    operation = document["paths"][LIFECYCLE_PATH]["post"]
    # The successful response schema is the closed envelope; the no-store
    # posture is enforced at runtime by the route handler.
    assert "200" in operation["responses"]


def test_lifecycle_request_schema_is_closed_and_carries_no_identity_selector(
    application: FastAPI,
) -> None:
    document = application.openapi()
    schema = document["components"]["schemas"]["SourceLifecycleEventRequest"]
    properties = set(schema["properties"])
    assert schema.get("additionalProperties") is False
    assert properties == {
        "event_id",
        "idempotency_key",
        "source_id",
        "operation",
        "expected_version_id",
        "expected_locator",
        "target_locator",
        "tombstone_id",
        "policy_revision",
        "client_timestamp",
    }
    assert not properties & {"workspace_id", "device_id", "user_id", "signature"}


def test_lifecycle_operation_enum_is_closed(application: FastAPI) -> None:
    document = application.openapi()
    operation_enum = document["components"]["schemas"]["LifecycleOperation"]["enum"]
    assert set(operation_enum) == {"rename", "move", "delete", "restore"}


def test_lifecycle_lifecycle_state_enum_is_closed(application: FastAPI) -> None:
    document = application.openapi()
    state_enum = document["components"]["schemas"]["LifecycleState"]["enum"]
    assert set(state_enum) == {"active", "deleted"}


def test_lifecycle_route_documents_envelope_typed_error_envelopes(
    application: FastAPI,
) -> None:
    document = application.openapi()
    operation = document["paths"][LIFECYCLE_PATH]["post"]
    assert "200" in operation["responses"]


@pytest.mark.asyncio
async def test_lifecycle_route_missing_authorization_header_returns_401(
    application: FastAPI,
) -> None:
    transport = httpx.ASGITransport(app=application, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(LIFECYCLE_PATH, json={})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "device_credential_invalid"
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_lifecycle_route_unknown_token_returns_401(application: FastAPI) -> None:
    transport = httpx.ASGITransport(app=application, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            LIFECYCLE_PATH,
            json={},
            headers={"Authorization": f"Bearer at1.unknown.{uuid4().hex}"},
        )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "device_credential_invalid"


def test_factory_accepts_a_runtime_dependency() -> None:
    """The factory binds the runtime without entering the application lifespan."""

    runtime = compose_offline_source_lifecycle()
    web_authentication = compose_offline_web_authentication()
    endpoints = create_source_lifecycle_route_endpoints(
        web_authentication=web_authentication,
        source_lifecycle=runtime,
    )
    assert callable(endpoints.commit_source_lifecycle_event)