"""Closed device-sync error contract: registry pins and sentinel hygiene.

Asserts the exact fourteen-code registry set of spec section 13 with fixed
category, retryability and empty safe-detail allowlists, the five closed
action-reason tokens, the one mapping onto central registry codes, the tested
HTTP status/retryable pairs, that every code carries one registry definition,
one HTTP mapping and one structured diagnostic reason, and that locator,
digest and raw-payload sentinels chained as causes never surface in any
rendering of the typed error.
"""

from __future__ import annotations

import pytest

from personal_os.api_contracts import HTTP_ERROR_STATUSES
from personal_os.device_sync.contracts import ManifestActionReason
from personal_os.device_sync.errors import (
    CENTRAL_ERROR_CODE_BY_DEVICE_CODE,
    DeviceSyncError,
    DeviceSyncErrorCode,
)
from personal_os.device_sync.metrics import DeviceSyncOperation
from personal_os.diagnostics.events import (
    DiagnosticEvent,
    EventName,
    build_registered_event,
)
from personal_os.error_contracts.codes import ERROR_DEFINITIONS, ErrorCategory, ErrorCode

#: The exact closed error-code set this domain adds (verbatim values).
DEVICE_SYNC_ERROR_CODES = {
    "device_cursor_gap",
    "device_cursor_regression",
    "device_cursor_ack_ahead",
    "device_event_unavailable",
    "device_event_integrity_failed",
    "device_manifest_not_found",
    "device_manifest_expired",
    "device_manifest_state_invalid",
    "device_manifest_page_invalid",
    "device_manifest_page_replay_mismatch",
    "device_manifest_digest_mismatch",
    "device_manifest_policy_advanced",
    "device_download_integrity_failed",
    "device_sync_dependency_unavailable",
}

#: Exact category, retryability and safe-detail allowlist per code.
EXPECTED_REGISTRY_CONTRACT = {
    "device_cursor_gap": (ErrorCategory.INTEGRITY, False),
    "device_cursor_regression": (ErrorCategory.CONFLICT, False),
    "device_cursor_ack_ahead": (ErrorCategory.CONFLICT, False),
    "device_event_unavailable": (ErrorCategory.CONFLICT, False),
    "device_event_integrity_failed": (ErrorCategory.INTEGRITY, False),
    "device_manifest_not_found": (ErrorCategory.CONFLICT, False),
    "device_manifest_expired": (ErrorCategory.CONFLICT, False),
    "device_manifest_state_invalid": (ErrorCategory.CONFLICT, False),
    "device_manifest_page_invalid": (ErrorCategory.VALIDATION, False),
    "device_manifest_page_replay_mismatch": (ErrorCategory.CONFLICT, False),
    "device_manifest_digest_mismatch": (ErrorCategory.VALIDATION, False),
    "device_manifest_policy_advanced": (ErrorCategory.CONFLICT, False),
    "device_download_integrity_failed": (ErrorCategory.INTEGRITY, False),
    "device_sync_dependency_unavailable": (ErrorCategory.DEPENDENCY, True),
}

#: The tested HTTP status per public error code (spec section 13 table).
EXPECTED_HTTP_STATUSES = {
    "device_cursor_gap": 409,
    "device_cursor_regression": 409,
    "device_cursor_ack_ahead": 409,
    "device_event_unavailable": 404,
    "device_event_integrity_failed": 409,
    "device_manifest_not_found": 404,
    "device_manifest_expired": 410,
    "device_manifest_state_invalid": 409,
    "device_manifest_page_invalid": 422,
    "device_manifest_page_replay_mismatch": 409,
    "device_manifest_digest_mismatch": 422,
    "device_manifest_policy_advanced": 409,
    "device_download_integrity_failed": 422,
    "device_sync_dependency_unavailable": 503,
}


def test_device_sync_error_code_set_is_exact() -> None:
    assert {code.value for code in DeviceSyncErrorCode} == DEVICE_SYNC_ERROR_CODES
    assert len(DeviceSyncErrorCode) == 14
    with pytest.raises(ValueError):
        DeviceSyncErrorCode("device_unknown_failure")


def test_registry_category_and_retryability_are_fixed() -> None:
    assert set(EXPECTED_REGISTRY_CONTRACT) == DEVICE_SYNC_ERROR_CODES
    for value, (category, retryable) in EXPECTED_REGISTRY_CONTRACT.items():
        definition = ERROR_DEFINITIONS[ErrorCode(value)]
        assert (definition.category, definition.is_retryable) == (category, retryable), value
        assert definition.allowed_detail_fields == frozenset(), value
        assert definition.safe_message, value


