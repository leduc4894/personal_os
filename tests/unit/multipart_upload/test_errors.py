"""Closed multipart upload error contract: registry pins and sentinel hygiene.

Asserts the exact closed ``multipart_*`` code set of the Child 7 spec error
vocabulary with fixed category, retryability and empty safe-detail allowlists,
the closure of the typed exception around its codes, and that staging key,
provider upload ID, provider ETag, presigned URL and raw-digest sentinels
chained as causes never surface in any rendering of the typed error.
"""

from __future__ import annotations

import pytest

from personal_os.error_contracts.codes import ERROR_DEFINITIONS, ErrorCategory, ErrorCode
from personal_os.multipart_upload.errors import MultipartUploadError

#: The exact closed error-code set this domain adds (verbatim spec values).
MULTIPART_ERROR_CODES = {
    "multipart_session_not_found",
    "multipart_session_expired",
    "multipart_session_state_invalid",
    "multipart_part_invalid",
    "multipart_part_url_rejected",
    "multipart_provider_state_invalid",
    "multipart_completion_in_progress",
    "multipart_integrity_failed",
    "multipart_policy_denied",
    "multipart_cleanup_failed",
    "multipart_local_content_changed",
    "multipart_dependency_unavailable",
}

#: Exact category and retryability per code (spec 7: input/state/integrity/
#: policy errors never retry; completion-in-progress, part-URL rejection,
#: cleanup and typed dependency outages retry with bounded backoff).
EXPECTED_REGISTRY_CONTRACT = {
    "multipart_session_not_found": (ErrorCategory.CONFLICT, False),
    "multipart_session_expired": (ErrorCategory.CONFLICT, False),
    "multipart_session_state_invalid": (ErrorCategory.CONFLICT, False),
    "multipart_part_invalid": (ErrorCategory.VALIDATION, False),
    "multipart_part_url_rejected": (ErrorCategory.DEPENDENCY, True),
    "multipart_provider_state_invalid": (ErrorCategory.INTEGRITY, False),
    "multipart_completion_in_progress": (ErrorCategory.CONFLICT, True),
    "multipart_integrity_failed": (ErrorCategory.INTEGRITY, False),
    "multipart_policy_denied": (ErrorCategory.AUTHORIZATION, False),
    "multipart_cleanup_failed": (ErrorCategory.DEPENDENCY, True),
    "multipart_local_content_changed": (ErrorCategory.CONFLICT, False),
    "multipart_dependency_unavailable": (ErrorCategory.DEPENDENCY, True),
}


def test_multipart_error_code_set_is_exact() -> None:
    registered = {code.value for code in MultipartUploadError.allowed_codes}

    assert registered == MULTIPART_ERROR_CODES
    assert len(MultipartUploadError.allowed_codes) == len(MULTIPART_ERROR_CODES)
    assert all(ErrorCode(value) in ERROR_DEFINITIONS for value in MULTIPART_ERROR_CODES)


def test_multipart_registry_category_and_retryability_are_fixed() -> None:
    assert set(EXPECTED_REGISTRY_CONTRACT) == MULTIPART_ERROR_CODES
    for value, (category, retryable) in EXPECTED_REGISTRY_CONTRACT.items():
        definition = ERROR_DEFINITIONS[ErrorCode(value)]
        assert (definition.category, definition.is_retryable) == (category, retryable), value
        assert definition.safe_message, value


def test_multipart_codes_accept_no_safe_detail_field() -> None:
    for value in MULTIPART_ERROR_CODES:
        assert ERROR_DEFINITIONS[ErrorCode(value)].allowed_detail_fields == frozenset(), value
        with pytest.raises(ValueError, match="not registered for this error code"):
            MultipartUploadError(ErrorCode(value), safe_details={"reason": "staging_key_invalid"})


def test_only_the_four_typed_outages_retry() -> None:
    retryable = {value for value in MULTIPART_ERROR_CODES if EXPECTED_REGISTRY_CONTRACT[value][1]}

    assert retryable == {
        "multipart_part_url_rejected",
        "multipart_completion_in_progress",
        "multipart_cleanup_failed",
        "multipart_dependency_unavailable",
    }


def test_error_rejects_codes_outside_the_closed_set() -> None:
    with pytest.raises(ValueError, match="not valid for this exception type"):
        MultipartUploadError(ErrorCode.SMALL_FILE_SIZE_LIMIT_EXCEEDED)
    with pytest.raises(ValueError, match="not valid for this exception type"):
        MultipartUploadError(ErrorCode.API_REQUEST_MALFORMED)


def test_errors_never_echo_key_provider_url_or_digest_sentinels() -> None:
    staging_key_sentinel = "staging/do-not-leak/upload-session"
    provider_id_sentinel = "provider-upload-id-DO-NOT-LEAK"
    etag_sentinel = '"provider-etag-DO-NOT-LEAK"'
    url_sentinel = "https://storage.example.com/signed?X-Amz-Signature=secret"
    digest_sentinel = "ab" * 32
    cause = RuntimeError(
        f"key={staging_key_sentinel} upload_id={provider_id_sentinel} "
        f"etag={etag_sentinel} url={url_sentinel} digest={digest_sentinel}"
    )
    for error_code in sorted(MultipartUploadError.allowed_codes, key=lambda code: code.value):
        error = MultipartUploadError(error_code)
        error.__cause__ = cause
        rendered = f"{error!r} {error} {error.to_safe_dict()}"
        for sentinel in (
            staging_key_sentinel,
            provider_id_sentinel,
            etag_sentinel,
            url_sentinel,
            digest_sentinel,
        ):
            assert sentinel not in rendered, error_code.value


def test_registry_metadata_serializes_closed_shapes_only() -> None:
    error = MultipartUploadError(ErrorCode.MULTIPART_SESSION_EXPIRED)

    assert error.to_safe_dict() == {
        "error_code": "multipart_session_expired",
        "category": "conflict",
        "is_retryable": False,
        "safe_message": ERROR_DEFINITIONS[ErrorCode.MULTIPART_SESSION_EXPIRED].safe_message,
        "safe_details": {},
    }
