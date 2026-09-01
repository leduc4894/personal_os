"""Source conflict OpenAPI contract: the committed snapshot surface.

These tests pin the published Conflict Inbox surface of the committed
snapshot the generated TypeScript client is built from: all four semantic
operation ids with exactly their served methods, the strict source conflict
schemas closed against extra properties, the closed enum vocabularies of
the conflict kind, status, candidate kind, resolution kind, resolution
outcome and evidence role, the resolve request admitting no raw-bytes or
identity-selector member, and the hygiene that no source conflict surface
documents a receipt, object key, digest or provider detail. The snapshot
itself must stay the deterministic fresh render of the offline export so
the client and the served schema cannot drift.
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

SOURCE_CONFLICT_OPERATION_IDS: Final[dict[tuple[str, str], str]] = {
    ("/api/sync/conflicts", "get"): "listSourceConflicts",
    ("/api/sync/conflicts/{conflict_id}", "get"): "getSourceConflict",
    ("/api/sync/conflicts/{conflict_id}/evidence/{role}", "get"): (
        "downloadSourceConflictEvidence"
    ),
    ("/api/sync/conflicts/{conflict_id}/resolve", "post"): "resolveSourceConflict",
}

#: The strict source conflict schemas the snapshot must carry.
SOURCE_CONFLICT_SCHEMA_NAMES: Final[tuple[str, ...]] = (
    "SourceConflictData",
    "SourceConflictDetailData",
    "SourceConflictPageData",
    "SourceConflictResolveRequest",
    "SourceConflictResolutionData",
    "ApiEnvelope_SourceConflictPageData_",
    "ApiEnvelope_SourceConflictDetailData_",
    "ApiEnvelope_SourceConflictResolutionData_",
)

#: Substrings no source conflict schema or operation name may carry.
FORBIDDEN_MARKERS: Final[tuple[str, ...]] = (
    "receipt",
    "object_key",
    "provider",
    "bucket",
    "etag",
    "presign",
    "callback",
    "digest",
    "sha256",
    "secret",
    "raw",
    "bytes",
)


@pytest.fixture
def schema() -> dict[str, Any]:
    """Load the committed snapshot the generated client is built from."""

    return json.loads(SNAPSHOT_PATH.read_bytes())


def test_every_source_conflict_operation_id_is_published(schema: dict[str, Any]) -> None:
    for (path, method), operation_id in SOURCE_CONFLICT_OPERATION_IDS.items():
        assert schema["paths"][path][method]["operationId"] == operation_id, (path, method)


def test_every_source_conflict_schema_is_strict_and_present(schema: dict[str, Any]) -> None:
    schemas = schema["components"]["schemas"]
    for schema_name in SOURCE_CONFLICT_SCHEMA_NAMES:
        assert schema_name in schemas, schema_name
        assert schemas[schema_name]["additionalProperties"] is False, schema_name


def test_the_conflict_vocabularies_publish_their_closed_enums(
    schema: dict[str, Any],
) -> None:
    schemas = schema["components"]["schemas"]
    assert set(schemas["ConflictKind"]["enum"]) == {
        "stale_content",
        "edit_remote_delete",
        "delete_remote_edit",
        "locator_collision",
    }
    assert set(schemas["ConflictStatus"]["enum"]) == {
        "open",
        "resolving",
        "resolved",
        "superseded",
    }
    assert set(schemas["ConflictCandidateKind"]["enum"]) == {"content", "delete"}
    assert set(schemas["ConflictResolutionKind"]["enum"]) == {
        "keep_remote",
        "keep_local",
        "save_merged",
    }
    assert set(schemas["ConflictResolutionOutcome"]["enum"]) == {
        "resolved",
        "stale_successor",
    }
    assert set(schemas["ConflictEvidenceRole"]["enum"]) == {"base", "remote", "candidate"}
    evidence_operation = schema["paths"]["/api/sync/conflicts/{conflict_id}/evidence/{role}"]["get"]
    role_parameters = [
        parameter for parameter in evidence_operation["parameters"] if parameter["name"] == "role"
    ]
    assert len(role_parameters) == 1
    assert role_parameters[0]["schema"] == {"$ref": "#/components/schemas/ConflictEvidenceRole"}


def test_the_resolve_request_admits_no_raw_bytes_or_identity_selector(
    schema: dict[str, Any],
) -> None:
    properties = schema["components"]["schemas"]["SourceConflictResolveRequest"]["properties"]
    assert set(properties) == {
        "resolution_event_id",
        "idempotency_key",
        "resolution_kind",
        "reviewed_remote_version_id",
        "verified_candidate_object_id",
    }
    for name, member in properties.items():
        lowered = name.lower()
        for marker in FORBIDDEN_MARKERS:
            assert marker not in lowered, name
        rendered = json.dumps(member)
        assert "binary" not in rendered, name


def test_the_evidence_download_documents_exactly_one_octet_stream(
    schema: dict[str, Any],
) -> None:
    operation = schema["paths"]["/api/sync/conflicts/{conflict_id}/evidence/{role}"]["get"]
    success = operation["responses"]["200"]
    assert set(success["content"]) == {"application/octet-stream"}
    schema_entry = success["content"]["application/octet-stream"]["schema"]
    assert schema_entry == {"type": "string", "format": "binary"}


def test_the_listing_documents_its_bounded_pagination_parameters(
    schema: dict[str, Any],
) -> None:
    operation = schema["paths"]["/api/sync/conflicts"]["get"]
    parameters = {parameter["name"]: parameter for parameter in operation["parameters"]}
    assert set(parameters) == {"limit", "exclusive_start_conflict_id"}
    assert parameters["limit"]["required"] is False
    assert parameters["limit"]["schema"]["minimum"] == 1
    assert parameters["limit"]["schema"]["maximum"] == 200
    assert parameters["limit"]["schema"]["default"] == 50
    cursor_schema = parameters["exclusive_start_conflict_id"]["schema"]
    assert cursor_schema["anyOf"][0] == {"format": "uuid", "type": "string"}


def _source_conflict_property_names(schema: dict[str, Any]) -> set[str]:
    """Every field name the source conflict schemas and operations expose."""

    names: set[str] = set()
    for schema_name in SOURCE_CONFLICT_SCHEMA_NAMES:
        if schema_name.startswith("ApiEnvelope_"):
            continue
        names |= set(schema["components"]["schemas"][schema_name]["properties"])
    for path, method in SOURCE_CONFLICT_OPERATION_IDS:
        operation = schema["paths"][path][method]
        names |= {parameter["name"] for parameter in operation.get("parameters", [])}
    return names


def test_no_source_conflict_surface_documents_key_receipt_or_provider_detail(
    schema: dict[str, Any],
) -> None:
    for name in _source_conflict_property_names(schema):
        lowered = name.lower()
        for marker in FORBIDDEN_MARKERS:
            assert marker not in lowered, name


def test_snapshot_is_the_deterministic_fresh_render(schema: dict[str, Any]) -> None:
    assert SNAPSHOT_PATH.read_bytes() == render_openapi_json()
    assert json.loads(render_openapi_json()) == schema
