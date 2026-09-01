"""Source lifecycle service orchestration: replay → policy → store.

Every test pins one slice of the orchestration contract:
- Exact replay returns the canonical committed result before the policy
  port is consulted, with the metrics replay outcome recorded.
- A fresh successful commit records the ``committed`` outcome exactly
  once; an exact replay never records a committed counter.
- Allowed and denied/indeterminate rename/move hand the decision
  unchanged to the store; the projection intent selection is the store's
  concern and is not re-decided by the service.
- Unconditional delete routes through the policy port and the store with
  the same shape as rename/move; the service never short-circuits.
- Allowed and denied/indeterminate restore follow the same flow as
  rename/move.
- Cancellation: any typed or scriptable exception raised by the policy or
  the store propagates verbatim; no service-internal retry wraps it.
- Safe error mapping: every typed ``SourceLifecycleErrorCode`` raised by
  the store is mapped to a ``record_rejection`` call with the matching
  closed ``error_code`` label, never a re-raised non-typed exception.

The tests deliberately avoid inspecting raw locator text or title values:
only canonical UUIDs, operation tokens and outcomes cross the boundary.
"""

from __future__ import annotations

from datetime import UTC

import pytest
from tests.unit.source_lifecycle.fakes import (
    CAPTURE_DELETE_REMOTE_EDIT,
    CAPTURE_LOCATOR_COLLISION,
    POLICY_EVALUATE_LIFECYCLE,
    STORE_COMMIT,
    STORE_RESOLVE_COMMITTED,
    CallLedger,
    FakeLifecycleConflictCaptureGateway,
    FakeLifecyclePolicy,
    FakeLifecycleStore,
    SequencedUtcClock,
    build_commit_outcome_unknown_error,
    build_commit_result,
    build_conflict_receipt,
    build_decision,
    build_delete_command,
    build_device_context,
    build_diagnostic_context,
    build_input_invalid_error,
    build_locator_conflict_error,
    build_locator_missing_error,
    build_move_command,
    build_rename_command,
    build_restore_command,
    build_tombstone_closed_error,
    build_tombstone_not_found_error,
    build_version_conflict_error,
)

from personal_os.error_contracts.codes import ErrorCode
from personal_os.source_conflicts.errors import SourceConflictError
from personal_os.source_lifecycle.commands import (
    LifecycleConflictKind,
    LifecycleOperation,
)
from personal_os.source_lifecycle.errors import SourceLifecycleErrorCode
from personal_os.source_lifecycle.fingerprint import fingerprint_lifecycle_command
from personal_os.source_lifecycle.metrics import (
    LifecycleMetricOutcome,
    SourceLifecycleMetrics,
)
from personal_os.source_lifecycle.ports import (
    LifecyclePolicyOutcome,
)
from personal_os.source_lifecycle.service import SourceLifecycleService


class _RecordingMetrics:
    """Bounded in-memory recorder for service-level metrics.

    Records only the closed ``operation``, ``outcome`` and ``error_code``
    labels; no UUID, locator, title or fingerprint crosses the boundary.
    """

    def __init__(self) -> None:
        self.commit_records: list[tuple[LifecycleOperation, LifecycleMetricOutcome, float]] = []
        self.rejection_records: list[tuple[LifecycleOperation, SourceLifecycleErrorCode]] = []

    def record_commit(
        self,
        *,
        operation: LifecycleOperation,
        outcome: LifecycleMetricOutcome,
        duration_seconds: float,
    ) -> None:
        self.commit_records.append((operation, outcome, duration_seconds))

    def record_rejection(
        self,
        *,
        operation: LifecycleOperation,
        error_code: SourceLifecycleErrorCode,
    ) -> None:
        self.rejection_records.append((operation, error_code))

    def commit_count(self, operation: LifecycleOperation, outcome: LifecycleMetricOutcome) -> int:
        return sum(
            1
            for recorded_operation, recorded_outcome, _ in self.commit_records
            if recorded_operation is operation and recorded_outcome is outcome
        )

    def rejection_count(
        self, operation: LifecycleOperation, error_code: SourceLifecycleErrorCode
    ) -> int:
        return sum(
            1
            for recorded_operation, recorded_code in self.rejection_records
            if recorded_operation is operation and recorded_code is error_code
        )


class _UnsetProtocol:
    """Placeholder used purely to keep type-checkers honest until the service ships."""


def _build_service(
    *,
    store: FakeLifecycleStore,
    policy: FakeLifecyclePolicy,
    metrics: _RecordingMetrics | None = None,
    clock: SequencedUtcClock | None = None,
    conflict_capture: FakeLifecycleConflictCaptureGateway | None = None,
) -> SourceLifecycleService:
    """Wire the service against the in-memory fakes."""

    metrics_sink: SourceLifecycleMetrics = metrics if metrics is not None else _RecordingMetrics()
    if not isinstance(metrics_sink, _RecordingMetrics):
        raise AssertionError("metrics sink must be the recording fake")
    if clock is None:
        clock = SequencedUtcClock(
            moments=[
                _moment(0),
                _moment(1),
                _moment(2),
            ]
        )
    return SourceLifecycleService(
        store=store,
        policy=policy,
        conflict_capture=(
            conflict_capture
            if conflict_capture is not None
            else FakeLifecycleConflictCaptureGateway(ledger=CallLedger())
        ),
        metrics=metrics_sink,
        clock=clock,
    )


