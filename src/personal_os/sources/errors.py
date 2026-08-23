"""Typed source-publication errors and the closed safe-detail token sets.

``SourcePublicationError`` and ``ProjectionDispatchError`` bind the
source-publication subsystems to the closed error registry. Driver exceptions,
SQL statements, titles, idempotency keys, request fingerprints, object keys and
provider messages remain chained only as internal causes and are never copied
into the typed error, its safe details or diagnostics; that contract is
enforced by
:class:`personal_os.error_contracts.exceptions.ApplicationError`.

The detail token sets below are closed ``SafeToken`` constants: input and
receipt-stale reasons, source lifecycle states and projection kinds. UUID,
integer and token details come only from these closed shapes, never caller
text.
"""

from __future__ import annotations

from personal_os.diagnostics.events import SafeToken
from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import ApplicationError

TITLE_INVALID: SafeToken = SafeToken.parse("title_invalid")
IDEMPOTENCY_KEY_INVALID: SafeToken = SafeToken.parse("idempotency_key_invalid")
ACTOR_INVALID: SafeToken = SafeToken.parse("actor_invalid")
EXPECTED_OBJECT_INVALID: SafeToken = SafeToken.parse("expected_object_invalid")
CLIENT_TIMESTAMP_INVALID: SafeToken = SafeToken.parse("client_timestamp_invalid")

#: Closed reason tokens accepted by ``source_publish_input_invalid``.
PUBLISH_INPUT_REASONS: tuple[SafeToken, ...] = (
    TITLE_INVALID,
    IDEMPOTENCY_KEY_INVALID,
    ACTOR_INVALID,
    EXPECTED_OBJECT_INVALID,
    CLIENT_TIMESTAMP_INVALID,
)

#: Closed reason tokens accepted by ``source_verified_receipt_stale``.
RECEIPT_STALE_REASONS: tuple[SafeToken, ...] = (SafeToken.parse("older_than_allowed_age"),)

#: Closed source lifecycle states accepted by ``source_state_invalid``.
SOURCE_STATES: tuple[SafeToken, ...] = (
    SafeToken.parse("active"),
    SafeToken.parse("stored_not_indexed"),
    SafeToken.parse("pending"),
    SafeToken.parse("deleted"),
)

#: Closed projection kinds accepted by the projection dispatch codes.
PROJECTION_KINDS: tuple[SafeToken, ...] = (
    SafeToken.parse("qdrant"),
    SafeToken.parse("neo4j"),
)


class SourcePublicationError(ApplicationError):
    """Source create/update publication failures across validation and conflict.

    The closed code set covers command input validation, business conflict and
    identity misuse, content-object integrity and retryable concurrency or
    unknown-commit dependency failures. Safe detail fields are registry-bound:
    input and receipt failures accept a single closed ``reason`` token,
    conflict and dependency failures accept the registered UUID/integer
    identifiers and the state token.
    """

    allowed_codes = frozenset(
        {
            ErrorCode.SOURCE_PUBLISH_INPUT_INVALID,
            ErrorCode.SOURCE_NOT_FOUND,
            ErrorCode.SOURCE_ALREADY_EXISTS,
            ErrorCode.SOURCE_STATE_INVALID,
            ErrorCode.SOURCE_VERSION_CONFLICT,
            ErrorCode.SOURCE_IDEMPOTENCY_MISMATCH,
            ErrorCode.SOURCE_EVENT_IDENTITY_MISMATCH,
            ErrorCode.SOURCE_VERIFIED_RECEIPT_STALE,
            ErrorCode.SOURCE_CONTENT_OBJECT_CONFLICT,
            ErrorCode.SOURCE_LOCATOR_CONFLICT,
            ErrorCode.SOURCE_CONCURRENCY_BUSY,
            ErrorCode.SOURCE_CONCURRENCY_INVARIANT_FAILED,
            ErrorCode.SOURCE_COMMIT_OUTCOME_UNKNOWN,
        }
    )


class ProjectionDispatchError(ApplicationError):
    """Projection-intent dispatch failures: dependency and contract integrity.

    The closed code set is disjoint from :class:`SourcePublicationError`:
    dispatch unavailability is retryable dependency failure and a contract
    violation is a terminal integrity failure. Both accept a single
    ``projection_kind`` detail from the closed token set above.
    """

    allowed_codes = frozenset(
        {
            ErrorCode.PROJECTION_DISPATCH_UNAVAILABLE,
            ErrorCode.PROJECTION_INTENT_CONTRACT_INVALID,
        }
    )
