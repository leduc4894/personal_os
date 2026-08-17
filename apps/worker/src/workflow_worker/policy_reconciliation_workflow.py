"""Temporal batched workflow and adapters for publication reconciliation.

This is the worker-owned composition boundary over the Temporal SDK (spec 15):
:class:`PolicyReconciliationWorkflow` walks the workspace's sources in bounded
single-activity batches — each activity executes exactly one committed
database batch (at most 500 sources) with its own retry policy whose
non-retryable set is the closed typed terminal codes, heartbeats once per
committed batch and carries the stable keyset cursor forward — and continues
as new after 20 batches or 10,000 evaluated sources so one history never
grows unboundedly. A superseded revision stops the loop cleanly; a completed
scan runs the completion activity, which writes the idempotent
``exclusion_policy.reconciliation_completed`` audit row and records the
reconciliation lag after the durable transition.

The closed ``exclusion_policy_reconciliation/v1`` input carries only the
contract tag, the two opaque UUIDs and the publication source checkpoint, so
no rule operand, locator, title or content byte can ever enter Temporal
history. The starter derives the deterministic workflow ID
``exclusion-policy-reconciliation/{workspace_id}/{policy_revision_id}`` and
starts with ``ALLOW_DUPLICATE_FAILED_ONLY`` plus ``USE_EXISTING``: a
re-driven failed run (dependency failure released the durable intent back to
pending) starts a fresh deterministic execution, while a running or completed
execution converges — a lost start acknowledgement can never produce a
second concurrent run, and an idempotent re-run of a completed reconciliation
verifies insert-once evidence instead of duplicating effects.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import Final, Protocol
from uuid import UUID

from temporalio import activity, workflow
from temporalio.client import Client, RPCTimeoutOrCancelledError
from temporalio.common import (
    RetryPolicy,
    WorkflowIDConflictPolicy,
    WorkflowIDReusePolicy,
)
from temporalio.exceptions import ApplicationError as TemporalApplicationError
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError

from personal_os.diagnostics.context import DiagnosticContext
from personal_os.diagnostics.events import SafeToken
from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import ApplicationError
from personal_os.exclusion_policy.errors import ExclusionPolicyError
from personal_os.exclusion_policy.reconciliation import (
    RECONCILIATION_CONTINUE_AS_NEW_BATCHES,
    RECONCILIATION_CONTINUE_AS_NEW_SOURCES,
    RECONCILIATION_CONTRACT,
    ReconciliationContinuation,
    ReconciliationCounters,
    ReconciliationExecutionOutcome,
    ReconciliationInput,
    ReconciliationProgress,
    reconciliation_workflow_id,
)

with workflow.unsafe.imports_passed_through():
    from postgresql_source_store.policy_reconciliation import (
        RECONCILIATION_EXECUTION_FAILED_ERROR_CODE,
        LeasedPolicyReconciliation,
        ReconciliationBatchOutcome,
    )

#: Workflow/activity identity pins (spec 15 and the pinned queue).
POLICY_RECONCILIATION_WORKFLOW_TYPE_NAME: Final[str] = "PolicyReconciliationWorkflow"
POLICY_RECONCILIATION_TASK_QUEUE: Final[str] = "exclusion-policy-reconciliation"
POLICY_RECONCILIATION_BATCH_ACTIVITY_NAME: Final[str] = "run_reconciliation_batch_activity"
POLICY_RECONCILIATION_COMPLETION_ACTIVITY_NAME: Final[str] = "complete_reconciliation_activity"
POLICY_RECONCILIATION_ACTIVITY_NAME: Final[str] = POLICY_RECONCILIATION_BATCH_ACTIVITY_NAME
POLICY_RECONCILIATION_ACTIVITY_NAMES: Final[tuple[str, ...]] = (
    POLICY_RECONCILIATION_BATCH_ACTIVITY_NAME,
    POLICY_RECONCILIATION_COMPLETION_ACTIVITY_NAME,
)

#: The workflow input contract tag is the domain reconciliation contract.
POLICY_RECONCILIATION_REFERENCE_CONTRACT: Final[str] = RECONCILIATION_CONTRACT

#: The bounded activity retry: infrastructure failures retry from the same
#: captured cursor (a failed batch wrote nothing), while the typed terminal
#: codes are non-retryable and durably fail the reconciliation intent.
POLICY_RECONCILIATION_BATCH_ACTIVITY_MAXIMUM_ATTEMPTS: Final[int] = 5

#: One batch is at most 500 sources; the start-to-close bound leaves a wide
#: margin and the heartbeat timeout must exceed any single batch duration.
POLICY_RECONCILIATION_ACTIVITY_START_TO_CLOSE_TIMEOUT: Final[timedelta] = timedelta(minutes=5)
POLICY_RECONCILIATION_HEARTBEAT_TIMEOUT: Final[timedelta] = timedelta(minutes=2)

#: The caller-side bound for every Temporal RPC the starter issues.
POLICY_RECONCILIATION_START_TIMEOUT: Final[timedelta] = timedelta(seconds=10)

#: The closed non-retryable error types (typed registry code values). The
#: retryable replan drift (``exclusion_policy_snapshot_outdated``) and the
#: dependency-failure family stay retryable so Temporal re-reads and replans.
POLICY_RECONCILIATION_NON_RETRYABLE_ERROR_TYPES: Final[tuple[str, ...]] = (
    "exclusion_policy_input_invalid",
    "exclusion_policy_not_initialized",
    "projection_intent_contract_invalid",
    "internal_error",
)


@dataclass(frozen=True, slots=True)
class PolicyReconciliationBatchReference:
    """The closed batch-activity input: the workflow input plus the cursor."""

    contract: str
    workspace_id: UUID
    policy_revision_id: UUID
    source_checkpoint_event_sequence: int
    after_source_id: UUID | None


@dataclass(frozen=True, slots=True)
class PolicyReconciliationCompletionReference:
    """The closed completion-activity input with the cumulative counters."""

    contract: str
    workspace_id: UUID
    policy_revision_id: UUID
    source_checkpoint_event_sequence: int
    counters: ReconciliationCounters


class PolicyReconciliationOutcome(StrEnum):
    """The closed workflow outcomes, mirroring the domain execution set."""

    COMPLETED = ReconciliationExecutionOutcome.COMPLETED.value
    SUPERSEDED = ReconciliationExecutionOutcome.SUPERSEDED.value


class PolicyReconciliationStartOutcome(StrEnum):
    """The closed dispatch outcomes of one workflow start attempt."""

    STARTED = "started"
    EXISTING = "existing"


def reconciliation_retry_policy() -> RetryPolicy:
    """The bounded retry policy of the batch and completion activities."""

    return RetryPolicy(
        initial_interval=timedelta(seconds=1),
        maximum_interval=timedelta(seconds=30),
        maximum_attempts=POLICY_RECONCILIATION_BATCH_ACTIVITY_MAXIMUM_ATTEMPTS,
        non_retryable_error_types=list(POLICY_RECONCILIATION_NON_RETRYABLE_ERROR_TYPES),
    )


def should_continue_as_new(*, run_batch_count: int, run_evaluated_sources: int) -> bool:
    """The pure continue-as-new rule: 20 batches or 10,000 sources per run."""

    if run_batch_count < 0 or run_evaluated_sources < 0:
        raise ValueError("run counters must not be negative")
    return (
        run_batch_count >= RECONCILIATION_CONTINUE_AS_NEW_BATCHES
        or run_evaluated_sources >= RECONCILIATION_CONTINUE_AS_NEW_SOURCES
    )


def accumulate_counters(
    counters: ReconciliationCounters, outcome: ReconciliationBatchOutcome
) -> ReconciliationCounters:
    """Fold one committed batch's closed counters into the run totals."""

    return ReconciliationCounters(
        evaluated_sources=counters.evaluated_sources + outcome.evaluated_sources,
        to_excluded_sources=counters.to_excluded_sources + outcome.to_excluded_sources,
        to_allowed_sources=counters.to_allowed_sources + outcome.to_allowed_sources,
        unchanged_sources=counters.unchanged_sources + outcome.unchanged_sources,
    )


