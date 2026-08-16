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
        ErrorCode.DEVICE_CREDENTIAL_INVALID: 401,
        ErrorCode.DEVICE_REVOKED: 401,
        ErrorCode.DEVICE_TOKEN_REUSE_DETECTED: 401,
        ErrorCode.PLUGIN_VERSION_UNSUPPORTED: 426,
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
