"""Multipart upload OpenAPI contract: the committed snapshot surface.

These tests pin the published multipart surface of the committed snapshot
the generated TypeScript client is built from: the five semantic operation
ids of spec §5 with exactly their served methods, the ``AccessCredential``
Bearer scheme bound by every operation, the closed response schemas —
exactly one URL-bearing model, and no private staging/provider identity
anywhere in the document — and the deterministic fresh-render guarantee
that keeps the client and the served schema from drifting. The snapshot
itself is byte-compared against the offline export so a regeneration that
changes any route shape must land as an explicit snapshot change.
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
    ("/api/uploads/multipart-sessions/{session_id}/abort", "post"): "abortMultipartUploadSession",
}

#: The exact members the part-URL data schema exposes: the sole signed-URL
#: surface of the API, beside its exact derived byte window.
PART_URL_DATA_MEMBERS: Final[frozenset[str]] = frozenset(
    {"part_number", "offset_bytes", "size_bytes", "url", "expires_at"}
)

#: Markers of private staging/provider identity no multipart schema,
#: description or example may ever carry (spec §4.1/§7).
FORBIDDEN_FIELD_MARKERS: Final[tuple[str, ...]] = (
    "provider_upload_id",
    "staging_key",
    "upload_id",
    "etag",
    "signature",
    "X-Amz",
    "provider",
)


@pytest.fixture
def document() -> dict[str, Any]:
    """Load the committed snapshot the generated client is built from."""

    return json.loads(SNAPSHOT_PATH.read_bytes())


def _operation(document: dict[str, Any], path: str, method: str) -> dict[str, Any]:
    operation = document["paths"][path][method]
    assert isinstance(operation, dict)
    return operation


def test_openapi_exposes_only_safe_multipart_response_fields(document: dict[str, Any]) -> None:
    text = json.dumps(document)
    assert "provider_upload_id" not in text
    assert "staging_key" not in text
    assert "completeMultipartUploadSession" in text


def test_document_carries_exactly_the_five_multipart_operations(
    document: dict[str, Any],
) -> None:
    for (path, method), operation_id in MULTIPART_OPERATION_IDS.items():
        operation = _operation(document, path, method)
        assert operation["operationId"] == operation_id
        assert operation["security"] == [{"AccessCredential": []}], operation_id


def test_no_other_multipart_operation_exists(document: dict[str, Any]) -> None:
    operation_ids = {
        operation["operationId"]
        for path_item in document["paths"].values()
        for operation in path_item.values()
        if isinstance(operation, dict) and "operationId" in operation
    }
    multipart_ids = set(MULTIPART_OPERATION_IDS.values())
    assert multipart_ids <= operation_ids
    assert not any(
        operation_id.startswith("multipart") and operation_id not in multipart_ids
        for operation_id in operation_ids
    )


def test_every_multipart_operation_exposes_only_safe_field_names(
    document: dict[str, Any],
) -> None:
    """No private staging/provider identity appears as a wire field name.

    The strict whole-document scan of the snapshot test above proves no
    ``provider_upload_id``/``staging_key`` text exists at all; this pin
    walks every multipart operation's parameter names and response schema
    property names (resolving ``$ref`` into components) so no future
    response model can grow an upload ID, ETag, signature or other
    provider-identity member.
    """

    schemas = document["components"]["schemas"]

    def _property_names(schema: dict[str, Any]) -> set[str]:
        if "$ref" in schema:
            referenced = schemas[schema["$ref"].split("/")[-1]]
            assert isinstance(referenced, dict)
            return _property_names(referenced)
        names = set(schema.get("properties", {}))
        for nested in schema.get("properties", {}).values():
            if isinstance(nested, dict) and "$ref" in nested:
                names |= _property_names(nested)
        return names

    for (path, method), operation_id in MULTIPART_OPERATION_IDS.items():
        operation = _operation(document, path, method)
        field_names = {
            parameter["name"]
            for parameter in operation.get("parameters", [])
            if isinstance(parameter, dict)
        }
        for response in operation["responses"].values():
            if not isinstance(response, dict):
                continue
            for schema in response.get("content", {}).values():
                if isinstance(schema, dict):
                    field_names |= _property_names(schema)
        for forbidden in FORBIDDEN_FIELD_MARKERS:
            assert not any(forbidden in field_name for field_name in field_names), (
                operation_id,
                forbidden,
            )


def test_the_part_url_model_is_the_sole_url_bearing_schema(document: dict[str, Any]) -> None:
    schemas = document["components"]["schemas"]
    part_url = schemas["MultipartPartUrlData"]
    properties = set(part_url["properties"])
    assert properties == PART_URL_DATA_MEMBERS
    url_bearing = {
        name
        for name, schema in schemas.items()
        if isinstance(schema, dict) and "url" in schema.get("properties", {})
    }
    assert "MultipartPartUrlData" in url_bearing
    for multipart_schema in (
        "MultipartSessionPlanData",
        "MultipartSessionStatusData",
        "MultipartCompletionData",
    ):
        assert multipart_schema not in url_bearing
        assert multipart_schema in schemas


def test_snapshot_is_the_deterministic_fresh_render(document: dict[str, Any]) -> None:
    assert SNAPSHOT_PATH.read_bytes() == render_openapi_json()
    assert json.loads(render_openapi_json()) == document