def reconciliation_input_for_lease(
    lease: LeasedPolicyReconciliation,
) -> ReconciliationInput:
    """Build the closed workflow input from one leased reconciliation intent."""

    return ReconciliationInput(
        contract=RECONCILIATION_CONTRACT,
        workspace_id=lease.workspace_id,
        policy_revision_id=lease.policy_revision_id,
        source_checkpoint_event_sequence=lease.source_checkpoint_event_sequence,
    )


@dataclass(frozen=True, slots=True)
class _ReconciliationHeartbeatPayload:
    """The closed heartbeat details: evaluated sources and committed batches."""

    evaluated_sources: int
    batch_count: int


@workflow.defn(name=POLICY_RECONCILIATION_WORKFLOW_TYPE_NAME)
class PolicyReconciliationWorkflow:
    """The batched reconciliation workflow (spec 15).

    The workflow owns no I/O: it schedules one activity per bounded batch,
    accumulates the closed counters, heartbeats through the activity after
    every committed batch, continues as new at the pinned bounds with the
    stable cursor and totals, and runs the completion activity when the scan
    ends. A superseded revision returns the closed outcome without any later
    projection effect.
    """

    @workflow.run
    async def run(self, reference: ReconciliationInput | ReconciliationContinuation) -> str:
        continuation = reference if isinstance(reference, ReconciliationContinuation) else None
        base = continuation if continuation is not None else reference
        after_source_id = continuation.after_source_id if continuation else None
        counters = continuation.counters if continuation else ReconciliationCounters()
        # The continue-as-new budget is per run: a fresh run starts at zero
        # even though the counters and cursor carry over.
        run_batch_count = 0
        run_evaluated_sources = 0
        while True:
            outcome: ReconciliationBatchOutcome = await workflow.execute_activity(
                POLICY_RECONCILIATION_BATCH_ACTIVITY_NAME,
                PolicyReconciliationBatchReference(
                    contract=POLICY_RECONCILIATION_REFERENCE_CONTRACT,
                    workspace_id=base.workspace_id,
                    policy_revision_id=base.policy_revision_id,
                    source_checkpoint_event_sequence=(base.source_checkpoint_event_sequence),
                    after_source_id=after_source_id,
                ),
                start_to_close_timeout=POLICY_RECONCILIATION_ACTIVITY_START_TO_CLOSE_TIMEOUT,
                heartbeat_timeout=POLICY_RECONCILIATION_HEARTBEAT_TIMEOUT,
                retry_policy=reconciliation_retry_policy(),
                result_type=ReconciliationBatchOutcome,
            )
            if outcome.superseded:
                return PolicyReconciliationOutcome.SUPERSEDED.value
            counters = accumulate_counters(counters, outcome)
            run_batch_count += 1
            run_evaluated_sources += outcome.evaluated_sources
            if not outcome.has_more:
                await workflow.execute_activity(
                    POLICY_RECONCILIATION_COMPLETION_ACTIVITY_NAME,
                    PolicyReconciliationCompletionReference(
                        contract=POLICY_RECONCILIATION_REFERENCE_CONTRACT,
                        workspace_id=base.workspace_id,
                        policy_revision_id=base.policy_revision_id,
                        source_checkpoint_event_sequence=(base.source_checkpoint_event_sequence),
                        counters=counters,
                    ),
                    start_to_close_timeout=(POLICY_RECONCILIATION_ACTIVITY_START_TO_CLOSE_TIMEOUT),
                    retry_policy=reconciliation_retry_policy(),
                )
                return PolicyReconciliationOutcome.COMPLETED.value
            after_source_id = outcome.last_source_id
            if should_continue_as_new(
                run_batch_count=run_batch_count,
                run_evaluated_sources=run_evaluated_sources,
            ):
                workflow.continue_as_new(
                    ReconciliationContinuation(
                        contract=RECONCILIATION_CONTRACT,
                        workspace_id=base.workspace_id,
                        policy_revision_id=base.policy_revision_id,
                        source_checkpoint_event_sequence=(base.source_checkpoint_event_sequence),
                        after_source_id=after_source_id,
                        counters=counters,
                    )
                )


