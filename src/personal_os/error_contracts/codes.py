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
    CONFIGURATION_SECRET_INVALID = "configuration_secret_invalid"
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
    OBJECT_STORAGE_CONFIGURATION_INVALID = "object_storage_configuration_invalid"
    OBJECT_STORAGE_INPUT_INVALID = "object_storage_input_invalid"
    OBJECT_STORAGE_BUSY = "object_storage_busy"
    OBJECT_STORAGE_UNAVAILABLE = "object_storage_unavailable"
    OBJECT_STORAGE_ACCESS_DENIED = "object_storage_access_denied"
    OBJECT_STORAGE_CONTRACT_INVALID = "object_storage_contract_invalid"
    OBJECT_STORAGE_OBJECT_MISSING = "object_storage_object_missing"
    OBJECT_STORAGE_INTEGRITY_FAILED = "object_storage_integrity_failed"
    OBJECT_STORAGE_METADATA_CONFLICT = "object_storage_metadata_conflict"
    SOURCE_PUBLISH_INPUT_INVALID = "source_publish_input_invalid"
    SOURCE_NOT_FOUND = "source_not_found"
    SOURCE_ALREADY_EXISTS = "source_already_exists"
    SOURCE_STATE_INVALID = "source_state_invalid"
    SOURCE_VERSION_CONFLICT = "source_version_conflict"
    SOURCE_IDEMPOTENCY_MISMATCH = "source_idempotency_mismatch"
    SOURCE_EVENT_IDENTITY_MISMATCH = "source_event_identity_mismatch"
    SOURCE_VERIFIED_RECEIPT_STALE = "source_verified_receipt_stale"
    SOURCE_CONTENT_OBJECT_CONFLICT = "source_content_object_conflict"
    SOURCE_CONCURRENCY_BUSY = "source_concurrency_busy"
    SOURCE_CONCURRENCY_INVARIANT_FAILED = "source_concurrency_invariant_failed"
    SOURCE_COMMIT_OUTCOME_UNKNOWN = "source_commit_outcome_unknown"
    PROJECTION_DISPATCH_UNAVAILABLE = "projection_dispatch_unavailable"
    PROJECTION_INTENT_CONTRACT_INVALID = "projection_intent_contract_invalid"
    IDENTITY_BOOTSTRAP_INPUT_INVALID = "identity_bootstrap_input_invalid"
    IDENTITY_BOOTSTRAP_STATE_CONFLICT = "identity_bootstrap_state_conflict"
    CANONICAL_READ_STATE_INVALID = "canonical_read_state_invalid"
    CANONICAL_RECOVERY_ENVIRONMENT_REFUSED = "canonical_recovery_environment_refused"
    CANONICAL_RECOVERY_CONFIGURATION_INVALID = "canonical_recovery_configuration_invalid"
    CANONICAL_RECOVERY_SNAPSHOT_BUSY = "canonical_recovery_snapshot_busy"
    CANONICAL_RECOVERY_BUNDLE_EXISTS = "canonical_recovery_bundle_exists"
    CANONICAL_RECOVERY_BUNDLE_INVALID = "canonical_recovery_bundle_invalid"
    CANONICAL_RECOVERY_TARGET_NOT_EMPTY = "canonical_recovery_target_not_empty"
    CANONICAL_RECOVERY_DEPENDENCY_UNAVAILABLE = "canonical_recovery_dependency_unavailable"
    CANONICAL_RECOVERY_INTEGRITY_FAILED = "canonical_recovery_integrity_failed"
    CANONICAL_RECOVERY_RESTORE_FAILED = "canonical_recovery_restore_failed"
    API_REQUEST_MALFORMED = "api_request_malformed"
    API_REQUEST_VALIDATION_FAILED = "api_request_validation_failed"
    API_ROUTE_NOT_FOUND = "api_route_not_found"
    API_METHOD_NOT_ALLOWED = "api_method_not_allowed"


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
        ErrorCode.CONFIGURATION_SECRET_INVALID: ErrorDefinition(
            category=ErrorCategory.CONFIGURATION,
            is_retryable=False,
            safe_message="A configured secret value is invalid",
            allowed_detail_fields=frozenset({"reason"}),
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
        ErrorCode.OBJECT_STORAGE_CONFIGURATION_INVALID: ErrorDefinition(
            category=ErrorCategory.CONFIGURATION,
            is_retryable=False,
            safe_message="Object-storage configuration is invalid",
            allowed_detail_fields=frozenset({"count", "field_names"}),
        ),
        ErrorCode.OBJECT_STORAGE_INPUT_INVALID: ErrorDefinition(
            category=ErrorCategory.VALIDATION,
            is_retryable=False,
            safe_message="Object-storage input is invalid",
            allowed_detail_fields=frozenset({"reason"}),
        ),
        ErrorCode.OBJECT_STORAGE_BUSY: ErrorDefinition(
            category=ErrorCategory.DEPENDENCY,
            is_retryable=True,
            safe_message="Local object-storage capacity is temporarily unavailable",
            allowed_detail_fields=frozenset(),
        ),
        ErrorCode.OBJECT_STORAGE_UNAVAILABLE: ErrorDefinition(
            category=ErrorCategory.DEPENDENCY,
            is_retryable=True,
            safe_message="Canonical object storage is temporarily unavailable",
            allowed_detail_fields=frozenset(),
        ),
        ErrorCode.OBJECT_STORAGE_ACCESS_DENIED: ErrorDefinition(
            category=ErrorCategory.AUTHORIZATION,
            is_retryable=False,
            safe_message="Canonical object storage denied access",
            allowed_detail_fields=frozenset(),
        ),
        ErrorCode.OBJECT_STORAGE_CONTRACT_INVALID: ErrorDefinition(
            category=ErrorCategory.INTEGRITY,
            is_retryable=False,
            safe_message="Object-store response violated the adapter contract",
            allowed_detail_fields=frozenset(),
        ),
        ErrorCode.OBJECT_STORAGE_OBJECT_MISSING: ErrorDefinition(
            category=ErrorCategory.INTEGRITY,
            is_retryable=False,
            safe_message="An expected canonical object is missing",
            allowed_detail_fields=frozenset(),
        ),
        ErrorCode.OBJECT_STORAGE_INTEGRITY_FAILED: ErrorDefinition(
            category=ErrorCategory.INTEGRITY,
            is_retryable=False,
            safe_message="Canonical object integrity verification failed",
            allowed_detail_fields=frozenset(),
        ),
        ErrorCode.OBJECT_STORAGE_METADATA_CONFLICT: ErrorDefinition(
            category=ErrorCategory.CONFLICT,
            is_retryable=False,
            safe_message=("Existing canonical object metadata conflicts with expected metadata"),
            allowed_detail_fields=frozenset(),
        ),
        ErrorCode.SOURCE_PUBLISH_INPUT_INVALID: ErrorDefinition(
            category=ErrorCategory.VALIDATION,
            is_retryable=False,
            safe_message="Source publication input is invalid",
            allowed_detail_fields=frozenset({"reason"}),
        ),
        ErrorCode.SOURCE_NOT_FOUND: ErrorDefinition(
            category=ErrorCategory.CONFLICT,
            is_retryable=False,
            safe_message="The referenced source does not exist",
            allowed_detail_fields=frozenset({"source_id"}),
        ),
        ErrorCode.SOURCE_ALREADY_EXISTS: ErrorDefinition(
            category=ErrorCategory.CONFLICT,
            is_retryable=False,
            safe_message="A source with this identity already exists",
            allowed_detail_fields=frozenset({"source_id"}),
        ),
        ErrorCode.SOURCE_STATE_INVALID: ErrorDefinition(
            category=ErrorCategory.CONFLICT,
            is_retryable=False,
            safe_message="The source is not in a state that accepts publication",
            allowed_detail_fields=frozenset({"source_id", "source_state"}),
        ),
        ErrorCode.SOURCE_VERSION_CONFLICT: ErrorDefinition(
            category=ErrorCategory.CONFLICT,
            is_retryable=False,
            safe_message="The publication base conflicts with the current source version",
            allowed_detail_fields=frozenset({"source_id", "current_version_id", "content_version"}),
        ),
        ErrorCode.SOURCE_IDEMPOTENCY_MISMATCH: ErrorDefinition(
            category=ErrorCategory.CONFLICT,
            is_retryable=False,
            safe_message="The idempotency key was reused with a different request",
            allowed_detail_fields=frozenset({"source_id"}),
        ),
        ErrorCode.SOURCE_EVENT_IDENTITY_MISMATCH: ErrorDefinition(
            category=ErrorCategory.CONFLICT,
            is_retryable=False,
            safe_message="The event identity was reused with a different request",
            allowed_detail_fields=frozenset({"source_id", "event_id"}),
        ),
        ErrorCode.SOURCE_VERIFIED_RECEIPT_STALE: ErrorDefinition(
            category=ErrorCategory.VALIDATION,
            is_retryable=False,
            safe_message="The verified receipt is stale and must be reverified",
            allowed_detail_fields=frozenset({"reason"}),
        ),
        ErrorCode.SOURCE_CONTENT_OBJECT_CONFLICT: ErrorDefinition(
            category=ErrorCategory.INTEGRITY,
            is_retryable=False,
            safe_message=(
                "Existing canonical content-object metadata conflicts with expected metadata"
            ),
            allowed_detail_fields=frozenset({"source_id"}),
        ),
        ErrorCode.SOURCE_CONCURRENCY_BUSY: ErrorDefinition(
            category=ErrorCategory.DEPENDENCY,
            is_retryable=True,
            safe_message="Source publication concurrency limits are temporarily exhausted",
            allowed_detail_fields=frozenset({"source_id"}),
        ),
        ErrorCode.SOURCE_CONCURRENCY_INVARIANT_FAILED: ErrorDefinition(
            category=ErrorCategory.INTEGRITY,
            is_retryable=False,
            safe_message="A source publication concurrency invariant failed",
            allowed_detail_fields=frozenset({"source_id"}),
        ),
        ErrorCode.SOURCE_COMMIT_OUTCOME_UNKNOWN: ErrorDefinition(
            category=ErrorCategory.DEPENDENCY,
            is_retryable=True,
            safe_message="The publication commit outcome could not be determined",
            allowed_detail_fields=frozenset({"source_id"}),
        ),
        ErrorCode.PROJECTION_DISPATCH_UNAVAILABLE: ErrorDefinition(
            category=ErrorCategory.DEPENDENCY,
            is_retryable=True,
            safe_message="Projection dispatch is temporarily unavailable",
            allowed_detail_fields=frozenset({"projection_kind"}),
        ),
        ErrorCode.PROJECTION_INTENT_CONTRACT_INVALID: ErrorDefinition(
            category=ErrorCategory.INTEGRITY,
            is_retryable=False,
            safe_message="A projection intent violated its dispatch contract",
            allowed_detail_fields=frozenset({"projection_kind"}),
        ),
        ErrorCode.IDENTITY_BOOTSTRAP_INPUT_INVALID: ErrorDefinition(
            category=ErrorCategory.VALIDATION,
            is_retryable=False,
            safe_message="identity bootstrap input is invalid",
            allowed_detail_fields=frozenset({"reason"}),
        ),
        ErrorCode.IDENTITY_BOOTSTRAP_STATE_CONFLICT: ErrorDefinition(
            category=ErrorCategory.CONFLICT,
            is_retryable=False,
            safe_message="identity bootstrap state conflicts with canonical state",
            allowed_detail_fields=frozenset({}),
        ),
        ErrorCode.CANONICAL_READ_STATE_INVALID: ErrorDefinition(
            category=ErrorCategory.INTEGRITY,
            is_retryable=False,
            safe_message="The canonical current-source reference is missing or inconsistent",
            allowed_detail_fields=frozenset({"source_id"}),
        ),
        ErrorCode.CANONICAL_RECOVERY_ENVIRONMENT_REFUSED: ErrorDefinition(
            category=ErrorCategory.AUTHORIZATION,
            is_retryable=False,
            safe_message="The recovery environment is not authorized for this operation",
            allowed_detail_fields=frozenset({"operation"}),
        ),
        ErrorCode.CANONICAL_RECOVERY_CONFIGURATION_INVALID: ErrorDefinition(
            category=ErrorCategory.CONFIGURATION,
            is_retryable=False,
            safe_message="Recovery configuration is invalid",
            allowed_detail_fields=frozenset({"reason"}),
        ),
        ErrorCode.CANONICAL_RECOVERY_SNAPSHOT_BUSY: ErrorDefinition(
            category=ErrorCategory.DEPENDENCY,
            is_retryable=True,
            safe_message="A canonical backup snapshot is temporarily unavailable",
            allowed_detail_fields=frozenset(),
        ),
        ErrorCode.CANONICAL_RECOVERY_BUNDLE_EXISTS: ErrorDefinition(
            category=ErrorCategory.CONFLICT,
            is_retryable=False,
            safe_message="A recovery bundle with this identity already exists",
            allowed_detail_fields=frozenset({"bundle_id"}),
        ),
        ErrorCode.CANONICAL_RECOVERY_BUNDLE_INVALID: ErrorDefinition(
            category=ErrorCategory.INTEGRITY,
            is_retryable=False,
            safe_message="The recovery bundle failed canonical manifest validation",
            allowed_detail_fields=frozenset({"reason"}),
        ),
        ErrorCode.CANONICAL_RECOVERY_TARGET_NOT_EMPTY: ErrorDefinition(
            category=ErrorCategory.CONFLICT,
            is_retryable=False,
            safe_message="The restore target is not empty",
            allowed_detail_fields=frozenset(),
        ),
        ErrorCode.CANONICAL_RECOVERY_DEPENDENCY_UNAVAILABLE: ErrorDefinition(
            category=ErrorCategory.DEPENDENCY,
            is_retryable=True,
            safe_message="A recovery dependency is temporarily unavailable",
            allowed_detail_fields=frozenset({"dependency"}),
        ),
        ErrorCode.CANONICAL_RECOVERY_INTEGRITY_FAILED: ErrorDefinition(
            category=ErrorCategory.INTEGRITY,
            is_retryable=False,
            safe_message="Recovery integrity verification failed",
            allowed_detail_fields=frozenset({"component"}),
        ),
        ErrorCode.CANONICAL_RECOVERY_RESTORE_FAILED: ErrorDefinition(
            category=ErrorCategory.INTEGRITY,
            is_retryable=False,
            safe_message="The canonical restore failed",
            allowed_detail_fields=frozenset({"component"}),
        ),
        ErrorCode.API_REQUEST_MALFORMED: ErrorDefinition(
            category=ErrorCategory.VALIDATION,
            is_retryable=False,
            safe_message="The API request is malformed",
            allowed_detail_fields=frozenset(),
        ),
        ErrorCode.API_REQUEST_VALIDATION_FAILED: ErrorDefinition(
            category=ErrorCategory.VALIDATION,
            is_retryable=False,
            safe_message="The API request failed validation",
            allowed_detail_fields=frozenset({"field_names"}),
        ),
        ErrorCode.API_ROUTE_NOT_FOUND: ErrorDefinition(
            category=ErrorCategory.VALIDATION,
            is_retryable=False,
            safe_message="The requested API route does not exist",
            allowed_detail_fields=frozenset(),
        ),
        ErrorCode.API_METHOD_NOT_ALLOWED: ErrorDefinition(
            category=ErrorCategory.VALIDATION,
            is_retryable=False,
            safe_message="The API route does not allow this method",
            allowed_detail_fields=frozenset(),
        ),
    }
)


if set(ERROR_DEFINITIONS) != set(ErrorCode):
    raise RuntimeError("error definition registry is incomplete")
