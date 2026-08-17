"""Leased and fenced projection-intent persistence over the canonical baseline.

:class:`PostgresqlProjectionIntentStore` implements the
:class:`~personal_os.sources.ports.ProjectionIntentStore` port against the
migrated ``knowledge.projection_intents`` table (design section 11). ``claim_batch``
selects due ``pending`` rows ``FOR UPDATE SKIP LOCKED`` ordered by
``(available_at, created_at, projection_intent_id)`` inside the pinned batch
limit, writes the leased status with a caller-injected UUIDv7 fence token and
a database-time expiry, and commits before returning — no network I/O ever
runs inside the claim transaction, so concurrent claimers can never own one
intent. ``reclaim_expired`` returns overdue leases to ``pending`` under the
same row skip, incrementing the attempt count, recording the closed
``projection_dispatch_lease_expired`` error code and applying the bounded
exponential backoff. The three fenced transitions affect a row only when the
exact intent ID, ``status='leased'`` and lease token all match; a stale token
affects zero rows, emits the registered stale-lease diagnostic and never
overwrites state.

Every persisted state timestamp and lease expiry is PostgreSQL time
(``CURRENT_TIMESTAMP``); the caller's injected ``now`` reading is used only
for due/expiry comparisons and availability validation. The attempt count
changes only on a known dispatch outcome or a lease expiry. Every statement
is schema-qualified through the Task 6 Core metadata and parameter-bound;
driver failures are classified by SQLSTATE alone and mapped onto the closed
projection error codes, so SQL, parameters, DSNs and driver text never leave
the adapter.
"""

from __future__ import annotations

import asyncio
import random
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Final, Protocol, runtime_checkable
from uuid import UUID, uuid7

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from personal_os.diagnostics.events import EventName, SafeToken
from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import ApplicationError, InternalApplicationError
from personal_os.sources.errors import PROJECTION_KINDS, ProjectionDispatchError
from personal_os.sources.metrics import ProjectionKind, SourcePublicationMetrics
from personal_os.sources.projection_dispatch import (
    LEASE_EXPIRED_ERROR_CODE,
    PROJECTION_CLAIM_BATCH_LIMIT,
    PROJECTION_LEASE_SECONDS,
    LeasedProjectionIntent,
    ProjectionIntentOriginKind,
    lease_reclaimed_diagnostic_fields,
    projection_retry_backoff_seconds,
    stale_lease_diagnostic_fields,
)
from postgresql_source_store.engine import apply_transaction_bounds
from postgresql_source_store.error_mapping import (
    RETRY_JITTER_MAXIMUM_SECONDS,
    RETRY_JITTER_MINIMUM_SECONDS,
    DatabaseFailureKind,
    classify_database_failure,
)
from postgresql_source_store.tables import projection_intents


class ProjectionIntentStatus(StrEnum):
    """The closed intent lifecycle states (the migration CHECK set)."""

    PENDING = "pending"
    LEASED = "leased"
    DISPATCHED = "dispatched"
    TERMINAL = "terminal"


#: The ``last_error_code`` CHECK constraint accepts ``^[a-z][a-z0-9_]{0,99}$``;
#: ``SafeToken`` is wider, so the stricter column grammar is enforced here.
_ERROR_CODE_COLUMN_GRAMMAR: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9_]{0,99}$")

_KNOWN_PROJECTION_KINDS: Final[frozenset[SafeToken]] = frozenset(PROJECTION_KINDS)


@runtime_checkable
class ProjectionDiagnosticSink(Protocol):
    """Structural sink the composition root satisfies with its diagnostic logger."""

    def emit(self, event_name: EventName, fields: Mapping[str, object] | None = None) -> None: ...


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None:
        raise ValueError(f"{field_name} must be timezone-aware")


def _require_column_safe_error_code(error_code: SafeToken) -> None:
    if _ERROR_CODE_COLUMN_GRAMMAR.fullmatch(error_code.value) is None:
        raise ValueError("error_code does not satisfy the last_error_code column contract")


def _require_positive(value: int, field_name: str) -> None:
    if value < 1:
        raise ValueError(f"{field_name} must be positive")


