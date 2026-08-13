"""Closed error registry: categories, codes and per-code safe definitions."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final


class ErrorCategory(StrEnum):
    CONFIGURATION = "configuration"
    VALIDATION = "validation"
    AUTHORIZATION = "authorization"
    CONFLICT = "conflict"
    INTEGRITY = "integrity"
    DEPENDENCY = "dependency"
    INTERNAL = "internal"


class ErrorCode(StrEnum):
    CONFIGURATION_INVALID = "configuration_invalid"
    CONFIGURATION_UNKNOWN_KEY = "configuration_unknown_key"
    SECRET_FILE_MISSING = "secret_file_missing"
    SECRET_FILE_OUTSIDE_ROOT = "secret_file_outside_root"
    SECRET_FILE_INVALID_TYPE = "secret_file_invalid_type"
    SECRET_FILE_INSECURE_PERMISSIONS = "secret_file_insecure_permissions"
    SECRET_FILE_TOO_LARGE = "secret_file_too_large"
    SECRET_FILE_INVALID_ENCODING = "secret_file_invalid_encoding"
    SECRET_FILE_EMPTY = "secret_file_empty"
    DIAGNOSTIC_CONTEXT_INVALID = "diagnostic_context_invalid"
    DIAGNOSTIC_PAYLOAD_REJECTED = "diagnostic_payload_rejected"
    INTERNAL_ERROR = "internal_error"
    DATABASE_MIGRATION_CONFIGURATION_INVALID = "database_migration_configuration_invalid"
    DATABASE_CONNECTION_UNAVAILABLE = "database_connection_unavailable"
    DATABASE_MIGRATION_BUSY = "database_migration_busy"
    DATABASE_SCHEMA_CONTRACT_INVALID = "database_schema_contract_invalid"
    DATABASE_DESTRUCTIVE_DOWNGRADE_REFUSED = "database_destructive_downgrade_refused"


@dataclass(frozen=True, slots=True)
class ErrorDefinition:
    """Registry metadata for one error code: category, retryability and safe detail rules."""

    category: ErrorCategory
    is_retryable: bool
    safe_message: str
    allowed_detail_fields: frozenset[str]


ERROR_DEFINITIONS: Final[Mapping[ErrorCode, ErrorDefinition]] = MappingProxyType(
    {
        ErrorCode.CONFIGURATION_INVALID: ErrorDefinition(
            category=ErrorCategory.CONFIGURATION,
            is_retryable=False,
            safe_message="Runtime configuration is invalid",
            allowed_detail_fields=frozenset({"count", "field_names"}),
        ),
        ErrorCode.CONFIGURATION_UNKNOWN_KEY: ErrorDefinition(
            category=ErrorCategory.CONFIGURATION,
            is_retryable=False,
            safe_message="Runtime configuration contains an unsupported key",
            allowed_detail_fields=frozenset({"count"}),
        ),
        ErrorCode.SECRET_FILE_MISSING: ErrorDefinition(
            category=ErrorCategory.CONFIGURATION,
            is_retryable=False,
            safe_message="A required secret file is unavailable",
            allowed_detail_fields=frozenset({"reason"}),
        ),
        ErrorCode.SECRET_FILE_OUTSIDE_ROOT: ErrorDefinition(
            category=ErrorCategory.CONFIGURATION,
            is_retryable=False,
            safe_message="A secret file is outside the configured boundary",
            allowed_detail_fields=frozenset({"reason"}),
        ),
        ErrorCode.SECRET_FILE_INVALID_TYPE: ErrorDefinition(
            category=ErrorCategory.CONFIGURATION,
            is_retryable=False,
            safe_message="A secret path does not identify a regular file",
            allowed_detail_fields=frozenset({"reason"}),
        ),
        ErrorCode.SECRET_FILE_INSECURE_PERMISSIONS: ErrorDefinition(
            category=ErrorCategory.CONFIGURATION,
            is_retryable=False,
            safe_message="A secret file has unsafe write permissions",
            allowed_detail_fields=frozenset({"reason"}),
        ),
        ErrorCode.SECRET_FILE_TOO_LARGE: ErrorDefinition(
            category=ErrorCategory.CONFIGURATION,
            is_retryable=False,
            safe_message="A secret file exceeds the allowed size",
            allowed_detail_fields=frozenset({"reason"}),
        ),
        ErrorCode.SECRET_FILE_INVALID_ENCODING: ErrorDefinition(
            category=ErrorCategory.CONFIGURATION,
            is_retryable=False,
            safe_message="A secret file is not valid accepted text",
            allowed_detail_fields=frozenset({"reason"}),
        ),
        ErrorCode.SECRET_FILE_EMPTY: ErrorDefinition(
            category=ErrorCategory.CONFIGURATION,
            is_retryable=False,
            safe_message="A secret file contains no usable value",
            allowed_detail_fields=frozenset({"reason"}),
        ),
        ErrorCode.DIAGNOSTIC_CONTEXT_INVALID: ErrorDefinition(
            category=ErrorCategory.VALIDATION,
            is_retryable=False,
            safe_message="Diagnostic context input is invalid",
            allowed_detail_fields=frozenset({"reason"}),
        ),
        ErrorCode.DIAGNOSTIC_PAYLOAD_REJECTED: ErrorDefinition(
            category=ErrorCategory.VALIDATION,
            is_retryable=False,
            safe_message="Diagnostic data violated the safe event contract",
            allowed_detail_fields=frozenset({"reason", "count"}),
        ),
        ErrorCode.INTERNAL_ERROR: ErrorDefinition(
            category=ErrorCategory.INTERNAL,
            is_retryable=False,
            safe_message="An unexpected internal error occurred",
            allowed_detail_fields=frozenset(),
        ),
        ErrorCode.DATABASE_MIGRATION_CONFIGURATION_INVALID: ErrorDefinition(
            category=ErrorCategory.CONFIGURATION,
            is_retryable=False,
            safe_message="Database migration configuration is invalid",
            allowed_detail_fields=frozenset(),
        ),
        ErrorCode.DATABASE_CONNECTION_UNAVAILABLE: ErrorDefinition(
            category=ErrorCategory.DEPENDENCY,
            is_retryable=True,
            safe_message="The canonical database is unavailable",
            allowed_detail_fields=frozenset(),
        ),
        ErrorCode.DATABASE_MIGRATION_BUSY: ErrorDefinition(
            category=ErrorCategory.DEPENDENCY,
            is_retryable=True,
            safe_message="Another database migration is in progress",
            allowed_detail_fields=frozenset(),
        ),
        ErrorCode.DATABASE_SCHEMA_CONTRACT_INVALID: ErrorDefinition(
            category=ErrorCategory.INTEGRITY,
            is_retryable=False,
            safe_message="The canonical database schema contract is invalid",
            allowed_detail_fields=frozenset(),
        ),
        ErrorCode.DATABASE_DESTRUCTIVE_DOWNGRADE_REFUSED: ErrorDefinition(
            category=ErrorCategory.AUTHORIZATION,
            is_retryable=False,
            safe_message="Destructive database downgrade is not authorized",
            allowed_detail_fields=frozenset(),
        ),
    }
)


if set(ERROR_DEFINITIONS) != set(ErrorCode):
    raise RuntimeError("error definition registry is incomplete")