def _moment(offset_seconds: float) -> datetime:  # type: ignore[name-defined]  # noqa: F821
    from datetime import datetime, timedelta

    base = datetime(2026, 8, 20, 1, 2, 3, tzinfo=UTC)
    return base + timedelta(seconds=offset_seconds)


@pytest.mark.asyncio
async def test_exact_replay_returns_committed_result_without_policy_or_commit_call() -> None:
    """An exact fingerprint replay must skip the policy port and the commit path."""

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
    service = _build_service(store=store, policy=policy, metrics=metrics)

    result = await service.commit(
        command=command,
        device_context=device_context,
        diagnostic_context=build_diagnostic_context(),
    )

    assert result is committed
    assert ledger.entries == [STORE_RESOLVE_COMMITTED]
    assert policy.calls == []
    assert store.commit_fingerprints == []
    assert metrics.commit_count(LifecycleOperation.RENAME, LifecycleMetricOutcome.REPLAYED) == 1


@pytest.mark.asyncio
async def test_allowed_rename_hands_decision_unchanged_to_store_commit() -> None:
    """An allowed rename routes through the policy port and the store commit."""

    command = build_rename_command()
    device_context = build_device_context()
    decision = build_decision(
        device_context=device_context,
        command=command,
        outcome=LifecyclePolicyOutcome.ALLOWED,
    )
    commit_result = build_commit_result(command)
    ledger = CallLedger()
    store = FakeLifecycleStore(ledger=ledger, commit_result=commit_result)
    policy = FakeLifecyclePolicy(ledger=ledger, decision=decision)
    metrics = _RecordingMetrics()
    service = _build_service(store=store, policy=policy, metrics=metrics)

    result = await service.commit(
        command=command,
        device_context=device_context,
        diagnostic_context=build_diagnostic_context(),
    )

    assert result is commit_result
    assert ledger.entries == [STORE_RESOLVE_COMMITTED, POLICY_EVALUATE_LIFECYCLE, STORE_COMMIT]
    assert policy.calls == [(command, device_context)]
    assert store.commit_decisions == [decision]
    assert store.commit_commands == [command]
    assert metrics.commit_count(LifecycleOperation.RENAME, LifecycleMetricOutcome.COMMITTED) == 1


@pytest.mark.asyncio
async def test_fresh_commit_records_the_committed_counter_row() -> None:
    """The write side records COMMITTED, so the admin route's commit_counters
    can show a committed row (BACKLOG 2026-08-24 §5.4)."""

    command = build_rename_command()
    device_context = build_device_context()
    decision = build_decision(
        device_context=device_context,
        command=command,
        outcome=LifecyclePolicyOutcome.ALLOWED,
    )
    commit_result = build_commit_result(command)
    ledger = CallLedger()
    store = FakeLifecycleStore(ledger=ledger, commit_result=commit_result)
    policy = FakeLifecyclePolicy(ledger=ledger, decision=decision)
    metrics = _RecordingMetrics()
    service = _build_service(store=store, policy=policy, metrics=metrics)

    result = await service.commit(
        command=command,
        device_context=device_context,
        diagnostic_context=build_diagnostic_context(),
    )

    assert result is commit_result  # fresh commit, not a replay
    assert metrics.commit_count(LifecycleOperation.RENAME, LifecycleMetricOutcome.COMMITTED) == 1
    assert metrics.commit_count(LifecycleOperation.RENAME, LifecycleMetricOutcome.REPLAYED) == 0


@pytest.mark.asyncio
async def test_denied_rename_still_commits_and_hands_decision_unchanged() -> None:
    """A denied rename must still reach the store commit with the decision intact."""

    command = build_rename_command()
    device_context = build_device_context()
    decision = build_decision(
        device_context=device_context,
        command=command,
        outcome=LifecyclePolicyOutcome.DENIED,
    )
    commit_result = build_commit_result(command)
    ledger = CallLedger()
    store = FakeLifecycleStore(ledger=ledger, commit_result=commit_result)
    policy = FakeLifecyclePolicy(ledger=ledger, decision=decision)
    service = _build_service(store=store, policy=policy, metrics=_RecordingMetrics())

    result = await service.commit(
        command=command,
        device_context=device_context,
        diagnostic_context=build_diagnostic_context(),
    )

    assert result is commit_result
    assert ledger.entries == [STORE_RESOLVE_COMMITTED, POLICY_EVALUATE_LIFECYCLE, STORE_COMMIT]
    assert store.commit_decisions == [decision]
    assert store.commit_decisions[0].outcome is LifecyclePolicyOutcome.DENIED