def claim_available_select_statement(now: datetime, limit: int) -> sa.Select[tuple[Any, ...]]:
    """Build the due-intent claim select with the pinned order and row skip.

    Only ``pending`` rows whose availability has passed the injected ``now``
    reading match, and only rows whose origin is ``source_event``: a pending
    policy-transition intent must never reach the source-event dispatcher or
    start ``SourceIngestionWorkflow``. The pinned
    ``(available_at, created_at, projection_intent_id)`` order and ``FOR UPDATE
    SKIP LOCKED`` keep concurrent claimers disjoint.
    """
    _require_aware(now, "now")
    if limit < 1 or limit > PROJECTION_CLAIM_BATCH_LIMIT:
        raise ValueError("limit must be between 1 and the pinned claim batch limit")
    return (
        sa.select(
            projection_intents.c.projection_intent_id,
            projection_intents.c.workspace_id,
            projection_intents.c.origin_kind,
            projection_intents.c.event_id,
            projection_intents.c.policy_revision_id,
            projection_intents.c.source_id,
            projection_intents.c.source_version_id,
            projection_intents.c.projection_kind,
            projection_intents.c.operation,
            projection_intents.c.attempt_count,
        )
        .where(
            projection_intents.c.status == ProjectionIntentStatus.PENDING,
            projection_intents.c.available_at
            <= sa.bindparam("now", now, type_=sa.DateTime(timezone=True)),
            projection_intents.c.origin_kind == ProjectionIntentOriginKind.SOURCE_EVENT.value,
        )
        .order_by(
            projection_intents.c.available_at,
            projection_intents.c.created_at,
            projection_intents.c.projection_intent_id,
        )
        .limit(limit)
        .with_for_update(skip_locked=True)
    )


def lease_intent_update_statement(projection_intent_id: UUID) -> sa.Update:
    """Build the guarded lease write with database-time expiry.

    The fence matches only the still-``pending`` unleased row; the expiry is
    ``CURRENT_TIMESTAMP`` plus the pinned lease duration, so the lease CHECK
    constraint (``leased_until > updated_at``) always holds with one clock.
    """
    return (
        sa.update(projection_intents)
        .values(
            status=ProjectionIntentStatus.LEASED,
            lease_token=sa.bindparam("lease_token", type_=sa.Uuid()),
            leased_until=sa.func.current_timestamp()
            + sa.func.make_interval(0, 0, 0, 0, 0, 0, PROJECTION_LEASE_SECONDS),
            updated_at=sa.func.current_timestamp(),
        )
        .where(
            projection_intents.c.projection_intent_id == projection_intent_id,
            projection_intents.c.status == ProjectionIntentStatus.PENDING,
            projection_intents.c.lease_token.is_(None),
        )
        .returning(projection_intents.c.leased_until)
    )


def expired_lease_select_statement(now: datetime) -> sa.Select[tuple[Any, ...]]:
    """Build the overdue-lease reclaim select with the pinned row skip."""
    _require_aware(now, "now")
    return (
        sa.select(
            projection_intents.c.projection_intent_id,
            projection_intents.c.projection_kind,
            projection_intents.c.attempt_count,
            projection_intents.c.lease_token,
        )
        .where(
            projection_intents.c.status == ProjectionIntentStatus.LEASED,
            projection_intents.c.leased_until
            <= sa.bindparam("now", now, type_=sa.DateTime(timezone=True)),
        )
        .order_by(projection_intents.c.projection_intent_id)
        .with_for_update(skip_locked=True)
    )


def reclaim_lease_update_statement(
    projection_intent_id: UUID, lease_token: UUID, backoff_seconds: int
) -> sa.Update:
    """Build the expired-lease return to ``pending`` with bounded backoff.

    The attempt count increments exactly once (the lease expiry is a known
    outcome), the lease columns clear, the closed lease-expired error code is
    recorded, and availability moves to database time plus the bounded
    backoff computed from the prior attempt count.
    """
    _require_positive(backoff_seconds, "backoff_seconds")
    return (
        sa.update(projection_intents)
        .values(
            status=ProjectionIntentStatus.PENDING,
            attempt_count=projection_intents.c.attempt_count + 1,
            lease_token=sa.null(),
            leased_until=sa.null(),
            last_error_code=LEASE_EXPIRED_ERROR_CODE.value,
            available_at=sa.func.current_timestamp()
            + sa.func.make_interval(
                0,
                0,
                0,
                0,
                0,
                0,
                sa.bindparam("backoff_seconds", backoff_seconds, type_=sa.Integer()),
            ),
            updated_at=sa.func.current_timestamp(),
        )
        .where(
            projection_intents.c.projection_intent_id == projection_intent_id,
            projection_intents.c.status == ProjectionIntentStatus.LEASED,
            projection_intents.c.lease_token == lease_token,
        )
    )


