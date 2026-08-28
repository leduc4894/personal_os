"""HTTP error vocabulary: registry pins, closed status table and transport errors."""

from __future__ import annotations

import pytest

from personal_os.api_contracts import HTTP_ERROR_STATUSES, ApiTransportError
from personal_os.api_contracts.errors import _build_closed_http_status_map
from personal_os.diagnostics.events import SafeToken
from personal_os.error_contracts.codes import ERROR_DEFINITIONS, ErrorCategory, ErrorCode
from personal_os.error_contracts.exceptions import ApplicationError

EXPECTED_API_ERRORS = {
    "api_request_malformed": (
        ErrorCategory.VALIDATION,
        False,
        "The API request is malformed",
        frozenset(),
    ),
    "api_request_validation_failed": (
        ErrorCategory.VALIDATION,
        False,
        "The API request failed validation",
        frozenset({"field_names"}),
    ),
    "api_route_not_found": (
        ErrorCategory.VALIDATION,
        False,
        "The requested API route does not exist",
        frozenset(),
    ),
    "api_method_not_allowed": (
        ErrorCategory.VALIDATION,
        False,
        "The API route does not allow this method",
        frozenset(),
    ),
}


def test_api_error_registry_entries_are_pinned_exactly() -> None:
    assert set(EXPECTED_API_ERRORS) <= {code.value for code in ErrorCode}
    for code_text, expected in EXPECTED_API_ERRORS.items():
        category, is_retryable, safe_message, allowed_detail_fields = expected
        definition = ERROR_DEFINITIONS[ErrorCode(code_text)]
        assert definition.category is category
        assert definition.is_retryable is is_retryable
        assert definition.safe_message == safe_message
        assert definition.allowed_detail_fields == allowed_detail_fields


