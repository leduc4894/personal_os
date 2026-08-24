"""Closed object-storage error and diagnostic registry completeness tests.

Asserts the exact nine error codes and their ``(category, is_retryable)`` map, the
closed ``ObjectStorageError.allowed_codes`` set, the exact safe-detail allowlists,
the closed input-reason tokens, the five registered events with their exact field
contracts, and the ``ObjectDigestPrefix`` safe-value contract.
"""

from __future__ import annotations

import pytest

from personal_os.diagnostics.events import (
    EVENT_DEFINITIONS,
    EventName,
    ObjectDigestPrefix,
    SafeToken,
)
from personal_os.error_contracts.codes import ERROR_DEFINITIONS, ErrorCategory, ErrorCode
from personal_os.object_storage.errors import (
    DIGEST_MISMATCH,
    MEDIA_TYPE_INVALID,
    SIZE_MISMATCH,
    SIZE_OUT_OF_RANGE,
    SPOOL_ADMISSION_WINDOW_EXPIRED,
    SPOOL_FREE_SPACE,
    SPOOL_PERMITS_EXHAUSTED,
    STREAM_INVALID,
    ObjectStorageError,
)


def test_object_storage_error_registry_is_exact() -> None:
    expected = {
        ErrorCode.OBJECT_STORAGE_CONFIGURATION_INVALID: (ErrorCategory.CONFIGURATION, False),
        ErrorCode.OBJECT_STORAGE_INPUT_INVALID: (ErrorCategory.VALIDATION, False),
        ErrorCode.OBJECT_STORAGE_BUSY: (ErrorCategory.DEPENDENCY, True),
        ErrorCode.OBJECT_STORAGE_UNAVAILABLE: (ErrorCategory.DEPENDENCY, True),
        ErrorCode.OBJECT_STORAGE_ACCESS_DENIED: (ErrorCategory.AUTHORIZATION, False),
        ErrorCode.OBJECT_STORAGE_CONTRACT_INVALID: (ErrorCategory.INTEGRITY, False),
        ErrorCode.OBJECT_STORAGE_OBJECT_MISSING: (ErrorCategory.INTEGRITY, False),
        ErrorCode.OBJECT_STORAGE_INTEGRITY_FAILED: (ErrorCategory.INTEGRITY, False),
        ErrorCode.OBJECT_STORAGE_METADATA_CONFLICT: (ErrorCategory.CONFLICT, False),
    }
    for code, (category, retryable) in expected.items():
        error = ObjectStorageError(code)
        assert (error.category, error.is_retryable) == (category, retryable)


def test_object_storage_error_allowed_codes_is_closed_to_nine() -> None:
    assert ObjectStorageError.allowed_codes == frozenset(
        {
            ErrorCode.OBJECT_STORAGE_CONFIGURATION_INVALID,
            ErrorCode.OBJECT_STORAGE_INPUT_INVALID,
            ErrorCode.OBJECT_STORAGE_BUSY,
            ErrorCode.OBJECT_STORAGE_UNAVAILABLE,
            ErrorCode.OBJECT_STORAGE_ACCESS_DENIED,
            ErrorCode.OBJECT_STORAGE_CONTRACT_INVALID,
            ErrorCode.OBJECT_STORAGE_OBJECT_MISSING,
            ErrorCode.OBJECT_STORAGE_INTEGRITY_FAILED,
            ErrorCode.OBJECT_STORAGE_METADATA_CONFLICT,
        }
    )


def test_object_storage_error_rejects_outside_code() -> None:
    with pytest.raises(ValueError, match="not valid for this exception type"):
        ObjectStorageError(ErrorCode.CONFIGURATION_INVALID)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("code", "allowed"),
    [
        (
            ErrorCode.OBJECT_STORAGE_CONFIGURATION_INVALID,
            frozenset({"count", "field_names"}),
        ),
        (ErrorCode.OBJECT_STORAGE_INPUT_INVALID, frozenset({"reason"})),
        (ErrorCode.OBJECT_STORAGE_BUSY, frozenset({"reason"})),
        (ErrorCode.OBJECT_STORAGE_UNAVAILABLE, frozenset()),
        (ErrorCode.OBJECT_STORAGE_ACCESS_DENIED, frozenset()),
        (ErrorCode.OBJECT_STORAGE_CONTRACT_INVALID, frozenset()),
        (ErrorCode.OBJECT_STORAGE_OBJECT_MISSING, frozenset()),
        (ErrorCode.OBJECT_STORAGE_INTEGRITY_FAILED, frozenset()),
        (ErrorCode.OBJECT_STORAGE_METADATA_CONFLICT, frozenset()),
    ],
)
def test_object_storage_safe_detail_allowlists_are_exact(
    code: ErrorCode, allowed: frozenset[str]
) -> None:
    assert ERROR_DEFINITIONS[code].allowed_detail_fields == allowed