def acknowledge_dispatched_statement(intent_id: UUID, lease_token: UUID) -> sa.Update:
    """Build the fenced dispatched acknowledgement (design 11.4)."""
    return (
        sa.update(projection_intents)
        .values(
            status=ProjectionIntentStatus.DISPATCHED,
            attempt_count=projection_intents.c.attempt_count + 1,
            lease_token=sa.null(),
            leased_until=sa.null(),
            dispatched_at=sa.func.current_timestamp(),
            last_error_code=sa.null(),
            updated_at=sa.func.current_timestamp(),
        )
        .where(
            projection_intents.c.projection_intent_id == intent_id,
            projection_intents.c.status == ProjectionIntentStatus.LEASED,
            projection_intents.c.lease_token == lease_token,
        )
    )


def release_retry_statement(
    intent_id: UUID,
    lease_token: UUID,
    error_code: SafeToken,
    available_at: datetime,
    now: datetime,
) -> sa.Update:
    """Build the fenced retryable release back to ``pending``.

    The availability must be an aware moment at or after the injected ``now``
    reading — callers derive it from the bounded backoff — and the closed
    error code must satisfy the column grammar. No dispatch timestamp is
    written: the attempt never produced a known dispatch outcome, but the
    retryable failure itself is the known outcome that increments the count.
    """
    _require_aware(now, "now")
    _require_aware(available_at, "available_at")
    if available_at < now:
        raise ValueError("available_at must not precede now")
    _require_column_safe_error_code(error_code)
    return (
        sa.update(projection_intents)
        .values(
            status=ProjectionIntentStatus.PENDING,
            attempt_count=projection_intents.c.attempt_count + 1,
            lease_token=sa.null(),
            leased_until=sa.null(),
            last_error_code=error_code.value,
            available_at=available_at,
            updated_at=sa.func.current_timestamp(),
        )
        .where(
            projection_intents.c.projection_intent_id == intent_id,
            projection_intents.c.status == ProjectionIntentStatus.LEASED,
            projection_intents.c.lease_token == lease_token,
        )
    )


def mark_terminal_statement(
    intent_id: UUID,
    lease_token: UUID,
    error_code: SafeToken,
    now: datetime,
) -> sa.Update:
    """Build the fenced non-retryable terminal transition.

    ``now`` is validated for the port's uniform aware-clock contract; the
    persisted state timestamp stays database time. Terminal keeps the
    required non-null error code the migration CHECK demands.
    """
    _require_aware(now, "now")
    _require_column_safe_error_code(error_code)
    return (
        sa.update(projection_intents)
        .values(
            status=ProjectionIntentStatus.TERMINAL,
            attempt_count=projection_intents.c.attempt_count + 1,
            lease_token=sa.null(),
            leased_until=sa.null(),
            last_error_code=error_code.value,
            updated_at=sa.func.current_timestamp(),
        )
        .where(
            projection_intents.c.projection_intent_id == intent_id,
            projection_intents.c.status == ProjectionIntentStatus.LEASED,
            projection_intents.c.lease_token == lease_token,
        )
    )


def projection_kind_token(value: str) -> SafeToken:
    """Convert a stored kind string into the closed projection-kind token.

    The migration CHECK already guarantees the closed set; a value outside it
    is an impossible row reported as the integrity failure, never a string
    crossing the boundary.
    """
    try:
        token = SafeToken.parse(value)
    except ValueError as cause:
        raise ProjectionDispatchError(ErrorCode.PROJECTION_INTENT_CONTRACT_INVALID) from cause
    if token not in _KNOWN_PROJECTION_KINDS:
        raise ProjectionDispatchError(ErrorCode.PROJECTION_INTENT_CONTRACT_INVALID)
    return token


def _claimable_origin_kind(value: str) -> ProjectionIntentOriginKind:
    """Convert a stored origin string into the closed origin discriminator.

    The claim select filters on ``source_event``; this conversion is the
    fail-closed runtime guard over the origin vocabulary. The database origin
    CHECK independently rejects every value outside the two legal kinds, so a
    different value reaching this boundary is an impossible row reported as
    the integrity failure rather than dispatched.
    """
    try:
        origin = ProjectionIntentOriginKind(value)
    except ValueError as cause:
        raise ProjectionDispatchError(ErrorCode.PROJECTION_INTENT_CONTRACT_INVALID) from cause
    if origin is not ProjectionIntentOriginKind.SOURCE_EVENT:
        raise ProjectionDispatchError(ErrorCode.PROJECTION_INTENT_CONTRACT_INVALID)
    return origin