def test_http_status_map_is_closed_for_the_api_surface() -> None:
    assert HTTP_ERROR_STATUSES == {
        ErrorCode.API_REQUEST_MALFORMED: 400,
        ErrorCode.API_REQUEST_VALIDATION_FAILED: 422,
        ErrorCode.API_ROUTE_NOT_FOUND: 404,
        ErrorCode.API_METHOD_NOT_ALLOWED: 405,
        ErrorCode.DATABASE_CONNECTION_UNAVAILABLE: 503,
        ErrorCode.DATABASE_SCHEMA_CONTRACT_INVALID: 503,
        ErrorCode.INTERNAL_ERROR: 500,
        # The authentication block of the design error contract (spec 17).
        ErrorCode.AUTHENTICATION_REQUIRED: 401,
        ErrorCode.AUTHENTICATION_FAILED: 401,
        ErrorCode.AUTHENTICATION_RATE_LIMITED: 429,
        ErrorCode.RECENT_AUTHENTICATION_REQUIRED: 403,
        ErrorCode.CSRF_VALIDATION_FAILED: 403,
        ErrorCode.AUTHORIZATION_SCOPE_DENIED: 403,
        ErrorCode.TOTP_ENROLLMENT_STATE_INVALID: 409,
        ErrorCode.DEVICE_AUTHORIZATION_PENDING: 409,
        ErrorCode.DEVICE_AUTHORIZATION_SLOW_DOWN: 429,
        ErrorCode.DEVICE_AUTHORIZATION_DENIED: 403,
        ErrorCode.DEVICE_AUTHORIZATION_EXPIRED: 410,
        ErrorCode.DEVICE_AUTHORIZATION_STATE_INVALID: 409,
        # The exact device-name confirmation mismatch of the Admin revoke
        # route (spec 14.1): a closed 409 conflict carrying no detail.
        ErrorCode.DEVICE_REVOCATION_CONFIRMATION_INVALID: 409,
        ErrorCode.DEVICE_CREDENTIAL_INVALID: 401,
        ErrorCode.DEVICE_REVOKED: 401,
        ErrorCode.DEVICE_TOKEN_REUSE_DETECTED: 401,
        ErrorCode.PLUGIN_VERSION_UNSUPPORTED: 426,
        # The exclusion-policy status column of the design error contract
        # (spec 19), wired when the policy routes landed.
        ErrorCode.EXCLUSION_POLICY_INPUT_INVALID: 422,
        ErrorCode.EXCLUSION_POLICY_NOT_INITIALIZED: 409,
        ErrorCode.EXCLUSION_POLICY_DRAFT_CONFLICT: 409,
        ErrorCode.EXCLUSION_POLICY_PREVIEW_PENDING: 409,
        ErrorCode.EXCLUSION_POLICY_PREVIEW_FAILED: 409,
        ErrorCode.EXCLUSION_POLICY_PREVIEW_EXPIRED: 410,
        ErrorCode.EXCLUSION_POLICY_PREVIEW_STALE: 409,
        ErrorCode.EXCLUSION_POLICY_CONFIRMATION_INVALID: 409,
        ErrorCode.EXCLUSION_POLICY_DENIED: 403,
        ErrorCode.EXCLUSION_POLICY_INDETERMINATE: 403,
        ErrorCode.EXCLUSION_POLICY_SNAPSHOT_OUTDATED: 409,
        ErrorCode.EXCLUSION_POLICY_SIGNING_UNAVAILABLE: 503,
        ErrorCode.EXCLUSION_POLICY_COMMIT_OUTCOME_UNKNOWN: 503,
        # The small-file sync block of the plugin journal design (spec 10/12),
        # wired when the sync routes landed: 422 for the validation and
        # integrity verdicts, 404 for an unknown operation token, 410 for the
        # expired operation and 409 for the identity and state conflicts.
        ErrorCode.SMALL_FILE_PREFLIGHT_INVALID: 422,
        ErrorCode.SMALL_FILE_OPERATION_NOT_FOUND: 404,
        ErrorCode.SMALL_FILE_OPERATION_EXPIRED: 410,
        ErrorCode.SMALL_FILE_OPERATION_IDENTITY_MISMATCH: 409,
        ErrorCode.SMALL_FILE_SIZE_LIMIT_EXCEEDED: 422,
        ErrorCode.SMALL_FILE_CONTENT_INTEGRITY_FAILED: 422,
        ErrorCode.SMALL_FILE_UPLOAD_STATE_INVALID: 409,
        # Source lifecycle exposes only this closed status mapping: input and
        # missing-locator failures are 400, missing tombstones are 404,
        # conflicts are 409 and ambiguous commit recovery is 503.
        ErrorCode.SOURCE_LIFECYCLE_INPUT_INVALID: 400,
        ErrorCode.SOURCE_LOCATOR_MISSING: 400,
        ErrorCode.SOURCE_LOCATOR_CONFLICT: 409,
        ErrorCode.SOURCE_TOMBSTONE_NOT_FOUND: 404,
        ErrorCode.SOURCE_TOMBSTONE_CLOSED: 409,
        ErrorCode.SOURCE_LIFECYCLE_VERSION_CONFLICT: 409,
        ErrorCode.SOURCE_LIFECYCLE_COMMIT_OUTCOME_UNKNOWN: 503,
        # The device cursor and manifest reconciliation block of the design
        # error contract (spec 13): 409 for the cursor/state conflicts and
        # integrity stops, 404 for unavailable events and runs, 410 for the
        # expired run, 422 for invalid pages/digests and download integrity,
        # and one retryable 503 for the dependency outage.
        ErrorCode.DEVICE_CURSOR_GAP: 409,
        ErrorCode.DEVICE_CURSOR_REGRESSION: 409,
        ErrorCode.DEVICE_CURSOR_ACK_AHEAD: 409,
        ErrorCode.DEVICE_EVENT_UNAVAILABLE: 404,
        ErrorCode.DEVICE_EVENT_INTEGRITY_FAILED: 409,
        ErrorCode.DEVICE_MANIFEST_NOT_FOUND: 404,
        ErrorCode.DEVICE_MANIFEST_EXPIRED: 410,
        ErrorCode.DEVICE_MANIFEST_STATE_INVALID: 409,
        ErrorCode.DEVICE_MANIFEST_PAGE_INVALID: 422,
        ErrorCode.DEVICE_MANIFEST_PAGE_REPLAY_MISMATCH: 409,
        ErrorCode.DEVICE_MANIFEST_DIGEST_MISMATCH: 422,
        ErrorCode.DEVICE_MANIFEST_POLICY_ADVANCED: 409,
        ErrorCode.DEVICE_DOWNLOAD_INTEGRITY_FAILED: 422,
        ErrorCode.DEVICE_SYNC_DEPENDENCY_UNAVAILABLE: 503,
        # The multipart upload block of the resumable multipart mobile
        # upload design (Child 7 spec 7), wired when the session routes
        # landed: 422 for the part/geometry validation and the decided
        # integrity verdicts, 404 for an unknown opaque session, 410 for
        # the expired session, 409 for the state/concurrent-completion
        # conflicts and the changed local file, 403 for the rechecked
        # policy denial, and one 503 per retryable dependency failure.
        ErrorCode.MULTIPART_SESSION_NOT_FOUND: 404,
        ErrorCode.MULTIPART_SESSION_EXPIRED: 410,
        ErrorCode.MULTIPART_SESSION_STATE_INVALID: 409,
        ErrorCode.MULTIPART_PART_INVALID: 422,
        ErrorCode.MULTIPART_PART_URL_REJECTED: 503,
        ErrorCode.MULTIPART_PROVIDER_STATE_INVALID: 422,
        ErrorCode.MULTIPART_COMPLETION_IN_PROGRESS: 409,
        ErrorCode.MULTIPART_INTEGRITY_FAILED: 422,
        ErrorCode.MULTIPART_POLICY_DENIED: 403,
        ErrorCode.MULTIPART_CLEANUP_FAILED: 503,
        ErrorCode.MULTIPART_LOCAL_CONTENT_CHANGED: 409,
        ErrorCode.MULTIPART_DEPENDENCY_UNAVAILABLE: 503,
    }