@pytest.mark.asyncio
async def test_indeterminate_rename_still_commits_and_hands_decision_unchanged() -> None:
    """An indeterminate rename must still reach the store commit with the decision intact."""

    command = build_rename_command()
    device_context = build_device_context()
    decision = build_decision(
        device_context=device_context,
        command=command,
        outcome=LifecyclePolicyOutcome.INDETERMINATE,
    )
    commit_result = build_commit_result(command)
    ledger = CallLedger()
    store = FakeLifecycleStore(ledger=ledger, commit_result=commit_result)
    policy = FakeLifecyclePolicy(ledger=ledger, decision=decision)
    service = _build_service(store=store, policy=policy, metrics=_RecordingMetrics())

    result = await service.commit(
        command=command,
        device_context=device_context,
        diagnostic_context=build_diagnostic_context(),
    )

    assert result is commit_result
    assert ledger.entries == [STORE_RESOLVE_COMMITTED, POLICY_EVALUATE_LIFECYCLE, STORE_COMMIT]
    assert store.commit_decisions == [decision]
    assert store.commit_decisions[0].outcome is LifecyclePolicyOutcome.INDETERMINATE


@pytest.mark.asyncio
async def test_allowed_move_hands_decision_unchanged_to_store_commit() -> None:
    """An allowed move follows the same orchestration path as an allowed rename."""

    command = build_move_command()
    device_context = build_device_context()
    decision = build_decision(
        device_context=device_context,
        command=command,
        outcome=LifecyclePolicyOutcome.ALLOWED,
    )
    commit_result = build_commit_result(command)
    ledger = CallLedger()
    store = FakeLifecycleStore(ledger=ledger, commit_result=commit_result)
    policy = FakeLifecyclePolicy(ledger=ledger, decision=decision)
    service = _build_service(store=store, policy=policy, metrics=_RecordingMetrics())

    result = await service.commit(
        command=command,
        device_context=device_context,
        diagnostic_context=build_diagnostic_context(),
    )

    assert result is commit_result
    assert ledger.entries == [STORE_RESOLVE_COMMITTED, POLICY_EVALUATE_LIFECYCLE, STORE_COMMIT]
    assert store.commit_decisions == [decision]
    assert store.commit_decisions[0].outcome is LifecyclePolicyOutcome.ALLOWED


@pytest.mark.asyncio
async def test_unconditional_delete_routes_through_policy_and_store() -> None:
    """Delete is unconditional on policy: the service must still evaluate the policy."""

    command = build_delete_command()
    device_context = build_device_context()
    decision = build_decision(
        device_context=device_context,
        command=command,
        outcome=LifecyclePolicyOutcome.ALLOWED,
    )
    commit_result = build_commit_result(command)
    ledger = CallLedger()
    store = FakeLifecycleStore(ledger=ledger, commit_result=commit_result)
    policy = FakeLifecyclePolicy(ledger=ledger, decision=decision)
    service = _build_service(store=store, policy=policy, metrics=_RecordingMetrics())

    result = await service.commit(
        command=command,
        device_context=device_context,
        diagnostic_context=build_diagnostic_context(),
    )

    assert result is commit_result
    assert ledger.entries == [STORE_RESOLVE_COMMITTED, POLICY_EVALUATE_LIFECYCLE, STORE_COMMIT]
    assert store.commit_decisions == [decision]
    assert policy.calls == [(command, device_context)]


@pytest.mark.asyncio
async def test_unconditional_delete_passes_denied_decision_to_store() -> None:
    """Delete must commit truthful state even when the policy returns DENIED.

    The store is the only place that selects the projection delete intent
    for delete operations; the service must not interfere.
    """

    command = build_delete_command()
    device_context = build_device_context()
    decision = build_decision(
        device_context=device_context,
        command=command,
        outcome=LifecyclePolicyOutcome.DENIED,
    )
    commit_result = build_commit_result(command)
    ledger = CallLedger()
    store = FakeLifecycleStore(ledger=ledger, commit_result=commit_result)
    policy = FakeLifecyclePolicy(ledger=ledger, decision=decision)
    service = _build_service(store=store, policy=policy, metrics=_RecordingMetrics())

    result = await service.commit(
        command=command,
        device_context=device_context,
        diagnostic_context=build_diagnostic_context(),
    )

    assert result is commit_result
    assert store.commit_decisions == [decision]
    assert store.commit_decisions[0].outcome is LifecyclePolicyOutcome.DENIED


