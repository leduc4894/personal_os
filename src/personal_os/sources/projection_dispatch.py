"""Provider-neutral projection-intent lease dispatch state machine.

This module owns the closed dispatch vocabulary every projection outbox
consumer shares (design section 11): the pinned claim batch limit, lease
duration and bounded exponential retry backoff ``min(300, 2 ** n)``; the
immutable :class:`LeasedProjectionIntent` view one claimer receives; and the
safe diagnostic payload builders for expired-lease reclaim and stale-lease
fencing. The module imports no database driver, provider SDK or composition
root; time arrives only through the caller's injected clock reading and lease
tokens through an injected UUIDv7 generator.

The PostgreSQL adapter in ``postgresql-source-store`` implements the
:class:`~personal_os.sources.ports.ProjectionIntentStore` port over these
rules; it owns database time for every persisted state timestamp and lease
expiry, while this module computes backoff availability from the injected
``now`` reading the caller passes in.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final
from uuid import UUID

from personal_os.diagnostics.events import SafeToken
from personal_os.sources.errors import PROJECTION_KINDS

#: Pinned dispatch bounds (design section 11.2).
PROJECTION_CLAIM_BATCH_LIMIT: Final[int] = 50
PROJECTION_LEASE_SECONDS: Final[int] = 60
PROJECTION_BACKOFF_BASE_MULTIPLIER: Final[int] = 2
PROJECTION_BACKOFF_CAP_SECONDS: Final[int] = 300

#: Recorded in ``last_error_code`` and the stale/reclaim diagnostics when a
#: lease expired before its holder's outcome arrived (design section 11.4).
LEASE_EXPIRED_ERROR_CODE: Final[SafeToken] = SafeToken.parse("projection_dispatch_lease_expired")

#: The dispatch outcome label for an acknowledgement rejected by lease fencing.
STALE_LEASE_OUTCOME: Final[SafeToken] = SafeToken.parse("stale_lease")

#: The registry error category carried by the stale-lease diagnostic payload.
_INTEGRITY_CATEGORY: Final[SafeToken] = SafeToken.parse("integrity")

#: The closed intent operations a lease may carry (the migration CHECK set).
PROJECTION_OPERATIONS: Final[frozenset[SafeToken]] = frozenset(
    {
        SafeToken.parse("upsert"),
        SafeToken.parse("delete"),
    }
)

_KNOWN_PROJECTION_KINDS: Final[frozenset[SafeToken]] = frozenset(PROJECTION_KINDS)


def projection_retry_backoff_seconds(prior_attempt_count: int) -> int:
    """Return the bounded exponential backoff in seconds for one retry.

    The sequence doubles from the pinned one-second initial value
    (``2 ** prior_attempt_count``) and is capped at the pinned five-minute
    maximum, so a Temporal outage stays pending with a bounded availability
    delay regardless of the attempt count.
    """
    if prior_attempt_count < 0:
        raise ValueError("prior_attempt_count must be non-negative")
    doubled = PROJECTION_BACKOFF_BASE_MULTIPLIER**prior_attempt_count
    return min(PROJECTION_BACKOFF_CAP_SECONDS, int(doubled))


def retry_available_at(now: datetime, prior_attempt_count: int) -> datetime:
    """Return the moment a retried intent becomes claimable again.

    The availability delay is the bounded backoff applied to the caller's
    injected clock reading; persistence itself still stamps database time.
    """
    _require_aware(now, "now")
    return now + timedelta(seconds=projection_retry_backoff_seconds(prior_attempt_count))


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None:
        raise ValueError(f"{field_name} must be timezone-aware")


@dataclass(frozen=True, slots=True)
class LeasedProjectionIntent:
    """One projection intent owned by exactly one claimer for its lease.

    Built only from a committed claim: the identity fields mirror the durable
    row, ``lease_token`` is the UUIDv7 fence the holder must present for every
    transition, and ``leased_until`` is the database-time expiry computed by
    the claiming transaction. The view is immutable and validates its closed
    vocabulary so an out-of-contract row can never cross the boundary.
    """

    projection_intent_id: UUID
    workspace_id: UUID
    event_id: UUID
    source_id: UUID
    source_version_id: UUID | None
    projection_kind: SafeToken
    operation: SafeToken
    attempt_count: int
    lease_token: UUID
    leased_until: datetime

    def __post_init__(self) -> None:
        if self.projection_kind not in _KNOWN_PROJECTION_KINDS:
            raise ValueError("projection_kind is not a registered projection kind")
        if self.operation not in PROJECTION_OPERATIONS:
            raise ValueError("operation is not a registered projection operation")
        if self.attempt_count < 0:
            raise ValueError("attempt_count must be non-negative")
        if self.leased_until.tzinfo is None:
            raise ValueError("leased_until must be timezone-aware")


def lease_reclaimed_diagnostic_fields(
    *, projection_kind: SafeToken, count: int
) -> dict[str, object]:
    """Build the expired-lease reclaim diagnostic payload fields.

    Only the closed projection-kind token and the reclaimed row count are
    disclosed; no intent, workspace or lease identity ever enters the event.
    """
    if count < 1:
        raise ValueError("count must be positive")
    return {"projection_kind": projection_kind, "count": count}


def stale_lease_diagnostic_fields(
    *, projection_kind: SafeToken, intent_id: UUID, attempt_count: int
) -> dict[str, object]:
    """Build the stale-lease fencing rejection payload fields.

    A fenced transition that affected zero rows reports the closed
    ``stale_lease`` outcome with the registered lease-expired error code: no
    dispatch work ran through the store for the rejected acknowledgement, so
    the recorded dispatch duration is zero. Only safe scalars are disclosed.
    """
    if attempt_count < 0:
        raise ValueError("attempt_count must be non-negative")
    return {
        "projection_kind": projection_kind,
        "outcome": STALE_LEASE_OUTCOME,
        "duration_ms": 0,
        "attempt_count": attempt_count,
        "intent_id": intent_id,
        "error_code": LEASE_EXPIRED_ERROR_CODE,
        "error_category": _INTEGRITY_CATEGORY,
        "is_retryable": False,
    }