def test_api_transport_error_allows_exactly_the_four_api_codes() -> None:
    assert ApiTransportError.allowed_codes == frozenset(
        ErrorCode(code_text) for code_text in EXPECTED_API_ERRORS
    )
    for error_code in ApiTransportError.allowed_codes:
        error = ApiTransportError(error_code)
        assert isinstance(error, ApplicationError)
        assert error.category is ErrorCategory.VALIDATION
        assert error.is_retryable is False
    with pytest.raises(ValueError, match="not valid for this exception type"):
        ApiTransportError(ErrorCode.INTERNAL_ERROR)


def test_api_request_validation_failed_accepts_only_field_names() -> None:
    error = ApiTransportError(
        ErrorCode.API_REQUEST_VALIDATION_FAILED,
        safe_details={"field_names": (SafeToken.parse("host"), SafeToken.parse("port"))},
    )
    assert error.to_safe_dict()["safe_details"] == {"field_names": ["host", "port"]}
    with pytest.raises(ValueError, match="not registered for this error code"):
        ApiTransportError(
            ErrorCode.API_REQUEST_VALIDATION_FAILED,
            safe_details={"count": 1},
        )


def test_status_map_builder_rejects_unlisted_and_incomplete_drafts() -> None:
    draft_with_unlisted_code = dict(HTTP_ERROR_STATUSES)
    draft_with_unlisted_code[ErrorCode.DATABASE_MIGRATION_BUSY] = 503
    with pytest.raises(ValueError, match="outside the approved table"):
        _build_closed_http_status_map(draft_with_unlisted_code)
    incomplete_draft = {
        code: status
        for code, status in HTTP_ERROR_STATUSES.items()
        if code is not ErrorCode.INTERNAL_ERROR
    }
    with pytest.raises(ValueError, match="misses approved codes"):
        _build_closed_http_status_map(incomplete_draft)
