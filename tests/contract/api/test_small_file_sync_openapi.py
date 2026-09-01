"""Small-file sync OpenAPI contract: the committed snapshot surface.

These tests pin the published sync surface of the committed snapshot the
generated TypeScript client is built from: the two semantic operation ids
with exactly their served methods, the ``AccessCredential`` Bearer scheme
bound by both operations, the closed response schemas — the preflight data
carrying exactly the members its union of outcomes admits and the terminal
result exactly the five safe receipt members — and the hygiene that no
sync operation documents a receipt, object key or provider detail. The
snapshot itself must stay the deterministic fresh render of the offline
export so the client and the served schema cannot drift.
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

SYNC_OPERATION_IDS: Final[dict[tuple[str, str], str]] = {
    ("/api/sync/journal-events/preflight", "post"): "preflightJournalEventUpload",
    ("/api/uploads/{operation_id}/content", "put"): "uploadSmallFileContent",
}

#: The exact members the preflight data schema may expose (spec 10.1 and the
#: Child 8 conflict bridge): one typed outcome plus at most the upload
#: handle/expiry, the frozen result, or the replayed conflict identity.
PREFLIGHT_DATA_MEMBERS: Final[frozenset[str]] = frozenset(
    {"outcome", "operation_id", "expires_at", "result", "conflict_id"}
)

#: The exact members of the safe canonical receipt (spec 10.3).
TERMINAL_RESULT_MEMBERS: Final[frozenset[str]] = frozenset(
    {"result_kind", "source_id", "source_version_id", "content_version", "committed_at"}
)

#: Substrings no small-file sync schema or description may carry.
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


def test_sync_operations_carry_their_semantic_ids_and_methods(
    schema: dict[str, Any],
) -> None:
    paths = dict(schema["paths"])
    for (path, method), operation_id in SYNC_OPERATION_IDS.items():
        assert set(paths[path]) == {method}, path
        assert paths[path][method]["operationId"] == operation_id, (path, method)


def test_both_sync_operations_bind_exactly_the_access_bearer_scheme(
    schema: dict[str, Any],
) -> None:
    for path, method in SYNC_OPERATION_IDS:
        operation = schema["paths"][path][method]
        assert operation["security"] == [{"AccessCredential": []}], (path, method)
        assert "AccessCredential" in schema["components"]["securitySchemes"]


def test_preflight_data_schema_is_closed_over_its_outcome_members(
    schema: dict[str, Any],
) -> None:
    preflight_data = schema["components"]["schemas"]["SmallFilePreflightData"]
    assert set(preflight_data["properties"]) == PREFLIGHT_DATA_MEMBERS
    assert preflight_data["additionalProperties"] is False
    assert (
        preflight_data["properties"]["outcome"]["$ref"]
        == "#/components/schemas/SmallFilePreflightOutcome"
    )
    outcome_enum = schema["components"]["schemas"]["SmallFilePreflightOutcome"]["enum"]
    assert set(outcome_enum) == {
        "single_part_upload",
        "multipart_upload",
        "committed_replay",
        "no_change",
        "excluded",
        "conflict",
    }


def test_terminal_result_schema_carries_only_the_safe_receipt_members(
    schema: dict[str, Any],
) -> None:
    terminal = schema["components"]["schemas"]["SmallFileTerminalResultData"]
    assert set(terminal["properties"]) == TERMINAL_RESULT_MEMBERS
    assert terminal["additionalProperties"] is False
    assert (
        terminal["properties"]["result_kind"]["$ref"]
        == "#/components/schemas/SmallFileTerminalResultKind"
    )
    result_kind_enum = schema["components"]["schemas"]["SmallFileTerminalResultKind"]["enum"]
    assert set(result_kind_enum) == {"committed", "no_change"}


def _sync_property_names(schema: dict[str, Any]) -> set[str]:
    """Every field name the sync schemas and operations expose."""

    names: set[str] = set()
    for schema_name in (
        "SmallFilePreflightRequest",
        "SmallFilePreflightData",
        "SmallFileTerminalResultData",
    ):
        names |= set(schema["components"]["schemas"][schema_name]["properties"])
    for path, method in SYNC_OPERATION_IDS:
        operation = schema["paths"][path][method]
        names |= {parameter["name"] for parameter in operation.get("parameters", [])}
    return names


def test_no_sync_surface_documents_receipt_object_or_provider_detail(
    schema: dict[str, Any],
) -> None:
    """Hygiene over exposed names: no receipt, object key or provider field.

    The scan runs over every exposed member name — request fields, response
    fields and path parameters — because prose descriptions legitimately say
    that no receipt ever crosses the wire.
    """

    for name in _sync_property_names(schema):
        lowered = name.lower()
        for marker in FORBIDDEN_MARKERS:
            assert marker not in lowered, name


def test_snapshot_is_the_deterministic_fresh_render(schema: dict[str, Any]) -> None:
    assert SNAPSHOT_PATH.read_bytes() == render_openapi_json()
    assert json.loads(render_openapi_json()) == schema
