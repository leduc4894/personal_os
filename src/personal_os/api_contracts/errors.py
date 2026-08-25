"""API transport error vocabulary and the closed HTTP status table.

HTTP status is selected only by this closed per-code table, never inferred
from an error category, an exception type or a provider response.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import (
    ApiTransportError as ApiTransportError,
)

_APPROVED_HTTP_STATUS_CODES: Final[frozenset[ErrorCode]] = frozenset(
    {
        ErrorCode.API_REQUEST_MALFORMED,
        ErrorCode.API_REQUEST_VALIDATION_FAILED,
        ErrorCode.API_ROUTE_NOT_FOUND,
        ErrorCode.API_METHOD_NOT_ALLOWED,
        ErrorCode.DATABASE_CONNECTION_UNAVAILABLE,
        ErrorCode.DATABASE_SCHEMA_CONTRACT_INVALID,
        ErrorCode.INTERNAL_ERROR,
        ErrorCode.AUTHENTICATION_REQUIRED,
        ErrorCode.AUTHENTICATION_FAILED,
        ErrorCode.AUTHENTICATION_RATE_LIMITED,
        ErrorCode.RECENT_AUTHENTICATION_REQUIRED,
        ErrorCode.CSRF_VALIDATION_FAILED,
        ErrorCode.AUTHORIZATION_SCOPE_DENIED,
        ErrorCode.TOTP_ENROLLMENT_STATE_INVALID,
        ErrorCode.DEVICE_AUTHORIZATION_PENDING,
        ErrorCode.DEVICE_AUTHORIZATION_SLOW_DOWN,
        ErrorCode.DEVICE_AUTHORIZATION_DENIED,
        ErrorCode.DEVICE_AUTHORIZATION_EXPIRED,
        ErrorCode.DEVICE_AUTHORIZATION_STATE_INVALID,
        ErrorCode.DEVICE_REVOCATION_CONFIRMATION_INVALID,
        ErrorCode.DEVICE_CREDENTIAL_INVALID,
        ErrorCode.DEVICE_REVOKED,
        ErrorCode.DEVICE_TOKEN_REUSE_DETECTED,
        ErrorCode.PLUGIN_VERSION_UNSUPPORTED,
        # The exclusion-policy block of the design error contract (spec 19).
        ErrorCode.EXCLUSION_POLICY_INPUT_INVALID,
        ErrorCode.EXCLUSION_POLICY_NOT_INITIALIZED,
        ErrorCode.EXCLUSION_POLICY_DRAFT_CONFLICT,
        ErrorCode.EXCLUSION_POLICY_PREVIEW_PENDING,
        ErrorCode.EXCLUSION_POLICY_PREVIEW_FAILED,
        ErrorCode.EXCLUSION_POLICY_PREVIEW_EXPIRED,
        ErrorCode.EXCLUSION_POLICY_PREVIEW_STALE,
        ErrorCode.EXCLUSION_POLICY_CONFIRMATION_INVALID,
        ErrorCode.EXCLUSION_POLICY_DENIED,
        ErrorCode.EXCLUSION_POLICY_INDETERMINATE,
        ErrorCode.EXCLUSION_POLICY_SNAPSHOT_OUTDATED,
        ErrorCode.EXCLUSION_POLICY_SIGNING_UNAVAILABLE,
        ErrorCode.EXCLUSION_POLICY_COMMIT_OUTCOME_UNKNOWN,
        # The small-file sync block of the plugin journal design (spec 10/12).
        ErrorCode.SMALL_FILE_PREFLIGHT_INVALID,
        ErrorCode.SMALL_FILE_OPERATION_NOT_FOUND,
        ErrorCode.SMALL_FILE_OPERATION_EXPIRED,
        ErrorCode.SMALL_FILE_OPERATION_IDENTITY_MISMATCH,
        ErrorCode.SMALL_FILE_SIZE_LIMIT_EXCEEDED,
        ErrorCode.SMALL_FILE_CONTENT_INTEGRITY_FAILED,
        ErrorCode.SMALL_FILE_UPLOAD_STATE_INVALID,
        # The source lifecycle block of the lifecycle API (spec 19.2): 400 for
        # validation and missing-locator, 404 for tombstone-not-found, 409 for
        # locator-conflict, tombstone-closed and version-conflict, 503 for the
        # ambiguous-commit recovery path.
        ErrorCode.SOURCE_LIFECYCLE_INPUT_INVALID,
        ErrorCode.SOURCE_LOCATOR_MISSING,
        ErrorCode.SOURCE_LOCATOR_CONFLICT,
        ErrorCode.SOURCE_TOMBSTONE_NOT_FOUND,
        ErrorCode.SOURCE_TOMBSTONE_CLOSED,
        ErrorCode.SOURCE_LIFECYCLE_VERSION_CONFLICT,
        ErrorCode.SOURCE_LIFECYCLE_COMMIT_OUTCOME_UNKNOWN,
        # The device sync block of the device cursor and manifest
        # reconciliation design (spec 13).
        ErrorCode.DEVICE_CURSOR_GAP,
        ErrorCode.DEVICE_CURSOR_REGRESSION,
        ErrorCode.DEVICE_CURSOR_ACK_AHEAD,
        ErrorCode.DEVICE_EVENT_UNAVAILABLE,
        ErrorCode.DEVICE_EVENT_INTEGRITY_FAILED,
        ErrorCode.DEVICE_MANIFEST_NOT_FOUND,
        ErrorCode.DEVICE_MANIFEST_EXPIRED,
        ErrorCode.DEVICE_MANIFEST_STATE_INVALID,
        ErrorCode.DEVICE_MANIFEST_PAGE_INVALID,
        ErrorCode.DEVICE_MANIFEST_PAGE_REPLAY_MISMATCH,
        ErrorCode.DEVICE_MANIFEST_DIGEST_MISMATCH,
        ErrorCode.DEVICE_MANIFEST_POLICY_ADVANCED,
        ErrorCode.DEVICE_DOWNLOAD_INTEGRITY_FAILED,
        ErrorCode.DEVICE_SYNC_DEPENDENCY_UNAVAILABLE,
    }
)


def _build_closed_http_status_map(
    status_by_code: Mapping[ErrorCode, int],
) -> dict[ErrorCode, int]:
    """Validate one status-table draft against the approved code set.

    Rejects drafts that map a code outside the approved table and drafts that
    miss an approved code, so the public map is closed by construction.
    """
    unlisted_codes = set(status_by_code) - _APPROVED_HTTP_STATUS_CODES
    if unlisted_codes:
        raise ValueError("http status mapping contains codes outside the approved table")
    missing_codes = _APPROVED_HTTP_STATUS_CODES - set(status_by_code)
    if missing_codes:
        raise ValueError("http status mapping misses approved codes")
    return dict(status_by_code)


HTTP_ERROR_STATUSES: Final[Mapping[ErrorCode, int]] = MappingProxyType(
    _build_closed_http_status_map(
        {
            ErrorCode.API_REQUEST_MALFORMED: 400,
            ErrorCode.API_REQUEST_VALIDATION_FAILED: 422,
            ErrorCode.API_ROUTE_NOT_FOUND: 404,
            ErrorCode.API_METHOD_NOT_ALLOWED: 405,
            ErrorCode.DATABASE_CONNECTION_UNAVAILABLE: 503,
            ErrorCode.DATABASE_SCHEMA_CONTRACT_INVALID: 503,
            ErrorCode.INTERNAL_ERROR: 500,
            # The authentication block of the design error contract (spec 17).
            ErrorCode.AUTHENTICATION_REQUIRED: 401,
            ErrorCode.AUTHENTICATION_FAILED: 401,
            ErrorCode.AUTHENTICATION_RATE_LIMITED: 429,
            ErrorCode.RECENT_AUTHENTICATION_REQUIRED: 403,
            ErrorCode.CSRF_VALIDATION_FAILED: 403,
            ErrorCode.AUTHORIZATION_SCOPE_DENIED: 403,
            ErrorCode.TOTP_ENROLLMENT_STATE_INVALID: 409,
            ErrorCode.DEVICE_AUTHORIZATION_PENDING: 409,
            ErrorCode.DEVICE_AUTHORIZATION_SLOW_DOWN: 429,
            ErrorCode.DEVICE_AUTHORIZATION_DENIED: 403,
            ErrorCode.DEVICE_AUTHORIZATION_EXPIRED: 410,
            ErrorCode.DEVICE_AUTHORIZATION_STATE_INVALID: 409,
            # The exact device-name confirmation mismatch of the Admin
            # revoke route (spec 14.1): a closed conflict with no detail.
            ErrorCode.DEVICE_REVOCATION_CONFIRMATION_INVALID: 409,
            ErrorCode.DEVICE_CREDENTIAL_INVALID: 401,
            ErrorCode.DEVICE_REVOKED: 401,
            ErrorCode.DEVICE_TOKEN_REUSE_DETECTED: 401,
            ErrorCode.PLUGIN_VERSION_UNSUPPORTED: 426,
            # The exclusion-policy status column of the spec 19 table: 422 for
            # input validation, 409 for the closed conflicts, 410 for the
            # expired preview, 403 for denial/indeterminacy and 503 for the
            # two dependency failures.
            ErrorCode.EXCLUSION_POLICY_INPUT_INVALID: 422,
            ErrorCode.EXCLUSION_POLICY_NOT_INITIALIZED: 409,
            ErrorCode.EXCLUSION_POLICY_DRAFT_CONFLICT: 409,
            ErrorCode.EXCLUSION_POLICY_PREVIEW_PENDING: 409,
            ErrorCode.EXCLUSION_POLICY_PREVIEW_FAILED: 409,
            ErrorCode.EXCLUSION_POLICY_PREVIEW_EXPIRED: 410,
            ErrorCode.EXCLUSION_POLICY_PREVIEW_STALE: 409,
            ErrorCode.EXCLUSION_POLICY_CONFIRMATION_INVALID: 409,
            ErrorCode.EXCLUSION_POLICY_DENIED: 403,
            ErrorCode.EXCLUSION_POLICY_INDETERMINATE: 403,
            ErrorCode.EXCLUSION_POLICY_SNAPSHOT_OUTDATED: 409,
            ErrorCode.EXCLUSION_POLICY_SIGNING_UNAVAILABLE: 503,
            ErrorCode.EXCLUSION_POLICY_COMMIT_OUTCOME_UNKNOWN: 503,
            # The small-file sync status column of the design error contract
            # (spec 10/12): 422 for the validation and integrity verdicts,
            # 404 for an unknown operation token, 410 for the expired
            # operation and 409 for the identity and state conflicts.
            ErrorCode.SMALL_FILE_PREFLIGHT_INVALID: 422,
            ErrorCode.SMALL_FILE_OPERATION_NOT_FOUND: 404,
            ErrorCode.SMALL_FILE_OPERATION_EXPIRED: 410,
            ErrorCode.SMALL_FILE_OPERATION_IDENTITY_MISMATCH: 409,
            ErrorCode.SMALL_FILE_SIZE_LIMIT_EXCEEDED: 422,
            ErrorCode.SMALL_FILE_CONTENT_INTEGRITY_FAILED: 422,
            ErrorCode.SMALL_FILE_UPLOAD_STATE_INVALID: 409,
            # The source lifecycle status column (spec 19.2): 400 for validation
            # and missing-locator, 404 for tombstone-not-found, 409 for the three
            # conflict verdicts and 503 for the ambiguous-commit recovery.
            ErrorCode.SOURCE_LIFECYCLE_INPUT_INVALID: 400,
            ErrorCode.SOURCE_LOCATOR_MISSING: 400,
            ErrorCode.SOURCE_LOCATOR_CONFLICT: 409,
            ErrorCode.SOURCE_TOMBSTONE_NOT_FOUND: 404,
            ErrorCode.SOURCE_TOMBSTONE_CLOSED: 409,
            ErrorCode.SOURCE_LIFECYCLE_VERSION_CONFLICT: 409,
            ErrorCode.SOURCE_LIFECYCLE_COMMIT_OUTCOME_UNKNOWN: 503,
            # The device sync status column of the design error contract
            # (spec 13): 409 for the cursor/state conflicts and integrity
            # stops, 404 for unavailable events and runs, 410 for the expired
            # run, 422 for invalid pages/digests and download integrity, and
            # one retryable 503 for the dependency outage.
            ErrorCode.DEVICE_CURSOR_GAP: 409,
            ErrorCode.DEVICE_CURSOR_REGRESSION: 409,
            ErrorCode.DEVICE_CURSOR_ACK_AHEAD: 409,
            ErrorCode.DEVICE_EVENT_UNAVAILABLE: 404,
            ErrorCode.DEVICE_EVENT_INTEGRITY_FAILED: 409,
            ErrorCode.DEVICE_MANIFEST_NOT_FOUND: 404,
            ErrorCode.DEVICE_MANIFEST_EXPIRED: 410,
            ErrorCode.DEVICE_MANIFEST_STATE_INVALID: 409,
            ErrorCode.DEVICE_MANIFEST_PAGE_INVALID: 422,
            ErrorCode.DEVICE_MANIFEST_PAGE_REPLAY_MISMATCH: 409,
            ErrorCode.DEVICE_MANIFEST_DIGEST_MISMATCH: 422,
            ErrorCode.DEVICE_MANIFEST_POLICY_ADVANCED: 409,
            ErrorCode.DEVICE_DOWNLOAD_INTEGRITY_FAILED: 422,
            ErrorCode.DEVICE_SYNC_DEPENDENCY_UNAVAILABLE: 503,
        }
    )
)
