"""Committed OpenAPI snapshot contract for the generated API client surface.

The snapshot under ``packages/api-client/openapi.json`` is the canonical input
for the generated TypeScript client. These tests fail whenever the composed
application drifts from the committed snapshot, when a response stops
referencing a named component schema, when an operation loses its explicit id,
when machine values (timestamps, hostnames, filesystem paths) leak into the
document, or when a strict Pydantic model stops closing its schema against
extra properties.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from api_runtime.openapi_export import render_openapi_json

from personal_os.package_metadata import distribution_version

SNAPSHOT_PATH = Path(__file__).resolve().parents[3] / "packages" / "api-client" / "openapi.json"

#: The closed route set with its canonical operation ids, keyed by path.
HEALTH_OPERATION_IDS: dict[str, str] = {
    "/api/health/live": "getApiLiveness",
    "/api/health/ready": "getApiReadiness",
}

#: Component schema names emitted for every frozen ``extra="forbid"`` model.
STRICT_MODEL_SCHEMA_NAMES: tuple[str, ...] = (
    "ApiEnvelope_LivenessData_",
    "ApiEnvelope_ReadinessData_",
    "ApiErrorBody",
    "ApiWarning",
    "LivenessData",
    "ReadinessData",
    "ReadinessChecks",
)

_URL_PATTERN = re.compile(r"\w+://")
_HOSTNAME_PATTERN = re.compile(r"\blocalhost\b|\b(?:\d{1,3}\.){3}\d{1,3}\b", re.IGNORECASE)
_DOMAIN_NAME_PATTERN = re.compile(
    r"\b[a-z0-9-]+\.(?:com|net|org|io|dev|local|internal)\b", re.IGNORECASE
)
_DATETIME_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")
_WINDOWS_DRIVE_PATTERN = re.compile(r"\b[A-Za-z]:[\\/]")
_BACKSLASH_PATTERN = re.compile(r"\\")

_FORBIDDEN_VALUE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("url", _URL_PATTERN),
    ("hostname or address", _HOSTNAME_PATTERN),
    ("domain name", _DOMAIN_NAME_PATTERN),
    ("timestamp", _DATETIME_PATTERN),
    ("windows drive path", _WINDOWS_DRIVE_PATTERN),
    ("backslash path", _BACKSLASH_PATTERN),
)


@pytest.fixture
def snapshot_document() -> dict[str, Any]:
    """Load the committed snapshot the generated client is built from."""
    return json.loads(SNAPSHOT_PATH.read_bytes())


def iter_document_strings(value: Any) -> Iterator[str]:
    """Yield every key and string value in the parsed document tree."""
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str):
                yield key
            yield from iter_document_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from iter_document_strings(item)
    elif isinstance(value, str):
        yield value


def test_committed_snapshot_is_byte_identical_to_fresh_render() -> None:
    assert SNAPSHOT_PATH.read_bytes() == render_openapi_json()


def test_document_header_and_route_set_are_pinned(snapshot_document: dict[str, Any]) -> None:
    assert snapshot_document["openapi"] == "3.1.0"
    assert snapshot_document["info"] == {
        "title": "Personal Knowledge API",
        "version": distribution_version(),
    }
    assert "servers" not in snapshot_document
    paths = snapshot_document["paths"]
    assert set(paths) == set(HEALTH_OPERATION_IDS)
    for path, operation_id in HEALTH_OPERATION_IDS.items():
        operations = paths[path]
        assert set(operations) == {"get"}, path
        assert operations["get"]["operationId"] == operation_id, path


def test_every_response_references_a_named_component_schema(
    snapshot_document: dict[str, Any],
) -> None:
    schemas = snapshot_document["components"]["schemas"]
    for path, operations in snapshot_document["paths"].items():
        for method, operation in operations.items():
            assert isinstance(operation.get("operationId"), str) and operation["operationId"], (
                path,
                method,
            )
            for status, response in operation["responses"].items():
                schema = response["content"]["application/json"]["schema"]
                assert set(schema) == {"$ref"}, (path, method, status)
                reference = schema["$ref"]
                assert reference.startswith("#/components/schemas/"), (path, method, status)
                assert reference.removeprefix("#/components/schemas/") in schemas, (
                    path,
                    method,
                    status,
                )


def test_document_carries_no_machine_values(snapshot_document: dict[str, Any]) -> None:
    rendered_strings = list(iter_document_strings(snapshot_document))
    assert rendered_strings
    for value in rendered_strings:
        for label, pattern in _FORBIDDEN_VALUE_PATTERNS:
            assert pattern.search(value) is None, (label, value)


def test_strict_model_schemas_close_extra_properties(
    snapshot_document: dict[str, Any],
) -> None:
    schemas = snapshot_document["components"]["schemas"]
    for schema_name in STRICT_MODEL_SCHEMA_NAMES:
        assert schema_name in schemas, schema_name
        assert schemas[schema_name]["additionalProperties"] is False, schema_name
