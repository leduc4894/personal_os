"""Registry metadata for the sixteen web/device authentication error codes.

Spec 17 fixes the HTTP, retryability and safe-detail contract of every
authentication code. These tests pin the registry definitions to that table,
prove the retryable codes accept only ``retry_after_seconds``, that
``plugin_version_unsupported`` accepts only the approved version bounds, and
that the domain error type rejects every code outside the closed set.
"""

from __future__ import annotations

import pytest

from personal_os.authentication.errors import AuthenticationError
from personal_os.diagnostics.events import SafeToken
from personal_os.error_contracts.codes import ERROR_DEFINITIONS, ErrorCategory, ErrorCode

#: Exact spec 17 mapping: (is_retryable, allowed detail fields).
SPEC_SEVENTEEN_RETRY_AND_DETAILS = {
    ErrorCode.AUTHENTICATION_REQUIRED: (False, frozenset()),
    ErrorCode.AUTHENTICATION_FAILED: (False, frozenset()),
    ErrorCode.AUTHENTICATION_RATE_LIMITED: (True, frozenset({"retry_after_seconds"})),
    ErrorCode.RECENT_AUTHENTICATION_REQUIRED: (False, frozenset()),
    ErrorCode.CSRF_VALIDATION_FAILED: (False, frozenset()),
    ErrorCode.AUTHORIZATION_SCOPE_DENIED: (False, frozenset()),
    ErrorCode.TOTP_ENROLLMENT_STATE_INVALID: (False, frozenset()),
    ErrorCode.DEVICE_AUTHORIZATION_PENDING: (True, frozenset({"retry_after_seconds"})),
    ErrorCode.DEVICE_AUTHORIZATION_SLOW_DOWN: (True, frozenset({"retry_after_seconds"})),
    ErrorCode.DEVICE_AUTHORIZATION_DENIED: (False, frozenset()),
    ErrorCode.DEVICE_AUTHORIZATION_EXPIRED: (False, frozenset()),
    ErrorCode.DEVICE_AUTHORIZATION_STATE_INVALID: (False, frozenset()),
    ErrorCode.DEVICE_CREDENTIAL_INVALID: (False, frozenset()),
    ErrorCode.DEVICE_REVOKED: (False, frozenset()),
    ErrorCode.DEVICE_TOKEN_REUSE_DETECTED: (False, frozenset()),
    ErrorCode.PLUGIN_VERSION_UNSUPPORTED: (False, frozenset({"approved_version_bounds"})),
}


def test_authentication_codes_match_spec_seventeen_retry_and_detail_contract() -> None:
    assert len(SPEC_SEVENTEEN_RETRY_AND_DETAILS) == 16
    for error_code, (is_retryable, allowed_fields) in SPEC_SEVENTEEN_RETRY_AND_DETAILS.items():
        definition = ERROR_DEFINITIONS[error_code]
        assert definition.is_retryable is is_retryable, error_code
        assert definition.allowed_detail_fields == allowed_fields, error_code
        assert definition.safe_message, error_code


def test_authentication_categories_are_closed_registry_members() -> None:
    for error_code in SPEC_SEVENTEEN_RETRY_AND_DETAILS:
        assert ERROR_DEFINITIONS[error_code].category in set(ErrorCategory), error_code


def test_rate_limited_codes_accept_only_retry_after_seconds() -> None:
    error = AuthenticationError(
        ErrorCode.AUTHENTICATION_RATE_LIMITED,
        safe_details={"retry_after_seconds": 900},
    )
    assert error.to_safe_dict()["safe_details"] == {"retry_after_seconds": 900}
    with pytest.raises(ValueError, match="not registered for this error code"):
        AuthenticationError(
            ErrorCode.DEVICE_AUTHORIZATION_SLOW_DOWN,
            safe_details={"reason": SafeToken.parse("poll_too_fast")},
        )


def test_plugin_version_unsupported_accepts_only_approved_bounds() -> None:
    error = AuthenticationError(
        ErrorCode.PLUGIN_VERSION_UNSUPPORTED,
        safe_details={
            "approved_version_bounds": (
                SafeToken.parse("1.13.0"),
                SafeToken.parse("1.13.1"),
            )
        },
    )
    assert error.to_safe_dict()["safe_details"] == {"approved_version_bounds": ["1.13.0", "1.13.1"]}
    with pytest.raises(ValueError, match="not registered for this error code"):
        AuthenticationError(
            ErrorCode.PLUGIN_VERSION_UNSUPPORTED,
            safe_details={"reason": SafeToken.parse("old_plugin")},
        )


def test_authentication_error_rejects_codes_outside_the_closed_set() -> None:
    with pytest.raises(ValueError, match="not valid for this exception type"):
        AuthenticationError(ErrorCode.CONFIGURATION_INVALID)


def test_authentication_error_serializes_registry_metadata_only() -> None:
    error = AuthenticationError(ErrorCode.DEVICE_CREDENTIAL_INVALID)
    assert error.to_safe_dict() == {
        "error_code": "device_credential_invalid",
        "category": ERROR_DEFINITIONS[ErrorCode.DEVICE_CREDENTIAL_INVALID].category.value,
        "is_retryable": False,
        "safe_message": ERROR_DEFINITIONS[ErrorCode.DEVICE_CREDENTIAL_INVALID].safe_message,
        "safe_details": {},
    }
