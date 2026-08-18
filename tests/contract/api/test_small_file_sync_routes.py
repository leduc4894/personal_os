"""Closed small-file sync route set: exact membership, methods and operation ids.

These tests compose the real application over both offline runtimes plus the
offline small-file-sync runtime and pin the served surface of spec 10: exactly
the two routes with exactly their methods and their manually assigned semantic
operation ids, no other verb on either path, no workspace/device/user selector
on the request schema, the raw content body documented as one
``application/octet-stream`` binary payload, and the dedicated
``AccessCredential`` Bearer scheme bound by both routes.
"""

from __future__ import annotations

from typing import Any, Final

import pytest
from api_runtime.application import create_api_application
from api_runtime.authentication_composition import compose_offline_web_authentication
from api_runtime.exclusion_policy_composition import compose_offline_exclusion_policy
from api_runtime.small_file_sync_composition import compose_offline_small_file_sync
from fastapi import FastAPI

from personal_os.runtime_configuration.models import RuntimeEnvironment

#: The closed small-file sync route set with exactly the served methods.
SYNC_ROUTE_METHODS: Final[dict[str, frozenset[str]]] = {
    "/api/sync/journal-events/preflight": frozenset({"POST"}),
    "/api/uploads/{operation_id}/content": frozenset({"PUT"}),
}

#: The exact semantic operation ids of the two sync operations.
SYNC_OPERATION_IDS: Final[dict[tuple[str, str], str]] = {
    ("/api/sync/journal-events/preflight", "post"): "preflightJournalEventUpload",
    ("/api/uploads/{operation_id}/content", "put"): "uploadSmallFileContent",
}


class _ReadyProbe:
    """Readiness probe stub: route-set inspection never consults it."""

    async def check(self) -> None: ...


@pytest.fixture
def application() -> FastAPI:
    return create_api_application(
        environment=RuntimeEnvironment.TEST,
        readiness_probe=_ReadyProbe(),
        web_authentication=compose_offline_web_authentication(),
        exclusion_policy=compose_offline_exclusion_policy(),
        small_file_sync=compose_offline_small_file_sync(),
    )


@pytest.fixture
def document(application: FastAPI) -> dict[str, Any]:
    return application.openapi()


def test_the_small_file_surface_is_exactly_the_two_routes(
    application: FastAPI, document: dict[str, Any]
) -> None:
    sync_paths = {
        route.path
        for route in application.routes
        if getattr(route, "path", "").startswith(("/api/sync/journal-events", "/api/uploads"))
    }
    assert sync_paths == set(SYNC_ROUTE_METHODS)
    for path, methods in SYNC_ROUTE_METHODS.items():
        assert set(document["paths"][path]) == set(method.lower() for method in methods), path


def test_both_operations_carry_their_semantic_ids(document: dict[str, Any]) -> None:
    for (path, method), operation_id in SYNC_OPERATION_IDS.items():
        assert document["paths"][path][method]["operationId"] == operation_id, (path, method)


def test_both_routes_bind_exactly_the_access_bearer_scheme(document: dict[str, Any]) -> None:
    for path, method in SYNC_OPERATION_IDS:
        operation = document["paths"][path][method]
        assert operation["security"] == [{"AccessCredential": []}], (path, method)


def test_the_preflight_request_schema_admits_no_identity_selector(
    document: dict[str, Any],
) -> None:
    schema_name = "SmallFilePreflightRequest"
    properties = document["components"]["schemas"][schema_name]["properties"]
    assert set(properties) == {
        "event_id",
        "idempotency_key",
        "operation",
        "local_file_id",
        "source_id",
        "base_version_id",
        "normalized_locator",
        "sha256",
        "size_bytes",
        "media_type",
        "policy_revision",
    }


def test_the_content_body_is_documented_as_one_octet_stream(
    document: dict[str, Any],
) -> None:
    operation = document["paths"]["/api/uploads/{operation_id}/content"]["put"]
    request_body = operation["requestBody"]
    assert request_body["required"] is True
    assert set(request_body["content"]) == {"application/octet-stream"}
    schema = request_body["content"]["application/octet-stream"]["schema"]
    assert schema == {"type": "string", "format": "binary"}


def test_the_content_operation_id_parameter_is_bounded_by_the_token_grammar(
    document: dict[str, Any],
) -> None:
    operation = document["paths"]["/api/uploads/{operation_id}/content"]["put"]
    (parameter,) = operation["parameters"]
    assert parameter["name"] == "operation_id"
    assert parameter["required"] is True
    schema = parameter["schema"]
    assert schema["type"] == "string"
    assert schema["pattern"] == "^[A-Za-z0-9_-]{32,128}$"
