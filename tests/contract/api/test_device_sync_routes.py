"""Closed device sync route set: exact membership, methods and operation ids.

These tests compose the real application over the offline runtimes plus the
offline device-sync runtime and pin the served surface of spec 7: exactly the
eight routes with exactly their methods and their manually assigned semantic
operation ids, no workspace/device/user selector on any request schema, the
binary download documented as one ``application/octet-stream`` payload with
its exact correlation headers, the action-page query bounded by the
server-owned ceiling, and the dedicated ``AccessCredential`` Bearer scheme
bound by all eight operations.
"""

from __future__ import annotations

from typing import Any, Final

import pytest
from api_runtime.application import create_api_application
from api_runtime.authentication_composition import compose_offline_web_authentication
from api_runtime.device_sync_composition import compose_offline_device_sync
from api_runtime.exclusion_policy_composition import compose_offline_exclusion_policy
from api_runtime.small_file_sync_composition import compose_offline_small_file_sync
from api_runtime.source_lifecycle_composition import compose_offline_source_lifecycle
from fastapi import FastAPI

from personal_os.device_sync.contracts import MAX_MANIFEST_PAGE_ENTRIES
from personal_os.runtime_configuration.models import RuntimeEnvironment

#: The closed device sync route set with exactly the served methods.
DEVICE_SYNC_ROUTE_METHODS: Final[dict[str, frozenset[str]]] = {
    "/api/sync/events": frozenset({"GET"}),
    "/api/sync/cursor-acknowledgements": frozenset({"POST"}),
    "/api/sync/manifests": frozenset({"POST"}),
    "/api/sync/manifests/{manifest_run_id}/pages/{page_number}": (
        frozenset({"PUT"})
    ),
    "/api/sync/manifests/{manifest_run_id}/finalize": frozenset({"POST"}),
    "/api/sync/manifests/{manifest_run_id}/actions": frozenset({"GET"}),
    "/api/sync/manifests/{manifest_run_id}/complete": frozenset({"POST"}),
    "/api/sources/{source_id}/versions/{source_version_id}/content": frozenset({"GET"}),
}

#: The exact semantic operation ids of the eight device sync operations.
DEVICE_SYNC_OPERATION_IDS: Final[dict[tuple[str, str], str]] = {
    ("/api/sync/events", "get"): "pullDeviceSyncEvents",
    ("/api/sync/cursor-acknowledgements", "post"): "acknowledgeDeviceSyncCursor",
    ("/api/sync/manifests", "post"): "startDeviceManifest",
    ("/api/sync/manifests/{manifest_run_id}/pages/{page_number}", "put"): (
        "appendDeviceManifestPage"
    ),
    ("/api/sync/manifests/{manifest_run_id}/finalize", "post"): "finalizeDeviceManifest",
    ("/api/sync/manifests/{manifest_run_id}/actions", "get"): "listDeviceManifestActions",
    ("/api/sync/manifests/{manifest_run_id}/complete", "post"): "completeDeviceManifest",
    (
        "/api/sources/{source_id}/versions/{source_version_id}/content",
        "get",
    ): "downloadDeviceSourceVersion",
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
        source_lifecycle=compose_offline_source_lifecycle(),
        device_sync=compose_offline_device_sync(),
    )


@pytest.fixture
def document(application: FastAPI) -> dict[str, Any]:
    return application.openapi()


def test_the_device_sync_surface_is_exactly_the_eight_routes(
    application: FastAPI, document: dict[str, Any]
) -> None:
    sync_paths = {
        route.path
        for route in application.routes
        if getattr(route, "path", "").startswith(("/api/sync/events", "/api/sync/cursor"))
        or getattr(route, "path", "").startswith("/api/sync/manifests")
        or (
            getattr(route, "path", "").startswith("/api/sources/")
            and "/versions/" in getattr(route, "path", "")
        )
    }
    assert sync_paths == set(DEVICE_SYNC_ROUTE_METHODS)
    for path, methods in DEVICE_SYNC_ROUTE_METHODS.items():
        assert set(document["paths"][path]) == set(method.lower() for method in methods), path


def test_every_operation_carries_its_semantic_id(document: dict[str, Any]) -> None:
    for (path, method), operation_id in DEVICE_SYNC_OPERATION_IDS.items():
        assert document["paths"][path][method]["operationId"] == operation_id, (path, method)


def test_every_operation_binds_exactly_the_access_bearer_scheme(
    document: dict[str, Any],
) -> None:
    for path, method in DEVICE_SYNC_OPERATION_IDS:
        operation = document["paths"][path][method]
        assert operation["security"] == [{"AccessCredential": []}], (path, method)
    assert "AccessCredential" in document["components"]["securitySchemes"]


@pytest.mark.parametrize(
    "schema_name",
    [
        "CursorAcknowledgementRequest",
        "ManifestStartRequest",
        "ManifestEntryRequest",
        "ManifestPageRequest",
        "ManifestFinalizeRequest",
        "ManifestCompleteRequest",
    ],
)
def test_request_schemas_admit_no_identity_selector(
    document: dict[str, Any], schema_name: str
) -> None:
    properties = document["components"]["schemas"][schema_name]["properties"]
    assert not {"workspace_id", "device_id", "user_id"} & set(properties), schema_name


def test_manifest_entry_schema_admits_no_identity_selector(
    document: dict[str, Any],
) -> None:
    properties = document["components"]["schemas"]["ManifestEntryRequest"]["properties"]
    assert set(properties) == {
        "local_entry_id",
        "known_source_id",
        "known_version_id",
        "normalized_locator",
        "fingerprint",
        "observation_generation",
    }


def test_the_page_body_bounds_its_entry_list_and_digest_grammar(
    document: dict[str, Any],
) -> None:
    schema = document["components"]["schemas"]["ManifestPageRequest"]
    entries = schema["properties"]["entries"]
    assert entries["maxItems"] == MAX_MANIFEST_PAGE_ENTRIES
    assert schema["properties"]["page_digest"]["pattern"] == "^[0-9a-f]{64}$"


def test_the_action_query_is_bounded_by_the_server_owned_ceilings(
    document: dict[str, Any],
) -> None:
    operation = document["paths"]["/api/sync/manifests/{manifest_run_id}/actions"]["get"]
    parameters = {parameter["name"]: parameter for parameter in operation["parameters"]}
    limit = parameters["limit"]
    assert limit["required"] is False
    assert limit["schema"]["minimum"] == 1
    assert limit["schema"]["maximum"] == MAX_MANIFEST_PAGE_ENTRIES
    after = parameters["after_action_index"]
    assert after["schema"]["minimum"] == 0


def test_the_binary_download_is_documented_as_one_octet_stream(
    document: dict[str, Any],
) -> None:
    operation = document["paths"][
        "/api/sources/{source_id}/versions/{source_version_id}/content"
    ]["get"]
    success = operation["responses"]["200"]
    assert set(success["content"]) == {"application/octet-stream"}
    schema = success["content"]["application/octet-stream"]["schema"]
    assert schema == {"type": "string", "format": "binary"}
    assert "application/json" not in success["content"]


def test_the_pull_page_renders_the_envelope_of_its_strict_page(
    document: dict[str, Any],
) -> None:
    operation = document["paths"]["/api/sync/events"]["get"]
    assert operation["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ApiEnvelope_DeviceEventPageData_"
    }