@pytest.mark.asyncio
async def test_allowed_restore_hands_decision_unchanged_to_store_commit() -> None:
    """An allowed restore follows the same orchestration path as rename/move."""

    command = build_restore_command()
    device_context = build_device_context()
    decision = build_decision(
        device_context=device_context,
        command=command,
        outcome=LifecyclePolicyOutcome.ALLOWED,
    )
    commit_result = build_commit_result(command)
    ledger = CallLedger()
    store = FakeLifecycleStore(ledger=ledger, commit_result=commit_result)
    policy = FakeLifecyclePolicy(ledger=ledger, decision=decision)
    service = _build_service(store=store, policy=policy, metrics=_RecordingMetrics())

    result = await service.commit(
        command=command,
        device_context=device_context,
        diagnostic_context=build_diagnostic_context(),
    )

    assert result is commit_result
    assert ledger.entries == [STORE_RESOLVE_COMMITTED, POLICY_EVALUATE_LIFECYCLE, STORE_COMMIT]
    assert store.commit_decisions == [decision]
    assert store.commit_decisions[0].outcome is LifecyclePolicyOutcome.ALLOWED


@pytest.mark.asyncio
async def test_denied_restore_still_commits_with_denied_decision() -> None:
    """A denied restore must still reach the store commit with the decision intact."""

    command = build_restore_command()
    device_context = build_device_context()
    decision = build_decision(
        device_context=device_context,
        command=command,
        outcome=LifecyclePolicyOutcome.DENIED,
    )
    commit_result = build_commit_result(command)
    ledger = CallLedger()
    store = FakeLifecycleStore(ledger=ledger, commit_result=commit_result)
    policy = FakeLifecyclePolicy(ledger=ledger, decision=decision)
    service = _build_service(store=store, policy=policy, metrics=_RecordingMetrics())

    result = await service.commit(
        command=command,
        device_context=device_context,
        diagnostic_context=build_diagnostic_context(),
    )

    assert result is commit_result
    assert store.commit_decisions == [decision]
    assert store.commit_decisions[0].outcome is LifecyclePolicyOutcome.DENIED


@pytest.mark.asyncio
async def test_indeterminate_restore_still_commits_with_indeterminate_decision() -> None:
    """An indeterminate restore must still reach the store commit."""

    command = build_restore_command()
    device_context = build_device_context()
    decision = build_decision(
        device_context=device_context,
        command=command,
        outcome=LifecyclePolicyOutcome.INDETERMINATE,
    )
    commit_result = build_commit_result(command)
    ledger = CallLedger()
    store = FakeLifecycleStore(ledger=ledger, commit_result=commit_result)
    policy = FakeLifecyclePolicy(ledger=ledger, decision=decision)
    service = _build_service(store=store, policy=policy, metrics=_RecordingMetrics())

    result = await service.commit(
        command=command,
        device_context=device_context,
        diagnostic_context=build_diagnostic_context(),
    )

    assert result is commit_result
    assert store.commit_decisions == [decision]
    assert store.commit_decisions[0].outcome is LifecyclePolicyOutcome.INDETERMINATE


@pytest.mark.asyncio
async def test_cancellation_during_policy_evaluation_propagates_without_retry() -> None:
    """A cancellation during the policy port call propagates verbatim, no retry."""

    command = build_rename_command()
    device_context = build_device_context()
    ledger = CallLedger()
    store = FakeLifecycleStore(ledger=ledger, commit_result=build_commit_result(command))
    policy = FakeLifecyclePolicy(
        ledger=ledger,
        error=build_input_invalid_error(),
    )
    metrics = _RecordingMetrics()
    service = _build_service(store=store, policy=policy, metrics=metrics)

    with pytest.raises(Exception) as exc_info:
        await service.commit(
            command=command,
            device_context=device_context,
            diagnostic_context=build_diagnostic_context(),
        )

    from personal_os.source_lifecycle.errors import SourceLifecycleError

    assert isinstance(exc_info.value, SourceLifecycleError)
    assert exc_info.value.code is SourceLifecycleErrorCode.INPUT_INVALID
    assert ledger.entries == [STORE_RESOLVE_COMMITTED, POLICY_EVALUATE_LIFECYCLE]
    assert store.commit_fingerprints == []
    assert (
        metrics.rejection_count(LifecycleOperation.RENAME, SourceLifecycleErrorCode.INPUT_INVALID)
        == 1
    )


@pytest.mark.asyncio
async def test_cancellation_during_store_commit_propagates_without_retry() -> None:
    """A typed store failure propagates verbatim, the service does not retry it."""

    command = build_rename_command()
    device_context = build_device_context()
    decision = build_decision(device_context=device_context, command=command)
    commit_error = build_locator_conflict_error()
    ledger = CallLedger()
    store = FakeLifecycleStore(
        ledger=ledger,
        commit_result=build_commit_result(command),
        commit_error=commit_error,
    )
    policy = FakeLifecyclePolicy(ledger=ledger, decision=decision)
    metrics = _RecordingMetrics()
    service = _build_service(store=store, policy=policy, metrics=metrics)

    with pytest.raises(Exception) as exc_info:
        await service.commit(
            command=command,
            device_context=device_context,
            diagnostic_context=build_diagnostic_context(),
        )

    from personal_os.source_lifecycle.errors import SourceLifecycleError

    assert isinstance(exc_info.value, SourceLifecycleError)
    assert exc_info.value.code is SourceLifecycleErrorCode.LOCATOR_CONFLICT
    assert ledger.entries == [STORE_RESOLVE_COMMITTED, POLICY_EVALUATE_LIFECYCLE, STORE_COMMIT]
    assert (
        metrics.rejection_count(
            LifecycleOperation.RENAME, SourceLifecycleErrorCode.LOCATOR_CONFLICT
        )
        == 1
    )