def map_projection_failure(
    cause: BaseException, *, projection_kind: SafeToken | None = None
) -> ApplicationError:
    """Map a driver failure onto the closed projection error registry.

    Contention and unavailability map to the retryable dependency failure
    ``projection_dispatch_unavailable``; a non-database exception is an
    internal bug. Only the closed projection-kind token is ever disclosed;
    the cause stays chained and its text never crosses the boundary.
    """
    failure_kind = classify_database_failure(cause)
    if failure_kind is DatabaseFailureKind.NOT_DATABASE:
        return InternalApplicationError(ErrorCode.INTERNAL_ERROR)
    return ProjectionDispatchError(
        ErrorCode.PROJECTION_DISPATCH_UNAVAILABLE,
        safe_details={} if projection_kind is None else {"projection_kind": projection_kind},
    )


@dataclass(frozen=True, slots=True)
class ProjectionRetryPolicy:
    """Bounded contention retry for projection-intent store operations.

    Mirrors the publication retry semantics: at most ``maximum_attempts``
    attempts, cancellable 50-250 ms jitter between contention retries, typed
    application errors pass through untouched, and every non-retryable
    failure is mapped immediately onto the closed projection codes.
    """

    maximum_attempts: int = 3

    async def run[T](
        self,
        operation: Callable[[int], Awaitable[T]],
        *,
        projection_kind: SafeToken | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[float, float], float] = random.uniform,
    ) -> T:
        for attempt in range(1, self.maximum_attempts + 1):
            try:
                return await operation(attempt)
            except ApplicationError:
                raise
            except Exception as cause:
                if (
                    classify_database_failure(cause) is not DatabaseFailureKind.CONTENTION
                    or attempt == self.maximum_attempts
                ):
                    raise map_projection_failure(cause, projection_kind=projection_kind) from cause
                await sleep(jitter(RETRY_JITTER_MINIMUM_SECONDS, RETRY_JITTER_MAXIMUM_SECONDS))
        raise AssertionError("retry loop exhausted without a result")


@dataclass(frozen=True, slots=True)
class _IntentDispatchContext:
    """Safe closed-vocabulary context looked up for a stale-lease diagnostic."""

    projection_kind: SafeToken
    attempt_count: int


