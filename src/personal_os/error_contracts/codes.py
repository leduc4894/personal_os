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
    AUTHENTICATION_REQUIRED = "authentication_required"
    AUTHENTICATION_FAILED = "authentication_failed"
    AUTHENTICATION_RATE_LIMITED = "authentication_rate_limited"
    RECENT_AUTHENTICATION_REQUIRED = "recent_authentication_required"
    CSRF_VALIDATION_FAILED = "csrf_validation_failed"
    AUTHORIZATION_SCOPE_DENIED = "authorization_scope_denied"
    TOTP_ENROLLMENT_STATE_INVALID = "totp_enrollment_state_invalid"
    DEVICE_AUTHORIZATION_PENDING = "device_authorization_pending"
    DEVICE_AUTHORIZATION_SLOW_DOWN = "device_authorization_slow_down"
    DEVICE_AUTHORIZATION_DENIED = "device_authorization_denied"
    DEVICE_AUTHORIZATION_EXPIRED = "device_authorization_expired"
    DEVICE_AUTHORIZATION_STATE_INVALID = "device_authorization_state_invalid"
    DEVICE_REVOCATION_CONFIRMATION_INVALID = "device_revocation_confirmation_invalid"
    DEVICE_CREDENTIAL_INVALID = "device_credential_invalid"
    DEVICE_REVOKED = "device_revoked"
    DEVICE_TOKEN_REUSE_DETECTED = "device_token_reuse_detected"
    PLUGIN_VERSION_UNSUPPORTED = "plugin_version_unsupported"
    EXCLUSION_POLICY_INPUT_INVALID = "exclusion_policy_input_invalid"
    EXCLUSION_POLICY_NOT_INITIALIZED = "exclusion_policy_not_initialized"
    EXCLUSION_POLICY_DRAFT_CONFLICT = "exclusion_policy_draft_conflict"
    EXCLUSION_POLICY_PREVIEW_PENDING = "exclusion_policy_preview_pending"
    EXCLUSION_POLICY_PREVIEW_FAILED = "exclusion_policy_preview_failed"
    EXCLUSION_POLICY_PREVIEW_EXPIRED = "exclusion_policy_preview_expired"
    EXCLUSION_POLICY_PREVIEW_STALE = "exclusion_policy_preview_stale"
    EXCLUSION_POLICY_CONFIRMATION_INVALID = "exclusion_policy_confirmation_invalid"
    EXCLUSION_POLICY_DENIED = "exclusion_policy_denied"
    EXCLUSION_POLICY_INDETERMINATE = "exclusion_policy_indeterminate"
    EXCLUSION_POLICY_SNAPSHOT_OUTDATED = "exclusion_policy_snapshot_outdated"
    EXCLUSION_POLICY_SIGNING_UNAVAILABLE = "exclusion_policy_signing_unavailable"
    EXCLUSION_POLICY_COMMIT_OUTCOME_UNKNOWN = "exclusion_policy_commit_outcome_unknown"
    SMALL_FILE_PREFLIGHT_INVALID = "small_file_preflight_invalid"
    SMALL_FILE_OPERATION_NOT_FOUND = "small_file_operation_not_found"
    SMALL_FILE_OPERATION_EXPIRED = "small_file_operation_expired"
    SMALL_FILE_OPERATION_IDENTITY_MISMATCH = "small_file_operation_identity_mismatch"
    SMALL_FILE_SIZE_LIMIT_EXCEEDED = "small_file_size_limit_exceeded"
    SMALL_FILE_CONTENT_INTEGRITY_FAILED = "small_file_content_integrity_failed"
    SMALL_FILE_UPLOAD_STATE_INVALID = "small_file_upload_state_invalid"


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
        ErrorCode.AUTHENTICATION_REQUIRED: ErrorDefinition(
            category=ErrorCategory.AUTHORIZATION,
            is_retryable=False,
            safe_message="Authentication is required",
            allowed_detail_fields=frozenset(),
        ),
        ErrorCode.AUTHENTICATION_FAILED: ErrorDefinition(
            category=ErrorCategory.AUTHORIZATION,
            is_retryable=False,
            safe_message="Authentication failed",
            allowed_detail_fields=frozenset(),
        ),
        ErrorCode.AUTHENTICATION_RATE_LIMITED: ErrorDefinition(
            category=ErrorCategory.AUTHORIZATION,
            is_retryable=True,
            safe_message="Authentication is temporarily rate limited",
            allowed_detail_fields=frozenset({"retry_after_seconds"}),
        ),
        ErrorCode.RECENT_AUTHENTICATION_REQUIRED: ErrorDefinition(
            category=ErrorCategory.AUTHORIZATION,
            is_retryable=False,
            safe_message="Recent re-authentication is required",
            allowed_detail_fields=frozenset(),
        ),
        ErrorCode.CSRF_VALIDATION_FAILED: ErrorDefinition(
            category=ErrorCategory.AUTHORIZATION,
            is_retryable=False,
            safe_message="CSRF validation failed",
            allowed_detail_fields=frozenset(),
        ),
        ErrorCode.AUTHORIZATION_SCOPE_DENIED: ErrorDefinition(
            category=ErrorCategory.AUTHORIZATION,
            is_retryable=False,
            safe_message="The granted scope does not authorize this operation",
            allowed_detail_fields=frozenset(),
        ),
        ErrorCode.TOTP_ENROLLMENT_STATE_INVALID: ErrorDefinition(
            category=ErrorCategory.CONFLICT,
            is_retryable=False,
            safe_message="The TOTP enrollment state does not accept this action",
            allowed_detail_fields=frozenset(),
        ),
        ErrorCode.DEVICE_AUTHORIZATION_PENDING: ErrorDefinition(
            category=ErrorCategory.CONFLICT,
            is_retryable=True,
            safe_message="Device authorization is still pending",
            allowed_detail_fields=frozenset({"retry_after_seconds"}),
        ),
        ErrorCode.DEVICE_AUTHORIZATION_SLOW_DOWN: ErrorDefinition(
            category=ErrorCategory.AUTHORIZATION,
            is_retryable=True,
            safe_message="Device authorization polling is too frequent",
            allowed_detail_fields=frozenset({"retry_after_seconds"}),
        ),
        ErrorCode.DEVICE_AUTHORIZATION_DENIED: ErrorDefinition(
            category=ErrorCategory.AUTHORIZATION,
            is_retryable=False,
            safe_message="Device authorization was denied",
            allowed_detail_fields=frozenset(),
        ),
        ErrorCode.DEVICE_AUTHORIZATION_EXPIRED: ErrorDefinition(
            category=ErrorCategory.CONFLICT,
            is_retryable=False,
            safe_message="Device authorization expired",
            allowed_detail_fields=frozenset(),
        ),
        ErrorCode.DEVICE_AUTHORIZATION_STATE_INVALID: ErrorDefinition(
            category=ErrorCategory.CONFLICT,
            is_retryable=False,
            safe_message="Device authorization state does not accept this action",
            allowed_detail_fields=frozenset(),
        ),
        ErrorCode.DEVICE_REVOCATION_CONFIRMATION_INVALID: ErrorDefinition(
            category=ErrorCategory.CONFLICT,
            is_retryable=False,
            safe_message="The device-name confirmation does not match",
            allowed_detail_fields=frozenset(),
        ),
        ErrorCode.DEVICE_CREDENTIAL_INVALID: ErrorDefinition(
            category=ErrorCategory.AUTHORIZATION,
            is_retryable=False,
            safe_message="The device credential is invalid",
            allowed_detail_fields=frozenset(),
        ),
        ErrorCode.DEVICE_REVOKED: ErrorDefinition(
            category=ErrorCategory.AUTHORIZATION,
            is_retryable=False,
            safe_message="The device is revoked",
            allowed_detail_fields=frozenset(),
        ),
        ErrorCode.DEVICE_TOKEN_REUSE_DETECTED: ErrorDefinition(
            category=ErrorCategory.AUTHORIZATION,
            is_retryable=False,
            safe_message="Device credential reuse was detected",
            allowed_detail_fields=frozenset(),
        ),
        ErrorCode.PLUGIN_VERSION_UNSUPPORTED: ErrorDefinition(
            category=ErrorCategory.VALIDATION,
            is_retryable=False,
            safe_message="The plugin version is not supported",
            allowed_detail_fields=frozenset({"approved_version_bounds"}),
        ),
        # The exclusion-policy block of the design error contract (spec 19).
        # HTTP statuses come from that table (422/409/410/403/503) and are wired
        # into the closed api_contracts status map when the routes land.
        ErrorCode.EXCLUSION_POLICY_INPUT_INVALID: ErrorDefinition(
            category=ErrorCategory.VALIDATION,
            is_retryable=False,
            safe_message="Exclusion policy input is invalid",
            allowed_detail_fields=frozenset({"reason", "rule_index"}),
        ),
        ErrorCode.EXCLUSION_POLICY_NOT_INITIALIZED: ErrorDefinition(
            category=ErrorCategory.CONFLICT,
            is_retryable=False,
            safe_message="No exclusion policy revision has been published",
            allowed_detail_fields=frozenset(),
        ),
        ErrorCode.EXCLUSION_POLICY_DRAFT_CONFLICT: ErrorDefinition(
            category=ErrorCategory.CONFLICT,
            is_retryable=False,
            safe_message="The exclusion policy draft was modified concurrently",
            allowed_detail_fields=frozenset({"current_draft_version"}),
        ),
        ErrorCode.EXCLUSION_POLICY_PREVIEW_PENDING: ErrorDefinition(
            category=ErrorCategory.CONFLICT,
            is_retryable=True,
            safe_message="The exclusion policy preview is not ready yet",
            allowed_detail_fields=frozenset({"retry_after_seconds"}),
        ),
        ErrorCode.EXCLUSION_POLICY_PREVIEW_FAILED: ErrorDefinition(
            category=ErrorCategory.CONFLICT,
            is_retryable=False,
            safe_message="The exclusion policy preview failed",
            allowed_detail_fields=frozenset({"reason"}),
        ),
        ErrorCode.EXCLUSION_POLICY_PREVIEW_EXPIRED: ErrorDefinition(
            category=ErrorCategory.CONFLICT,
            is_retryable=False,
            safe_message="The exclusion policy preview expired",
            allowed_detail_fields=frozenset(),
        ),
        ErrorCode.EXCLUSION_POLICY_PREVIEW_STALE: ErrorDefinition(
            category=ErrorCategory.CONFLICT,
            is_retryable=False,
            safe_message="The exclusion policy preview is stale",
            allowed_detail_fields=frozenset({"reason"}),
        ),
        ErrorCode.EXCLUSION_POLICY_CONFIRMATION_INVALID: ErrorDefinition(
            category=ErrorCategory.CONFLICT,
            is_retryable=False,
            safe_message="The exclusion policy confirmation phrase is invalid",
            allowed_detail_fields=frozenset(),
        ),
        ErrorCode.EXCLUSION_POLICY_DENIED: ErrorDefinition(
            category=ErrorCategory.AUTHORIZATION,
            is_retryable=False,
            safe_message="The exclusion policy denied this operation",
            allowed_detail_fields=frozenset({"policy_revision_number"}),
        ),
        ErrorCode.EXCLUSION_POLICY_INDETERMINATE: ErrorDefinition(
            category=ErrorCategory.AUTHORIZATION,
            is_retryable=False,
            safe_message="The exclusion policy decision is indeterminate",
            allowed_detail_fields=frozenset({"reason"}),
        ),
        ErrorCode.EXCLUSION_POLICY_SNAPSHOT_OUTDATED: ErrorDefinition(
            category=ErrorCategory.CONFLICT,
            is_retryable=True,
            safe_message="The exclusion policy snapshot is outdated",
            allowed_detail_fields=frozenset({"current_policy_revision_number"}),
        ),
        ErrorCode.EXCLUSION_POLICY_SIGNING_UNAVAILABLE: ErrorDefinition(
            category=ErrorCategory.DEPENDENCY,
            is_retryable=False,
            safe_message="Exclusion policy signing is unavailable",
            allowed_detail_fields=frozenset(),
        ),
        ErrorCode.EXCLUSION_POLICY_COMMIT_OUTCOME_UNKNOWN: ErrorDefinition(
            category=ErrorCategory.DEPENDENCY,
            is_retryable=True,
            safe_message="The exclusion policy commit outcome could not be determined",
            allowed_detail_fields=frozenset(),
        ),
        # The small-file sync block of the plugin journal design (spec 10/12):
        # every code is terminal for the triggering request, the closed
        # outcomes map onto the plugin's non-retrying journal states, and
        # locators, digests, operation tokens and payload details never enter
        # safe details. HTTP statuses are wired into the closed api_contracts
        # status map when the routes land.
        ErrorCode.SMALL_FILE_PREFLIGHT_INVALID: ErrorDefinition(
            category=ErrorCategory.VALIDATION,
            is_retryable=False,
            safe_message="The small-file preflight request is invalid",
            allowed_detail_fields=frozenset({"reason"}),
        ),
        ErrorCode.SMALL_FILE_OPERATION_NOT_FOUND: ErrorDefinition(
            category=ErrorCategory.CONFLICT,
            is_retryable=False,
            safe_message="The upload operation does not exist",
            allowed_detail_fields=frozenset(),
        ),
        ErrorCode.SMALL_FILE_OPERATION_EXPIRED: ErrorDefinition(
            category=ErrorCategory.CONFLICT,
            is_retryable=False,
            safe_message="The upload operation expired",
            allowed_detail_fields=frozenset(),
        ),
        ErrorCode.SMALL_FILE_OPERATION_IDENTITY_MISMATCH: ErrorDefinition(
            category=ErrorCategory.CONFLICT,
            is_retryable=False,
            safe_message="The upload operation identity does not match this request",
            allowed_detail_fields=frozenset(),
        ),
        ErrorCode.SMALL_FILE_SIZE_LIMIT_EXCEEDED: ErrorDefinition(
            category=ErrorCategory.VALIDATION,
            is_retryable=False,
            safe_message="The file exceeds the single-part upload size limit",
            allowed_detail_fields=frozenset(),
        ),
        ErrorCode.SMALL_FILE_CONTENT_INTEGRITY_FAILED: ErrorDefinition(
            category=ErrorCategory.INTEGRITY,
            is_retryable=False,
            safe_message="Small-file content failed integrity verification",
            allowed_detail_fields=frozenset(),
        ),
        ErrorCode.SMALL_FILE_UPLOAD_STATE_INVALID: ErrorDefinition(
            category=ErrorCategory.CONFLICT,
            is_retryable=False,
            safe_message="The upload operation state does not accept this action",
            allowed_detail_fields=frozenset(),
        ),
    }
)


if set(ERROR_DEFINITIONS) != set(ErrorCode):
    raise RuntimeError("error definition registry is incomplete")
