"""Temporal workflow-start adapter for source-ingestion projection dispatch.

This is the worker-owned composition boundary over the Temporal SDK (design
section 11.3): it derives the deterministic workflow identity
(``SourceIngestionWorkflow`` at ``source-ingestion/{workspace_id}/{event_id}``
on the pinned ``source-ingestion`` task queue), builds the closed
``source_ingestion_reference/v1`` input carrying only the contract tag plus the
workspace/event/source/source-version UUIDs, and starts executions with
``Client.start_workflow()`` under the pinned 10-second caller timeout,
duplicate-run rejection (``REJECT_DUPLICATE``) for closed executions and
``USE_EXISTING`` for running ones; the duplicate-run resolution ``describe()``
carries the same bound. An already-started execution is resolved by describing
it: the
exact deterministic execution — running, completed, or continued-as-new — is
accepted as ``existing`` and never terminated or replaced; any other type or an
abnormally closed execution is the terminal integrity failure. SDK exceptions
map onto the closed projection error codes without provider text crossing the
boundary: only the chained cause retains the SDK error, and no title, object
key, hash, path, content or provider message ever enters workflow input,
identity or history.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import Final, Protocol
from uuid import UUID

from temporalio.client import Client, RPCTimeoutOrCancelledError, WorkflowExecutionStatus
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError

from personal_os.error_contracts.codes import ErrorCode
from personal_os.sources.errors import ProjectionDispatchError
from personal_os.sources.projection_dispatch import (
    LeasedProjectionIntent,
    ProjectionIntentOriginKind,
)

#: The pinned workflow type name every dispatch starts (design section 11.3).
PROJECTION_WORKFLOW_TYPE_NAME: Final[str] = "SourceIngestionWorkflow"

#: The pinned task queue every dispatch targets.
PROJECTION_WORKFLOW_TASK_QUEUE: Final[str] = "source-ingestion"

#: The workflow ID prefix; the full ID is ``{prefix}/{workspace_id}/{event_id}``.
PROJECTION_WORKFLOW_ID_PREFIX: Final[str] = "source-ingestion"

#: The input contract tag (design section 11.3).
SOURCE_INGESTION_REFERENCE_CONTRACT: Final[str] = "source_ingestion_reference/v1"

#: The caller-side bound for every Temporal RPC the dispatch path issues:
#: the workflow start, the duplicate-run resolution describe, and (via
#: ``asyncio.wait_for``) the process's client connect (design section 11.2).
PROJECTION_WORKFLOW_START_TIMEOUT: Final[timedelta] = timedelta(seconds=10)

#: The closed-execution states resolved as the exact deterministic execution
#: already accepted; every other state is an abnormal closure.
_ACCEPTED_EXECUTION_STATUSES: Final[frozenset[WorkflowExecutionStatus]] = frozenset(
    {
        WorkflowExecutionStatus.RUNNING,
        WorkflowExecutionStatus.COMPLETED,
        WorkflowExecutionStatus.CONTINUED_AS_NEW,
    }
)


@dataclass(frozen=True, slots=True)
class SourceIngestionReference:
    """The closed ``source_ingestion_reference/v1`` workflow input.

    Only the contract tag and the four entity UUIDs are members, so the
    default JSON codec can serialize nothing else into Temporal history: raw
    content, titles, object keys, hashes, paths and provider data have no field
    to occupy.
    """

    contract: str
    workspace_id: UUID
    event_id: UUID
    source_id: UUID
    source_version_id: UUID


class ProjectionWorkflowStartResult(StrEnum):
    """The closed dispatch outcomes of one workflow start attempt."""

    STARTED = "started"
    EXISTING = "existing"


def projection_workflow_id(workspace_id: UUID, event_id: UUID) -> str:
    """Derive the deterministic workflow ID for one source event.

    Both projection intents of one event derive the same ID, so a concurrent
    or retried dispatch resolves to one workflow execution.
    """
    return f"{PROJECTION_WORKFLOW_ID_PREFIX}/{workspace_id}/{event_id}"


def source_ingestion_reference_for_intent(
    intent: LeasedProjectionIntent,
) -> SourceIngestionReference:
    """Build the workflow input for one leased projection intent.

    An intent without a source version cannot satisfy the closed input
    contract and is the terminal integrity failure, never a workflow start.
    Only the ``source_event`` origin with its non-null event reference may
    start ``SourceIngestionWorkflow``: a ``policy_transition`` intent is not
    a source edit and never reaches this input contract.
    """
    if intent.source_version_id is None:
        raise ProjectionDispatchError(ErrorCode.PROJECTION_INTENT_CONTRACT_INVALID)
    if intent.origin_kind is not ProjectionIntentOriginKind.SOURCE_EVENT or intent.event_id is None:
        raise ProjectionDispatchError(ErrorCode.PROJECTION_INTENT_CONTRACT_INVALID)
    return SourceIngestionReference(
        contract=SOURCE_INGESTION_REFERENCE_CONTRACT,
        workspace_id=intent.workspace_id,
        event_id=intent.event_id,
        source_id=intent.source_id,
        source_version_id=intent.source_version_id,
    )


class ProjectionWorkflowStarter(Protocol):
    """The start port the dispatch runtime consumes.

    Returns the closed start outcome or raises
    :class:`~personal_os.sources.errors.ProjectionDispatchError` with the
    registry retryability for the attempt; no Temporal handle, payload or
    provider text crosses back to the caller.
    """

    async def start_source_ingestion(
        self, reference: SourceIngestionReference
    ) -> ProjectionWorkflowStartResult: ...


def _unavailable_failure() -> ProjectionDispatchError:
    return ProjectionDispatchError(ErrorCode.PROJECTION_DISPATCH_UNAVAILABLE)


def _terminal_contract_failure() -> ProjectionDispatchError:
    return ProjectionDispatchError(ErrorCode.PROJECTION_INTENT_CONTRACT_INVALID)


class TemporalProjectionWorkflowStarter:
    """Start one deterministic source-ingestion workflow per event.

    Holds only the composition-owned Temporal client and the pinned queue/timeout
    bounds; it opens no connection itself. Every Temporal RPC it issues — the
    start and the duplicate-run resolution describe — carries the pinned
    caller-side timeout, so no accepted-but-unanswered call can hang a caller.
    Every SDK exception is mapped to a closed projection outcome with the
    provider error chained as the internal cause only.

    Under ``USE_EXISTING`` the server natively resolves a concurrently running
    execution by returning its handle, which surfaces as ``STARTED``; the
    ``EXISTING`` outcome is emitted only on the server-rejected duplicate-run
    path (in practice a closed execution, or a rejection race) resolved by the
    bounded describe. Both outcomes acknowledge dispatched identically, so no
    extra disambiguating RPC is issued per start.
    """

    def __init__(
        self,
        client: Client,
        *,
        task_queue: str = PROJECTION_WORKFLOW_TASK_QUEUE,
        start_timeout: timedelta = PROJECTION_WORKFLOW_START_TIMEOUT,
    ) -> None:
        if task_queue != PROJECTION_WORKFLOW_TASK_QUEUE:
            raise ValueError("task queue must be the pinned source-ingestion queue")
        self._client = client
        self._task_queue = task_queue
        self._start_timeout = start_timeout

    async def start_source_ingestion(
        self, reference: SourceIngestionReference
    ) -> ProjectionWorkflowStartResult:
        workflow_id = projection_workflow_id(reference.workspace_id, reference.event_id)
        try:
            await self._client.start_workflow(
                PROJECTION_WORKFLOW_TYPE_NAME,
                reference,
                id=workflow_id,
                task_queue=self._task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
                rpc_timeout=self._start_timeout,
            )
        except WorkflowAlreadyStartedError as cause:
            return await self._resolve_existing_execution(workflow_id, cause)
        except RPCError as cause:
            raise self._map_rpc_error(cause) from cause
        except RPCTimeoutOrCancelledError as cause:
            raise _unavailable_failure() from cause
        except Exception as cause:
            # An unexpected SDK failure never becomes terminal: the attempt
            # stays retryable and the provider error stays chained internally.
            raise _unavailable_failure() from cause
        return ProjectionWorkflowStartResult.STARTED

    async def _resolve_existing_execution(
        self, workflow_id: str, cause: WorkflowAlreadyStartedError
    ) -> ProjectionWorkflowStartResult:
        """Resolve a rejected duplicate run by describing the existing execution.

        The describe carries the same pinned caller-side timeout as the start,
        so a hung resolution surfaces as the retryable unavailable failure
        instead of blocking the dispatch group unboundedly. The exact
        deterministic execution — same pinned type and task queue, running,
        completed or continued-as-new — is accepted and never terminated,
        replaced or signalled. Any other type, queue or an abnormal closure is
        the terminal integrity failure.
        """
        try:
            description = await self._client.get_workflow_handle(workflow_id).describe(
                rpc_timeout=self._start_timeout
            )
        except RPCError as describe_cause:
            raise self._map_rpc_error(describe_cause) from describe_cause
        except RPCTimeoutOrCancelledError as describe_cause:
            raise _unavailable_failure() from describe_cause
        except Exception as describe_cause:
            raise _unavailable_failure() from describe_cause
        if (
            description.workflow_type != PROJECTION_WORKFLOW_TYPE_NAME
            or description.task_queue != self._task_queue
        ):
            raise _terminal_contract_failure() from cause
        if description.status in _ACCEPTED_EXECUTION_STATUSES:
            return ProjectionWorkflowStartResult.EXISTING
        raise _terminal_contract_failure() from cause

    @staticmethod
    def _map_rpc_error(cause: RPCError) -> ProjectionDispatchError:
        # Every Temporal RPC failure keeps the dispatch retryable: the caller
        # could not learn the start outcome, the provider message never
        # crosses the boundary and the cause stays chained internally.
        return _unavailable_failure()
