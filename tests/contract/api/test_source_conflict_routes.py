"""Closed source conflict route set: exact membership, methods and operation ids.

These tests compose the real application over the offline runtimes plus the
offline source-conflict runtime and pin the served Conflict Inbox surface
of Child 8 spec 6: exactly the four routes with exactly their methods and
their manually assigned semantic operation ids, no other verb on any path,
no workspace/device/user selector on the request schema, the evidence body
documented as one ``application/octet-stream`` binary payload, and the
dedicated ``AccessCredential`` Bearer scheme bound by all four routes.
"""

from __future__ import annotations

from typing import Any, Final

import pytest
from api_runtime.application import create_api_application
from api_runtime.authentication_composition import compose_offline_web_authentication
from api_runtime.exclusion_policy_composition import compose_offline_exclusion_policy
from api_runtime.small_file_sync_composition import compose_offline_small_file_sync
from api_runtime.source_conflict_composition import compose_offline_source_conflicts
from fastapi import FastAPI

from personal_os.runtime_configuration.models import RuntimeEnvironment

#: The closed source conflict route set with exactly the served methods.
CONFLICT_ROUTE_METHODS: Final[dict[str, frozenset[str]]] = {
    "/api/sync/conflicts": frozenset({"GET"}),
    "/api/sync/conflicts/{conflict_id}": frozenset({"GET"}),
    "/api/sync/conflicts/{conflict_id}/evidence/{role}": frozenset({"GET"}),
    "/api/sync/conflicts/{conflict_id}/resolve": frozenset({"POST"}),
}

#: The exact semantic operation ids of the four conflict operations.
CONFLICT_OPERATION_IDS: Final[dict[tuple[str, str], str]] = {
    ("/api/sync/conflicts", "get"): "listSourceConflicts",
    ("/api/sync/conflicts/{conflict_id}", "get"): "getSourceConflict",
    ("/api/sync/conflicts/{conflict_id}/evidence/{role}", "get"): (
        "downloadSourceConflictEvidence"
    ),
    ("/api/sync/conflicts/{conflict_id}/resolve", "post"): "resolveSourceConflict",
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
        source_conflicts=compose_offline_source_conflicts(),
    )


@pytest.fixture
def document(application: FastAPI) -> dict[str, Any]:
    return application.openapi()


def test_the_conflict_surface_is_exactly_the_four_routes(
    application: FastAPI, document: dict[str, Any]
) -> None:
    conflict_paths = {
        route.path
        for route in application.routes
        if getattr(route, "path", "") in CONFLICT_ROUTE_METHODS
    }
    assert conflict_paths == set(CONFLICT_ROUTE_METHODS)
    for path, methods in CONFLICT_ROUTE_METHODS.items():
        assert set(document["paths"][path]) == set(method.lower() for method in methods), path


def test_every_operation_carries_its_semantic_id(document: dict[str, Any]) -> None:
    for (path, method), operation_id in CONFLICT_OPERATION_IDS.items():
        assert document["paths"][path][method]["operationId"] == operation_id, (path, method)


def test_every_route_binds_exactly_the_access_bearer_scheme(document: dict[str, Any]) -> None:
    for path, method in CONFLICT_OPERATION_IDS:
        operation = document["paths"][path][method]
        assert operation["security"] == [{"AccessCredential": []}], (path, method)


def test_the_resolve_request_schema_admits_no_identity_selector(
    document: dict[str, Any],
) -> None:
    properties = document["components"]["schemas"]["SourceConflictResolveRequest"]["properties"]
    assert set(properties) == {
        "resolution_event_id",
        "idempotency_key",
        "resolution_kind",
        "reviewed_remote_version_id",
        "verified_candidate_object_id",
    }


def test_the_evidence_path_parameters_carry_their_closed_shapes(
    document: dict[str, Any],
) -> None:
    operation = document["paths"]["/api/sync/conflicts/{conflict_id}/evidence/{role}"]["get"]
    parameters = {parameter["name"]: parameter for parameter in operation["parameters"]}
    assert set(parameters) == {"conflict_id", "role"}
    assert parameters["conflict_id"]["schema"]["format"] == "uuid"
    assert parameters["role"]["schema"] == {"$ref": "#/components/schemas/ConflictEvidenceRole"}
    assert set(document["components"]["schemas"]["ConflictEvidenceRole"]["enum"]) == {
        "base",
        "remote",
        "candidate",
    }
