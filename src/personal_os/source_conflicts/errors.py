"""Typed source-conflict errors and the closed safe-detail token set.

``SourceConflictError`` binds this domain to the closed error registry.
Conflict UUIDs, locators, digests, object keys, candidate bytes and any
merged draft stay out of the typed error: the registry message and code
are the only text rendered, and the single ``reason`` detail accepted by
``source_conflict_input_invalid`` comes only from the closed
:data:`CONFLICT_INPUT_INVALID_REASONS` token set below. That contract is
enforced by :class:`personal_os.error_contracts.exceptions.ApplicationError`.

A stale reviewed remote version is not an error: the resolver returns the
typed ``STALE_SUCCESSOR`` outcome, so no code in this set names it.
"""

from __future__ import annotations

from personal_os.diagnostics.events import SafeToken
from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import ApplicationError

CONFLICT_KIND_INVALID: SafeToken = SafeToken.parse("conflict_kind_invalid")
WORKSPACE_ID_INVALID: SafeToken = SafeToken.parse("workspace_id_invalid")
SOURCE_ID_INVALID: SafeToken = SafeToken.parse("source_id_invalid")
EVENT_ID_INVALID: SafeToken = SafeToken.parse("event_id_invalid")
DEVICE_ID_INVALID: SafeToken = SafeToken.parse("device_id_invalid")
IDEMPOTENCY_KEY_INVALID: SafeToken = SafeToken.parse("idempotency_key_invalid")
BASE_VERSION_INVALID: SafeToken = SafeToken.parse("base_version_invalid")
REMOTE_VERSION_INVALID: SafeToken = SafeToken.parse("remote_version_invalid")
CANDIDATE_INVALID: SafeToken = SafeToken.parse("candidate_invalid")
DELETION_APPLY_UNSUPPORTED: SafeToken = SafeToken.parse("deletion_apply_unsupported")
LOCATOR_INVALID: SafeToken = SafeToken.parse("locator_invalid")
RESOLUTION_KIND_INVALID: SafeToken = SafeToken.parse("resolution_kind_invalid")
RESOLUTION_EVENT_ID_INVALID: SafeToken = SafeToken.parse("resolution_event_id_invalid")
REVIEWED_REMOTE_INVALID: SafeToken = SafeToken.parse("reviewed_remote_invalid")
CANDIDATE_OBJECT_INVALID: SafeToken = SafeToken.parse("candidate_object_invalid")

#: Closed reason tokens accepted by ``source_conflict_input_invalid``; one
#: per capture or resolve field or shape rule of the spec 4.1 table. The
#: dedicated ``deletion_apply_unsupported`` token names the resolution
#: boundary where a ``keep_local`` resolution would have to apply a
#: deletion intent: applying a deletion is lifecycle-domain work this
#: domain refuses, distinct from a malformed candidate shape.
CONFLICT_INPUT_INVALID_REASONS: tuple[SafeToken, ...] = (
    CONFLICT_KIND_INVALID,
    WORKSPACE_ID_INVALID,
    SOURCE_ID_INVALID,
    EVENT_ID_INVALID,
    DEVICE_ID_INVALID,
    IDEMPOTENCY_KEY_INVALID,
    BASE_VERSION_INVALID,
    REMOTE_VERSION_INVALID,
    CANDIDATE_INVALID,
    DELETION_APPLY_UNSUPPORTED,
    LOCATOR_INVALID,
    RESOLUTION_KIND_INVALID,
    RESOLUTION_EVENT_ID_INVALID,
    REVIEWED_REMOTE_INVALID,
    CANDIDATE_OBJECT_INVALID,
)


class SourceConflictError(ApplicationError):
    """Source-conflict failures across validation, state and evidence.

    The closed code set covers a malformed capture or resolve input, an
    unknown conflict, a conflict in a state that accepts no further action,
    a reused idempotency key with a different request, evidence that is
    unavailable or fails integrity verification, and the two retryable
    dependency outages. Every code except the two dependency outages is
    terminal for the triggering request, and only
    ``source_conflict_input_invalid`` accepts a safe detail, the single
    closed ``reason`` token.
    """

    allowed_codes = frozenset(
        {
            ErrorCode.SOURCE_CONFLICT_INPUT_INVALID,
            ErrorCode.SOURCE_CONFLICT_NOT_FOUND,
            ErrorCode.SOURCE_CONFLICT_STATE_INVALID,
            ErrorCode.SOURCE_CONFLICT_IDEMPOTENCY_MISMATCH,
            ErrorCode.SOURCE_CONFLICT_EVIDENCE_UNAVAILABLE,
            ErrorCode.SOURCE_CONFLICT_EVIDENCE_INTEGRITY_FAILED,
            ErrorCode.SOURCE_CONFLICT_DEPENDENCY_UNAVAILABLE,
            ErrorCode.SOURCE_CONFLICT_COMMIT_OUTCOME_UNKNOWN,
        }
    )


__all__ = [
    "CONFLICT_INPUT_INVALID_REASONS",
    "SourceConflictError",
]