@pytest.mark.parametrize(
    "error_factory",
    [
        build_input_invalid_error,
        build_locator_missing_error,
        build_locator_conflict_error,
        build_tombstone_not_found_error,
        build_tombstone_closed_error,
        build_version_conflict_error,
        build_commit_outcome_unknown_error,
    ],
    ids=[
        "input_invalid",
        "locator_missing",
        "locator_conflict",
        "tombstone_not_found",
        "tombstone_closed",
        "version_conflict",
        "commit_outcome_unknown",
    ],
)
@pytest.mark.asyncio
async def test_store_typed_error_maps_to_rejection_metric_and_propagates(
    error_factory,
) -> None:
    """Every typed ``SourceLifecycleErrorCode`` is mapped to a rejection metric."""

    from personal_os.source_lifecycle.errors import SourceLifecycleError

    command = build_rename_command()
    device_context = build_device_context()
    decision = build_decision(device_context=device_context, command=command)
    ledger = CallLedger()
    store = FakeLifecycleStore(
        ledger=ledger,
        commit_result=build_commit_result(command),
        commit_error=error_factory(),
    )
    policy = FakeLifecyclePolicy(ledger=ledger, decision=decision)
    metrics = _RecordingMetrics()
    service = _build_service(store=store, policy=policy, metrics=metrics)

    raised = error_factory()
    expected_code = raised.code

    with pytest.raises(SourceLifecycleError) as exc_info:
        await service.commit(
            command=command,
            device_context=device_context,
            diagnostic_context=build_diagnostic_context(),
        )

    assert exc_info.value.code is expected_code
    assert metrics.rejection_count(LifecycleOperation.RENAME, expected_code) == 1
    # No commit metric is recorded for a typed rejection — only the rejection label.
    assert metrics.commit_count(LifecycleOperation.RENAME, LifecycleMetricOutcome.COMMITTED) == 0


@pytest.mark.asyncio
async def test_replay_path_records_replayed_outcome_never_rejection() -> None:
    """An exact replay records only the replayed outcome, never a committed
    counter or a rejection."""

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
    service = _build_service(store=store, policy=policy, metrics=metrics)

    await service.commit(
        command=command,
        device_context=device_context,
        diagnostic_context=build_diagnostic_context(),
    )

    assert metrics.commit_count(LifecycleOperation.RENAME, LifecycleMetricOutcome.REPLAYED) == 1
    assert metrics.commit_count(LifecycleOperation.RENAME, LifecycleMetricOutcome.COMMITTED) == 0
    assert metrics.rejection_records == []


@pytest.mark.asyncio
async def test_service_does_not_wrap_store_commit_in_additional_retries() -> None:
    """The service commits once per invocation; the store owns the bounded retry policy."""

    command = build_rename_command()
    device_context = build_device_context()
    decision = build_decision(device_context=device_context, command=command)
    commit_result = build_commit_result(command)
    ledger = CallLedger()
    store = FakeLifecycleStore(ledger=ledger, commit_result=commit_result)
    policy = FakeLifecyclePolicy(ledger=ledger, decision=decision)
    service = _build_service(store=store, policy=policy, metrics=_RecordingMetrics())

    await service.commit(
        command=command,
        device_context=device_context,
        diagnostic_context=build_diagnostic_context(),
    )

    assert ledger.entries.count(STORE_COMMIT) == 1
    assert len(store.commit_decisions) == 1


@pytest.mark.asyncio
async def test_replay_path_records_duration_as_non_negative_finite_seconds() -> None:
    """An exact replay records a finite non-negative duration."""

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
    clock = SequencedUtcClock(moments=[_moment(0), _moment(5)])
    service = _build_service(store=store, policy=policy, metrics=metrics, clock=clock)

    await service.commit(
        command=command,
        device_context=device_context,
        diagnostic_context=build_diagnostic_context(),
    )

    [(_, _, duration_seconds)] = metrics.commit_records
    assert duration_seconds == pytest.approx(5.0)


@pytest.mark.asyncio
async def test_each_operation_is_routed_with_its_own_metric_label() -> None:
    """The metrics label carries the closed ``LifecycleOperation`` token."""

    cases = [
        (build_rename_command(), LifecycleOperation.RENAME),
        (build_move_command(), LifecycleOperation.MOVE),
        (build_delete_command(), LifecycleOperation.DELETE),
        (build_restore_command(), LifecycleOperation.RESTORE),
    ]

    for command, expected_operation in cases:
        committed = build_commit_result(command)
        device_context = build_device_context()
        decision = build_decision(device_context=device_context, command=command)
        ledger = CallLedger()
        store = FakeLifecycleStore(
            ledger=ledger,
            commit_result=committed,
            committed_result=committed,
        )
        policy = FakeLifecyclePolicy(ledger=ledger, decision=decision)
        metrics = _RecordingMetrics()
        service = _build_service(store=store, policy=policy, metrics=metrics)

        await service.commit(
            command=command,
            device_context=device_context,
            diagnostic_context=build_diagnostic_context(),
        )

        replay_operations = {record[0] for record in metrics.commit_records}
        assert replay_operations == {expected_operation}


