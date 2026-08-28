"""Closed multipart upload route set: exact membership, methods and ids.

These tests compose the real application over the offline runtimes plus the
offline multipart-upload runtime and pin the served surface of the Child 7
spec 5 contract: exactly the five session routes with exactly their methods
and their manually assigned semantic operation ids, the dedicated
``AccessCredential`` Bearer scheme bound by every route, the opaque session
ID path parameter bounded by its grammar, the part number bounded by the
maximum geometry, no workspace/device/user selector on the create schema,
and the one part-URL response referencing its own strict schema — the sole
component a signed URL may appear in.
"""

from __future__ import annotations

from typing import Any, Final

import pytest
from api_runtime.application import create_api_application
from api_runtime.authentication_composition import compose_offline_web_authentication
from api_runtime.exclusion_policy_composition import compose_offline_exclusion_policy
from api_runtime.multipart_upload_composition import compose_offline_multipart_upload
from api_runtime.small_file_sync_composition import compose_offline_small_file_sync
from fastapi import FastAPI

from personal_os.runtime_configuration.models import RuntimeEnvironment

#: The closed multipart session route set with exactly the served methods.
MULTIPART_ROUTE_METHODS: Final[dict[str, frozenset[str]]] = {
    "/api/uploads/multipart-sessions": frozenset({"POST"}),
    "/api/uploads/multipart-sessions/{session_id}": frozenset({"GET"}),
    "/api/uploads/multipart-sessions/{session_id}/parts/{part_number}/url": frozenset({"POST"}),
    "/api/uploads/multipart-sessions/{session_id}/complete": frozenset({"POST"}),
    "/api/uploads/multipart-sessions/{session_id}/abort": frozenset({"POST"}),
}

#: The exact semantic operation ids of the five multipart operations.
MULTIPART_OPERATION_IDS: Final[dict[tuple[str, str], str]] = {
    ("/api/uploads/multipart-sessions", "post"): "createMultipartUploadSession",
    ("/api/uploads/multipart-sessions/{session_id}", "get"): "getMultipartUploadSession",
    (
        "/api/uploads/multipart-sessions/{session_id}/parts/{part_number}/url",
        "post",
    ): "issueMultipartPartUrl",
    ("/api/uploads/multipart-sessions/{session_id}/complete", "post"): (
        "completeMultipartUploadSession"
    ),
    ("/api/uploads/multipart-sessions/{session_id}/abort", "post"): ("abortMultipartUploadSession"),
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
        multipart_upload=compose_offline_multipart_upload(),
    )


@pytest.fixture
def document(application: FastAPI) -> dict[str, Any]:
    return application.openapi()


def test_the_multipart_surface_is_exactly_the_five_routes(
    application: FastAPI, document: dict[str, Any]
) -> None:
    multipart_paths = {
        route.path
        for route in application.routes
        if getattr(route, "path", "").startswith("/api/uploads/multipart-sessions")
    }
    assert multipart_paths == set(MULTIPART_ROUTE_METHODS)
    for path, methods in MULTIPART_ROUTE_METHODS.items():
        assert set(document["paths"][path]) == set(method.lower() for method in methods), path


def test_every_operation_carries_its_semantic_id(document: dict[str, Any]) -> None:
    for (path, method), operation_id in MULTIPART_OPERATION_IDS.items():
        assert document["paths"][path][method]["operationId"] == operation_id, (path, method)


def test_every_route_binds_exactly_the_access_bearer_scheme(
    document: dict[str, Any],
) -> None:
    for path, method in MULTIPART_OPERATION_IDS:
        operation = document["paths"][path][method]
        assert operation["security"] == [{"AccessCredential": []}], (path, method)


def test_the_session_id_parameters_are_bounded_by_the_opaque_grammar(
    document: dict[str, Any],
) -> None:
    for path, method in MULTIPART_OPERATION_IDS:
        if "{session_id}" not in path:
            continue
        parameters = document["paths"][path][method]["parameters"]
        session_parameter = next(
            parameter for parameter in parameters if parameter["name"] == "session_id"
        )
        assert session_parameter["required"] is True
        schema = session_parameter["schema"]
        assert schema["type"] == "string"
        assert schema["pattern"] == "^[A-Za-z0-9_-]{32,128}$", (path, method)


def test_the_part_number_parameter_is_bounded_by_the_maximum_geometry(
    document: dict[str, Any],
) -> None:
    operation = document["paths"][
        "/api/uploads/multipart-sessions/{session_id}/parts/{part_number}/url"
    ]["post"]
    parameters = operation["parameters"]
    part_parameter = next(
        parameter for parameter in parameters if parameter["name"] == "part_number"
    )
    assert part_parameter["required"] is True
    schema = part_parameter["schema"]
    assert schema["type"] == "integer"
    assert schema["minimum"] == 1
    assert schema["maximum"] == 13


def test_the_create_request_schema_admits_no_identity_selector(
    document: dict[str, Any],
) -> None:
    schema_name = "MultipartSessionCreateRequest"
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


def test_every_multipart_response_references_a_strict_named_schema(
    document: dict[str, Any],
) -> None:
    schemas = document["components"]["schemas"]
    envelope_names = {
        "ApiEnvelope_MultipartSessionPlanData_",
        "ApiEnvelope_MultipartSessionStatusData_",
        "ApiEnvelope_MultipartPartUrlData_",
        "ApiEnvelope_MultipartCompletionData_",
    }
    for name in envelope_names:
        assert name in schemas, name
        assert schemas[name]["additionalProperties"] is False, name
        assert schemas[name]["properties"]["data"].get("anyOf") is not None, name
    for path, method in MULTIPART_OPERATION_IDS:
        operation = document["paths"][path][method]
        assert "422" not in operation["responses"], (path, method)


def test_the_url_response_schema_is_the_sole_url_carrying_component(
    document: dict[str, Any],
) -> None:
    schemas = document["components"]["schemas"]
    url_schema = schemas["MultipartPartUrlData"]
    assert url_schema["additionalProperties"] is False
    assert set(url_schema["properties"]) == {
        "part_number",
        "offset_bytes",
        "size_bytes",
        "url",
        "expires_at",
    }
    for schema_name in (
        "MultipartSessionPlanData",
        "MultipartSessionStatusData",
        "MultipartCompletionData",
        "SmallFileTerminalResultData",
    ):
        properties = schemas[schema_name]["properties"]
        assert "url" not in properties, schema_name
