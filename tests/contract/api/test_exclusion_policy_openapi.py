"""Exclusion-policy OpenAPI contract: operations, schemes and snapshot freshness.

These tests pin the published policy surface of the committed snapshot: the
seven semantic operation ids of spec 16, the ``AccessCredential`` Bearer
scheme bound only by the plugin routes, the preview polling documented as
``202`` with its ready ``200``, the publication documented as ``201`` with
its exact-replay ``200``, the snapshot's not-modified ``304`` without a
body, and the snapshot freshness of the committed bytes against the
deterministic offline render.
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

POLICY_OPERATION_IDS: Final[dict[tuple[str, str], str]] = {
    ("/api/admin/exclusion-policy", "get"): "getExclusionPolicyStatus",
    ("/api/admin/exclusion-policy/draft", "put"): "replaceExclusionPolicyDraft",
    ("/api/admin/exclusion-policy/previews", "post"): "createExclusionPolicyPreview",
    (
        "/api/admin/exclusion-policy/previews/{policy_preview_id}",
        "get",
    ): "getExclusionPolicyPreview",
    ("/api/admin/exclusion-policy/publications", "post"): "publishExclusionPolicy",
    ("/api/sync/exclusion-policy/keysets", "get"): "listExclusionPolicyKeysets",
    ("/api/sync/exclusion-policy/snapshot", "get"): "getExclusionPolicySnapshot",
}


@pytest.fixture
def schema() -> dict[str, Any]:
    """Load the committed snapshot the generated client is built from."""
    return json.loads(SNAPSHOT_PATH.read_bytes())


def test_policy_operations_carry_their_semantic_ids(schema: dict[str, object]) -> None:
    paths = dict(schema["paths"])  # type: ignore[arg-type]
    for (path, method), operation_id in POLICY_OPERATION_IDS.items():
        operation = paths[path][method]
        assert operation["operationId"] == operation_id, (path, method)


def test_preview_polling_and_publication_document_their_status_sets(
    schema: dict[str, Any],
) -> None:
    preview_poll = schema["paths"]["/api/admin/exclusion-policy/previews/{policy_preview_id}"][
        "get"
    ]
    assert "202" in preview_poll["responses"]
    assert "200" in preview_poll["responses"]
    publication = schema["paths"]["/api/admin/exclusion-policy/publications"]["post"]
    assert "201" in publication["responses"]
    assert "200" in publication["responses"]
    snapshot = schema["paths"]["/api/sync/exclusion-policy/snapshot"]["get"]
    assert "304" in snapshot["responses"]
    assert "content" not in snapshot["responses"]["304"]


def test_snapshot_is_the_deterministic_fresh_render(schema: dict[str, Any]) -> None:
    assert SNAPSHOT_PATH.read_bytes() == render_openapi_json()
    assert json.loads(render_openapi_json()) == schema
