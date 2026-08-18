"""Typed small-file sync errors and the closed safe-detail token set.

``SmallFileSyncError`` binds this domain to the closed error registry. The
locator, content digest, operation token, declared sizes and any raw payload
stay out of the typed error: the registry message and code are the only text
rendered, and the single ``reason`` detail accepted by
``small_file_preflight_invalid`` comes only from the closed
:data:`PREFLIGHT_INVALID_REASONS` token set below. That contract is enforced
by :class:`personal_os.error_contracts.exceptions.ApplicationError`.
"""

from __future__ import annotations

from personal_os.diagnostics.events import SafeToken
from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import ApplicationError

EVENT_ID_INVALID: SafeToken = SafeToken.parse("event_id_invalid")
IDEMPOTENCY_KEY_INVALID: SafeToken = SafeToken.parse("idempotency_key_invalid")
OPERATION_INVALID: SafeToken = SafeToken.parse("operation_invalid")
UPDATE_BASE_MISSING: SafeToken = SafeToken.parse("update_base_missing")
CREATE_BASE_PRESENT: SafeToken = SafeToken.parse("create_base_present")
LOCAL_FILE_ID_INVALID: SafeToken = SafeToken.parse("local_file_id_invalid")
LOCATOR_INVALID: SafeToken = SafeToken.parse("locator_invalid")
DIGEST_INVALID: SafeToken = SafeToken.parse("digest_invalid")
SIZE_BYTES_INVALID: SafeToken = SafeToken.parse("size_bytes_invalid")
MEDIA_TYPE_INVALID: SafeToken = SafeToken.parse("media_type_invalid")
POLICY_REVISION_INVALID: SafeToken = SafeToken.parse("policy_revision_invalid")

#: Closed reason tokens accepted by ``small_file_preflight_invalid``; one per
#: preflight field or shape rule of spec 10.1.
PREFLIGHT_INVALID_REASONS: tuple[SafeToken, ...] = (
    EVENT_ID_INVALID,
    IDEMPOTENCY_KEY_INVALID,
    OPERATION_INVALID,
    UPDATE_BASE_MISSING,
    CREATE_BASE_PRESENT,
    LOCAL_FILE_ID_INVALID,
    LOCATOR_INVALID,
    DIGEST_INVALID,
    SIZE_BYTES_INVALID,
    MEDIA_TYPE_INVALID,
    POLICY_REVISION_INVALID,
)


class SmallFileSyncError(ApplicationError):
    """Small-file preflight/upload failures across validation and conflict.

    The closed code set covers a malformed preflight, an unknown, expired or
    identity-mismatched upload operation, the frozen single-part size limit,
    a content-integrity failure and an upload in a state that accepts no
    further action. Every code is terminal for the triggering request — none
    automatically retries — and only ``small_file_preflight_invalid`` accepts
    a safe detail, the single closed ``reason`` token.
    """

    allowed_codes = frozenset(
        {
            ErrorCode.SMALL_FILE_PREFLIGHT_INVALID,
            ErrorCode.SMALL_FILE_OPERATION_NOT_FOUND,
            ErrorCode.SMALL_FILE_OPERATION_EXPIRED,
            ErrorCode.SMALL_FILE_OPERATION_IDENTITY_MISMATCH,
            ErrorCode.SMALL_FILE_SIZE_LIMIT_EXCEEDED,
            ErrorCode.SMALL_FILE_CONTENT_INTEGRITY_FAILED,
            ErrorCode.SMALL_FILE_UPLOAD_STATE_INVALID,
        }
    )
