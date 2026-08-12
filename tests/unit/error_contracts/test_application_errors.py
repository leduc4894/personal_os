from __future__ import annotations

from personal_os.diagnostics.events import SafeToken
from personal_os.error_contracts.codes import ErrorCategory, ErrorCode
from personal_os.error_contracts.exceptions import ConfigurationError


def test_error_uses_registry_metadata_and_safe_details() -> None:
    error = ConfigurationError(
        ErrorCode.CONFIGURATION_INVALID,
        safe_details={"count": 1, "field_names": (SafeToken.parse("log_level"),)},
    )
    assert error.category is ErrorCategory.CONFIGURATION
    assert error.is_retryable is False
    assert error.to_safe_dict() == {
        "error_code": "configuration_invalid",
        "category": "configuration",
        "is_retryable": False,
        "safe_message": "Runtime configuration is invalid",
        "safe_details": {"count": 1, "field_names": ["log_level"]},
    }


def test_error_never_serializes_cause_text() -> None:
    sentinel = "DO_NOT_LEAK_ERROR_CAUSE"
    try:
        raise ValueError(sentinel)
    except ValueError as cause:
        error = ConfigurationError(ErrorCode.CONFIGURATION_INVALID)
        error.__cause__ = cause
    rendered = f"{error!r} {error} {error.to_safe_dict()}"
    assert sentinel not in rendered