class PolicyReconciliationStorePort(Protocol):
    """The store slice the activities consume."""

    async def run_reconciliation_batch(
        self,
        workspace_id: UUID,
        policy_revision_id: UUID,
        source_checkpoint_event_sequence: int,
        after_source_id: UUID | None,
        heartbeat: Callable[[ReconciliationProgress], Awaitable[None]] | None = None,
    ) -> ReconciliationBatchOutcome: ...

    async def complete_reconciliation(
        self,
        workspace_id: UUID,
        policy_revision_id: UUID,
        counters: ReconciliationCounters,
        context: DiagnosticContext | None = None,
    ) -> bool: ...

    async def fail_reconciliation(
        self,
        workspace_id: UUID,
        policy_revision_id: UUID,
        error_code: SafeToken,
        *,
        retryable: bool,
    ) -> bool: ...


def _current_activity_attempt() -> int | None:
    """The one-based Temporal attempt, or ``None`` outside a worker."""

    try:
        return activity.info().attempt
    except RuntimeError:
        return None


class PolicyReconciliationActivities:
    """The batch and completion activities over the durable store.

    The batch activity forwards the store's post-commit heartbeat through the
    injected sink (the Temporal heartbeat channel in production, a recorder in
    unit tests), maps typed failures onto the closed Temporal error registry —
    registry retryability alone decides whether Temporal retries — and durably
    releases the reconciliation intent only on a final retryable attempt or a
    non-retryable failure, so a dependency failure leaves reconciliation
    pending with bounded backoff.
    """

    def __init__(
        self,
        *,
        store: PolicyReconciliationStorePort,
        attempt_reader: Callable[[], int | None] | None = None,
        heartbeat_sink: Callable[[ReconciliationProgress], None] | None = None,
    ) -> None:
        self._store = store
        self._attempt_reader = (
            attempt_reader if attempt_reader is not None else _current_activity_attempt
        )
        self._heartbeat_sink = heartbeat_sink

    async def _heartbeat(self, progress: ReconciliationProgress) -> None:
        if self._heartbeat_sink is not None:
            self._heartbeat_sink(progress)
            return
        try:
            activity.heartbeat(
                _ReconciliationHeartbeatPayload(
                    evaluated_sources=progress.evaluated_sources,
                    batch_count=progress.batch_count,
                )
            )
        except RuntimeError:
            # Outside a Temporal worker (unit tests) there is no heartbeat
            # channel; the store's progress contract is unchanged.
            return

    @activity.defn(name=POLICY_RECONCILIATION_BATCH_ACTIVITY_NAME)
    async def run_reconciliation_batch_activity(
        self, reference: PolicyReconciliationBatchReference
    ) -> ReconciliationBatchOutcome:
        if reference.contract != POLICY_RECONCILIATION_REFERENCE_CONTRACT:
            raise TemporalApplicationError("exclusion_policy_input_invalid", non_retryable=True)
        try:
            return await self._store.run_reconciliation_batch(
                reference.workspace_id,
                reference.policy_revision_id,
                reference.source_checkpoint_event_sequence,
                reference.after_source_id,
                self._heartbeat,
            )
        except ApplicationError as error:
            raise await self._apply_failure(
                reference.workspace_id,
                reference.policy_revision_id,
                error,
            ) from error
        except Exception:
            await self._apply_retryable_failure(
                reference.workspace_id, reference.policy_revision_id
            )
            raise

    @activity.defn(name=POLICY_RECONCILIATION_COMPLETION_ACTIVITY_NAME)
    async def complete_reconciliation_activity(
        self, reference: PolicyReconciliationCompletionReference
    ) -> str:
        if reference.contract != POLICY_RECONCILIATION_REFERENCE_CONTRACT:
            raise TemporalApplicationError("exclusion_policy_input_invalid", non_retryable=True)
        try:
            # ``False`` acknowledges an idempotent replay: the completion
            # audit row already exists and no second effect is wanted.
            await self._store.complete_reconciliation(
                reference.workspace_id,
                reference.policy_revision_id,
                reference.counters,
            )
        except ApplicationError as error:
            raise await self._apply_failure(
                reference.workspace_id,
                reference.policy_revision_id,
                error,
            ) from error
        except Exception:
            await self._apply_retryable_failure(
                reference.workspace_id, reference.policy_revision_id
            )
            raise
        return PolicyReconciliationOutcome.COMPLETED.value

    async def _apply_failure(
        self, workspace_id: UUID, policy_revision_id: UUID, error: ApplicationError
    ) -> TemporalApplicationError:
        """Durably record the failure, then build the closed Temporal error.

        A non-retryable typed code fails the durable row terminally before the
        error surfaces; a retryable code retries and releases the row back to
        pending with bounded backoff only when no Temporal attempt remains.
        """

        non_retryable = not error.is_retryable
        error_code = SafeToken.parse(error.error_code.value)
        if non_retryable:
            await self._store.fail_reconciliation(
                workspace_id, policy_revision_id, error_code, retryable=False
            )
        else:
            await self._release_retryable_final_attempt(
                workspace_id, policy_revision_id, error_code
            )
        return TemporalApplicationError(error.error_code.value, non_retryable=non_retryable)

    async def _apply_retryable_failure(self, workspace_id: UUID, policy_revision_id: UUID) -> None:
        """Release the durable row only when no Temporal retry remains."""

        await self._release_retryable_final_attempt(
            workspace_id, policy_revision_id, RECONCILIATION_EXECUTION_FAILED_ERROR_CODE
        )

    async def _release_retryable_final_attempt(
        self, workspace_id: UUID, policy_revision_id: UUID, error_code: SafeToken
    ) -> None:
        attempt = self._attempt_reader()
        is_final_attempt = (
            attempt is None or attempt >= POLICY_RECONCILIATION_BATCH_ACTIVITY_MAXIMUM_ATTEMPTS
        )
        if is_final_attempt:
            await self._store.fail_reconciliation(
                workspace_id, policy_revision_id, error_code, retryable=True
            )