def test_object_storage_input_reasons_are_closed_safe_tokens() -> None:
    expected = {
        SIZE_OUT_OF_RANGE: "size_out_of_range",
        SIZE_MISMATCH: "size_mismatch",
        DIGEST_MISMATCH: "digest_mismatch",
        MEDIA_TYPE_INVALID: "media_type_invalid",
        STREAM_INVALID: "stream_invalid",
    }
    for token, value in expected.items():
        assert isinstance(token, SafeToken)
        assert token.value == value


def test_object_storage_busy_reasons_are_closed_safe_tokens() -> None:
    expected = {
        SPOOL_FREE_SPACE: "spool_free_space",
        SPOOL_ADMISSION_WINDOW_EXPIRED: "spool_admission_window_expired",
        SPOOL_PERMITS_EXHAUSTED: "spool_permits_exhausted",
    }
    for token, value in expected.items():
        assert isinstance(token, SafeToken)
        assert token.value == value


def test_object_storage_busy_accepts_only_registered_reason() -> None:
    error = ObjectStorageError(
        ErrorCode.OBJECT_STORAGE_BUSY,
        safe_details={"reason": SPOOL_FREE_SPACE},
    )
    assert error.to_safe_dict()["safe_details"] == {"reason": "spool_free_space"}
    assert error.is_retryable


def test_object_storage_input_invalid_accepts_only_registered_reason() -> None:
    error = ObjectStorageError(
        ErrorCode.OBJECT_STORAGE_INPUT_INVALID,
        safe_details={"reason": SIZE_OUT_OF_RANGE},
    )
    assert error.to_safe_dict()["safe_details"] == {"reason": "size_out_of_range"}


def test_object_storage_events_are_registered_with_exact_fields() -> None:
    expected = {
        EventName.OBJECT_STORAGE_OPERATION_SUCCEEDED: (
            frozenset({"operation", "duration_ms", "size_bytes", "attempt_count", "provider"}),
            frozenset({"operation", "duration_ms", "size_bytes", "attempt_count", "provider"}),
        ),
        EventName.OBJECT_STORAGE_OPERATION_FAILED: (
            frozenset(
                {
                    "operation",
                    "duration_ms",
                    "attempt_count",
                    "provider",
                    "error_code",
                    "error_category",
                    "is_retryable",
                }
            ),
            frozenset(
                {
                    "operation",
                    "duration_ms",
                    "attempt_count",
                    "provider",
                    "error_code",
                    "error_category",
                    "is_retryable",
                    "size_bytes",
                    "object_digest_prefix",
                }
            ),
        ),
        EventName.OBJECT_STORAGE_OBJECT_DEDUPLICATED: (
            frozenset({"operation", "duration_ms", "size_bytes", "attempt_count", "provider"}),
            frozenset(
                {
                    "operation",
                    "duration_ms",
                    "size_bytes",
                    "attempt_count",
                    "provider",
                    "object_digest_prefix",
                }
            ),
        ),
        EventName.OBJECT_STORAGE_INTEGRITY_FAILED: (
            frozenset(
                {
                    "operation",
                    "duration_ms",
                    "attempt_count",
                    "provider",
                    "error_code",
                    "error_category",
                    "is_retryable",
                }
            ),
            frozenset(
                {
                    "operation",
                    "duration_ms",
                    "attempt_count",
                    "provider",
                    "error_code",
                    "error_category",
                    "is_retryable",
                    "size_bytes",
                    "object_digest_prefix",
                }
            ),
        ),
        EventName.OBJECT_STORAGE_SPOOL_CLEANUP_DEGRADED: (
            frozenset({"operation", "count", "reason"}),
            frozenset(
                {
                    "operation",
                    "count",
                    "reason",
                }
            ),
        ),
        EventName.OBJECT_STORAGE_CLIENT_CLOSE_DEGRADED: (
            frozenset(
                {
                    "operation",
                    "reason",
                    "error_code",
                    "error_category",
                    "is_retryable",
                }
            ),
            frozenset(
                {
                    "operation",
                    "reason",
                    "error_code",
                    "error_category",
                    "is_retryable",
                }
            ),
        ),
    }
    for event_name, (required, allowed) in expected.items():
        definition = EVENT_DEFINITIONS[event_name]
        assert definition.required_fields == required
        assert definition.allowed_fields == allowed


def test_object_digest_prefix_accepts_twelve_lowercase_hex() -> None:
    assert str(ObjectDigestPrefix.parse("0123456789ab")) == "0123456789ab"


@pytest.mark.parametrize(
    "value",
    [
        "0123456789a",  # too short
        "0123456789abc",  # too long
        "0123456789AB",  # uppercase
        "0123456789ag",  # non-hex
        "",
    ],
)
def test_object_digest_prefix_rejects_other_shapes(value: str) -> None:
    with pytest.raises(ValueError, match="object digest prefix"):
        ObjectDigestPrefix.parse(value)
