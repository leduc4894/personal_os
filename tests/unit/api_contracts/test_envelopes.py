"""Envelope contract tests: XOR invariant, safe serialization and derivation."""

from __future__ import annotations

from uuid import uuid7

import pytest
from pydantic import ValidationError

from personal_os.api_contracts import (
    ApiEnvelope,
    ApiErrorBody,
    ApiWarning,
    LivenessData,
    error_envelope,
    success_envelope,
)
from personal_os.diagnostics.events import SafeToken
from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import ConfigurationError


def test_success_and_error_envelopes_are_mutually_exclusive() -> None:
    request_id = uuid7()
    success = success_envelope(request_id=request_id, data=LivenessData())
    assert success.model_dump(mode="json") == {
        "request_id": str(request_id),
        "data": {"status": "live", "service": "api"},
        "warnings": [],
        "error": None,
    }
    with pytest.raises(ValidationError):
        ApiEnvelope[LivenessData](
            request_id=request_id,
            data=LivenessData(),
            warnings=(),
            error=ApiErrorBody(
                code=ErrorCode.INTERNAL_ERROR,
                message="An unexpected internal error occurred",
                retryable=False,
                details={},
            ),
        )


def test_envelope_rejects_missing_outcome() -> None:
    with pytest.raises(ValidationError, match="exactly one of data or error"):
        ApiEnvelope[LivenessData](
            request_id=uuid7(),
            data=None,
            warnings=(),
            error=None,
        )


def test_success_envelope_serializes_registered_warnings() -> None:
    request_id = uuid7()
    warning = ApiWarning(
        code="contract.updated",
        message="The response contract gained a field",
        details={"field_count": 1},
    )
    success = success_envelope(
        request_id=request_id,
        data=LivenessData(),
        warnings=(warning,),
    )
    assert success.model_dump(mode="json") == {
        "request_id": str(request_id),
        "data": {"status": "live", "service": "api"},
        "warnings": [
            {
                "code": "contract.updated",
                "message": "The response contract gained a field",
                "details": {"field_count": 1},
            }
        ],
        "error": None,
    }


def test_envelope_is_frozen_and_closed_for_extra_fields() -> None:
    success = success_envelope(request_id=uuid7(), data=LivenessData())
    with pytest.raises(ValidationError):
        success.data = None  # type: ignore[misc]
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ApiWarning(code="w1", message="m", details={}, unregistered_field="value")  # type: ignore[call-arg]


def test_warning_schema_constrains_code_message_and_details() -> None:
    valid = ApiWarning(
        code="w1",
        message="m",
        details={"count": 1, "names": ("a", "b"), "is_deprecated": True},
    )
    assert valid.details == {"count": 1, "names": ("a", "b"), "is_deprecated": True}
    for invalid_code in ("UPPER", "-leading", "a" * 65, "", "sp ace"):
        with pytest.raises(ValidationError):
            ApiWarning(code=invalid_code, message="m", details={})
    with pytest.raises(ValidationError):
        ApiWarning(code="w1", message="", details={})
    with pytest.raises(ValidationError):
        ApiWarning(code="w1", message="m" * 161, details={})
    for invalid_details in ({"nested": {"a": 1}}, {"ratio": 1.5}, {"missing": None}):
        with pytest.raises(ValidationError):
            ApiWarning(code="w1", message="m", details=invalid_details)


def test_error_envelope_copies_only_registered_safe_values() -> None:
    request_id = uuid7()
    error = ConfigurationError(
        ErrorCode.CONFIGURATION_INVALID,
        safe_details={
            "count": 2,
            "field_names": (SafeToken.parse("host"), SafeToken.parse("port")),
        },
    )
    failure = error_envelope(request_id=request_id, error=error)
    assert failure.data is None
    assert failure.error is not None
    assert failure.error.code is ErrorCode.CONFIGURATION_INVALID
    assert failure.error.message == "Runtime configuration is invalid"
    assert failure.error.retryable is False
    assert failure.error.details == {"count": 2, "field_names": ("host", "port")}
    assert failure.model_dump(mode="json") == {
        "request_id": str(request_id),
        "data": None,
        "warnings": [],
        "error": {
            "code": "configuration_invalid",
            "message": "Runtime configuration is invalid",
            "retryable": False,
            "details": {"count": 2, "field_names": ["host", "port"]},
        },
    }
