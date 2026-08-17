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

#: The closed route set with its canonical operation ids and methods.
ROUTE_OPERATION_IDS: dict[str, dict[str, str]] = {
    "/api/health/live": {"get": "getApiLiveness"},
    "/api/health/ready": {"get": "getApiReadiness"},
    "/api/auth/login": {"post": "login"},
    "/api/auth/session": {"get": "getSession"},
    "/api/auth/logout": {"post": "logout"},
    "/api/auth/reauthenticate": {"post": "reauthenticate"},
    "/api/auth/password": {"put": "changePassword"},
    "/api/auth/totp/verify": {"post": "verifyTotpChallenge"},
    "/api/auth/totp/enrollments": {"post": "createTotpEnrollment"},
    "/api/auth/totp/enrollments/{enrollment_id}/verify": {"post": "verifyTotpEnrollment"},
    "/api/auth/totp/recovery": {"post": "startTotpRecovery"},
    "/api/auth/totp/recovery-codes/regenerate": {"post": "regenerateTotpRecoveryCodes"},
    "/api/auth/totp": {"delete": "disableTotp"},
    "/api/auth/device-authorizations": {"post": "createDeviceAuthorization"},
    "/api/auth/device-authorizations/lookup": {"post": "lookupDeviceAuthorization"},
    "/api/auth/device-authorizations/{grant_id}/approve": {"post": "approveDeviceAuthorization"},
    "/api/auth/device-authorizations/{grant_id}/deny": {"post": "denyDeviceAuthorization"},
    "/api/auth/device-authorizations/{grant_id}/poll": {"post": "pollDeviceAuthorization"},
    "/api/auth/device-tokens/refresh": {"post": "refreshDeviceToken"},
    "/api/auth/device-tokens/revoke-current": {"post": "revokeCurrentDeviceToken"},
    "/api/admin/devices": {"get": "listAdminDevices"},
    "/api/admin/devices/{device_id}/revoke": {"post": "revokeAdminDevice"},
    "/api/admin/exclusion-policy": {"get": "getExclusionPolicyStatus"},
    "/api/admin/exclusion-policy/draft": {"put": "replaceExclusionPolicyDraft"},
    "/api/admin/exclusion-policy/previews": {"post": "createExclusionPolicyPreview"},
    "/api/admin/exclusion-policy/previews/{policy_preview_id}": {
        "get": "getExclusionPolicyPreview"
    },
    "/api/admin/exclusion-policy/publications": {"post": "publishExclusionPolicy"},
    "/api/sync/exclusion-policy/keysets": {"get": "listExclusionPolicyKeysets"},
    "/api/sync/exclusion-policy/snapshot": {"get": "getExclusionPolicySnapshot"},
}

#: Component schema names emitted for every frozen ``extra="forbid"`` model.
STRICT_MODEL_SCHEMA_NAMES: tuple[str, ...] = (
    "ApiEnvelope_LivenessData_",
    "ApiEnvelope_ReadinessData_",
    "ApiEnvelope_SessionData_",
    "ApiErrorBody",
    "ApiWarning",
    "LivenessData",
    "LoginRequest",
    "PasswordChangeRequest",
    "ReadinessData",
    "ReadinessChecks",
    "ReauthenticateRequest",
    "RecoveryCodesData",
    "RecoveryLimitedContext",
    "SessionData",
    "TotpCodeRequest",
    "TotpEnrollmentData",
    "TotpEnrollmentOfferData",
    "TotpEnrollmentRequest",
    "TotpProofRequest",
    "TotpRecoveryRequest",
    "DeviceGrantContextData",
    "DeviceGrantData",
    "DeviceGrantDecisionData",
    "DeviceGrantExchangeData",
    "DeviceGrantLookupRequest",
    "DeviceGrantRequest",
    "DeviceRefreshRequest",
    "RefreshedDeviceTokenData",
    "DeviceSelfRevokeData",
    "AdminDeviceData",
    "AdminDeviceListData",
    "AdminDeviceRevokeRequest",
    "AdminDeviceRevokeData",
    "ExclusionPolicyStatusData",
    "PolicyDraftData",
    "PolicyDraftReplaceRequest",
    "PolicyDraftRuleRequest",
    "PolicyKeysetEnvelopeData",
    "PolicyKeysetKeyData",
    "PolicyKeysetPageData",
    "PolicyKeysetPayloadData",
    "PolicyKeysetSignatureData",
    "PolicyPreviewCountersData",
    "PolicyPreviewCursorData",
    "PolicyPreviewData",
    "PolicyPreviewResultRowData",
    "PolicyPublicationData",
    "PolicyPublicationRequest",
    "PolicyReconciliationSummaryData",
    "PolicyRuleData",
    "PolicySnapshotPayloadData",
    "PolicySnapshotRuleData",
    "PolicySnapshotSignatureData",
    "SignedPolicySnapshotData",
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
    assert set(paths) == set(ROUTE_OPERATION_IDS)
    for path, expected_operations in ROUTE_OPERATION_IDS.items():
        operations = paths[path]
        assert set(operations) == set(expected_operations), path
        for method, operation_id in expected_operations.items():
            assert operations[method]["operationId"] == operation_id, (path, method)


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
                if status == "304":
                    # The snapshot's not-modified response carries headers
                    # only: the entity tag replaces the body (spec 16.2).
                    assert "content" not in response, (path, method, status)
                    continue
                schema = response["content"]["application/json"]["schema"]
                assert set(schema) == {"$ref"}, (path, method, status)
                reference = schema["$ref"]
                assert reference.startswith("#/components/schemas/"), (path, method, status)
                assert reference.removeprefix("#/components/schemas/") in schemas, (
                    path,
                    method,
                    status,
                )


def test_document_never_advertises_the_framework_validation_error_shape(
    snapshot_document: dict[str, Any],
) -> None:
    # The shared request-validation handler emits the canonical error envelope
    # (api_request_validation_failed / api_request_malformed), never FastAPI's
    # HTTPValidationError, so no route may document that default 422 shape.
    schemas = snapshot_document["components"]["schemas"]
    assert "HTTPValidationError" not in schemas
    assert "ValidationError" not in schemas
    for path, operations in snapshot_document["paths"].items():
        for method, operation in operations.items():
            assert "422" not in operation["responses"], (path, method)


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
