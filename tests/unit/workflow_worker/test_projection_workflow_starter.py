"""Unit contracts for the Temporal projection workflow-start adapter.

Every case pins one rule from design section 11.3: the deterministic workflow
identity (type, ID derivation, fixed task queue), the closed
``source_ingestion_reference/v1`` input carrying only the contract tag plus the
four UUIDs, identical identity/input for two intents of one event, zero
sensitive sentinels in the serialized Temporal input, and the duplicate
execution behavior — ``USE_EXISTING`` for a running execution, rejection of a
duplicate run for a closed execution resolved by describing the exact closed
deterministic execution as accepted without terminating or replacing it. SDK
exceptions must map onto the closed projection error codes without provider
text crossing the boundary.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from temporalio.client import (
    RPCTimeoutOrCancelledError,
    WorkflowExecutionStatus,
)
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.converter import DataConverter
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode
from workflow_worker.projection_workflow_starter import (
    PROJECTION_WORKFLOW_ID_PREFIX,
    PROJECTION_WORKFLOW_START_TIMEOUT,
    PROJECTION_WORKFLOW_TASK_QUEUE,
    PROJECTION_WORKFLOW_TYPE_NAME,
    SOURCE_INGESTION_REFERENCE_CONTRACT,
    ProjectionWorkflowStartResult,
    SourceIngestionReference,
    TemporalProjectionWorkflowStarter,
    projection_workflow_id,
    source_ingestion_reference_for_intent,
)

from personal_os.diagnostics.events import SafeToken
from personal_os.error_contracts.codes import ErrorCode
from personal_os.sources.errors import ProjectionDispatchError
from personal_os.sources.projection_dispatch import LeasedProjectionIntent

_LEAKAGE_SENTINELS: tuple[str, ...] = (
    "sentinel-title",
    "objects/sha256/sentinel-object-key",
    "sentinel-content-hash",
    "sentinel/path/value",
    "sentinel-raw-content",
    "sentinel provider exception detail",
)


class _Missing:
    """Sentinel distinguishing an omitted field from an explicit ``None``."""


_MISSING = _Missing()


def _leased_intent(
    *,
    workspace_id: UUID | None = None,
    event_id: UUID | None = None,
    source_id: UUID | None = None,
    source_version_id: UUID | None | _Missing = _MISSING,
    projection_kind: str = "qdrant",
    attempt_count: int = 0,
) -> LeasedProjectionIntent:
    resolved_source_version: UUID | None
    if isinstance(source_version_id, _Missing):
        resolved_source_version = uuid4()
    else:
        resolved_source_version = source_version_id
    return LeasedProjectionIntent(
        projection_intent_id=uuid4(),
        workspace_id=workspace_id if workspace_id is not None else uuid4(),
        event_id=event_id if event_id is not None else uuid4(),
        source_id=source_id if source_id is not None else uuid4(),
        source_version_id=resolved_source_version,
        projection_kind=SafeToken.parse(projection_kind),
        operation=SafeToken.parse("upsert"),
        attempt_count=attempt_count,
        lease_token=uuid4(),
        leased_until=datetime.now(UTC) + timedelta(seconds=60),
    )


def _serialize_input(reference: SourceIngestionReference) -> bytes:
    (payload,) = DataConverter.default.payload_converter.to_payloads([reference])
    return payload.data


# --- Deterministic input contract ---------------------------------------------


def test_workflow_identity_contract_is_pinned() -> None:
    workspace_id = uuid4()
    event_id = uuid4()

    workflow_id = projection_workflow_id(workspace_id, event_id)

    assert PROJECTION_WORKFLOW_TYPE_NAME == "SourceIngestionWorkflow"
    assert PROJECTION_WORKFLOW_TASK_QUEUE == "source-ingestion"
    assert PROJECTION_WORKFLOW_ID_PREFIX == "source-ingestion"
    assert SOURCE_INGESTION_REFERENCE_CONTRACT == "source_ingestion_reference/v1"
    assert timedelta(seconds=10) == PROJECTION_WORKFLOW_START_TIMEOUT
    assert workflow_id == f"source-ingestion/{workspace_id}/{event_id}"


def test_reference_serializes_to_only_contract_tag_and_uuids() -> None:
    workspace_id = uuid4()
    event_id = uuid4()
    source_id = uuid4()
    source_version_id = uuid4()
    intent = _leased_intent(
        workspace_id=workspace_id,
        event_id=event_id,
        source_id=source_id,
        source_version_id=source_version_id,
    )

    reference = source_ingestion_reference_for_intent(intent)
    decoded = json.loads(_serialize_input(reference))

    assert decoded == {
        "contract": SOURCE_INGESTION_REFERENCE_CONTRACT,
        "workspace_id": str(workspace_id),
        "event_id": str(event_id),
        "source_id": str(source_id),
        "source_version_id": str(source_version_id),
    }


def test_serialized_input_and_identity_contain_no_sensitive_sentinels() -> None:
    intent = _leased_intent()
    sentinels = {
        "title": "sentinel-title",
        "object_key": "objects/sha256/sentinel-object-key",
        "hash": "sentinel-content-hash",
        "path": "sentinel/path/value",
        "content": "sentinel-raw-content",
        "provider_exception": "sentinel provider exception detail",
    }

    reference = source_ingestion_reference_for_intent(intent)
    workflow_id = projection_workflow_id(reference.workspace_id, reference.event_id)
    scanned = _serialize_input(reference) + workflow_id.encode()

    for name, sentinel in sentinels.items():
        assert sentinel.encode() not in scanned, f"{name} sentinel leaked into Temporal input"


def test_two_intents_for_one_event_share_workflow_identity_and_input() -> None:
    workspace_id = uuid4()
    event_id = uuid4()
    source_id = uuid4()
    source_version_id = uuid4()
    shared = {
        "workspace_id": workspace_id,
        "event_id": event_id,
        "source_id": source_id,
        "source_version_id": source_version_id,
    }
    first = _leased_intent(projection_kind="qdrant", **shared)
    second = _leased_intent(projection_kind="neo4j", **shared)

    first_reference = source_ingestion_reference_for_intent(first)
    second_reference = source_ingestion_reference_for_intent(second)

    assert projection_workflow_id(
        first_reference.workspace_id, first_reference.event_id
    ) == projection_workflow_id(second_reference.workspace_id, second_reference.event_id)
    assert _serialize_input(first_reference) == _serialize_input(second_reference)


def test_intent_without_source_version_id_is_contract_invalid() -> None:
    intent = _leased_intent(source_version_id=None)

    with pytest.raises(ProjectionDispatchError) as outcome:
        source_ingestion_reference_for_intent(intent)

    assert outcome.value.error_code is ErrorCode.PROJECTION_INTENT_CONTRACT_INVALID
    assert outcome.value.is_retryable is False


# --- Duplicate execution and SDK error mapping --------------------------------


@dataclass
class RecordedStart:
    workflow: str
    arg: object
    id: str
    task_queue: str
    id_reuse_policy: object
    id_conflict_policy: object
    rpc_timeout: timedelta | None


@dataclass
class RecordedHandleCall:
    method: str
    workflow_id: str


class FakeWorkflowHandle:
    def __init__(self, client: FakeTemporalClient, workflow_id: str) -> None:
        self._client = client
        self._workflow_id = workflow_id

    async def describe(self) -> object:
        self._client.handle_calls.append(RecordedHandleCall("describe", self._workflow_id))
        if self._client.describe_error is not None:
            raise self._client.describe_error
        assert self._client.description is not None
        return self._client.description

    async def terminate(self, *_args: object, **_kwargs: object) -> None:
        self._client.handle_calls.append(RecordedHandleCall("terminate", self._workflow_id))
        raise AssertionError("terminate must never be called on an existing execution")

    async def cancel(self, *_args: object, **_kwargs: object) -> None:
        self._client.handle_calls.append(RecordedHandleCall("cancel", self._workflow_id))
        raise AssertionError("cancel must never be called on an existing execution")

    async def signal(self, *_args: object, **_kwargs: object) -> None:
        self._client.handle_calls.append(RecordedHandleCall("signal", self._workflow_id))
        raise AssertionError("signal must never be called on an existing execution")


class FakeTemporalClient:
    def __init__(
        self,
        *,
        start_error: BaseException | None = None,
        description: object | None = None,
        describe_error: BaseException | None = None,
    ) -> None:
        self.starts: list[RecordedStart] = []
        self.handle_calls: list[RecordedHandleCall] = []
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
    workflow_type: str = PROJECTION_WORKFLOW_TYPE_NAME
    task_queue: str = PROJECTION_WORKFLOW_TASK_QUEUE
    status: object = WorkflowExecutionStatus.RUNNING


def _reference() -> SourceIngestionReference:
    return source_ingestion_reference_for_intent(_leased_intent())


def _starter(client: FakeTemporalClient) -> TemporalProjectionWorkflowStarter:
    return TemporalProjectionWorkflowStarter(client)  # type: ignore[arg-type]


def test_start_uses_pinned_type_queue_policies_and_timeout() -> None:
    client = FakeTemporalClient()
    reference = _reference()

    result = asyncio.run(_starter(client).start_source_ingestion(reference))

    assert result is ProjectionWorkflowStartResult.STARTED
    assert len(client.starts) == 1
    recorded = client.starts[0]
    assert recorded.workflow == "SourceIngestionWorkflow"
    assert recorded.id == projection_workflow_id(reference.workspace_id, reference.event_id)
    assert recorded.task_queue == "source-ingestion"
    assert recorded.id_reuse_policy == WorkflowIDReusePolicy.REJECT_DUPLICATE
    assert recorded.id_conflict_policy == WorkflowIDConflictPolicy.USE_EXISTING
    assert recorded.rpc_timeout == timedelta(seconds=10)


def test_running_execution_resolves_as_existing_under_use_existing() -> None:
    client = FakeTemporalClient(
        start_error=WorkflowAlreadyStartedError("wid", PROJECTION_WORKFLOW_TYPE_NAME),
        description=_FakeDescription(status=WorkflowExecutionStatus.RUNNING),
    )

    result = asyncio.run(_starter(client).start_source_ingestion(_reference()))

    assert result is ProjectionWorkflowStartResult.EXISTING
    assert [call.method for call in client.handle_calls] == ["describe"]


def test_exact_closed_deterministic_execution_resolves_accepted_never_replaced() -> None:
    client = FakeTemporalClient(
        start_error=WorkflowAlreadyStartedError("wid", PROJECTION_WORKFLOW_TYPE_NAME),
        description=_FakeDescription(status=WorkflowExecutionStatus.COMPLETED),
    )

    result = asyncio.run(_starter(client).start_source_ingestion(_reference()))

    assert result is ProjectionWorkflowStartResult.EXISTING
    methods = [call.method for call in client.handle_calls]
    assert methods == ["describe"]
    assert "terminate" not in methods and "cancel" not in methods and "signal" not in methods
    assert len(client.starts) == 1


def test_continued_as_new_closed_execution_resolves_accepted() -> None:
    client = FakeTemporalClient(
        start_error=WorkflowAlreadyStartedError("wid", PROJECTION_WORKFLOW_TYPE_NAME),
        description=_FakeDescription(status=WorkflowExecutionStatus.CONTINUED_AS_NEW),
    )

    result = asyncio.run(_starter(client).start_source_ingestion(_reference()))

    assert result is ProjectionWorkflowStartResult.EXISTING


def test_closed_execution_of_another_type_is_terminal_integrity_failure() -> None:
    client = FakeTemporalClient(
        start_error=WorkflowAlreadyStartedError("wid", "SomeOtherWorkflow"),
        description=_FakeDescription(
            workflow_type="SomeOtherWorkflow", status=WorkflowExecutionStatus.COMPLETED
        ),
    )

    with pytest.raises(ProjectionDispatchError) as outcome:
        asyncio.run(_starter(client).start_source_ingestion(_reference()))

    assert outcome.value.error_code is ErrorCode.PROJECTION_INTENT_CONTRACT_INVALID
    assert outcome.value.is_retryable is False


def test_abnormally_closed_execution_is_terminal_integrity_failure() -> None:
    client = FakeTemporalClient(
        start_error=WorkflowAlreadyStartedError("wid", PROJECTION_WORKFLOW_TYPE_NAME),
        description=_FakeDescription(status=WorkflowExecutionStatus.FAILED),
    )

    with pytest.raises(ProjectionDispatchError) as outcome:
        asyncio.run(_starter(client).start_source_ingestion(_reference()))

    assert outcome.value.error_code is ErrorCode.PROJECTION_INTENT_CONTRACT_INVALID


def test_unavailable_rpc_error_maps_retryable_without_provider_text() -> None:
    client = FakeTemporalClient(
        start_error=RPCError("sentinel provider exception detail", RPCStatusCode.UNAVAILABLE, b"")
    )

    with pytest.raises(ProjectionDispatchError) as outcome:
        asyncio.run(_starter(client).start_source_ingestion(_reference()))

    error = outcome.value
    assert error.error_code is ErrorCode.PROJECTION_DISPATCH_UNAVAILABLE
    assert error.is_retryable is True
    rendered = repr(error) + str(error) + repr(error.to_safe_dict())
    for sentinel in _LEAKAGE_SENTINELS:
        assert sentinel not in rendered


def test_rpc_timeout_maps_retryable_without_provider_text() -> None:
    client = FakeTemporalClient(
        start_error=RPCError(
            "sentinel provider exception detail", RPCStatusCode.DEADLINE_EXCEEDED, b""
        )
    )

    with pytest.raises(ProjectionDispatchError) as outcome:
        asyncio.run(_starter(client).start_source_ingestion(_reference()))

    error = outcome.value
    assert error.error_code is ErrorCode.PROJECTION_DISPATCH_UNAVAILABLE
    rendered = repr(error) + str(error)
    assert "sentinel provider exception detail" not in rendered


def test_rpc_cancelled_error_maps_retryable() -> None:
    client = FakeTemporalClient(start_error=RPCTimeoutOrCancelledError())

    with pytest.raises(ProjectionDispatchError) as outcome:
        asyncio.run(_starter(client).start_source_ingestion(_reference()))

    assert outcome.value.error_code is ErrorCode.PROJECTION_DISPATCH_UNAVAILABLE
    assert outcome.value.is_retryable is True


def test_describe_failure_during_resolution_maps_retryable() -> None:
    client = FakeTemporalClient(
        start_error=WorkflowAlreadyStartedError("wid", PROJECTION_WORKFLOW_TYPE_NAME),
        describe_error=RPCError("boom", RPCStatusCode.UNAVAILABLE, b""),
    )

    with pytest.raises(ProjectionDispatchError) as outcome:
        asyncio.run(_starter(client).start_source_ingestion(_reference()))

    assert outcome.value.error_code is ErrorCode.PROJECTION_DISPATCH_UNAVAILABLE
    assert outcome.value.is_retryable is True


def test_task_queue_must_match_the_pinned_queue() -> None:
    client = FakeTemporalClient()

    with pytest.raises(ValueError, match="task queue"):
        TemporalProjectionWorkflowStarter(client, task_queue="other-queue")  # type: ignore[arg-type]
