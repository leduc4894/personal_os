"""Strict framework-neutral response envelopes, warnings and error bodies.

The envelope is the single outer response shape for every application and
health route: ``{request_id, data, warnings, error}`` with exactly one of
``data`` or ``error`` present. Models are frozen and closed for extra fields,
and no module here imports a web framework.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import ApplicationError

type ApiDetailValue = bool | int | str | tuple[bool | int | str, ...]


class ApiWarning(BaseModel):
    """One non-fatal advisory attached to a response.

    The schema is fixed now for contract stability; this child registers no
    warning vocabulary and every route returns an empty warning list. Later
    children must register public warning codes before using them.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    code: str = Field(pattern=r"^[a-z0-9][a-z0-9._:-]{0,63}$")
    message: str = Field(min_length=1, max_length=160)
    details: Mapping[str, ApiDetailValue]


class ApiErrorBody(BaseModel):
    """The failure body: registry code, safe message, retryability and details."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    code: ErrorCode
    message: str
    retryable: bool
    details: Mapping[str, ApiDetailValue]


class ApiEnvelope[DataT](BaseModel):
    """The single strict outer response shape for every API response."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    request_id: UUID
    data: DataT | None
    warnings: tuple[ApiWarning, ...] = ()
    error: ApiErrorBody | None

    @model_validator(mode="after")
    def require_one_outcome(self) -> ApiEnvelope[DataT]:
        if (self.data is None) == (self.error is None):
            raise ValueError("exactly one of data or error must be present")
        return self


def success_envelope[DataT](
    *,
    request_id: UUID,
    data: DataT,
    warnings: tuple[ApiWarning, ...] = (),
) -> ApiEnvelope[DataT]:
    """Build the success envelope: data present, error null."""
    return ApiEnvelope(
        request_id=request_id,
        data=data,
        warnings=warnings,
        error=None,
    )


def error_envelope(
    *,
    request_id: UUID,
    error: ApplicationError,
    warnings: tuple[ApiWarning, ...] = (),
) -> ApiEnvelope[None]:
    """Build the failure envelope from an error's registered safe payload.

    Code, safe message, retryability and details are copied only from
    ``ApplicationError.to_safe_dict()``; an arbitrary message or detail mapping
    is never accepted.
    """
    safe_payload = error.to_safe_dict()
    return ApiEnvelope(
        request_id=request_id,
        data=None,
        warnings=warnings,
        error=ApiErrorBody(
            code=ErrorCode(cast("str", safe_payload["error_code"])),
            message=cast("str", safe_payload["safe_message"]),
            retryable=cast("bool", safe_payload["is_retryable"]),
            details=cast("Mapping[str, ApiDetailValue]", safe_payload["safe_details"]),
        ),
    )