class PostgresqlProjectionIntentStore:
    """Projection-intent lease store over the canonical PostgreSQL baseline.

    The store takes the composition-owned :class:`AsyncEngine` plus the
    injected seams: the UUIDv7 lease-token generator, the optional structural
    diagnostic sink (satisfied by the configured diagnostic logger) and the
    optional low-cardinality metrics recorder. It opens no connection at
    construction; every method runs one ``READ COMMITTED`` transaction behind
    the pinned ``SET LOCAL`` bounds, and every claim commits before the
    caller can perform any network I/O.
    """

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        lease_token_generator: Callable[[], UUID] | None = None,
        diagnostics: ProjectionDiagnosticSink | None = None,
        metrics: SourcePublicationMetrics | None = None,
        retry: ProjectionRetryPolicy | None = None,
    ) -> None:
        self._engine = engine
        self._lease_token_generator: Callable[[], UUID] = (
            lease_token_generator if lease_token_generator is not None else uuid7
        )
        self._diagnostics = diagnostics
        self._metrics = metrics
        self._retry = retry if retry is not None else ProjectionRetryPolicy()

    async def reclaim_expired(self, now: datetime) -> int:
        return await self._retry.run(lambda _attempt: self._reclaim_expired_once(now))

    async def claim_batch(self, now: datetime, limit: int) -> tuple[LeasedProjectionIntent, ...]:
        return await self._retry.run(lambda _attempt: self._claim_batch_once(now, limit))

    async def acknowledge_dispatched(
        self, intent_id: UUID, lease_token: UUID, now: datetime
    ) -> bool:
        return await self._retry.run(
            lambda _attempt: self._fenced_transition_once(
                acknowledge_dispatched_statement(intent_id, lease_token), intent_id
            )
        )

    async def release_retry(
        self,
        intent_id: UUID,
        lease_token: UUID,
        error_code: SafeToken,
        available_at: datetime,
        now: datetime,
    ) -> bool:
        return await self._retry.run(
            lambda _attempt: self._fenced_transition_once(
                release_retry_statement(intent_id, lease_token, error_code, available_at, now),
                intent_id,
            )
        )

    async def mark_terminal(
        self,
        intent_id: UUID,
        lease_token: UUID,
        error_code: SafeToken,
        now: datetime,
    ) -> bool:
        return await self._retry.run(
            lambda _attempt: self._fenced_transition_once(
                mark_terminal_statement(intent_id, lease_token, error_code, now), intent_id
            )
        )

    async def _claim_batch_once(
        self, now: datetime, limit: int
    ) -> tuple[LeasedProjectionIntent, ...]:
        claimed: list[LeasedProjectionIntent] = []
        async with (
            self._engine.connect() as connection,
            connection.begin(),
        ):
            await apply_transaction_bounds(connection)
            rows = (await connection.execute(claim_available_select_statement(now, limit))).all()
            for row in rows:
                kind = projection_kind_token(str(row.projection_kind))
                lease_token = self._lease_token_generator()
                leased = await connection.execute(
                    lease_intent_update_statement(row.projection_intent_id),
                    {"lease_token": lease_token},
                )
                leased_row = leased.one_or_none()
                if leased_row is None or leased.rowcount != 1:
                    # Impossible while the row lock is held: the selected
                    # pending row changed shape mid-transaction.
                    raise ProjectionDispatchError(
                        ErrorCode.PROJECTION_INTENT_CONTRACT_INVALID,
                        safe_details={"projection_kind": kind},
                    )
                claimed.append(
                    LeasedProjectionIntent(
                        projection_intent_id=row.projection_intent_id,
                        workspace_id=row.workspace_id,
                        origin_kind=_claimable_origin_kind(str(row.origin_kind)),
                        event_id=row.event_id,
                        policy_revision_id=row.policy_revision_id,
                        source_id=row.source_id,
                        source_version_id=row.source_version_id,
                        projection_kind=kind,
                        operation=SafeToken.parse(str(row.operation)),
                        attempt_count=int(row.attempt_count),
                        lease_token=lease_token,
                        leased_until=leased_row.leased_until,
                    )
                )
        return tuple(claimed)

    async def _reclaim_expired_once(self, now: datetime) -> int:
        reclaimed_by_kind: dict[SafeToken, int] = {}
        async with (
            self._engine.connect() as connection,
            connection.begin(),
        ):
            await apply_transaction_bounds(connection)
            rows = (await connection.execute(expired_lease_select_statement(now))).all()
            for row in rows:
                kind = projection_kind_token(str(row.projection_kind))
                backoff_seconds = projection_retry_backoff_seconds(int(row.attempt_count))
                guarded = await connection.execute(
                    reclaim_lease_update_statement(
                        row.projection_intent_id, row.lease_token, backoff_seconds
                    )
                )
                if guarded.rowcount != 1:
                    raise ProjectionDispatchError(
                        ErrorCode.PROJECTION_INTENT_CONTRACT_INVALID,
                        safe_details={"projection_kind": kind},
                    )
                reclaimed_by_kind[kind] = reclaimed_by_kind.get(kind, 0) + 1
        for kind, count in reclaimed_by_kind.items():
            if self._diagnostics is not None:
                self._diagnostics.emit(
                    EventName.PROJECTION_INTENT_LEASE_RECLAIMED,
                    lease_reclaimed_diagnostic_fields(projection_kind=kind, count=count),
                )
            if self._metrics is not None:
                metric_kind = ProjectionKind(kind.value)
                for _ in range(count):
                    self._metrics.record_lease_reclaimed(projection_kind=metric_kind)
        return sum(reclaimed_by_kind.values())

    async def _fenced_transition_once(self, statement: sa.Update, intent_id: UUID) -> bool:
        async with (
            self._engine.connect() as connection,
            connection.begin(),
        ):
            await apply_transaction_bounds(connection)
            result = await connection.execute(statement)
            if result.rowcount == 1:
                return True
            # Zero rows: the lease is stale (expired and reclaimed, or held
            # by another fence). State is never overwritten; only the closed
            # stale-lease diagnostic is emitted from safe looked-up fields.
            context = await self._select_dispatch_context(connection, intent_id)
            if self._diagnostics is not None and context is not None:
                self._diagnostics.emit(
                    EventName.PROJECTION_INTENT_DISPATCH_FAILED,
                    stale_lease_diagnostic_fields(
                        projection_kind=context.projection_kind,
                        intent_id=intent_id,
                        attempt_count=context.attempt_count,
                    ),
                )
            return False

    @staticmethod
    async def _select_dispatch_context(
        connection: AsyncConnection, intent_id: UUID
    ) -> _IntentDispatchContext | None:
        result = await connection.execute(
            sa.select(
                projection_intents.c.projection_kind,
                projection_intents.c.attempt_count,
            ).where(projection_intents.c.projection_intent_id == intent_id)
        )
        row = result.one_or_none()
        if row is None:
            return None
        return _IntentDispatchContext(
            projection_kind=projection_kind_token(str(row.projection_kind)),
            attempt_count=int(row.attempt_count),
        )
