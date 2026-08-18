"""Closed small-file sync error contract: registry pins and sentinel hygiene.

Asserts the exact seven-code registry set with fixed category, retryability and
safe-detail allowlists, the closed preflight-invalid reason vocabulary, the
closure of the typed exception around its codes, and that locator, digest,
token and raw-payload sentinels chained as causes never surface in any
rendering of the typed error.
"""

from __future__ import annotations

import pytest

from personal_os.diagnostics.events import SafeToken
from personal_os.error_contracts.codes import ERROR_DEFINITIONS, ErrorCategory, ErrorCode
from personal_os.small_file_sync.errors import (
    PREFLIGHT_INVALID_REASONS,
    SmallFileSyncError,
)

#: The exact closed error-code set this domain adds (verbatim values).
SMALL_FILE_ERROR_CODES = {
    "small_file_preflight_invalid",
    "small_file_operation_not_found",
    "small_file_operation_expired",
    "small_file_operation_identity_mismatch",
    "small_file_size_limit_exceeded",
    "small_file_content_integrity_failed",
    "small_file_upload_state_invalid",
}

#: Exact category, retryability and safe-detail allowlist per code.
EXPECTED_REGISTRY_CONTRACT = {
    "small_file_preflight_invalid": (ErrorCategory.VALIDATION, False, frozenset({"reason"})),
    "small_file_operation_not_found": (ErrorCategory.CONFLICT, False, frozenset()),
    "small_file_operation_expired": (ErrorCategory.CONFLICT, False, frozenset()),
    "small_file_operation_identity_mismatch": (ErrorCategory.CONFLICT, False, frozenset()),
    "small_file_size_limit_exceeded": (ErrorCategory.VALIDATION, False, frozenset()),
    "small_file_content_integrity_failed": (ErrorCategory.INTEGRITY, False, frozenset()),
    "small_file_upload_state_invalid": (ErrorCategory.CONFLICT, False, frozenset()),
}

#: The codes that must accept no safe detail field at all.
NO_DETAIL_CODES = sorted(SMALL_FILE_ERROR_CODES - {"small_file_preflight_invalid"})


def test_small_file_error_code_set_is_exact() -> None:
    registered = {code.value for code in SmallFileSyncError.allowed_codes}
    assert registered == SMALL_FILE_ERROR_CODES
    assert len(SmallFileSyncError.allowed_codes) == 7
    assert all(ErrorCode(value) in ERROR_DEFINITIONS for value in SMALL_FILE_ERROR_CODES)


def test_small_file_registry_category_retryability_and_details_are_fixed() -> None:
    assert set(EXPECTED_REGISTRY_CONTRACT) == SMALL_FILE_ERROR_CODES
    for value, (category, retryable, allowed_fields) in EXPECTED_REGISTRY_CONTRACT.items():
        definition = ERROR_DEFINITIONS[ErrorCode(value)]
        assert (definition.category, definition.is_retryable) == (category, retryable), value
        assert definition.allowed_detail_fields == allowed_fields, value
        assert definition.safe_message, value


def test_no_small_file_error_automatically_retries() -> None:
    for error_code in SmallFileSyncError.allowed_codes:
        assert ERROR_DEFINITIONS[error_code].is_retryable is False, error_code


def test_preflight_invalid_reason_vocabulary_is_closed() -> None:
    assert {token.value for token in PREFLIGHT_INVALID_REASONS} == {
        "event_id_invalid",
        "idempotency_key_invalid",
        "operation_invalid",
        "update_base_missing",
        "create_base_present",
        "local_file_id_invalid",
        "locator_invalid",
        "digest_invalid",
        "size_bytes_invalid",
        "media_type_invalid",
        "policy_revision_invalid",
    }
    for token in PREFLIGHT_INVALID_REASONS:
        assert isinstance(token, SafeToken)


def test_preflight_invalid_accepts_only_closed_reason_tokens() -> None:
    error = SmallFileSyncError(
        ErrorCode.SMALL_FILE_PREFLIGHT_INVALID,
        safe_details={"reason": PREFLIGHT_INVALID_REASONS[0]},
    )
    assert error.to_safe_dict()["safe_details"] == {"reason": PREFLIGHT_INVALID_REASONS[0].value}
    with pytest.raises(ValueError, match="not an accepted safe scalar"):
        SmallFileSyncError(
            ErrorCode.SMALL_FILE_PREFLIGHT_INVALID,
            safe_details={"reason": "locator_invalid"},
        )


def test_error_rejects_codes_outside_the_closed_set() -> None:
    with pytest.raises(ValueError, match="not valid for this exception type"):
        SmallFileSyncError(ErrorCode.SOURCE_NOT_FOUND)
    with pytest.raises(ValueError, match="not valid for this exception type"):
        SmallFileSyncError(ErrorCode.API_REQUEST_MALFORMED)


@pytest.mark.parametrize("code_text", NO_DETAIL_CODES)
def test_closed_codes_accept_no_safe_detail_field(code_text: str) -> None:
    with pytest.raises(ValueError, match="not registered for this error code"):
        SmallFileSyncError(
            ErrorCode(code_text),
            safe_details={"reason": SafeToken.parse("locator_invalid")},
        )
    with pytest.raises(ValueError, match="not registered for this error code"):
        SmallFileSyncError(ErrorCode(code_text), safe_details={"operation_id": "opaque-token"})


def test_errors_never_echo_locator_digest_token_or_payload_sentinels() -> None:
    locator_sentinel = "vault/do-not-leak/secret-notes.md"
    digest_sentinel = "ab" * 32
    token_sentinel = "Qm9ndXNTeXpjRWxlZW1FZ0Rhenp1R2h1"
    payload_sentinel = "RAW-FILE-BYTES-DO-NOT-LEAK"
    cause = RuntimeError(
        f"locator={locator_sentinel} digest={digest_sentinel} "
        f"token={token_sentinel} payload={payload_sentinel}"
    )
    for error_code in sorted(SmallFileSyncError.allowed_codes, key=lambda code: code.value):
        error = SmallFileSyncError(error_code)
        error.__cause__ = cause
        rendered = f"{error!r} {error} {error.to_safe_dict()}"
        for sentinel in (locator_sentinel, digest_sentinel, token_sentinel, payload_sentinel):
            assert sentinel not in rendered, error_code.value


def test_registry_metadata_serializes_closed_shapes_only() -> None:
    error = SmallFileSyncError(
        ErrorCode.SMALL_FILE_PREFLIGHT_INVALID,
        safe_details={"reason": SafeToken.parse("size_bytes_invalid")},
    )
    assert error.to_safe_dict() == {
        "error_code": "small_file_preflight_invalid",
        "category": "validation",
        "is_retryable": False,
        "safe_message": ERROR_DEFINITIONS[ErrorCode.SMALL_FILE_PREFLIGHT_INVALID].safe_message,
        "safe_details": {"reason": "size_bytes_invalid"},
    }
