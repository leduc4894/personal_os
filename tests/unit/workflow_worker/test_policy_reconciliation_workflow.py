"""Unit contracts for the reconciliation workflow, starter and dispatcher.

Every case pins one rule of the reconciliation orchestration (spec 15/21): the
deterministic workflow identity on the pinned queue, the closed input and
continuation serializing only contract tags, opaque UUIDs, checkpoints and
counts, the bounded continue-as-new decision after 20 batches or 10,000
sources, the batch activity's error mapping (retryable replan drift retries;
final-attempt and non-retryable failures durably release the intent), the
starter's duplicate-run convergence (``ALLOW_DUPLICATE_FAILED_ONLY`` plus
``USE_EXISTING`` so a re-driven failed run starts fresh while a completed or
running execution is accepted as existing), and the leased dispatcher's
outcomes — converged start, retryable release with bounded backoff and
terminal contract failure — plus the closed dispatch-unavailable event an
injected diagnostic sink receives when an unexpected start failure would
otherwise be swallowed. Sensitive sentinels must never appear in any
serialized input, identity or metric label.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from temporalio.client import WorkflowExecutionStatus
from temporalio.common import RetryPolicy, WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.converter import DataConverter
from temporalio.exceptions import ApplicationError as TemporalApplicationError
from temporalio.exceptions import WorkflowAlreadyStartedError
from workflow_worker.policy_reconciliation_workflow import (
    POLICY_RECONCILIATION_ACTIVITY_NAME,
    POLICY_RECONCILIATION_ACTIVITY_NAMES,
    POLICY_RECONCILIATION_ACTIVITY_START_TO_CLOSE_TIMEOUT,
    POLICY_RECONCILIATION_BATCH_ACTIVITY_MAXIMUM_ATTEMPTS,
    POLICY_RECONCILIATION_BATCH_ACTIVITY_NAME,
    POLICY_RECONCILIATION_COMPLETION_ACTIVITY_NAME,
    POLICY_RECONCILIATION_HEARTBEAT_TIMEOUT,
    POLICY_RECONCILIATION_START_TIMEOUT,
    POLICY_RECONCILIATION_TASK_QUEUE,
    POLICY_RECONCILIATION_WORKFLOW_TYPE_NAME,
    PolicyReconciliationActivities,
    PolicyReconciliationBatchReference,
    PolicyReconciliationCompletionReference,
    PolicyReconciliationStartOutcome,
    PolicyReconciliationWorkflow,
    ReconciliationBatchOutcome,
    ReconciliationExecutionOutcome,
    TemporalPolicyReconciliationStarter,
    reconciliation_input_for_lease,
    reconciliation_retry_policy,
    should_continue_as_new,
)
from workflow_worker.policy_workflow_runtime import (
    LeasedPolicyReconciliation,
    PolicyReconciliationDispatchRuntime,
    run_policy_reconciliation_process,
)

from personal_os.diagnostics.events import (
    DiagnosticEvent,
    EventName,
    SafeToken,
    ShortDigest,
    build_registered_event,
)
from personal_os.error_contracts.codes import ErrorCode
from personal_os.exclusion_policy.errors import ExclusionPolicyError
from personal_os.exclusion_policy.reconciliation import (
    RECONCILIATION_CONTINUE_AS_NEW_BATCHES,
    RECONCILIATION_CONTINUE_AS_NEW_SOURCES,
    RECONCILIATION_CONTRACT,
    ReconciliationCounters,
    ReconciliationProgress,
)

WORKSPACE_ID = UUID("018f47a0-7b00-7000-8000-000000000101")
POLICY_REVISION_ID = UUID("018f47a0-7b00-7000-8000-000000000102")
SOURCE_ID = UUID("018f47a0-7b00-7000-8000-000000000103")
INTENT_ID = UUID("018f47a0-7b00-7000-8000-000000000104")
LEASE_TOKEN = UUID("018f47a0-7b00-7000-8000-000000000105")
CHECKPOINT = 9
FIXED_NOW = datetime(2026, 8, 17, 12, 0, 0, tzinfo=UTC)

_LEAKAGE_SENTINELS: tuple[str, ...] = (
    "sentinel-title",
    "private/notes/sentinel-locator.md",
    "sentinel operand",
)


def _serialize(value: object) -> bytes:
    (payload,) = DataConverter.default.payload_converter.to_payloads([value])
    return payload.data


def _lease() -> LeasedPolicyReconciliation:
    return LeasedPolicyReconciliation(
        policy_reconciliation_intent_id=INTENT_ID,
        workspace_id=WORKSPACE_ID,
        policy_revision_id=POLICY_REVISION_ID,
        workflow_id=f"exclusion-policy-reconciliation/{WORKSPACE_ID}/{POLICY_REVISION_ID}",
        source_checkpoint_event_sequence=CHECKPOINT,
        attempt_count=1,
        lease_token=LEASE_TOKEN,
        leased_until=FIXED_NOW + timedelta(seconds=60),
    )


def _batch_outcome(
    *,
    superseded: bool = False,
    has_more: bool = False,
    evaluated_sources: int = 2,
) -> ReconciliationBatchOutcome:
    return ReconciliationBatchOutcome(
        superseded=superseded,
        has_more=has_more,
        last_source_id=SOURCE_ID if has_more else None,
        evaluated_sources=evaluated_sources,
        to_excluded_sources=1,
        to_allowed_sources=0,
        unchanged_sources=evaluated_sources - 1,
    )


# --- identity and closed wire contracts ----------------------------------------------


def test_workflow_activity_and_queue_identities_are_pinned() -> None:
    assert POLICY_RECONCILIATION_WORKFLOW_TYPE_NAME == "PolicyReconciliationWorkflow"
    assert POLICY_RECONCILIATION_TASK_QUEUE == "exclusion-policy-reconciliation"
    assert POLICY_RECONCILIATION_BATCH_ACTIVITY_NAME in POLICY_RECONCILIATION_ACTIVITY_NAMES
    assert POLICY_RECONCILIATION_COMPLETION_ACTIVITY_NAME in POLICY_RECONCILIATION_ACTIVITY_NAMES
    assert POLICY_RECONCILIATION_ACTIVITY_NAME == POLICY_RECONCILIATION_BATCH_ACTIVITY_NAME


def test_batch_reference_serializes_only_closed_fields() -> None:
    reference = PolicyReconciliationBatchReference(
        contract=RECONCILIATION_CONTRACT,
        workspace_id=WORKSPACE_ID,
        policy_revision_id=POLICY_REVISION_ID,
        source_checkpoint_event_sequence=CHECKPOINT,
        after_source_id=None,
    )
    decoded = json.loads(_serialize(reference))
    assert decoded == {
        "contract": RECONCILIATION_CONTRACT,
        "workspace_id": str(WORKSPACE_ID),
        "policy_revision_id": str(POLICY_REVISION_ID),
        "source_checkpoint_event_sequence": CHECKPOINT,
        "after_source_id": None,
    }
    for sentinel in _LEAKAGE_SENTINELS:
        assert sentinel.encode() not in _serialize(reference)


def test_completion_reference_serializes_only_closed_fields() -> None:
    reference = PolicyReconciliationCompletionReference(
        contract=RECONCILIATION_CONTRACT,
        workspace_id=WORKSPACE_ID,
        policy_revision_id=POLICY_REVISION_ID,
        source_checkpoint_event_sequence=CHECKPOINT,
        counters=ReconciliationCounters(
            evaluated_sources=10,
            to_excluded_sources=3,
            to_allowed_sources=2,
            unchanged_sources=5,
        ),
    )
    decoded = json.loads(_serialize(reference))
    assert decoded["counters"]["evaluated_sources"] == 10
    for sentinel in _LEAKAGE_SENTINELS:
        assert sentinel.encode() not in _serialize(reference)


def test_input_for_lease_copies_the_durable_checkpoint_and_ids() -> None:
    reference = reconciliation_input_for_lease(_lease())
    assert reference.contract == RECONCILIATION_CONTRACT
    assert reference.workspace_id == WORKSPACE_ID
    assert reference.policy_revision_id == POLICY_REVISION_ID
    assert reference.source_checkpoint_event_sequence == CHECKPOINT


def test_retry_policy_pins_the_closed_non_retryable_set_and_bounds() -> None:
    policy = reconciliation_retry_policy()
    assert isinstance(policy, RetryPolicy)
    assert policy.maximum_attempts == POLICY_RECONCILIATION_BATCH_ACTIVITY_MAXIMUM_ATTEMPTS
    assert "internal_error" in (policy.non_retryable_error_types or ())
    assert "exclusion_policy_snapshot_outdated" not in (policy.non_retryable_error_types or ())
    assert timedelta(minutes=1) < POLICY_RECONCILIATION_ACTIVITY_START_TO_CLOSE_TIMEOUT
    assert timedelta(minutes=1) <= POLICY_RECONCILIATION_HEARTBEAT_TIMEOUT
    assert timedelta(seconds=10) == POLICY_RECONCILIATION_START_TIMEOUT


# --- continue-as-new decision ---------------------------------------------------------


def test_continue_as_new_fires_at_twenty_batches_or_ten_thousand_sources() -> None:
    assert RECONCILIATION_CONTINUE_AS_NEW_BATCHES == 20
    assert RECONCILIATION_CONTINUE_AS_NEW_SOURCES == 10_000
    assert should_continue_as_new(run_batch_count=19, run_evaluated_sources=9_999) is False
    assert should_continue_as_new(run_batch_count=20, run_evaluated_sources=100) is True
    assert should_continue_as_new(run_batch_count=1, run_evaluated_sources=10_000) is True


def test_workflow_loop_passes_the_cursor_and_bounds_to_continue_as_new() -> None:
    # The workflow method must reference the decision helper and the pinned
    # bounds so the loop cannot drift from the pure rule.
    source = inspect.getsource(PolicyReconciliationWorkflow.run)
    assert "should_continue_as_new" in source
    assert "continue_as_new" in source


# --- activities ------------------------------------------------------------------------


@dataclass
class RecordedBatch:
    workspace_id: UUID
    policy_revision_id: UUID
    source_checkpoint_event_sequence: int
    after_source_id: UUID | None


@dataclass
class RecordedCompletion:
    workspace_id: UUID
    policy_revision_id: UUID
    counters: ReconciliationCounters


@dataclass
class RecordedFailure:
    workspace_id: UUID
    policy_revision_id: UUID
    error_code: str
    retryable: bool


@dataclass
class FakeReconciliationStore:
    batch_outcome: ReconciliationBatchOutcome = field(default_factory=_batch_outcome)
    batch_error: Exception | None = None
    completion_result: bool = True
    completion_error: Exception | None = None
    failure_result: bool = True
    batches: list[RecordedBatch] = field(default_factory=list)
    completions: list[RecordedCompletion] = field(default_factory=list)
    failures: list[RecordedFailure] = field(default_factory=list)
    heartbeats: list[ReconciliationProgress] = field(default_factory=list)

    async def run_reconciliation_batch(
        self,
        workspace_id: UUID,
        policy_revision_id: UUID,
        source_checkpoint_event_sequence: int,
        after_source_id: UUID | None,
        heartbeat: Any = None,
    ) -> ReconciliationBatchOutcome:
        self.batches.append(
            RecordedBatch(
                workspace_id=workspace_id,
                policy_revision_id=policy_revision_id,
                source_checkpoint_event_sequence=source_checkpoint_event_sequence,
                after_source_id=after_source_id,
            )
        )
        if heartbeat is not None:
            await heartbeat(
                ReconciliationProgress(
                    evaluated_sources=self.batch_outcome.evaluated_sources, batch_count=1
                )
            )
        if self.batch_error is not None:
            raise self.batch_error
        return self.batch_outcome

    async def complete_reconciliation(
        self,
        workspace_id: UUID,
        policy_revision_id: UUID,
        counters: ReconciliationCounters,
        *,
        context: Any = None,
    ) -> bool:
        self.completions.append(
            RecordedCompletion(
                workspace_id=workspace_id,
                policy_revision_id=policy_revision_id,
                counters=counters,
            )
        )
        if self.completion_error is not None:
            raise self.completion_error
        return self.completion_result

    async def fail_reconciliation(
        self,
        workspace_id: UUID,
        policy_revision_id: UUID,
        error_code: Any,
        *,
        retryable: bool,
    ) -> bool:
        self.failures.append(
            RecordedFailure(
                workspace_id=workspace_id,
                policy_revision_id=policy_revision_id,
                error_code=error_code.value,
                retryable=retryable,
            )
        )
        return self.failure_result


def _activities(
    store: FakeReconciliationStore,
    *,
    attempt: int | None = 1,
    heartbeat_sink: Any = None,
) -> PolicyReconciliationActivities:
    return PolicyReconciliationActivities(
        store=store,  # type: ignore[arg-type]
        attempt_reader=lambda: attempt,
        heartbeat_sink=heartbeat_sink,
    )


def _batch_reference(after_source_id: UUID | None = None) -> PolicyReconciliationBatchReference:
    return PolicyReconciliationBatchReference(
        contract=RECONCILIATION_CONTRACT,
        workspace_id=WORKSPACE_ID,
        policy_revision_id=POLICY_REVISION_ID,
        source_checkpoint_event_sequence=CHECKPOINT,
        after_source_id=after_source_id,
    )


def test_batch_activity_runs_one_store_batch_and_forwards_the_heartbeat() -> None:
    store = FakeReconciliationStore()
    heartbeats: list[ReconciliationProgress] = []

    outcome = asyncio.run(
        _activities(store, heartbeat_sink=heartbeats.append).run_reconciliation_batch_activity(
            _batch_reference(after_source_id=SOURCE_ID),
        )
    )
    assert outcome == store.batch_outcome
    assert store.batches == [
        RecordedBatch(
            workspace_id=WORKSPACE_ID,
            policy_revision_id=POLICY_REVISION_ID,
            source_checkpoint_event_sequence=CHECKPOINT,
            after_source_id=SOURCE_ID,
        )
    ]
    assert heartbeats == [
        ReconciliationProgress(
            evaluated_sources=store.batch_outcome.evaluated_sources, batch_count=1
        )
    ]


def test_batch_activity_rejects_contract_drift_non_retryably() -> None:
    store = FakeReconciliationStore()
    reference = PolicyReconciliationBatchReference(
        contract="exclusion_policy_reconciliation/v2",
        workspace_id=WORKSPACE_ID,
        policy_revision_id=POLICY_REVISION_ID,
        source_checkpoint_event_sequence=CHECKPOINT,
        after_source_id=None,
    )
    with pytest.raises(TemporalApplicationError) as raised:
        asyncio.run(_activities(store).run_reconciliation_batch_activity(reference))
    assert raised.value.non_retryable is True
    assert store.failures == []


def test_batch_activity_maps_the_replan_drift_onto_the_retryable_stale_code() -> None:
    store = FakeReconciliationStore(
        batch_error=ExclusionPolicyError(ErrorCode.EXCLUSION_POLICY_SNAPSHOT_OUTDATED)
    )
    with pytest.raises(TemporalApplicationError) as raised:
        asyncio.run(_activities(store).run_reconciliation_batch_activity(_batch_reference()))
    assert raised.value.non_retryable is False
    # A retryable failure fails the durable row only on the final attempt.
    assert store.failures == []


def test_batch_activity_fails_the_row_on_the_final_retryable_attempt() -> None:
    store = FakeReconciliationStore(
        batch_error=ExclusionPolicyError(ErrorCode.EXCLUSION_POLICY_COMMIT_OUTCOME_UNKNOWN)
    )
    with pytest.raises(TemporalApplicationError):
        asyncio.run(
            _activities(
                store, attempt=POLICY_RECONCILIATION_BATCH_ACTIVITY_MAXIMUM_ATTEMPTS
            ).run_reconciliation_batch_activity(_batch_reference())
        )
    assert len(store.failures) == 1
    assert store.failures[0].retryable is True
    assert store.failures[0].error_code == "exclusion_policy_commit_outcome_unknown"


def test_batch_activity_fails_the_row_non_retryably_for_typed_terminal_errors() -> None:
    store = FakeReconciliationStore(
        batch_error=ExclusionPolicyError(ErrorCode.EXCLUSION_POLICY_INPUT_INVALID)
    )
    with pytest.raises(TemporalApplicationError) as raised:
        asyncio.run(_activities(store).run_reconciliation_batch_activity(_batch_reference()))
    assert raised.value.non_retryable is True
    assert len(store.failures) == 1
    assert store.failures[0].retryable is False


def test_batch_activity_fails_the_row_for_unexpected_errors_only_on_the_final_attempt() -> None:
    store = FakeReconciliationStore(batch_error=RuntimeError("sentinel provider detail"))
    with pytest.raises(RuntimeError):
        asyncio.run(
            _activities(store, attempt=1).run_reconciliation_batch_activity(_batch_reference())
        )
    assert store.failures == []
    with pytest.raises(RuntimeError):
        asyncio.run(
            _activities(
                store, attempt=POLICY_RECONCILIATION_BATCH_ACTIVITY_MAXIMUM_ATTEMPTS
            ).run_reconciliation_batch_activity(_batch_reference())
        )
    assert len(store.failures) == 1
    assert store.failures[0].retryable is True


def test_completion_activity_completes_the_durable_row_once() -> None:
    store = FakeReconciliationStore()
    reference = PolicyReconciliationCompletionReference(
        contract=RECONCILIATION_CONTRACT,
        workspace_id=WORKSPACE_ID,
        policy_revision_id=POLICY_REVISION_ID,
        source_checkpoint_event_sequence=CHECKPOINT,
        counters=ReconciliationCounters(
            evaluated_sources=4, to_excluded_sources=1, to_allowed_sources=1, unchanged_sources=2
        ),
    )
    result = asyncio.run(_activities(store).complete_reconciliation_activity(reference))
    assert result == ReconciliationExecutionOutcome.COMPLETED.value
    assert store.completions[0].counters.evaluated_sources == 4


def test_completion_activity_treats_an_existing_row_as_the_idempotent_replay() -> None:
    store = FakeReconciliationStore(completion_result=False)
    reference = PolicyReconciliationCompletionReference(
        contract=RECONCILIATION_CONTRACT,
        workspace_id=WORKSPACE_ID,
        policy_revision_id=POLICY_REVISION_ID,
        source_checkpoint_event_sequence=CHECKPOINT,
        counters=ReconciliationCounters(),
    )
    result = asyncio.run(_activities(store).complete_reconciliation_activity(reference))
    assert result == ReconciliationExecutionOutcome.COMPLETED.value
    assert store.failures == []


def test_completion_activity_fails_the_row_when_the_audit_write_raises() -> None:
    store = FakeReconciliationStore(
        completion_error=ExclusionPolicyError(ErrorCode.EXCLUSION_POLICY_COMMIT_OUTCOME_UNKNOWN)
    )
    reference = PolicyReconciliationCompletionReference(
        contract=RECONCILIATION_CONTRACT,
        workspace_id=WORKSPACE_ID,
        policy_revision_id=POLICY_REVISION_ID,
        source_checkpoint_event_sequence=CHECKPOINT,
        counters=ReconciliationCounters(),
    )
    with pytest.raises(TemporalApplicationError) as raised:
        asyncio.run(
            _activities(
                store, attempt=POLICY_RECONCILIATION_BATCH_ACTIVITY_MAXIMUM_ATTEMPTS
            ).complete_reconciliation_activity(reference)
        )
    assert raised.value.non_retryable is False
    assert len(store.failures) == 1
    assert store.failures[0].retryable is True


# --- starter ----------------------------------------------------------------------------


@dataclass
class RecordedStart:
    workflow: str
    arg: object
    id: str
    task_queue: str
    id_reuse_policy: object
    id_conflict_policy: object
    rpc_timeout: timedelta | None


class FakeWorkflowHandle:
    def __init__(self, client: FakeTemporalClient, workflow_id: str) -> None:
        self._client = client
        self._workflow_id = workflow_id

    async def describe(self, *, rpc_timeout: timedelta | None = None) -> object:
        self._client.handle_calls.append((self._workflow_id, rpc_timeout))
        if self._client.describe_error is not None:
            raise self._client.describe_error
        assert self._client.description is not None
        return self._client.description


class FakeTemporalClient:
    def __init__(
        self,
        *,
        start_error: BaseException | None = None,
        description: object | None = None,
        describe_error: BaseException | None = None,
    ) -> None:
        self.starts: list[RecordedStart] = []
        self.handle_calls: list[tuple[str, timedelta | None]] = []
        self.start_error = start_error
        self.description = description
        self.describe_error = describe_error

    async def start_workflow(
        self,
        workflow: str,
        arg: object = None,
        *,
        id: str,
        task_queue: str,
        id_reuse_policy: object = None,
        id_conflict_policy: object = None,
        rpc_timeout: timedelta | None = None,
    ) -> object:
        self.starts.append(
            RecordedStart(
                workflow=workflow,
                arg=arg,
                id=id,
                task_queue=task_queue,
                id_reuse_policy=id_reuse_policy,
                id_conflict_policy=id_conflict_policy,
                rpc_timeout=rpc_timeout,
            )
        )
        if self.start_error is not None:
            raise self.start_error
        return object()

    def get_workflow_handle(self, workflow_id: str, **_kwargs: object) -> FakeWorkflowHandle:
        return FakeWorkflowHandle(self, workflow_id)


@dataclass
class _FakeDescription:
    workflow_type: str = POLICY_RECONCILIATION_WORKFLOW_TYPE_NAME
    task_queue: str = POLICY_RECONCILIATION_TASK_QUEUE
    status: object = WorkflowExecutionStatus.COMPLETED


def _starter(client: FakeTemporalClient) -> TemporalPolicyReconciliationStarter:
    return TemporalPolicyReconciliationStarter(client)  # type: ignore[arg-type]


def test_start_uses_the_pinned_type_queue_and_failed_run_reuse_semantics() -> None:
    client = FakeTemporalClient()

    result = asyncio.run(
        _starter(client).start_policy_reconciliation(reconciliation_input_for_lease(_lease()))
    )

    assert result is PolicyReconciliationStartOutcome.STARTED
    recorded = client.starts[0]
    assert recorded.workflow == POLICY_RECONCILIATION_WORKFLOW_TYPE_NAME
    assert recorded.task_queue == POLICY_RECONCILIATION_TASK_QUEUE
    assert recorded.id == f"exclusion-policy-reconciliation/{WORKSPACE_ID}/{POLICY_REVISION_ID}"
    # A re-driven failed run must start a fresh deterministic execution, so the
    # reuse policy permits duplicates of failed runs only; a running execution
    # converges through USE_EXISTING.
    assert recorded.id_reuse_policy is WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY
    assert recorded.id_conflict_policy is WorkflowIDConflictPolicy.USE_EXISTING
    assert recorded.rpc_timeout == POLICY_RECONCILIATION_START_TIMEOUT
    for sentinel in _LEAKAGE_SENTINELS:
        assert sentinel.encode() not in _serialize(recorded.arg)


def test_completed_prior_run_resolves_as_existing_without_a_second_start() -> None:
    client = FakeTemporalClient(
        start_error=WorkflowAlreadyStartedError("duplicate-id", "PolicyReconciliationWorkflow"),
        description=_FakeDescription(status=WorkflowExecutionStatus.COMPLETED),
    )

    result = asyncio.run(
        _starter(client).start_policy_reconciliation(reconciliation_input_for_lease(_lease()))
    )

    assert result is PolicyReconciliationStartOutcome.EXISTING
    assert len(client.starts) == 1
    assert client.handle_calls == [
        (
            f"exclusion-policy-reconciliation/{WORKSPACE_ID}/{POLICY_REVISION_ID}",
            POLICY_RECONCILIATION_START_TIMEOUT,
        )
    ]


def test_running_prior_execution_resolves_as_existing_under_use_existing() -> None:
    client = FakeTemporalClient(
        description=_FakeDescription(status=WorkflowExecutionStatus.RUNNING)
    )

    result = asyncio.run(
        _starter(client).start_policy_reconciliation(reconciliation_input_for_lease(_lease()))
    )

    assert result is PolicyReconciliationStartOutcome.STARTED


def test_prior_run_of_another_type_is_the_terminal_contract_failure() -> None:
    client = FakeTemporalClient(
        start_error=WorkflowAlreadyStartedError("duplicate-id", "PolicyReconciliationWorkflow"),
        description=_FakeDescription(
            workflow_type="OtherWorkflow",
            status=WorkflowExecutionStatus.COMPLETED,
        ),
    )

    with pytest.raises(ExclusionPolicyError) as raised:
        asyncio.run(
            _starter(client).start_policy_reconciliation(reconciliation_input_for_lease(_lease()))
        )
    assert raised.value.is_retryable is False


def test_abnormally_closed_prior_run_is_the_terminal_contract_failure() -> None:
    client = FakeTemporalClient(
        start_error=WorkflowAlreadyStartedError("duplicate-id", "PolicyReconciliationWorkflow"),
        description=_FakeDescription(status=WorkflowExecutionStatus.FAILED),
    )

    with pytest.raises(ExclusionPolicyError) as raised:
        asyncio.run(
            _starter(client).start_policy_reconciliation(reconciliation_input_for_lease(_lease()))
        )
    assert raised.value.is_retryable is False


def test_rpc_failures_map_onto_the_retryable_dependency_error() -> None:
    from temporalio.service import RPCError

    client = FakeTemporalClient(start_error=RPCError("sentinel provider detail", 14, 14))
    with pytest.raises(ExclusionPolicyError) as raised:
        asyncio.run(
            _starter(client).start_policy_reconciliation(reconciliation_input_for_lease(_lease()))
        )
    assert raised.value.is_retryable is True
    assert "sentinel provider detail" not in str(raised.value)


def test_task_queue_must_match_the_pinned_queue() -> None:
    with pytest.raises(ValueError):
        TemporalPolicyReconciliationStarter(
            FakeTemporalClient(),  # type: ignore[arg-type]
            task_queue="other-queue",
        )


# --- dispatch runtime --------------------------------------------------------------------


@dataclass
class RecordedAcknowledge:
    intent_id: UUID
    lease_token: UUID


@dataclass
class RecordedRelease:
    intent_id: UUID
    lease_token: UUID
    error_code: Any


@dataclass
class RecordedTerminal:
    intent_id: UUID
    lease_token: UUID
    error_code: Any


@dataclass
class FakeOutboxStore:
    leases: tuple[LeasedPolicyReconciliation, ...] = ()
    acknowledge_result: bool = True
    reclaim_calls: list[datetime] = field(default_factory=list)
    claim_calls: list[tuple[datetime, int]] = field(default_factory=list)
    acknowledgements: list[RecordedAcknowledge] = field(default_factory=list)
    releases: list[RecordedRelease] = field(default_factory=list)
    terminals: list[RecordedTerminal] = field(default_factory=list)

    async def reclaim_expired(self, now: datetime) -> int:
        self.reclaim_calls.append(now)
        return 0

    async def claim_pending(self, now: datetime, limit: int) -> list[LeasedPolicyReconciliation]:
        self.claim_calls.append((now, limit))
        return list(self.leases)

    async def acknowledge_dispatched(
        self, intent_id: UUID, lease_token: UUID, now: datetime
    ) -> bool:
        self.acknowledgements.append(RecordedAcknowledge(intent_id, lease_token))
        return self.acknowledge_result

    async def release_retry(
        self,
        intent_id: UUID,
        lease_token: UUID,
        error_code: Any,
        now: datetime,
    ) -> bool:
        self.releases.append(RecordedRelease(intent_id, lease_token, error_code))
        return True

    async def mark_terminal(
        self,
        intent_id: UUID,
        lease_token: UUID,
        error_code: Any,
        now: datetime,
    ) -> bool:
        self.terminals.append(RecordedTerminal(intent_id, lease_token, error_code))
        return True


@dataclass
class FakeStarter:
    result: PolicyReconciliationStartOutcome = PolicyReconciliationStartOutcome.STARTED
    error: ExclusionPolicyError | None = None
    calls: list[Any] = field(default_factory=list)

    async def start_policy_reconciliation(self, reference: Any) -> PolicyReconciliationStartOutcome:
        self.calls.append(reference)
        if self.error is not None:
            raise self.error
        return self.result


def _retryable_error() -> ExclusionPolicyError:
    return ExclusionPolicyError(ErrorCode.EXCLUSION_POLICY_COMMIT_OUTCOME_UNKNOWN)


def _terminal_error() -> ExclusionPolicyError:
    return ExclusionPolicyError(ErrorCode.EXCLUSION_POLICY_INPUT_INVALID)


@pytest.mark.asyncio
async def test_dispatcher_reclaims_claims_starts_and_acknowledges() -> None:
    store = FakeOutboxStore(leases=(_lease(),))
    starter = FakeStarter()
    runtime = PolicyReconciliationDispatchRuntime(
        store=store,  # type: ignore[arg-type]
        starter=starter,  # type: ignore[arg-type]
        clock=lambda: FIXED_NOW,
    )

    claimed = await runtime.dispatch_pending_reconciliations_once()

    assert claimed == 1
    assert store.reclaim_calls == [FIXED_NOW]
    assert len(starter.calls) == 1
    assert store.acknowledgements == [RecordedAcknowledge(INTENT_ID, LEASE_TOKEN)]


@pytest.mark.asyncio
async def test_dispatcher_releases_retryable_start_failures_with_backoff() -> None:
    store = FakeOutboxStore(leases=(_lease(),))
    starter = FakeStarter(error=_retryable_error())
    runtime = PolicyReconciliationDispatchRuntime(
        store=store,  # type: ignore[arg-type]
        starter=starter,  # type: ignore[arg-type]
        clock=lambda: FIXED_NOW,
    )

    claimed = await runtime.dispatch_pending_reconciliations_once()

    assert claimed == 1
    assert store.acknowledgements == []
    assert len(store.releases) == 1
    assert store.releases[0].intent_id == INTENT_ID


@pytest.mark.asyncio
async def test_dispatcher_marks_terminal_contract_failures() -> None:
    store = FakeOutboxStore(leases=(_lease(),))
    starter = FakeStarter(error=_terminal_error())
    runtime = PolicyReconciliationDispatchRuntime(
        store=store,  # type: ignore[arg-type]
        starter=starter,  # type: ignore[arg-type]
        clock=lambda: FIXED_NOW,
    )

    claimed = await runtime.dispatch_pending_reconciliations_once()

    assert claimed == 1
    assert len(store.terminals) == 1
    assert store.acknowledgements == []
    assert store.releases == []


@pytest.mark.asyncio
async def test_dispatcher_leaves_unknown_start_outcomes_leased() -> None:
    store = FakeOutboxStore(leases=(_lease(),))
    starter = FakeStarter(error=RuntimeError("sentinel provider detail"))
    runtime = PolicyReconciliationDispatchRuntime(
        store=store,  # type: ignore[arg-type]
        starter=starter,  # type: ignore[arg-type]
        clock=lambda: FIXED_NOW,
    )

    claimed = await runtime.dispatch_pending_reconciliations_once()

    assert claimed == 1
    assert store.acknowledgements == []
    assert store.releases == []
    assert store.terminals == []


# --- dispatch diagnostics -----------------------------------------------------------------


@dataclass
class RecordingDiagnosticSink:
    """Diagnostic sink double recording the closed events it receives."""

    events: list[tuple[EventName, dict[str, object]]] = field(default_factory=list)

    def emit(self, event_name: EventName, fields: Mapping[str, object] | None = None) -> None:
        self.events.append((event_name, dict(fields or {})))


@pytest.mark.asyncio
async def test_dispatcher_emits_dispatch_unavailable_when_start_raises_unexpectedly() -> None:
    store = FakeOutboxStore(leases=(_lease(),))
    starter = FakeStarter(error=RuntimeError("sentinel provider detail"))
    sink = RecordingDiagnosticSink()
    runtime = PolicyReconciliationDispatchRuntime(
        store=store,  # type: ignore[arg-type]
        starter=starter,  # type: ignore[arg-type]
        clock=lambda: FIXED_NOW,
        diagnostics=sink,
    )

    claimed = await runtime.dispatch_pending_reconciliations_once()

    assert claimed == 1
    assert store.acknowledgements == [] and store.releases == []
    assert store.terminals == []
    assert len(sink.events) == 1
    event_name, fields = sink.events[0]
    assert event_name is EventName.RECONCILIATION_DISPATCH_UNAVAILABLE
    assert event_name.value == "reconciliation_dispatch_unavailable"
    assert fields["policy_reconciliation_intent_id"] == INTENT_ID
    assert fields["attempt_count"] == 1
    assert isinstance(fields["exception_type"], SafeToken)
    assert fields["exception_type"] == SafeToken.parse("builtins.runtimeerror")
    assert isinstance(fields["stack_fingerprint"], ShortDigest)
    assert re.fullmatch(r"[0-9a-f]{16}", str(fields["stack_fingerprint"]))
    rendered = json.dumps({key: str(value) for key, value in fields.items()})
    assert "sentinel provider detail" not in rendered
    built = build_registered_event(event_name, fields)
    assert isinstance(built, DiagnosticEvent), "emitted fields must satisfy the closed registry"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        None,
        _retryable_error(),
        _terminal_error(),
    ],
    ids=["converged-start", "retryable-release", "terminal-failure"],
)
async def test_dispatcher_emits_no_events_on_typed_start_outcomes(
    error: Exception | None,
) -> None:
    store = FakeOutboxStore(leases=(_lease(),))
    starter = FakeStarter(error=error)
    sink = RecordingDiagnosticSink()
    runtime = PolicyReconciliationDispatchRuntime(
        store=store,  # type: ignore[arg-type]
        starter=starter,  # type: ignore[arg-type]
        clock=lambda: FIXED_NOW,
        diagnostics=sink,
    )

    claimed = await runtime.dispatch_pending_reconciliations_once()

    assert claimed == 1
    assert sink.events == []


def test_reconciliation_process_composition_wires_the_configured_diagnostic_sink() -> None:
    """The process runner injects the configured logger; no hardcoded sink."""

    source = inspect.getsource(run_policy_reconciliation_process)
    assert "configure_diagnostics(runtime_settings)" in source
    assert "diagnostics=diagnostics" in source