def test_source_lifecycle_service_exposes_the_injected_ports() -> None:
    """The service surfaces the injected store, policy and metrics ports."""

    ledger = CallLedger()
    store = FakeLifecycleStore(
        ledger=ledger,
        commit_result=build_commit_result(build_rename_command()),
    )
    policy = FakeLifecyclePolicy(ledger=ledger)
    metrics = _RecordingMetrics()
    service = _build_service(store=store, policy=policy, metrics=metrics)

    assert service.store is store
    assert service.policy is policy
    assert service.metrics is metrics
    assert hasattr(service.store, "resolve_committed")
    assert hasattr(service.store, "commit")
    assert hasattr(service.policy, "evaluate_lifecycle")


# --- lifecycle race conflict capture ---------------------------------------


@pytest.mark.asyncio
async def test_delete_version_conflict_captures_delete_remote_edit_and_reraises() -> None:
    """A delete that lost to a remote edit hands its race evidence to the
    shared conflict-capture gateway before the typed rejection reraises."""

    command = build_delete_command()
    device_context = build_device_context()
    decision = build_decision(device_context=device_context, command=command)
    ledger = CallLedger()
    capture = FakeLifecycleConflictCaptureGateway(
        ledger=ledger,
        receipt=build_conflict_receipt(command, LifecycleConflictKind.DELETE_REMOTE_EDIT),
    )
    store = FakeLifecycleStore(
        ledger=ledger,
        commit_result=build_commit_result(command),
        commit_error=build_version_conflict_error(),
    )
    policy = FakeLifecyclePolicy(ledger=ledger, decision=decision)
    metrics = _RecordingMetrics()
    service = _build_service(store=store, policy=policy, metrics=metrics, conflict_capture=capture)

    with pytest.raises(Exception) as exc_info:
        await service.commit(
            command=command,
            device_context=device_context,
            diagnostic_context=build_diagnostic_context(),
        )

    from personal_os.source_lifecycle.errors import SourceLifecycleError

    assert isinstance(exc_info.value, SourceLifecycleError)
    assert exc_info.value.code is SourceLifecycleErrorCode.VERSION_CONFLICT
    assert capture.delete_race_calls == [
        (command, device_context, fingerprint_lifecycle_command(command))
    ]
    assert capture.locator_collision_calls == []
    assert ledger.entries == [
        STORE_RESOLVE_COMMITTED,
        POLICY_EVALUATE_LIFECYCLE,
        STORE_COMMIT,
        CAPTURE_DELETE_REMOTE_EDIT,
    ]
    # The rejection label still records exactly once; capture adds no commit row.
    assert (
        metrics.rejection_count(
            LifecycleOperation.DELETE, SourceLifecycleErrorCode.VERSION_CONFLICT
        )
        == 1
    )
    assert metrics.commit_count(LifecycleOperation.DELETE, LifecycleMetricOutcome.COMMITTED) == 0


@pytest.mark.asyncio
async def test_rename_version_conflict_reraises_without_capture() -> None:
    """A byteless rename/move version race has no closed conflict kind: the
    typed rejection propagates and the capture gateway is never consulted."""

    command = build_rename_command()
    device_context = build_device_context()
    decision = build_decision(device_context=device_context, command=command)
    ledger = CallLedger()
    capture = FakeLifecycleConflictCaptureGateway(ledger=ledger)
    store = FakeLifecycleStore(
        ledger=ledger,
        commit_result=build_commit_result(command),
        commit_error=build_version_conflict_error(),
    )
    policy = FakeLifecyclePolicy(ledger=ledger, decision=decision)
    service = _build_service(store=store, policy=policy, conflict_capture=capture)

    with pytest.raises(Exception) as exc_info:
        await service.commit(
            command=command,
            device_context=device_context,
            diagnostic_context=build_diagnostic_context(),
        )

    from personal_os.source_lifecycle.errors import SourceLifecycleError

    assert isinstance(exc_info.value, SourceLifecycleError)
    assert exc_info.value.code is SourceLifecycleErrorCode.VERSION_CONFLICT
    assert capture.delete_race_calls == []
    assert capture.locator_collision_calls == []
    assert CAPTURE_DELETE_REMOTE_EDIT not in ledger.entries
    assert CAPTURE_LOCATOR_COLLISION not in ledger.entries


