"""Bounded transaction retry and evidence-based ambiguous-commit recovery.

These tests prove the transaction runner's closed semantics with injected
sleep, jitter and fault doubles: at most three total attempts for deadlock,
serialization failure and bounded lock contention; 50-250 ms cancellable
jitter between attempts; business errors and integrity failures never retry;
and an uncertain commit acknowledgement resolves through the recovery lookup
only — a committed replay is returned, a retry happens only after the lookup
proves absence, and an unavailable lookup returns the retryable
``source_commit_outcome_unknown`` without ever claiming a rollback.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
import sqlalchemy.exc as sa_exc

# Imported first: loading the diagnostics package before the error-contracts
# exceptions module keeps their module-level re-export cycle resolvable.
from personal_os.diagnostics.events import SafeToken  # noqa: F401
from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import InternalApplicationError
from personal_os.sources.errors import SourcePublicationError
from postgresql_source_store.error_mapping import (
    RETRY_JITTER_MAXIMUM_SECONDS,
    RETRY_JITTER_MINIMUM_SECONDS,
    DatabaseRetryPolicy,
)

_SENTINEL_STATEMENT = "SELECT do-not-emit-sql FROM knowledge.sync_events"
_SENTINEL_DRIVER_TEXT = "do-not-emit-driver-text"


class _DriverFailure(Exception):
    """Fake driver exception carrying a SQLSTATE and sentinel driver text."""

    def __init__(self, sqlstate: str | None) -> None:
        super().__init__(_SENTINEL_DRIVER_TEXT)
        self.sqlstate = sqlstate


def _ambiguous_failure() -> sa_exc.DBAPIError:
    """A connection-class failure whose commit outcome is uncertain."""
    return sa_exc.DBAPIError(_SENTINEL_STATEMENT, {}, _DriverFailure("08006"))


def _contention_failure() -> sa_exc.DBAPIError:
    return sa_exc.DBAPIError(_SENTINEL_STATEMENT, {}, _DriverFailure("40P01"))


class _SleepRecorder:
    """Awaitable sleep double recording every requested delay."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


class _JitterRecorder:
    """Deterministic jitter double recording the requested bounds."""

    def __init__(self) -> None:
        self.bounds: list[tuple[float, float]] = []

    def __call__(self, minimum: float, maximum: float) -> float:
        self.bounds.append((minimum, maximum))
        return minimum


class _RecoveryRecorder:
    """Recovery double proving presence, absence or its own failure."""

    def __init__(self, outcome: str, committed: str | None = None) -> None:
        self._outcome = outcome
        self._committed = committed
        self.calls: int = 0

    async def __call__(self) -> str | None:
        self.calls += 1
        if self._outcome == "committed":
            return self._committed
        if self._outcome == "absent":
            return None
        raise sa_exc.OperationalError(_SENTINEL_STATEMENT, {}, _DriverFailure(None))


# --- ambiguous commit recovery --------------------------------------------------


@pytest.mark.asyncio
async def test_ambiguous_failure_without_recovery_maps_to_unknown_outcome() -> None:
    sleep = _SleepRecorder()
    attempts: list[int] = []

    async def operation(attempt: int) -> str:
        attempts.append(attempt)
        raise _ambiguous_failure()

    with pytest.raises(SourcePublicationError) as captured:
        await DatabaseRetryPolicy().run(
            operation, source_id=uuid4(), sleep=sleep, jitter=_JitterRecorder()
        )
    assert captured.value.error_code is ErrorCode.SOURCE_COMMIT_OUTCOME_UNKNOWN
    assert attempts == [1]
    assert sleep.delays == []


@pytest.mark.asyncio
async def test_recovery_finding_committed_result_returns_it_without_retry() -> None:
    sleep = _SleepRecorder()
    recovery = _RecoveryRecorder("committed", committed="replayed-result")
    attempts: list[int] = []

    async def operation(attempt: int) -> str:
        attempts.append(attempt)
        raise _ambiguous_failure()

    result = await DatabaseRetryPolicy().run(
        operation,
        source_id=uuid4(),
        sleep=sleep,
        jitter=_JitterRecorder(),
        recover=recovery,
    )
    assert result == "replayed-result"
    assert attempts == [1]
    assert recovery.calls == 1
    assert sleep.delays == []


