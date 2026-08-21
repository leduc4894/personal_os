"""Pure SQLSTATE classification, bounded contention retry and safe error mapping.

The adapter owns the database failure boundary: driver exceptions are
classified by SQLSTATE and exception shape only, retried at most three times
for deadlock, serialization failure and bounded lock contention with
cancellable 50-250 ms jitter, and finally mapped onto the closed Task 4 error
codes. Raw SQLSTATE values, SQL statements, bound parameters, DSNs and driver
messages remain chained only as internal causes; they are never copied into a
typed error, its safe details or diagnostics (design sections 9.3-9.4).
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Final
from uuid import UUID

import psycopg
import sqlalchemy.exc as sa_exc

from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import ApplicationError, InternalApplicationError
from personal_os.source_lifecycle.errors import SourceLifecycleError
from personal_os.sources.errors import SourcePublicationError

#: Deadlock, serialization failure, lock-not-available and query-canceled
#: (statement/lock timeout) are the only retryable contention conditions.
RETRYABLE_CONTENTION_SQLSTATES: Final[frozenset[str]] = frozenset(
    {"40001", "40P01", "55P03", "57014"}
)

#: Connection-class SQLSTATE prefix (``08xxx``) plus shutdown, connection-count
#: and cannot-connect-now failures: PostgreSQL is unreachable, never merely busy.
_UNAVAILABLE_SQLSTATE_PREFIX: Final[str] = "08"
_UNAVAILABLE_SQLSTATES: Final[frozenset[str]] = frozenset({"53300", "57P01", "57P02", "57P03"})

#: Cancellable retry jitter bounds from the canonical transaction contract.
RETRY_JITTER_MINIMUM_SECONDS: Final[float] = 0.05
RETRY_JITTER_MAXIMUM_SECONDS: Final[float] = 0.25


class DatabaseFailureKind(StrEnum):
    """Closed classification of a driver or database failure."""

    CONTENTION = "contention"
    UNAVAILABLE = "unavailable"
    UNCLASSIFIED_DATABASE = "unclassified_database"
    NOT_DATABASE = "not_database"


def _extract_sqlstate(cause: BaseException) -> str | None:
    """Return the first SQLSTATE found on the cause or its wrapped origin.

    Only the closed SQLSTATE string is read; the driver message, statement and
    parameters are never touched by classification.
    """
    for candidate in (getattr(cause, "orig", None), cause):
        sqlstate = getattr(candidate, "sqlstate", None)
        if isinstance(sqlstate, str) and sqlstate:
            return sqlstate
    return None


def classify_database_failure(cause: BaseException) -> DatabaseFailureKind:
    """Classify a failure into the closed :class:`DatabaseFailureKind` set.

    Pure and side-effect free: only the SQLSTATE and the exception shape are
    inspected, never the message text. Unknown database failures fail closed as
    unclassified: never retried and never treated as an uncertain commit
    acknowledgement (only connection-class unavailability is), and
    non-database exceptions are internal bugs.
    """
    sqlstate = _extract_sqlstate(cause)
    if sqlstate is not None:
        if sqlstate in RETRYABLE_CONTENTION_SQLSTATES:
            return DatabaseFailureKind.CONTENTION
        if sqlstate.startswith(_UNAVAILABLE_SQLSTATE_PREFIX) or sqlstate in _UNAVAILABLE_SQLSTATES:
            return DatabaseFailureKind.UNAVAILABLE
    if isinstance(
        cause,
        sa_exc.InterfaceError
        | sa_exc.OperationalError
        | psycopg.InterfaceError
        | psycopg.OperationalError,
    ):
        return DatabaseFailureKind.UNAVAILABLE
    if isinstance(cause, sa_exc.DBAPIError | psycopg.Error):
        return DatabaseFailureKind.UNCLASSIFIED_DATABASE
    return DatabaseFailureKind.NOT_DATABASE


def map_database_failure(cause: BaseException, *, source_id: UUID) -> ApplicationError:
    """Map a database or driver failure onto the closed error registry.

    Contention maps to the retryable ``source_concurrency_busy``; unavailability
    and any unclassified database failure map to the retryable
    ``source_commit_outcome_unknown`` because the transaction outcome could not
    be determined and must never be guessed (design section 9.4). A non-database
    exception is an internal bug and crosses the boundary as ``internal_error``.
    The cause remains chained only; its text never enters the mapped error.
    """
    failure_kind = classify_database_failure(cause)
    if failure_kind is DatabaseFailureKind.CONTENTION:
        return SourcePublicationError(
            ErrorCode.SOURCE_CONCURRENCY_BUSY, safe_details={"source_id": source_id}
        )
    if failure_kind is DatabaseFailureKind.NOT_DATABASE:
        return InternalApplicationError(ErrorCode.INTERNAL_ERROR)
    return SourcePublicationError(
        ErrorCode.SOURCE_COMMIT_OUTCOME_UNKNOWN, safe_details={"source_id": source_id}
    )


@dataclass(frozen=True, slots=True)
class DatabaseRetryPolicy:
    """Bounded retry for deadlock, serialization and lock-contention failures.

    At most ``maximum_attempts`` attempts run; each retry sleeps a cancellable
    jitter between the pinned 50-250 ms bounds. Typed application errors pass
    through untouched, and every non-retryable failure is mapped immediately by
    :func:`map_database_failure` without propagating driver text.

    A write transaction may supply ``recover`` for the uncertain-commit case
    (design section 9.4): when a connection-class failure (unavailability)
    makes the commit acknowledgement uncertain, the recovery lookup runs on a
    fresh bounded connection and decides the outcome from evidence only — a
    committed replay is returned, a retry happens only after the lookup proves
    absence, and an unavailable lookup raises the retryable
    ``source_commit_outcome_unknown`` without ever claiming a rollback. Every
    other database failure — business errors, integrity violations and any
    other server-returned SQLSTATE on a healthy connection — proves a
    deterministic rollback and never retries.
    """

    maximum_attempts: int = 3

    async def run[T](
        self,
        operation: Callable[[int], Awaitable[T]],
        *,
        source_id: UUID,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[float, float], float] = random.uniform,
        recover: Callable[[], Awaitable[T | None]] | None = None,
    ) -> T:
        for attempt in range(1, self.maximum_attempts + 1):
            try:
                return await operation(attempt)
            except (ApplicationError, SourceLifecycleError):
                raise
            except Exception as cause:
                failure_kind = classify_database_failure(cause)
                if failure_kind is DatabaseFailureKind.NOT_DATABASE:
                    mapped = map_database_failure(cause, source_id=source_id)
                    raise mapped from cause
                if failure_kind is DatabaseFailureKind.CONTENTION:
                    if attempt == self.maximum_attempts:
                        mapped = map_database_failure(cause, source_id=source_id)
                        raise mapped from cause
                elif failure_kind is DatabaseFailureKind.UNAVAILABLE and recover is not None:
                    # Only a connection-class failure makes the commit
                    # acknowledgement uncertain. A server-returned SQLSTATE on
                    # a healthy connection (for example an integrity
                    # constraint violation) already proves a deterministic
                    # rollback, so it maps directly below without retrying.
                    recovered = await self._resolve_uncertain_outcome(recover, source_id=source_id)
                    if recovered is not None:
                        return recovered
                    # The lookup proved absence, so a retry cannot duplicate.
                    if attempt == self.maximum_attempts:
                        mapped = map_database_failure(cause, source_id=source_id)
                        raise mapped from cause
                else:
                    mapped = map_database_failure(cause, source_id=source_id)
                    raise mapped from cause
                await sleep(jitter(RETRY_JITTER_MINIMUM_SECONDS, RETRY_JITTER_MAXIMUM_SECONDS))
        raise AssertionError("retry loop exhausted without a result")

    @staticmethod
    async def _resolve_uncertain_outcome[T](
        recover: Callable[[], Awaitable[T | None]], *, source_id: UUID
    ) -> T | None:
        """Run the fresh-connection outcome lookup for an ambiguous commit.

        A found committed result is returned as the replay evidence; ``None``
        is a proven absence; a typed application error (for example an
        identity misuse discovered by the lookup) propagates untouched. Any
        other lookup failure means PostgreSQL could not prove presence or
        absence, so the outcome stays unknown and retryable — a rollback is
        never claimed without evidence.
        """
        try:
            return await recover()
        except ApplicationError:
            raise
        except Exception as lookup_cause:
            raise SourcePublicationError(
                ErrorCode.SOURCE_COMMIT_OUTCOME_UNKNOWN,
                safe_details={"source_id": source_id},
            ) from lookup_cause