def test_only_the_dependency_failure_is_retryable() -> None:
    retryable = {
        value
        for value in DEVICE_SYNC_ERROR_CODES
        if ERROR_DEFINITIONS[ErrorCode(value)].is_retryable
    }
    assert retryable == {"device_sync_dependency_unavailable"}


def test_device_codes_map_one_to_one_onto_central_registry_codes() -> None:
    assert set(CENTRAL_ERROR_CODE_BY_DEVICE_CODE) == set(DeviceSyncErrorCode)
    central_values = [code.value for code in CENTRAL_ERROR_CODE_BY_DEVICE_CODE.values()]
    assert sorted(central_values) == sorted(DEVICE_SYNC_ERROR_CODES)
    assert len(set(central_values)) == len(central_values)
    for device_code, central_code in CENTRAL_ERROR_CODE_BY_DEVICE_CODE.items():
        assert device_code.value == central_code.value, device_code
    assert DeviceSyncError.allowed_codes == frozenset(CENTRAL_ERROR_CODE_BY_DEVICE_CODE.values())


def test_http_status_mapping_is_pinned() -> None:
    for value, status in EXPECTED_HTTP_STATUSES.items():
        assert HTTP_ERROR_STATUSES[ErrorCode(value)] == status, value


def test_every_code_has_one_registry_definition_http_mapping_and_structured_reason() -> None:
    for device_code in DeviceSyncErrorCode:
        central_code = CENTRAL_ERROR_CODE_BY_DEVICE_CODE[device_code]
        assert central_code in ERROR_DEFINITIONS, device_code
        assert central_code in HTTP_ERROR_STATUSES, device_code
        assert HTTP_ERROR_STATUSES[central_code] == EXPECTED_HTTP_STATUSES[device_code.value]
        built = build_registered_event(
            EventName.DEVICE_SYNC_OPERATION_REJECTED,
            {
                "operation": DeviceSyncOperation.PULL,
                "reason": device_code,
                "duration_ms": 1,
            },
        )
        assert isinstance(built, DiagnosticEvent), device_code


def test_typed_error_carries_the_domain_code_and_registry_metadata() -> None:
    error = DeviceSyncError(DeviceSyncErrorCode.CURSOR_GAP)
    assert error.code is DeviceSyncErrorCode.CURSOR_GAP
    assert error.error_code is ErrorCode.DEVICE_CURSOR_GAP
    assert error.category is ErrorCategory.INTEGRITY
    assert error.is_retryable is False
    assert error.to_safe_dict() == {
        "error_code": "device_cursor_gap",
        "category": "integrity",
        "is_retryable": False,
        "safe_message": ERROR_DEFINITIONS[ErrorCode.DEVICE_CURSOR_GAP].safe_message,
        "safe_details": {},
    }
    retryable = DeviceSyncError(DeviceSyncErrorCode.DEPENDENCY_UNAVAILABLE)
    assert retryable.is_retryable is True


def test_error_rejects_codes_outside_the_closed_set() -> None:
    with pytest.raises(ValueError, match="not valid for this exception type"):
        DeviceSyncError(ErrorCode.SOURCE_NOT_FOUND)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="not valid for this exception type"):
        DeviceSyncError(ErrorCode.API_REQUEST_MALFORMED)  # type: ignore[arg-type]


def test_action_reason_vocabulary_is_closed() -> None:
    assert {reason.value for reason in ManifestActionReason} == {
        "device_manifest_identity_ambiguous",
        "device_manifest_local_diverged",
        "device_manifest_target_occupied",
        "device_manifest_action_stale",
        "device_manifest_policy_excluded",
    }
    with pytest.raises(ValueError):
        ManifestActionReason("device_manifest_hash_matched")


def test_errors_never_echo_locator_digest_or_payload_sentinels() -> None:
    locator_sentinel = "vault/do-not-leak/secret-notes.md"
    digest_sentinel = "ab" * 32
    payload_sentinel = "RAW-FILE-BYTES-DO-NOT-LEAK"
    cause = RuntimeError(
        f"locator={locator_sentinel} digest={digest_sentinel} payload={payload_sentinel}"
    )
    for device_code in sorted(DeviceSyncErrorCode, key=lambda code: code.value):
        error = DeviceSyncError(device_code)
        error.__cause__ = cause
        rendered = f"{error!r} {error} {error.to_safe_dict()}"
        for sentinel in (locator_sentinel, digest_sentinel, payload_sentinel):
            assert sentinel not in rendered, device_code.value
