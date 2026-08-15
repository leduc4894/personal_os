"""Registry contract tests for the identity bootstrap fragment."""

from personal_os.diagnostics.events import EVENT_DEFINITIONS, DiagnosticLevel, EventName, ResultCode
from personal_os.error_contracts.codes import (
    ERROR_DEFINITIONS,
    ErrorCategory,
    ErrorCode,
)


def test_identity_bootstrap_error_codes_match_design_table() -> None:
    input_invalid = ERROR_DEFINITIONS[ErrorCode.IDENTITY_BOOTSTRAP_INPUT_INVALID]
    assert input_invalid.category is ErrorCategory.VALIDATION
    assert input_invalid.is_retryable is False
    assert input_invalid.allowed_detail_fields == frozenset({"reason"})

    state_conflict = ERROR_DEFINITIONS[ErrorCode.IDENTITY_BOOTSTRAP_STATE_CONFLICT]
    assert state_conflict.category is ErrorCategory.CONFLICT
    assert state_conflict.is_retryable is False
    assert state_conflict.allowed_detail_fields == frozenset({})


def test_identity_bootstrap_events_match_design_registry() -> None:
    succeeded = EVENT_DEFINITIONS[EventName.IDENTITY_BOOTSTRAP_SUCCEEDED]
    assert succeeded.level is DiagnosticLevel.INFO
    assert succeeded.result_code is ResultCode.SUCCEEDED
    assert succeeded.allowed_fields == frozenset(
        {"outcome", "user_id", "workspace_id", "device_id"}
    )

    replayed = EVENT_DEFINITIONS[EventName.IDENTITY_BOOTSTRAP_REPLAYED]
    assert replayed.level is DiagnosticLevel.INFO
    assert replayed.result_code is ResultCode.SUCCEEDED

    rejected = EVENT_DEFINITIONS[EventName.IDENTITY_BOOTSTRAP_REJECTED]
    assert rejected.level is DiagnosticLevel.WARNING
    assert rejected.result_code is ResultCode.REJECTED