@pytest.mark.asyncio
async def test_rename_locator_conflict_captures_locator_collision_and_reraises() -> None:
    """A rename whose target is held by another active source hands the race
    to the capture gateway with the command fingerprint intact."""

    command = build_rename_command()
    device_context = build_device_context()
    decision = build_decision(device_context=device_context, command=command)
    ledger = CallLedger()
    capture = FakeLifecycleConflictCaptureGateway(
        ledger=ledger,
        receipt=build_conflict_receipt(command, LifecycleConflictKind.LOCATOR_COLLISION),
    )
    store = FakeLifecycleStore(
        ledger=ledger,
        commit_result=build_commit_result(command),
        commit_error=build_locator_conflict_error(),
    )
    policy = FakeLifecyclePolicy(ledger=ledger, decision=decision)
    metrics = _RecordingMetrics()
    service = _build_service(store=store, policy=policy, metrics=metrics, conflict_capture=capture)

    with pytest.raises(Exception) as exc_info:
        await service.commit(
            command=command,
            device_context=device_context,
            diagnostic_context=build_diagnostic_context(),
        )

    from personal_os.source_lifecycle.errors import SourceLifecycleError

    assert isinstance(exc_info.value, SourceLifecycleError)
    assert exc_info.value.code is SourceLifecycleErrorCode.LOCATOR_CONFLICT
    assert capture.locator_collision_calls == [
        (command, device_context, fingerprint_lifecycle_command(command))
    ]
    assert capture.delete_race_calls == []
    assert ledger.entries[-1] == CAPTURE_LOCATOR_COLLISION
    assert (
        metrics.rejection_count(
            LifecycleOperation.RENAME, SourceLifecycleErrorCode.LOCATOR_CONFLICT
        )
        == 1
    )


@pytest.mark.asyncio
async def test_restore_locator_conflict_captures_locator_collision() -> None:
    """A restore racing onto a held target locator follows the same capture
    path as rename/move."""

    command = build_restore_command()
    device_context = build_device_context()
    decision = build_decision(device_context=device_context, command=command)
    ledger = CallLedger()
    capture = FakeLifecycleConflictCaptureGateway(
        ledger=ledger,
        receipt=build_conflict_receipt(command, LifecycleConflictKind.LOCATOR_COLLISION),
    )
    store = FakeLifecycleStore(
        ledger=ledger,
        commit_result=build_commit_result(command),
        commit_error=build_locator_conflict_error(),
    )
    policy = FakeLifecyclePolicy(ledger=ledger, decision=decision)
    service = _build_service(store=store, policy=policy, conflict_capture=capture)

    with pytest.raises(Exception) as exc_info:
        await service.commit(
            command=command,
            device_context=device_context,
            diagnostic_context=build_diagnostic_context(),
        )

    from personal_os.source_lifecycle.errors import SourceLifecycleError

    assert isinstance(exc_info.value, SourceLifecycleError)
    assert exc_info.value.code is SourceLifecycleErrorCode.LOCATOR_CONFLICT
    assert len(capture.locator_collision_calls) == 1


@pytest.mark.asyncio
async def test_delete_locator_conflict_reraises_without_capture() -> None:
    """A delete carries no target locator, so its locator-conflict rejection
    never reaches the collision capture member."""

    command = build_delete_command()
    device_context = build_device_context()
    decision = build_decision(device_context=device_context, command=command)
    ledger = CallLedger()
    capture = FakeLifecycleConflictCaptureGateway(ledger=ledger)
    store = FakeLifecycleStore(
        ledger=ledger,
        commit_result=build_commit_result(command),
        commit_error=build_locator_conflict_error(),
    )
    policy = FakeLifecyclePolicy(ledger=ledger, decision=decision)
    service = _build_service(store=store, policy=policy, conflict_capture=capture)

    with pytest.raises(Exception) as exc_info:
        await service.commit(
            command=command,
            device_context=device_context,
            diagnostic_context=build_diagnostic_context(),
        )

    from personal_os.source_lifecycle.errors import SourceLifecycleError

    assert isinstance(exc_info.value, SourceLifecycleError)
    assert exc_info.value.code is SourceLifecycleErrorCode.LOCATOR_CONFLICT
    assert capture.delete_race_calls == []
    assert capture.locator_collision_calls == []


@pytest.mark.asyncio
async def test_capture_race_not_confirmed_answer_still_reraises_the_typed_error() -> None:
    """A ``None`` capture answer (race no longer confirmed against canonical
    state) keeps the original typed rejection as the surfaced outcome."""

    command = build_delete_command()
    device_context = build_device_context()
    decision = build_decision(device_context=device_context, command=command)
    ledger = CallLedger()
    capture = FakeLifecycleConflictCaptureGateway(ledger=ledger, receipt=None)
    store = FakeLifecycleStore(
        ledger=ledger,
        commit_result=build_commit_result(command),
        commit_error=build_version_conflict_error(),
    )
    policy = FakeLifecyclePolicy(ledger=ledger, decision=decision)
    service = _build_service(store=store, policy=policy, conflict_capture=capture)

    with pytest.raises(Exception) as exc_info:
        await service.commit(
            command=command,
            device_context=device_context,
            diagnostic_context=build_diagnostic_context(),
        )

    from personal_os.source_lifecycle.errors import SourceLifecycleError

    assert isinstance(exc_info.value, SourceLifecycleError)
    assert exc_info.value.code is SourceLifecycleErrorCode.VERSION_CONFLICT
    assert len(capture.delete_race_calls) == 1


