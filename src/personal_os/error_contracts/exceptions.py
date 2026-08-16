"""Typed application errors bound to the closed error registry."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from uuid import UUID

from personal_os.diagnostics.events import (
    ObjectDigestPrefix,
    SafeDiagnosticScalar,
    SafeDiagnosticValue,
    SafeToken,
    ShortDigest,
)
from personal_os.error_contracts.codes import (
    ERROR_DEFINITIONS,
    ErrorCategory,
    ErrorCode,
    ErrorDefinition,
)

_MAX_SAFE_INTEGER: int = 2**63 - 1
_MAX_TUPLE_LENGTH: int = 64


def _validate_safe_scalar(value: object) -> None:
    """Reject any value outside the closed safe-scalar union.

    ``bool`` is accepted as its own type before the integer branch so booleans never
    flow through the integer range check; arbitrary strings, floats, lists, mappings,
    paths and user-defined objects are rejected.
    """
    if isinstance(value, bool):
        return
    if isinstance(value, int):
        if value < 0 or value > _MAX_SAFE_INTEGER:
            raise ValueError("integer safe detail is outside the accepted range")
        return
    if isinstance(value, StrEnum):
        return
    if isinstance(value, UUID):
        return
    if isinstance(value, SafeToken | ShortDigest | ObjectDigestPrefix):
        return
    raise ValueError("safe detail value is not an accepted safe scalar")


def _validate_safe_details(
    definition: ErrorDefinition,
    safe_details: Mapping[str, SafeDiagnosticValue],
) -> dict[str, SafeDiagnosticValue]:
    allowed = definition.allowed_detail_fields
    validated: dict[str, SafeDiagnosticValue] = {}
    for key, value in safe_details.items():
        if key not in allowed:
            raise ValueError("safe detail key is not registered for this error code")
        if isinstance(value, tuple):
            if len(value) > _MAX_TUPLE_LENGTH:
                raise ValueError("safe detail tuple exceeds the accepted length")
            for item in value:
                _validate_safe_scalar(item)
        else:
            _validate_safe_scalar(value)
        validated[key] = value
    return validated


def _canonical_uuid_text(value: UUID) -> str:
    """Render a UUID in canonical 8-4-4-4-12 form without coercing through ``str``."""

    hex_text = value.hex
    return f"{hex_text[:8]}-{hex_text[8:12]}-{hex_text[12:16]}-{hex_text[16:20]}-{hex_text[20:]}"


def _serialize_safe_scalar(value: SafeDiagnosticScalar) -> object:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, UUID):
        return _canonical_uuid_text(value)
    if isinstance(value, SafeToken | ShortDigest | ObjectDigestPrefix):
        return value.value
    # Unreachable: validation rejects every other type before serialization runs.
    raise TypeError("cannot serialize an unsupported safe scalar")


def _serialize_safe_value(value: SafeDiagnosticValue) -> object:
    if isinstance(value, tuple):
        return [_serialize_safe_scalar(item) for item in value]
    return _serialize_safe_scalar(value)


class ApplicationError(Exception):
    """Base typed error bound to the closed error registry.

    Category, retryability and safe message always come from the registry. ``__cause__``,
    exception arguments and rejected input are never serialized. The registry message and
    code are the only text passed to ``Exception.__init__``, so ``str(error)`` and
    ``repr(error)`` never expose a chained cause.
    """

    allowed_codes: frozenset[ErrorCode] = frozenset(ErrorCode)

    def __init__(
        self,
        error_code: ErrorCode,
        *,
        safe_details: Mapping[str, SafeDiagnosticValue] | None = None,
    ) -> None:
        if error_code not in self.allowed_codes:
            raise ValueError("error code is not valid for this exception type")
        definition = ERROR_DEFINITIONS[error_code]
        details = _validate_safe_details(definition, safe_details or {})
        self.error_code: ErrorCode = error_code
        self.category: ErrorCategory = definition.category
        self.is_retryable: bool = definition.is_retryable
        self.safe_message: str = definition.safe_message
        self.safe_details: Mapping[str, SafeDiagnosticValue] = MappingProxyType(details)
        super().__init__(f"{error_code.value}: {definition.safe_message}")

    def to_safe_dict(self) -> dict[str, object]:
        return {
            "error_code": self.error_code.value,
            "category": self.category.value,
            "is_retryable": self.is_retryable,
            "safe_message": self.safe_message,
            "safe_details": {
                key: _serialize_safe_value(value) for key, value in self.safe_details.items()
            },
        }


class ConfigurationError(ApplicationError):
    """Configuration shape, unknown-key or invalid-secret-value failures."""

    allowed_codes = frozenset(
        {
            ErrorCode.CONFIGURATION_INVALID,
            ErrorCode.CONFIGURATION_UNKNOWN_KEY,
            ErrorCode.CONFIGURATION_SECRET_INVALID,
        }
    )


class SecretFileError(ApplicationError):
    """Secret-file resolution failures: missing, out of bounds, wrong type or unreadable."""

    allowed_codes = frozenset(
        {
            ErrorCode.SECRET_FILE_MISSING,
            ErrorCode.SECRET_FILE_OUTSIDE_ROOT,
            ErrorCode.SECRET_FILE_INVALID_TYPE,
            ErrorCode.SECRET_FILE_INSECURE_PERMISSIONS,
            ErrorCode.SECRET_FILE_TOO_LARGE,
            ErrorCode.SECRET_FILE_INVALID_ENCODING,
            ErrorCode.SECRET_FILE_EMPTY,
        }
    )


class DiagnosticContextError(ApplicationError):
    """Invalid diagnostic context input or rejected diagnostic payload."""

    allowed_codes = frozenset(
        {
            ErrorCode.DIAGNOSTIC_CONTEXT_INVALID,
            ErrorCode.DIAGNOSTIC_PAYLOAD_REJECTED,
        }
    )


class InternalApplicationError(ApplicationError):
    """Unexpected internal failure mapped at a composition boundary."""

    allowed_codes = frozenset({ErrorCode.INTERNAL_ERROR})


class DatabaseMigrationError(ApplicationError):
    """Safe migration configuration, dependency and schema failures."""

    allowed_codes = frozenset(
        {
            ErrorCode.DATABASE_MIGRATION_CONFIGURATION_INVALID,
            ErrorCode.DATABASE_CONNECTION_UNAVAILABLE,
            ErrorCode.DATABASE_MIGRATION_BUSY,
            ErrorCode.DATABASE_SCHEMA_CONTRACT_INVALID,
            ErrorCode.DATABASE_DESTRUCTIVE_DOWNGRADE_REFUSED,
        }
    )


class ApiTransportError(ApplicationError):
    """API transport failures: malformed, invalid, unrouted or unmatched requests.

    The closed code set covers request-shape validation at the HTTP boundary.
    Rejected client input stays out of the typed error; only registered safe
    detail fields (``field_names`` for failed request validation) survive.
    """

    allowed_codes = frozenset(
        {
            ErrorCode.API_REQUEST_MALFORMED,
            ErrorCode.API_REQUEST_VALIDATION_FAILED,
            ErrorCode.API_ROUTE_NOT_FOUND,
            ErrorCode.API_METHOD_NOT_ALLOWED,
        }
    )