class PolicyReconciliationStarterProtocol(Protocol):
    """The start port the dispatch runtime consumes."""

    async def start_policy_reconciliation(
        self, reference: ReconciliationInput
    ) -> PolicyReconciliationStartOutcome: ...


def _unavailable_failure() -> ExclusionPolicyError:
    return ExclusionPolicyError(ErrorCode.EXCLUSION_POLICY_COMMIT_OUTCOME_UNKNOWN)


def _terminal_dispatch_failure() -> ExclusionPolicyError:
    from postgresql_source_store.policy_reconciliation import (
        RECONCILIATION_DISPATCH_TERMINAL_ERROR_CODE,
    )

    return ExclusionPolicyError(
        ErrorCode.EXCLUSION_POLICY_INPUT_INVALID,
        safe_details={"reason": RECONCILIATION_DISPATCH_TERMINAL_ERROR_CODE},
    )


class TemporalPolicyReconciliationStarter:
    """Start one deterministic reconciliation workflow per revision.

    Holds only the composition-owned Temporal client and the pinned
    queue/timeout bounds; every Temporal RPC it issues carries the pinned
    caller-side timeout. ``ALLOW_DUPLICATE_FAILED_ONLY`` plus
    ``USE_EXISTING`` implement the re-drive semantics: a failed closed run
    (dependency failure released the durable intent back to pending) starts a
    fresh deterministic execution that replays insert-once evidence, while a
    running execution converges on the same run and a completed one resolves
    as ``existing`` without a second execution. SDK exceptions map onto the
    closed policy codes with the provider error chained internally only.
    """

    def __init__(
        self,
        client: Client,
        *,
        task_queue: str = POLICY_RECONCILIATION_TASK_QUEUE,
        start_timeout: timedelta = POLICY_RECONCILIATION_START_TIMEOUT,
    ) -> None:
        if task_queue != POLICY_RECONCILIATION_TASK_QUEUE:
            raise ValueError("task queue must be the pinned reconciliation queue")
        self._client = client
        self._task_queue = task_queue
        self._start_timeout = start_timeout

    async def start_policy_reconciliation(
        self, reference: ReconciliationInput
    ) -> PolicyReconciliationStartOutcome:
        workflow_id = reconciliation_workflow_id(
            reference.workspace_id, reference.policy_revision_id
        )
        try:
            await self._client.start_workflow(
                POLICY_RECONCILIATION_WORKFLOW_TYPE_NAME,
                reference,
                id=workflow_id,
                task_queue=self._task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
                id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
                rpc_timeout=self._start_timeout,
            )
        except WorkflowAlreadyStartedError as cause:
            return await self._resolve_existing_execution(workflow_id, cause)
        except RPCError as cause:
            raise _unavailable_failure() from cause
        except RPCTimeoutOrCancelledError as cause:
            raise _unavailable_failure() from cause
        except Exception as cause:
            raise _unavailable_failure() from cause
        return PolicyReconciliationStartOutcome.STARTED

    async def _resolve_existing_execution(
        self, workflow_id: str, cause: WorkflowAlreadyStartedError
    ) -> PolicyReconciliationStartOutcome:
        """Resolve a rejected duplicate run by describing the execution.

        The rejected path covers a prior completed execution (a failed one
        would have started fresh under ``ALLOW_DUPLICATE_FAILED_ONLY``). The
        exact deterministic execution — same pinned type and task queue,
        completed or continued-as-new — is accepted and never terminated or
        replaced; any other shape is the terminal contract failure.
        """

        try:
            description = await self._client.get_workflow_handle(workflow_id).describe(
                rpc_timeout=self._start_timeout
            )
        except RPCError as describe_cause:
            raise _unavailable_failure() from describe_cause
        except Exception as describe_cause:
            raise _unavailable_failure() from describe_cause
        from temporalio.client import WorkflowExecutionStatus

        if (
            description.workflow_type != POLICY_RECONCILIATION_WORKFLOW_TYPE_NAME
            or description.task_queue != self._task_queue
        ):
            raise _terminal_dispatch_failure() from cause
        if description.status in (
            WorkflowExecutionStatus.COMPLETED,
            WorkflowExecutionStatus.CONTINUED_AS_NEW,
        ):
            return PolicyReconciliationStartOutcome.EXISTING
        raise _terminal_dispatch_failure() from cause


