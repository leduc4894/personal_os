"""SQLSTATE classification, bounded contention retry and safe database-error mapping.

These tests prove the closed classification of driver failures by SQLSTATE and
exception shape, the mapping onto the Task 4 closed error codes, the bounded
retry loop for deadlock/serialization/lock-contention failures with 50-250 ms
cancellable jitter, and that SQL statements, bound parameters and driver
messages never leave the adapter through a mapped error.
"""

from __future__ import annotations

import asyncio
import json
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
    RETRYABLE_CONTENTION_SQLSTATES,
    DatabaseFailureKind,
    DatabaseRetryPolicy,
    classify_database_failure,
    map_database_failure,
)

_SENTINEL_STATEMENT = "SELECT do-not-emit-sql FROM knowledge.sync_events"
_SENTINEL_PARAMETER_KEY = "do_not_emit_parameters"
_SENTINEL_PARAMETER_VALUE = "do-not-emit-parameters"
_SENTINEL_DRIVER_TEXT = "do-not-emit-driver-text"
_SENTINELS = (_SENTINEL_STATEMENT, _SENTINEL_PARAMETER_VALUE, _SENTINEL_DRIVER_TEXT)


class _DriverFailure(Exception):
    """Fake driver exception carrying a SQLSTATE and sentinel diagnostic text."""

    def __init__(self, sqlstate: str | None) -> None:
        super().__init__(_SENTINEL_DRIVER_TEXT)
        self.sqlstate = sqlstate


def _wrapped_failure(sqlstate: str | None) -> sa_exc.DBAPIError:
    return sa_exc.DBAPIError(
        _SENTINEL_STATEMENT,
        {_SENTINEL_PARAMETER_KEY: _SENTINEL_PARAMETER_VALUE},
        _DriverFailure(sqlstate),
    )


# --- SQLSTATE classification -------------------------------------------------


@pytest.mark.parametrize("sqlstate", sorted(RETRYABLE_CONTENTION_SQLSTATES))
def test_contention_sqlstates_classify_as_retryable_contention(sqlstate: str) -> None:
    assert classify_database_failure(_wrapped_failure(sqlstate)) is DatabaseFailureKind.CONTENTION


def test_contention_sqlstate_set_is_exactly_the_specified_four() -> None:
    assert frozenset({"40001", "40P01", "55P03", "57014"}) == RETRYABLE_CONTENTION_SQLSTATES


@pytest.mark.parametrize(
    "sqlstate",
    ["08000", "08001", "08003", "08006", "53300", "57P01", "57P02", "57P03"],
)
def test_connection_sqlstates_classify_as_unavailable(sqlstate: str) -> None:
    assert classify_database_failure(_wrapped_failure(sqlstate)) is (
        DatabaseFailureKind.UNAVAILABLE
    )


def test_operational_error_without_sqlstate_classifies_as_unavailable() -> None:
    cause = sa_exc.OperationalError(
        _SENTINEL_STATEMENT,
        {_SENTINEL_PARAMETER_KEY: _SENTINEL_PARAMETER_VALUE},
        _DriverFailure(None),
    )
    assert classify_database_failure(cause) is DatabaseFailureKind.UNAVAILABLE


def test_other_database_failure_classifies_as_unclassified_database() -> None:
    assert classify_database_failure(_wrapped_failure("23514")) is (
        DatabaseFailureKind.UNCLASSIFIED_DATABASE
    )


def test_non_database_failure_classifies_as_not_database() -> None:
    assert classify_database_failure(RuntimeError(_SENTINEL_DRIVER_TEXT)) is (
        DatabaseFailureKind.NOT_DATABASE
    )


# --- closed-code mapping -------------------------------------------------------


def test_contention_maps_to_retryable_concurrency_busy() -> None:
    source_id = uuid4()
    error = map_database_failure(_wrapped_failure("40P01"), source_id=source_id)
    assert isinstance(error, SourcePublicationError)
    assert error.error_code is ErrorCode.SOURCE_CONCURRENCY_BUSY
    assert error.is_retryable is True
    assert dict(error.safe_details) == {"source_id": source_id}


def test_unavailable_maps_to_retryable_unknown_commit_outcome() -> None:
    source_id = uuid4()
    error = map_database_failure(_wrapped_failure("08006"), source_id=source_id)
    assert isinstance(error, SourcePublicationError)
    assert error.error_code is ErrorCode.SOURCE_COMMIT_OUTCOME_UNKNOWN
    assert error.is_retryable is True
    assert dict(error.safe_details) == {"source_id": source_id}


def test_unclassified_database_failure_maps_to_unknown_commit_outcome() -> None:
    source_id = uuid4()
    error = map_database_failure(_wrapped_failure("23514"), source_id=source_id)
    assert isinstance(error, SourcePublicationError)
    assert error.error_code is ErrorCode.SOURCE_COMMIT_OUTCOME_UNKNOWN
    assert error.is_retryable is True


def test_non_database_failure_maps_to_internal_error() -> None:
    error = map_database_failure(RuntimeError(_SENTINEL_DRIVER_TEXT), source_id=uuid4())
    assert isinstance(error, InternalApplicationError)
    assert error.error_code is ErrorCode.INTERNAL_ERROR