@pytest.mark.asyncio
async def test_capture_gateway_failure_does_not_mask_the_lifecycle_error() -> None:
    """A typed conflict-capture failure never replaces the lifecycle
    rejection; the conflict domain's own sink already carries its reason."""

    command = build_delete_command()
    device_context = build_device_context()
    decision = build_decision(device_context=device_context, command=command)
    ledger = CallLedger()
    capture = FakeLifecycleConflictCaptureGateway(
        ledger=ledger,
        error=SourceConflictError(ErrorCode.SOURCE_CONFLICT_STATE_INVALID),
    )
    store = FakeLifecycleStore(
        ledger=ledger,
        commit_result=build_commit_result(command),
        commit_error=build_version_conflict_error(),
    )
    policy = FakeLifecyclePolicy(ledger=ledger, decision=decision)
    service = _build_service(store=store, policy=policy, conflict_capture=capture)

    with pytest.raises(Exception) as exc_info:
        await service.commit(
            command=command,
            device_context=device_context,
            diagnostic_context=build_diagnostic_context(),
        )

    from personal_os.source_lifecycle.errors import SourceLifecycleError

    assert isinstance(exc_info.value, SourceLifecycleError)
    assert exc_info.value.code is SourceLifecycleErrorCode.VERSION_CONFLICT


@pytest.mark.asyncio
async def test_locator_missing_rejection_never_consults_the_capture_gateway() -> None:
    """A lifecycle op against a remotely deleted source keeps its typed
    error: the lifecycle command carries no content candidate, so no
    edit-vs-delete conflict may be fabricated."""

    command = build_rename_command()
    device_context = build_device_context()
    decision = build_decision(device_context=device_context, command=command)
    ledger = CallLedger()
    capture = FakeLifecycleConflictCaptureGateway(ledger=ledger)
    store = FakeLifecycleStore(
        ledger=ledger,
        commit_result=build_commit_result(command),
        commit_error=build_locator_missing_error(),
    )
    policy = FakeLifecyclePolicy(ledger=ledger, decision=decision)
    service = _build_service(store=store, policy=policy, conflict_capture=capture)

    with pytest.raises(Exception) as exc_info:
        await service.commit(
            command=command,
            device_context=device_context,
            diagnostic_context=build_diagnostic_context(),
        )

    from personal_os.source_lifecycle.errors import SourceLifecycleError

    assert isinstance(exc_info.value, SourceLifecycleError)
    assert exc_info.value.code is SourceLifecycleErrorCode.LOCATOR_MISSING
    assert capture.delete_race_calls == []
    assert capture.locator_collision_calls == []


@pytest.mark.asyncio
async def test_exact_replay_never_consults_the_capture_gateway() -> None:
    """The replay path returns before any store failure can occur, so no
    capture attempt may ever follow an exact replay."""

    command = build_rename_command()
    committed = build_commit_result(command)
    device_context = build_device_context()
    ledger = CallLedger()
    capture = FakeLifecycleConflictCaptureGateway(ledger=ledger)
    store = FakeLifecycleStore(
        ledger=ledger,
        commit_result=committed,
        committed_result=committed,
    )
    policy = FakeLifecyclePolicy(ledger=ledger)
    service = _build_service(store=store, policy=policy, conflict_capture=capture)

    result = await service.commit(
        command=command,
        device_context=device_context,
        diagnostic_context=build_diagnostic_context(),
    )

    assert result is committed
    assert ledger.entries == [STORE_RESOLVE_COMMITTED]
    assert capture.delete_race_calls == []
    assert capture.locator_collision_calls == []


def test_service_exposes_the_injected_conflict_capture_port() -> None:
    """The service surfaces the injected conflict-capture gateway."""

    ledger = CallLedger()
    capture = FakeLifecycleConflictCaptureGateway(ledger=ledger)
    store = FakeLifecycleStore(
        ledger=ledger,
        commit_result=build_commit_result(build_rename_command()),
    )
    policy = FakeLifecyclePolicy(ledger=ledger)
    service = _build_service(store=store, policy=policy, conflict_capture=capture)

    assert service.conflict_capture is capture


def test_service_constructor_rejects_missing_metrics() -> None:
    """A metrics sink is mandatory so labels never fall through to a default."""

    ledger = CallLedger()
    store = FakeLifecycleStore(
        ledger=ledger,
        commit_result=build_commit_result(build_rename_command()),
    )
    policy = FakeLifecyclePolicy(ledger=ledger)

    with pytest.raises((TypeError, ValueError, AssertionError)):
        SourceLifecycleService(  # type: ignore[call-arg]
            store=store,
            policy=policy,
            clock=SequencedUtcClock(moments=[_moment(0), _moment(1)]),
        )
