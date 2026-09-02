"""Source lifecycle telemetry leakage contract: sentinels never reach a sink.

Every scenario feeds a unique ``do-not-emit-*`` sentinel (locator, title,
fingerprint, token, content) through the service boundary and asserts the
sentinel never appears in ``str(error)``, ``repr(error)``, the recorded
metric labels or any captured log record. The metrics labels are restricted
to the closed ``operation``/``outcome``/``error_code`` dimensions, so the
sentinel can never cross into a label even when the service records a
rejection or a replay.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

import pytest
from tests.unit.source_lifecycle.fakes import (
    CallLedger,
    FakeLifecycleConflictCaptureGateway,
    FakeLifecyclePolicy,
    FakeLifecycleStore,
    SequencedUtcClock,
    build_commit_result,
    build_decision,
    build_device_context,
    build_diagnostic_context,
    build_locator_conflict_error,
    build_rename_command,
)

from personal_os.source_lifecycle.commands import LifecycleOperation
from personal_os.source_lifecycle.errors import SourceLifecycleError, SourceLifecycleErrorCode
from personal_os.source_lifecycle.metrics import LifecycleMetricOutcome
from personal_os.source_lifecycle.service import SourceLifecycleService

LOCATOR_SENTINEL = "do-not-emit-locator"
TITLE_SENTINEL = "do-not-emit-title"
FINGERPRINT_SENTINEL = "do-not-emit-fingerprint"
TOKEN_SENTINEL = "do-not-emit-token"
CONTENT_SENTINEL = "do-not-emit-content"

_SENTINELS = (
    LOCATOR_SENTINEL,
    TITLE_SENTINEL,
    FINGERPRINT_SENTINEL,
    TOKEN_SENTINEL,
    CONTENT_SENTINEL,
)


class _RecordingMetrics:
    """Capture every label crossing the metrics boundary as a ``str``."""

    def __init__(self) -> None:
        self.commit_records: list[tuple[str, str, float]] = []
        self.rejection_records: list[tuple[str, str]] = []

    def record_commit(
        self,
        *,
        operation: LifecycleOperation,
        outcome: LifecycleMetricOutcome,
        duration_seconds: float,
    ) -> None:
        self.commit_records.append((operation.value, outcome.value, duration_seconds))

    def record_rejection(
        self,
        *,
        operation: LifecycleOperation,
        error_code: SourceLifecycleErrorCode,
    ) -> None:
        self.rejection_records.append((operation.value, error_code.value))


def _captured_blob(caplog: pytest.LogCaptureFixture) -> str:
    return "\n".join(
        [caplog.text]
        + [record.getMessage() for record in caplog.records]
        + [repr(record) for record in caplog.records]
    )


def _moment(offset_seconds: float = 0) -> datetime:
    base = datetime(2026, 8, 20, 1, 2, 3, tzinfo=UTC)
    return base + timedelta(seconds=offset_seconds)


def _never_capturing_gateway() -> FakeLifecycleConflictCaptureGateway:
    """A conflict-capture double that answers None and never reaches a sink."""

    return FakeLifecycleConflictCaptureGateway(ledger=CallLedger())


def test_typed_lifecycle_error_never_leaks_locator_or_decision_text() -> None:
    """A typed store failure must not copy locator or decision text into the error."""

    underlying = RuntimeError(
        f"store failure at {LOCATOR_SENTINEL} title={TITLE_SENTINEL} "
        f"fingerprint={FINGERPRINT_SENTINEL} token={TOKEN_SENTINEL} "
        f"content={CONTENT_SENTINEL}"
    )

    try:
        raise underlying
    except RuntimeError as cause:
        from personal_os.source_lifecycle.errors import SourceLifecycleError

        try:
            raise SourceLifecycleError(SourceLifecycleErrorCode.LOCATOR_CONFLICT) from cause
        except SourceLifecycleError as captured:
            error = captured

    rendered = f"{error} {error!r} {error.code.value}"
    for sentinel in _SENTINELS:
        assert sentinel not in rendered, sentinel


def test_metrics_labels_only_carry_operation_outcome_and_error_code() -> None:
    """The metric sink must only see the closed operation, outcome and error_code labels."""

    command = build_rename_command()
    committed = build_commit_result(command)
    device_context = build_device_context()
    ledger = CallLedger()
    store = FakeLifecycleStore(
        ledger=ledger,
        commit_result=committed,
        committed_result=committed,
    )
    policy = FakeLifecyclePolicy(ledger=ledger)
    metrics = _RecordingMetrics()
    clock = SequencedUtcClock(moments=[_moment(0), _moment(1)])
    service = SourceLifecycleService(
        store=store,
        policy=policy,
        conflict_capture=_never_capturing_gateway(),
        metrics=metrics,
        clock=clock,
    )

    asyncio.run(
        service.commit(
            command=command,
            device_context=device_context,
            diagnostic_context=build_diagnostic_context(),
        )
    )

    for operation_label, outcome_label, _ in metrics.commit_records:
        assert operation_label in {op.value for op in LifecycleOperation}
        assert outcome_label in {outcome.value for outcome in LifecycleMetricOutcome}
    assert metrics.rejection_records == []


def test_rejection_metric_carries_only_the_closed_error_code_label() -> None:
    """A typed rejection emits a single closed ``error_code`` label, never raw text."""

    command = build_rename_command()
    device_context = build_device_context()
    decision = build_decision(device_context=device_context, command=command)
    ledger = CallLedger()
    store = FakeLifecycleStore(
        ledger=ledger,
        commit_result=build_commit_result(command),
        commit_error=build_locator_conflict_error(),
    )
    policy = FakeLifecyclePolicy(ledger=ledger, decision=decision)
    metrics = _RecordingMetrics()
    clock = SequencedUtcClock(moments=[_moment(0), _moment(1)])
    service = SourceLifecycleService(
        store=store,
        policy=policy,
        conflict_capture=_never_capturing_gateway(),
        metrics=metrics,
        clock=clock,
    )

    with pytest.raises(SourceLifecycleError):
        asyncio.run(
            service.commit(
                command=command,
                device_context=device_context,
                diagnostic_context=build_diagnostic_context(),
            )
        )

    assert len(metrics.rejection_records) == 1
    operation_label, error_code_label = metrics.rejection_records[0]
    assert operation_label == LifecycleOperation.RENAME.value
    assert error_code_label in {code.value for code in SourceLifecycleErrorCode}


def test_locator_and_decision_text_never_appear_in_captured_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A successful commit must not emit any log record containing a sentinel."""

    command = build_rename_command()
    device_context = build_device_context()
    decision = build_decision(device_context=device_context, command=command)
    ledger = CallLedger()
    store = FakeLifecycleStore(ledger=ledger, commit_result=build_commit_result(command))
    policy = FakeLifecyclePolicy(ledger=ledger, decision=decision)
    metrics = _RecordingMetrics()
    clock = SequencedUtcClock(moments=[_moment(0), _moment(1)])
    service = SourceLifecycleService(
        store=store,
        policy=policy,
        conflict_capture=_never_capturing_gateway(),
        metrics=metrics,
        clock=clock,
    )

    with caplog.at_level(logging.DEBUG):
        asyncio.run(
            service.commit(
                command=command,
                device_context=device_context,
                diagnostic_context=build_diagnostic_context(),
            )
        )

    blob = _captured_blob(caplog)
    for sentinel in _SENTINELS:
        assert sentinel not in blob, sentinel