def test_mapped_errors_never_leak_sql_parameters_or_driver_text() -> None:
    source_id = uuid4()
    causes = (
        _wrapped_failure("40P01"),
        _wrapped_failure("08006"),
        _wrapped_failure("23514"),
        sa_exc.OperationalError(
            _SENTINEL_STATEMENT,
            {_SENTINEL_PARAMETER_KEY: _SENTINEL_PARAMETER_VALUE},
            _DriverFailure(None),
        ),
        RuntimeError(_SENTINEL_DRIVER_TEXT),
    )
    for cause in causes:
        error = map_database_failure(cause, source_id=source_id)
        rendered = f"{error} {error!r} {json.dumps(error.to_safe_dict(), default=repr)}"
        for sentinel in _SENTINELS:
            assert sentinel not in rendered, sentinel


# --- bounded contention retry --------------------------------------------------


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


@pytest.mark.asyncio
async def test_retry_succeeds_after_transient_contenttion() -> None:
    sleep = _SleepRecorder()
    jitter = _JitterRecorder()
    attempts: list[int] = []

    async def operation(attempt: int) -> str:
        attempts.append(attempt)
        if attempt < 3:
            raise _wrapped_failure("40P01")
        return "resolved"

    result = await DatabaseRetryPolicy().run(
        operation,
        source_id=uuid4(),
        sleep=sleep,
        jitter=jitter,
    )
    assert result == "resolved"
    assert attempts == [1, 2, 3]
    assert sleep.delays == [RETRY_JITTER_MINIMUM_SECONDS, RETRY_JITTER_MINIMUM_SECONDS]
    assert jitter.bounds == [(RETRY_JITTER_MINIMUM_SECONDS, RETRY_JITTER_MAXIMUM_SECONDS)] * 2


@pytest.mark.asyncio
async def test_retry_reraises_business_error_without_retrying() -> None:
    sleep = _SleepRecorder()
    business_error = SourcePublicationError(
        ErrorCode.SOURCE_IDEMPOTENCY_MISMATCH, safe_details={"source_id": uuid4()}
    )
    attempts: list[int] = []

    async def operation(attempt: int) -> None:
        attempts.append(attempt)
        raise business_error

    with pytest.raises(SourcePublicationError) as captured:
        await DatabaseRetryPolicy().run(
            operation, source_id=uuid4(), sleep=sleep, jitter=_JitterRecorder()
        )
    assert captured.value is business_error
    assert attempts == [1]
    assert sleep.delays == []


@pytest.mark.asyncio
async def test_non_contention_failure_maps_without_retry() -> None:
    sleep = _SleepRecorder()
    attempts: list[int] = []

    async def operation(attempt: int) -> None:
        attempts.append(attempt)
        raise _wrapped_failure("08006")

    with pytest.raises(SourcePublicationError) as captured:
        await DatabaseRetryPolicy().run(
            operation, source_id=uuid4(), sleep=sleep, jitter=_JitterRecorder()
        )
    assert attempts == [1]
    assert sleep.delays == []
    assert captured.value.error_code is ErrorCode.SOURCE_COMMIT_OUTCOME_UNKNOWN
    assert not isinstance(captured.value, _DriverFailure)


@pytest.mark.asyncio
async def test_exhausted_contention_maps_to_concurrency_busy() -> None:
    source_id = uuid4()
    sleep = _SleepRecorder()
    attempts: list[int] = []
    contention_cause = _wrapped_failure("40P01")

    async def operation(attempt: int) -> None:
        attempts.append(attempt)
        raise contention_cause

    with pytest.raises(SourcePublicationError) as captured:
        await DatabaseRetryPolicy(maximum_attempts=3).run(
            operation, source_id=source_id, sleep=sleep, jitter=_JitterRecorder()
        )
    assert attempts == [1, 2, 3]
    assert sleep.delays == [RETRY_JITTER_MINIMUM_SECONDS] * 2
    assert captured.value.error_code is ErrorCode.SOURCE_CONCURRENCY_BUSY
    assert captured.value.__cause__ is contention_cause


@pytest.mark.asyncio
async def test_retry_reraises_internal_non_database_error_as_internal_error() -> None:
    async def operation(attempt: int) -> None:
        raise RuntimeError(_SENTINEL_DRIVER_TEXT)

    with pytest.raises(InternalApplicationError) as captured:
        await DatabaseRetryPolicy().run(
            operation, source_id=uuid4(), sleep=_SleepRecorder(), jitter=_JitterRecorder()
        )
    assert captured.value.error_code is ErrorCode.INTERNAL_ERROR
    rendered = f"{captured.value} {captured.value!r}"
    assert _SENTINEL_DRIVER_TEXT not in rendered


@pytest.mark.asyncio
async def test_retry_reraises_cancellation_untouched() -> None:
    async def operation(attempt: int) -> None:
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await DatabaseRetryPolicy().run(
            operation, source_id=uuid4(), sleep=_SleepRecorder(), jitter=_JitterRecorder()
        )


def test_jitter_bounds_are_the_specified_50_to_250_ms() -> None:
    assert RETRY_JITTER_MINIMUM_SECONDS == 0.05
    assert RETRY_JITTER_MAXIMUM_SECONDS == 0.25


def test_policy_defaults_to_three_attempts() -> None:
    assert DatabaseRetryPolicy().maximum_attempts == 3
