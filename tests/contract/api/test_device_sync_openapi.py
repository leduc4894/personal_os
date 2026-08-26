"""Device sync OpenAPI contract: the committed snapshot surface.

These tests pin the published device sync surface of the committed snapshot
the generated TypeScript client is built from: all eight semantic operation
ids with exactly their served methods, the strict device sync schemas closed
against extra properties, the closed enum vocabularies of the run state and
the action kind, and the hygiene that no device sync surface documents a
receipt, object key or provider detail. The snapshot itself must stay the
deterministic fresh render of the offline export so the client and the
served schema cannot drift.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final

import pytest
from api_runtime.openapi_export import render_openapi_json

SNAPSHOT_PATH: Final[Path] = (
    Path(__file__).resolve().parents[3] / "packages" / "api-client" / "openapi.json"
)

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

#: The strict device sync schemas the snapshot must carry.
DEVICE_SYNC_SCHEMA_NAMES: Final[tuple[str, ...]] = (
    "CursorAcknowledgementRequest",
    "DeviceCursorReceiptData",
    "DeviceEventPageData",
    "DeviceSyncEventData",
    "ManifestActionData",
    "ManifestActionPageData",
    "ManifestCompleteRequest",
    "ManifestEntryRequest",
    "ManifestFinalizeRequest",
    "ManifestPageReceiptData",
    "ManifestPageRequest",
    "ManifestRunReceiptData",
    "ManifestStartRequest",
    "SourceFingerprintData",
    "ApiEnvelope_DeviceEventPageData_",
    "ApiEnvelope_DeviceCursorReceiptData_",
    "ApiEnvelope_ManifestRunReceiptData_",
    "ApiEnvelope_ManifestPageReceiptData_",
    "ApiEnvelope_ManifestActionPageData_",
)

#: Substrings no device sync schema or operation name may carry.
FORBIDDEN_MARKERS: Final[tuple[str, ...]] = (
    "receipt",
    "object_key",
    "provider",
    "bucket",
    "etag",
    "presign",
    "callback",
)


@pytest.fixture
def schema() -> dict[str, Any]:
    """Load the committed snapshot the generated client is built from."""

    return json.loads(SNAPSHOT_PATH.read_bytes())


def test_every_device_sync_operation_id_is_published(schema: dict[str, Any]) -> None:
    for (path, method), operation_id in DEVICE_SYNC_OPERATION_IDS.items():
        assert schema["paths"][path][method]["operationId"] == operation_id, (path, method)


def test_every_device_sync_schema_is_strict_and_present(schema: dict[str, Any]) -> None:
    schemas = schema["components"]["schemas"]
    for schema_name in DEVICE_SYNC_SCHEMA_NAMES:
        assert schema_name in schemas, schema_name
        assert schemas[schema_name]["additionalProperties"] is False, schema_name


def test_the_run_state_and_action_kinds_publish_their_closed_enums(
    schema: dict[str, Any],
) -> None:
    schemas = schema["components"]["schemas"]
    assert set(schemas["ManifestRunState"]["enum"]) == {
        "collecting",
        "planned",
        "applying",
        "completed",
        "expired",
        "failed",
    }
    assert set(schemas["ManifestActionKind"]["enum"]) == {
        "upload",
        "download",
        "apply_tombstone",
        "conflict",
        "no_change",
        "excluded",
    }
    assert set(schemas["DeviceEventType"]["enum"]) == {
        "created",
        "updated",
        "renamed",
        "moved",
        "deleted",
        "restored",
    }


def _device_sync_property_names(schema: dict[str, Any]) -> set[str]:
    """Every field name the device sync schemas and operations expose."""

    names: set[str] = set()
    for schema_name in DEVICE_SYNC_SCHEMA_NAMES:
        if schema_name.startswith("ApiEnvelope_"):
            continue
        names |= set(schema["components"]["schemas"][schema_name]["properties"])
    for path, method in DEVICE_SYNC_OPERATION_IDS:
        operation = schema["paths"][path][method]
        names |= {parameter["name"] for parameter in operation.get("parameters", [])}
    return names


def test_no_device_sync_surface_documents_receipt_object_or_provider_detail(
    schema: dict[str, Any],
) -> None:
    for name in _device_sync_property_names(schema):
        lowered = name.lower()
        for marker in FORBIDDEN_MARKERS:
            assert marker not in lowered, name


def test_snapshot_is_the_deterministic_fresh_render(schema: dict[str, Any]) -> None:
    assert SNAPSHOT_PATH.read_bytes() == render_openapi_json()
    assert json.loads(render_openapi_json()) == schema
