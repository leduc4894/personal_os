from __future__ import annotations

import pytest

from personal_os.diagnostics.events import SafeToken
from personal_os.error_contracts.codes import ErrorCategory, ErrorCode
from personal_os.error_contracts.exceptions import (
    ConfigurationError,
    DatabaseMigrationError,
)
from personal_os.object_storage.errors import SPOOL_FREE_SPACE, ObjectStorageError


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


def test_database_migration_errors_use_closed_safe_metadata() -> None:
    cases = {
        ErrorCode.DATABASE_MIGRATION_CONFIGURATION_INVALID: (
            ErrorCategory.CONFIGURATION,
            False,
        ),
        ErrorCode.DATABASE_CONNECTION_UNAVAILABLE: (ErrorCategory.DEPENDENCY, True),
        ErrorCode.DATABASE_MIGRATION_BUSY: (ErrorCategory.DEPENDENCY, True),
        ErrorCode.DATABASE_SCHEMA_CONTRACT_INVALID: (ErrorCategory.INTEGRITY, False),
        ErrorCode.DATABASE_DESTRUCTIVE_DOWNGRADE_REFUSED: (
            ErrorCategory.AUTHORIZATION,
            False,
        ),
    }
    for error_code, (category, is_retryable) in cases.items():
        error = DatabaseMigrationError(error_code)
        assert error.category is category
        assert error.is_retryable is is_retryable
        assert error.safe_details == {}


def test_database_migration_error_never_renders_driver_cause() -> None:
    error = DatabaseMigrationError(ErrorCode.DATABASE_CONNECTION_UNAVAILABLE)
    error.__cause__ = RuntimeError("DO_NOT_LEAK_DATABASE_DRIVER")
    rendered = f"{error!r} {error} {error.to_safe_dict()}"
    assert "DO_NOT_LEAK_DATABASE_DRIVER" not in rendered


def test_object_storage_configuration_invalid_accepts_count_and_field_names() -> None:
    error = ObjectStorageError(
        ErrorCode.OBJECT_STORAGE_CONFIGURATION_INVALID,
        safe_details={"count": 2, "field_names": (SafeToken.parse("endpoint"),)},
    )
    assert error.to_safe_dict()["safe_details"] == {
        "count": 2,
        "field_names": ["endpoint"],
    }


def test_object_storage_input_invalid_accepts_only_reason() -> None:
    error = ObjectStorageError(
        ErrorCode.OBJECT_STORAGE_INPUT_INVALID,
        safe_details={"reason": SafeToken.parse("size_out_of_range")},
    )
    assert error.to_safe_dict()["safe_details"] == {"reason": "size_out_of_range"}


@pytest.mark.parametrize(
    "code",
    [
        ErrorCode.OBJECT_STORAGE_UNAVAILABLE,
        ErrorCode.OBJECT_STORAGE_ACCESS_DENIED,
        ErrorCode.OBJECT_STORAGE_CONTRACT_INVALID,
        ErrorCode.OBJECT_STORAGE_OBJECT_MISSING,
        ErrorCode.OBJECT_STORAGE_INTEGRITY_FAILED,
        ErrorCode.OBJECT_STORAGE_METADATA_CONFLICT,
    ],
)
def test_other_object_storage_codes_accept_no_detail_field(code: ErrorCode) -> None:
    with pytest.raises(ValueError, match="not registered for this error code"):
        ObjectStorageError(code, safe_details={"reason": SafeToken.parse("size_out_of_range")})


def test_object_storage_busy_accepts_only_reason() -> None:
    error = ObjectStorageError(
        ErrorCode.OBJECT_STORAGE_BUSY,
        safe_details={"reason": SPOOL_FREE_SPACE},
    )
    assert error.to_safe_dict()["safe_details"] == {"reason": "spool_free_space"}
    with pytest.raises(ValueError, match="not registered for this error code"):
        ObjectStorageError(ErrorCode.OBJECT_STORAGE_BUSY, safe_details={"count": 1})