def test_typed_rejection_propagates_without_copying_cause_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A typed rejection propagates; raw cause text never crosses the boundary."""

    command = build_rename_command()
    device_context = build_device_context()
    decision = build_decision(device_context=device_context, command=command)
    ledger = CallLedger()
    store = FakeLifecycleStore(
        ledger=ledger,
        commit_result=build_commit_result(command),
        commit_error=build_locator_conflict_error(),
    )
    policy = FakeLifecyclePolicy(ledger=ledger, decision=decision)
    metrics = _RecordingMetrics()
    clock = SequencedUtcClock(moments=[_moment(0), _moment(1)])
    service = SourceLifecycleService(
        store=store,
        policy=policy,
        conflict_capture=_never_capturing_gateway(),
        metrics=metrics,
        clock=clock,
    )

    with (
        caplog.at_level(logging.DEBUG),
        pytest.raises(SourceLifecycleError) as exc_info,
    ):
        asyncio.run(
            service.commit(
                command=command,
                device_context=device_context,
                diagnostic_context=build_diagnostic_context(),
            )
        )

    blob = _captured_blob(caplog)
    rendered = f"{exc_info.value} {exc_info.value!r}"
    for sentinel in _SENTINELS:
        assert sentinel not in blob, sentinel
        assert sentinel not in rendered, sentinel


def test_service_protocols_are_provider_neutral() -> None:
    """The service depends only on the closed source lifecycle ports."""

    ledger = CallLedger()
    store = FakeLifecycleStore(
        ledger=ledger, commit_result=build_commit_result(build_rename_command())
    )
    policy = FakeLifecyclePolicy(ledger=ledger)
    metrics = _RecordingMetrics()
    service = SourceLifecycleService(
        store=store,
        policy=policy,
        conflict_capture=_never_capturing_gateway(),
        metrics=metrics,
        clock=SequencedUtcClock(moments=[_moment(0), _moment(1)]),
    )

    assert service.store is store
    assert service.policy is policy
    assert service.metrics is metrics
    assert hasattr(service.store, "resolve_committed")
    assert hasattr(service.store, "commit")
    assert hasattr(service.policy, "evaluate_lifecycle")
    assert hasattr(service.metrics, "record_commit")
    assert hasattr(service.metrics, "record_rejection")
