"""Temporal single-activity workflow and adapter for policy previews (spec 10).

This is the worker-owned composition boundary over the Temporal SDK:
:class:`PolicyPreviewWorkflow` executes exactly one activity —
``run_policy_preview_activity`` — under a bounded retry policy whose
non-retryable set is exactly the typed stale/not-initialized/terminal preview
errors, a start-to-close timeout beyond the fifteen-minute execution
deadline and a heartbeat timeout longer than one scan page. The activity
delegates to the durable preview store: every database statement, heartbeat
and fenced transition belongs to the store; the activity only forwards the
closed heartbeat payload, maps typed errors onto the closed outcome
vocabulary, and marks the durable row failed (closed safe error code) before
surfacing a non-retryable failure or a final-attempt retryable failure.

The closed ``exclusion_policy_preview_reference/v1`` input carries only the
contract tag, the two opaque UUIDs and the bound source checkpoint, so no
rule operand, locator, title or content byte can ever enter Temporal history.
The starter derives the deterministic workflow ID
``exclusion-policy-preview/{workspace_id}/{policy_preview_id}`` and starts
with duplicate-run rejection plus ``USE_EXISTING`` convergence, resolving a
lost start acknowledgement by describing the exact deterministic execution.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import Final, Protocol
from uuid import UUID, uuid4

from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.common import (
    RetryPolicy,
    WorkflowIDConflictPolicy,
    WorkflowIDReusePolicy,
)
from temporalio.exceptions import ApplicationError as TemporalApplicationError

from personal_os.diagnostics.context import DiagnosticContext
from personal_os.diagnostics.events import SafeToken
from personal_os.diagnostics.trace_context import SpanId, TraceContext, TraceId
from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import ApplicationError
from personal_os.exclusion_policy.errors import ExclusionPolicyError
from personal_os.exclusion_policy.previews import (
    PolicyPreviewRecord,
    PreviewProgress,
    policy_preview_workflow_id,
)

#: Workflow/activity identity pins (spec 10 and the pinned queue).
POLICY_PREVIEW_WORKFLOW_TYPE_NAME: Final[str] = "PolicyPreviewWorkflow"
POLICY_PREVIEW_TASK_QUEUE: Final[str] = "exclusion-policy-preview"
POLICY_PREVIEW_ACTIVITY_NAME: Final[str] = "run_policy_preview_activity"

#: The closed workflow input contract tag (spec 10: contract tag plus opaque
#: IDs and the checkpoint — nothing else).
POLICY_PREVIEW_REFERENCE_CONTRACT: Final[str] = "exclusion_policy_preview_reference/v1"

#: The bounded activity retry: infrastructure failures retry from the same
#: captured inputs (the rolled-back transaction proves no partial evidence),
#: while the typed stale and terminal failures are non-retryable.
POLICY_PREVIEW_ACTIVITY_MAXIMUM_ATTEMPTS: Final[int] = 5

#: The activity must outlive the fifteen-minute execution deadline bound.
POLICY_PREVIEW_START_TO_CLOSE_TIMEOUT: Final[timedelta] = timedelta(minutes=20)

#: Heartbeats fire once per completed 500-row page; the timeout must exceed
#: any single page's scan/evaluate/write duration by a wide margin.
POLICY_PREVIEW_HEARTBEAT_TIMEOUT: Final[timedelta] = timedelta(minutes=2)

#: The caller-side bound for every Temporal RPC the starter issues.
POLICY_PREVIEW_START_TIMEOUT: Final[timedelta] = timedelta(seconds=10)

#: The closed non-retryable error types (typed error code values).
POLICY_PREVIEW_NON_RETRYABLE_ERROR_TYPES: Final[tuple[str, ...]] = (
    "exclusion_policy_preview_stale",
    "exclusion_policy_not_initialized",
    "exclusion_policy_preview_expired",
    "exclusion_policy_preview_failed",
    "exclusion_policy_draft_conflict",
)

#: Closed durable safe error codes the activity writes. They mirror the store
#: constants without importing the SQLAlchemy-bearing adapter module here.
PREVIEW_EXECUTION_FAILED_ERROR_CODE: Final[SafeToken] = SafeToken.parse("preview_execution_failed")
PREVIEW_NOT_INITIALIZED_ERROR_CODE: Final[SafeToken] = SafeToken.parse(
    "exclusion_policy_not_initialized"
)
PREVIEW_DISPATCH_TERMINAL_ERROR_CODE: Final[SafeToken] = SafeToken.parse(
    "preview_dispatch_terminal"
)


@dataclass(frozen=True, slots=True)
class PolicyPreviewReference:
    """The closed ``exclusion_policy_preview_reference/v1`` workflow input.

    Only the contract tag, the two entity UUIDs and the bound checkpoint are
    members, so the default JSON codec can serialize nothing else into
    Temporal history: rule operands, locators, titles, paths and content have
    no field to occupy.
    """

    contract: str
    workspace_id: UUID
    policy_preview_id: UUID
    source_event_checkpoint: int


class PolicyPreviewExecutionOutcome(StrEnum):
    """The closed execution outcomes of one preview activity."""

    READY = "ready"


class PolicyPreviewStartOutcome(StrEnum):
    """The closed dispatch outcomes of one workflow start attempt."""

    STARTED = "started"
    EXISTING = "existing"


def preview_retry_policy() -> RetryPolicy:
    """The bounded retry policy of the single preview activity."""

    return RetryPolicy(
        initial_interval=timedelta(seconds=1),
        maximum_interval=timedelta(seconds=30),
        maximum_attempts=POLICY_PREVIEW_ACTIVITY_MAXIMUM_ATTEMPTS,
        non_retryable_error_types=list(POLICY_PREVIEW_NON_RETRYABLE_ERROR_TYPES),
    )


@dataclass(frozen=True, slots=True)
class _PreviewHeartbeatPayload:
    """The closed heartbeat details: evaluated subjects and batch count."""

    evaluated_subjects: int
    batch_count: int


@workflow.defn(name=POLICY_PREVIEW_WORKFLOW_TYPE_NAME)
class PolicyPreviewWorkflow:
    """The single-activity preview workflow (spec 10).

    The workflow owns no I/O: it schedules exactly one activity execution
    with the pinned retry, heartbeat and start-to-close bounds and returns
    the closed execution outcome. All snapshot, evaluation and persistence
    semantics live inside the activity's one database transaction.
    """

    @workflow.run
    async def run(self, reference: PolicyPreviewReference) -> str:
        outcome: str = await workflow.execute_activity(
            POLICY_PREVIEW_ACTIVITY_NAME,
            reference,
            start_to_close_timeout=POLICY_PREVIEW_START_TO_CLOSE_TIMEOUT,
            heartbeat_timeout=POLICY_PREVIEW_HEARTBEAT_TIMEOUT,
            retry_policy=preview_retry_policy(),
        )
        return outcome


class PolicyPreviewStorePort(Protocol):
    """The store slice the activity consumes (the durable port minus reads)."""

    async def run_preview_activity(
        self,
        preview_id: UUID,
        context: DiagnosticContext,
        heartbeat: Callable[[PreviewProgress], Awaitable[None]] | None = None,
    ) -> PolicyPreviewRecord: ...

    async def mark_preview_failed(self, preview_id: UUID, error_code: SafeToken) -> bool: ...


def preview_reference_for_lease(lease: PolicyPreviewLeaseLike) -> PolicyPreviewReference:
    """Build the closed workflow input for one leased preview row."""

    return PolicyPreviewReference(
        contract=POLICY_PREVIEW_REFERENCE_CONTRACT,
        workspace_id=lease.workspace_id,
        policy_preview_id=lease.policy_preview_id,
        source_event_checkpoint=lease.source_event_checkpoint,
    )


class PolicyPreviewLeaseLike(Protocol):
    """The leased-row fields the input builder reads."""

    @property
    def policy_preview_id(self) -> UUID: ...

    @property
    def workspace_id(self) -> UUID: ...

    @property
    def source_event_checkpoint(self) -> int: ...


def _current_activity_attempt() -> int | None:
    """The one-based Temporal attempt, or ``None`` outside a worker."""

    try:
        return activity.info().attempt
    except RuntimeError:
        return None


def _durable_safe_code_for(error: ApplicationError) -> SafeToken:
    """Pick the closed durable safe error code for one typed failure.

    A typed stale error already carries its closed reason token; the missing
    policy graph carries the registry code; every other typed failure uses
    the generic closed execution-failure code.
    """

    if error.error_code is ErrorCode.EXCLUSION_POLICY_PREVIEW_STALE:
        reason = error.safe_details.get("reason")
        if isinstance(reason, SafeToken):
            return reason
    if error.error_code is ErrorCode.EXCLUSION_POLICY_NOT_INITIALIZED:
        return PREVIEW_NOT_INITIALIZED_ERROR_CODE
    return PREVIEW_EXECUTION_FAILED_ERROR_CODE


class PolicyPreviewActivities:
    """The single preview activity over the durable preview store.

    The activity forwards the store's closed progress payload as Temporal
    heartbeats, maps typed failures onto the closed Temporal error
    vocabulary (registry retryability decides retryability) after durably
    failing the row, and marks the row failed with the generic closed code
    when a retryable failure reaches its final attempt. ``attempt_reader``
    is the injected unit seam for the Temporal attempt number.
    """

    def __init__(
        self,
        *,
        preview_store: PolicyPreviewStorePort,
        attempt_reader: Callable[[], int | None] | None = None,
    ) -> None:
        self._preview_store = preview_store
        self._attempt_reader = (
            attempt_reader if attempt_reader is not None else _current_activity_attempt
        )

    @activity.defn(name=POLICY_PREVIEW_ACTIVITY_NAME)
    async def run_policy_preview_activity(self, reference: PolicyPreviewReference) -> str:
        if reference.contract != POLICY_PREVIEW_REFERENCE_CONTRACT:
            raise TemporalApplicationError("exclusion_policy_input_invalid", non_retryable=True)

        async def heartbeat(progress: PreviewProgress) -> None:
            try:
                activity.heartbeat(
                    _PreviewHeartbeatPayload(
                        evaluated_subjects=progress.evaluated_subjects,
                        batch_count=progress.batch_count,
                    )
                )
            except RuntimeError:
                # Outside a Temporal worker (unit tests) there is no
                # heartbeat channel; the store's progress contract is
                # unchanged.
                return

        try:
            await self._preview_store.run_preview_activity(
                reference.policy_preview_id, _activity_context(), heartbeat
            )
        except ApplicationError as error:
            failure = await self._apply_failure(reference, error)
            raise failure from error
        except Exception:
            # An unexpected failure stays retryable until the final attempt,
            # where the durable row is failed with the closed generic code.
            await self._apply_retryable_failure(reference)
            raise
        return PolicyPreviewExecutionOutcome.READY.value

    async def _apply_failure(
        self, reference: PolicyPreviewReference, error: ApplicationError
    ) -> TemporalApplicationError:
        """Durably record the failure, then build the closed Temporal error.

        Registry retryability alone decides whether Temporal retries: the
        stale and terminal typed codes are non-retryable and fail the row
        first with their closed safe code; retryable codes surface with
        retries enabled and fail the row only on the final attempt.
        """

        non_retryable = not error.is_retryable
        if non_retryable:
            await self._preview_store.mark_preview_failed(
                reference.policy_preview_id, _durable_safe_code_for(error)
            )
        else:
            await self._apply_retryable_failure(reference)
        return TemporalApplicationError(error.error_code.value, non_retryable=non_retryable)

    async def _apply_retryable_failure(self, reference: PolicyPreviewReference) -> None:
        """Mark the row failed only when no Temporal retry remains."""

        attempt = self._attempt_reader()
        is_final_attempt = attempt is None or attempt >= POLICY_PREVIEW_ACTIVITY_MAXIMUM_ATTEMPTS
        if is_final_attempt:
            await self._preview_store.mark_preview_failed(
                reference.policy_preview_id, PREVIEW_EXECUTION_FAILED_ERROR_CODE
            )


def _activity_context() -> DiagnosticContext:
    """A correlation context for the store port; no content crosses it."""

    return DiagnosticContext(
        request_id=uuid4(),
        client_request_id=None,
        trace=TraceContext(
            trace_id=TraceId("0123456789abcdef0123456789abcdef"),
            remote_parent_span_id=None,
            local_span_id=SpanId("0123456789abcdef"),
            trace_flags=0,
        ),
        workflow_id=SafeToken.parse("policy_preview_workflow"),
    )


class PolicyPreviewStarterProtocol(Protocol):
    """The start port the dispatch runtime consumes."""

    async def start_policy_preview(
        self, reference: PolicyPreviewReference
    ) -> PolicyPreviewStartOutcome: ...


def _unavailable_failure() -> ExclusionPolicyError:
    return ExclusionPolicyError(ErrorCode.EXCLUSION_POLICY_COMMIT_OUTCOME_UNKNOWN)


def _terminal_dispatch_failure() -> ExclusionPolicyError:
    return ExclusionPolicyError(
        ErrorCode.EXCLUSION_POLICY_PREVIEW_FAILED,
        safe_details={"reason": PREVIEW_DISPATCH_TERMINAL_ERROR_CODE},
    )


class TemporalPolicyPreviewStarter:
    """Start one deterministic preview workflow per leased preview.

    Holds only the composition-owned Temporal client and the pinned
    queue/timeout bounds; every Temporal RPC it issues — the start and the
    duplicate-run resolution describe — carries the pinned caller-side
    timeout. Under ``USE_EXISTING`` the server natively resolves a
    concurrently running execution (surfacing as ``STARTED``); the server's
    duplicate-run rejection for a closed execution resolves through the
    bounded describe, which accepts the exact deterministic execution — same
    pinned type and task queue, running, completed or continued-as-new — as
    ``EXISTING`` and never terminates or replaces anything. SDK exceptions
    map onto the closed policy error codes with the provider error chained
    internally only.
    """

    def __init__(
        self,
        client: Client,
        *,
        task_queue: str = POLICY_PREVIEW_TASK_QUEUE,
        start_timeout: timedelta = POLICY_PREVIEW_START_TIMEOUT,
    ) -> None:
        if task_queue != POLICY_PREVIEW_TASK_QUEUE:
            raise ValueError("task queue must be the pinned exclusion-policy-preview queue")
        self._client = client
        self._task_queue = task_queue
        self._start_timeout = start_timeout

    async def start_policy_preview(
        self, reference: PolicyPreviewReference
    ) -> PolicyPreviewStartOutcome:
        workflow_id = policy_preview_workflow_id(
            reference.workspace_id, reference.policy_preview_id
        )
        try:
            await self._client.start_workflow(
                POLICY_PREVIEW_WORKFLOW_TYPE_NAME,
                reference,
                id=workflow_id,
                task_queue=self._task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
                rpc_timeout=self._start_timeout,
            )
        except Exception as cause:
            return await self._resolve_start_failure(workflow_id, cause)
        return PolicyPreviewStartOutcome.STARTED

    async def _resolve_start_failure(
        self, workflow_id: str, cause: Exception
    ) -> PolicyPreviewStartOutcome:
        if isinstance(cause, ApplicationError):
            raise cause
        from temporalio.exceptions import WorkflowAlreadyStartedError

        if isinstance(cause, WorkflowAlreadyStartedError):
            return await self._resolve_existing_execution(workflow_id, cause)
        raise _unavailable_failure() from cause

    async def _resolve_existing_execution(
        self, workflow_id: str, cause: Exception
    ) -> PolicyPreviewStartOutcome:
        """Resolve a rejected duplicate run by describing the execution."""

        from temporalio.client import WorkflowExecutionStatus

        try:
            description = await self._client.get_workflow_handle(workflow_id).describe(
                rpc_timeout=self._start_timeout
            )
        except Exception as describe_cause:
            raise _unavailable_failure() from describe_cause
        if (
            description.workflow_type != POLICY_PREVIEW_WORKFLOW_TYPE_NAME
            or description.task_queue != self._task_queue
        ):
            raise _terminal_dispatch_failure() from cause
        if description.status in (
            WorkflowExecutionStatus.RUNNING,
            WorkflowExecutionStatus.COMPLETED,
            WorkflowExecutionStatus.CONTINUED_AS_NEW,
        ):
            return PolicyPreviewStartOutcome.EXISTING
        raise _terminal_dispatch_failure() from cause


__all__ = [
    "POLICY_PREVIEW_ACTIVITY_MAXIMUM_ATTEMPTS",
    "POLICY_PREVIEW_ACTIVITY_NAME",
    "POLICY_PREVIEW_HEARTBEAT_TIMEOUT",
    "POLICY_PREVIEW_NON_RETRYABLE_ERROR_TYPES",
    "POLICY_PREVIEW_REFERENCE_CONTRACT",
    "POLICY_PREVIEW_START_TIMEOUT",
    "POLICY_PREVIEW_START_TO_CLOSE_TIMEOUT",
    "POLICY_PREVIEW_TASK_QUEUE",
    "POLICY_PREVIEW_WORKFLOW_TYPE_NAME",
    "PolicyPreviewActivities",
    "PolicyPreviewExecutionOutcome",
    "PolicyPreviewReference",
    "PolicyPreviewStartOutcome",
    "PolicyPreviewWorkflow",
    "TemporalPolicyPreviewStarter",
    "preview_reference_for_lease",
    "preview_retry_policy",
]