@pytest.mark.asyncio
async def test_proven_absence_retries_the_transaction_after_jitter() -> None:
    sleep = _SleepRecorder()
    jitter = _JitterRecorder()
    recovery = _RecoveryRecorder("absent")
    attempts: list[int] = []

    async def operation(attempt: int) -> str:
        attempts.append(attempt)
        if attempt == 1:
            raise _ambiguous_failure()
        return "committed"

    result = await DatabaseRetryPolicy().run(
        operation, source_id=uuid4(), sleep=sleep, jitter=jitter, recover=recovery
    )
    assert result == "committed"
    assert attempts == [1, 2]
    assert recovery.calls == 1
    assert sleep.delays == [RETRY_JITTER_MINIMUM_SECONDS]
    assert jitter.bounds == [(RETRY_JITTER_MINIMUM_SECONDS, RETRY_JITTER_MAXIMUM_SECONDS)]


@pytest.mark.asyncio
async def test_proven_absence_still_bounds_total_attempts_at_three() -> None:
    sleep = _SleepRecorder()
    recovery = _RecoveryRecorder("absent")
    attempts: list[int] = []
    ambiguous_cause = _ambiguous_failure()

    async def operation(attempt: int) -> str:
        attempts.append(attempt)
        raise ambiguous_cause

    with pytest.raises(SourcePublicationError) as captured:
        await DatabaseRetryPolicy(maximum_attempts=3).run(
            operation,
            source_id=uuid4(),
            sleep=sleep,
            jitter=_JitterRecorder(),
            recover=recovery,
        )
    assert captured.value.error_code is ErrorCode.SOURCE_COMMIT_OUTCOME_UNKNOWN
    assert captured.value.__cause__ is ambiguous_cause
    assert attempts == [1, 2, 3]
    assert recovery.calls == 3
    assert sleep.delays == [RETRY_JITTER_MINIMUM_SECONDS] * 2


@pytest.mark.asyncio
async def test_unavailable_recovery_lookup_raises_unknown_outcome_never_rollback() -> None:
    source_id = uuid4()
    sleep = _SleepRecorder()
    recovery = _RecoveryRecorder("unavailable")
    lookup_cause = None
    attempts: list[int] = []

    async def operation(attempt: int) -> str:
        attempts.append(attempt)
        raise _ambiguous_failure()

    with pytest.raises(SourcePublicationError) as captured:
        await DatabaseRetryPolicy().run(
            operation, source_id=source_id, sleep=sleep, jitter=_JitterRecorder(), recover=recovery
        )
    error = captured.value
    assert error.error_code is ErrorCode.SOURCE_COMMIT_OUTCOME_UNKNOWN
    assert dict(error.safe_details) == {"source_id": source_id}
    assert recovery.calls == 1
    assert attempts == [1]
    assert sleep.delays == []
    rendered = f"{error} {error!r} {error.to_safe_dict()!r}"
    assert _SENTINEL_DRIVER_TEXT not in rendered
    assert "rolled back" not in rendered
    assert "rolled back" not in error.safe_message
    lookup_cause = error.__cause__
    assert isinstance(lookup_cause, sa_exc.OperationalError)


@pytest.mark.asyncio
async def test_business_error_from_recovery_propagates_untouched() -> None:
    business_error = SourcePublicationError(
        ErrorCode.SOURCE_IDEMPOTENCY_MISMATCH, safe_details={"source_id": uuid4()}
    )
    sleep = _SleepRecorder()

    async def operation(attempt: int) -> str:
        raise _ambiguous_failure()

    async def recover() -> str | None:
        raise business_error

    with pytest.raises(SourcePublicationError) as captured:
        await DatabaseRetryPolicy().run(
            operation, source_id=uuid4(), sleep=sleep, jitter=_JitterRecorder(), recover=recover
        )
    assert captured.value is business_error
    assert sleep.delays == []