__all__ = [
    "POLICY_RECONCILIATION_ACTIVITY_NAME",
    "POLICY_RECONCILIATION_ACTIVITY_NAMES",
    "POLICY_RECONCILIATION_ACTIVITY_START_TO_CLOSE_TIMEOUT",
    "POLICY_RECONCILIATION_BATCH_ACTIVITY_MAXIMUM_ATTEMPTS",
    "POLICY_RECONCILIATION_BATCH_ACTIVITY_NAME",
    "POLICY_RECONCILIATION_COMPLETION_ACTIVITY_NAME",
    "POLICY_RECONCILIATION_HEARTBEAT_TIMEOUT",
    "POLICY_RECONCILIATION_NON_RETRYABLE_ERROR_TYPES",
    "POLICY_RECONCILIATION_REFERENCE_CONTRACT",
    "POLICY_RECONCILIATION_START_TIMEOUT",
    "POLICY_RECONCILIATION_TASK_QUEUE",
    "POLICY_RECONCILIATION_WORKFLOW_TYPE_NAME",
    "PolicyReconciliationActivities",
    "PolicyReconciliationBatchReference",
    "PolicyReconciliationCompletionReference",
    "PolicyReconciliationOutcome",
    "PolicyReconciliationStartOutcome",
    "PolicyReconciliationWorkflow",
    "ReconciliationBatchOutcome",
    "TemporalPolicyReconciliationStarter",
    "accumulate_counters",
    "reconciliation_input_for_lease",
    "reconciliation_retry_policy",
    "should_continue_as_new",
]
