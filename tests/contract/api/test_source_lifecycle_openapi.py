"""Source lifecycle OpenAPI contract: operation, scheme, enums and snapshot freshness.

These tests pin the committed source-lifecycle surface of the snapshot the
generated TypeScript client is built from: the exact
``commitSourceLifecycleEvent`` operation id with the single
``AccessCredential`` Bearer scheme, the closed ``LifecycleOperationValue``
and ``LifecycleStateValue`` enums, the request schema admitting no
workspace/device/user selector, the deterministic response envelope and the
snapshot freshness against the deterministic offline render.
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
GENERATED_SCHEMA_PATH: Final[Path] = SNAPSHOT_PATH.parent / "src" / "generated" / "schema.ts"

LIFECYCLE_PATH: Final[str] = "/api/sources/lifecycle-events"
LIFECYCLE_OPERATION_ID: Final[str] = "commitSourceLifecycleEvent"

OPERATION_VALUES: Final[frozenset[str]] = frozenset({"rename", "move", "delete", "restore"})
STATE_VALUES: Final[frozenset[str]] = frozenset({"active", "deleted"})
REQUEST_MEMBERS: Final[frozenset[str]] = frozenset(
    {
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
)
RESPONSE_MEMBERS: Final[frozenset[str]] = frozenset(
    {
        "source_id",
        "source_version_id",
        "event_id",
        "event_sequence",
        "state",
        "tombstone_id",
        "resulting_locator",
        "committed_at",
    }
)
FORBIDDEN_REQUEST_SELECTORS: Final[frozenset[str]] = frozenset(
    {"workspace_id", "device_id", "user_id", "signature", "actor_user_id"}
)


@pytest.fixture
def schema() -> dict[str, Any]:
    """Load the committed snapshot the generated client is built from."""

    return json.loads(SNAPSHOT_PATH.read_bytes())


def test_lifecycle_operation_carries_the_semantic_id_and_method(schema: dict[str, Any]) -> None:
    operation = schema["paths"][LIFECYCLE_PATH]["post"]
    assert operation["operationId"] == LIFECYCLE_OPERATION_ID


def test_lifecycle_operation_binds_exactly_the_access_bearer_scheme(
    schema: dict[str, Any],
) -> None:
    operation = schema["paths"][LIFECYCLE_PATH]["post"]
    assert operation["security"] == [{"AccessCredential": []}]


def test_lifecycle_request_schema_is_closed_over_documented_members(
    schema: dict[str, Any],
) -> None:
    request_schema = schema["components"]["schemas"]["SourceLifecycleEventRequest"]
    assert request_schema["additionalProperties"] is False
    assert set(request_schema["properties"]) == REQUEST_MEMBERS


def test_lifecycle_request_schema_rejects_identity_selectors(schema: dict[str, Any]) -> None:
    request_schema = schema["components"]["schemas"]["SourceLifecycleEventRequest"]
    forbidden = set(request_schema["properties"]) & FORBIDDEN_REQUEST_SELECTORS
    assert not forbidden, forbidden


def test_lifecycle_locator_wire_fields_interoperate_with_generated_client(
    schema: dict[str, Any],
) -> None:
    request_schema = schema["components"]["schemas"]["SourceLifecycleEventRequest"]
    for member in ("expected_locator", "target_locator"):
        alternatives = request_schema["properties"][member]["anyOf"]
        assert {alternative.get("type") for alternative in alternatives} == {
            "string",
            "null",
        }
        assert all("$ref" not in alternative for alternative in alternatives)

    generated_schema = GENERATED_SCHEMA_PATH.read_text(encoding="utf-8")
    assert "readonly expected_locator?: string | null;" in generated_schema
    assert "readonly target_locator?: string | null;" in generated_schema


def test_lifecycle_operation_enum_is_closed(schema: dict[str, Any]) -> None:
    enum = schema["components"]["schemas"]["LifecycleOperation"]
    assert enum["type"] == "string"
    assert set(enum["enum"]) == OPERATION_VALUES


def test_lifecycle_state_enum_is_closed(schema: dict[str, Any]) -> None:
    enum = schema["components"]["schemas"]["LifecycleState"]
    assert enum["type"] == "string"
    assert set(enum["enum"]) == STATE_VALUES


def test_lifecycle_response_schema_is_closed_over_safe_receipt_members(
    schema: dict[str, Any],
) -> None:
    response_schema = schema["components"]["schemas"]["SourceLifecycleCommitData"]
    assert response_schema["additionalProperties"] is False
    assert set(response_schema["properties"]) == RESPONSE_MEMBERS
    state_property = response_schema["properties"]["state"]
    assert state_property["$ref"].endswith("/LifecycleState")


def test_snapshot_is_the_deterministic_fresh_render(schema: dict[str, Any]) -> None:
    assert SNAPSHOT_PATH.read_bytes() == render_openapi_json()
    assert json.loads(render_openapi_json()) == schema