@pytest.mark.asyncio
async def test_integrity_failure_maps_without_retry_or_recovery_lookup() -> None:
    """A server-returned 23xxx proves a deterministic rollback.

    A constraint violation carries a server SQLSTATE on a healthy connection,
    so it is never an uncertain acknowledgement: the retryable
    ``source_commit_outcome_unknown`` mapping is returned after exactly one
    attempt, without consulting the recovery lookup or consuming jitter. The
    caller's sanctioned retry then flows through the preflight-style lookup,
    where an ``event_id`` collision hydrates into the typed
    ``source_event_identity_mismatch`` / ``source_idempotency_mismatch``
    rejection instead of the retry loop.
    """
    sleep = _SleepRecorder()
    recovery = _RecoveryRecorder("committed", committed="must-not-be-used")
    attempts: list[int] = []
    integrity_cause = sa_exc.DBAPIError(_SENTINEL_STATEMENT, {}, _DriverFailure("23505"))

    async def operation(attempt: int) -> str:
        attempts.append(attempt)
        raise integrity_cause

    with pytest.raises(SourcePublicationError) as captured:
        await DatabaseRetryPolicy().run(
            operation,
            source_id=uuid4(),
            sleep=sleep,
            jitter=_JitterRecorder(),
            recover=recovery,
        )
    error = captured.value
    assert error.error_code is ErrorCode.SOURCE_COMMIT_OUTCOME_UNKNOWN
    assert error.is_retryable is True
    assert error.__cause__ is integrity_cause
    assert attempts == [1]
    assert recovery.calls == 0
    assert sleep.delays == []


# --- failures that never retry or never consult recovery ------------------------


@pytest.mark.asyncio
async def test_non_database_failure_maps_to_internal_error_without_recovery() -> None:
    sleep = _SleepRecorder()
    recovery = _RecoveryRecorder("committed", committed="must-not-be-used")
    attempts: list[int] = []

    async def operation(attempt: int) -> str:
        attempts.append(attempt)
        raise RuntimeError(_SENTINEL_DRIVER_TEXT)

    with pytest.raises(InternalApplicationError) as captured:
        await DatabaseRetryPolicy().run(
            operation,
            source_id=uuid4(),
            sleep=sleep,
            jitter=_JitterRecorder(),
            recover=recovery,
        )
    assert captured.value.error_code is ErrorCode.INTERNAL_ERROR
    assert attempts == [1]
    assert recovery.calls == 0
    assert sleep.delays == []


@pytest.mark.asyncio
async def test_business_error_never_retries_and_never_recovers() -> None:
    sleep = _SleepRecorder()
    recovery = _RecoveryRecorder("committed", committed="must-not-be-used")
    business_error = SourcePublicationError(
        ErrorCode.SOURCE_VERSION_CONFLICT,
        safe_details={"source_id": uuid4(), "current_version_id": uuid4(), "content_version": 2},
    )
    attempts: list[int] = []

    async def operation(attempt: int) -> None:
        attempts.append(attempt)
        raise business_error

    with pytest.raises(SourcePublicationError) as captured:
        await DatabaseRetryPolicy().run(
            operation,
            source_id=uuid4(),
            sleep=sleep,
            jitter=_JitterRecorder(),
            recover=recovery,
        )
    assert captured.value is business_error
    assert attempts == [1]
    assert recovery.calls == 0
    assert sleep.delays == []


@pytest.mark.asyncio
async def test_contention_retry_never_consults_recovery() -> None:
    sleep = _SleepRecorder()
    recovery = _RecoveryRecorder("committed", committed="must-not-be-used")
    attempts: list[int] = []

    async def operation(attempt: int) -> str:
        attempts.append(attempt)
        if attempt < 3:
            raise _contention_failure()
        return "committed"

    result = await DatabaseRetryPolicy().run(
        operation, source_id=uuid4(), sleep=sleep, jitter=_JitterRecorder(), recover=recovery
    )
    assert result == "committed"
    assert attempts == [1, 2, 3]
    assert recovery.calls == 0
    assert sleep.delays == [RETRY_JITTER_MINIMUM_SECONDS] * 2


# --- cancellation -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancellation_during_recovery_retry_propagates_untouched() -> None:
    recovery = _RecoveryRecorder("absent")

    async def operation(attempt: int) -> str:
        raise _ambiguous_failure()

    class _CancellingSleep:
        async def __call__(self, delay: float) -> None:
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await DatabaseRetryPolicy().run(
            operation,
            source_id=uuid4(),
            sleep=_CancellingSleep(),
            jitter=_JitterRecorder(),
            recover=recovery,
        )
    assert recovery.calls == 1
