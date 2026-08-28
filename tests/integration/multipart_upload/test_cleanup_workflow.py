"""Temporal integration contracts of the exact multipart cleanup (spec 6.4).

The disposable stack's Temporal service (``knowledge`` namespace) backs every
case with the real worker registration: the dispatcher starts the
deterministic ``multipart_cleanup/{batch_token}`` workflow, the registered
batch activity drives the real exact-cleanup executor over the durable
session store, and the expired session's provider upload is aborted and its
exact staging object removed — never any other session's resource. A failed
cleanup persists its closed reason token and the exact bounded next retry
through the claim's lease token. Cancelling a running sweep leaves every
obligated row durably claimed-or-pending — lease expiry returns it to the
next sweep — so a cancelled workflow can never become an untracked staging
resource. The serialized workflow input carries only the contract tag and
the opaque batch token, and the full history is scanned for staging-key,
provider-upload-ID and ETag sentinels. A lost start acknowledgement
converges on the single completed execution.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
import temporalio.api.history.v1
from temporalio.client import Client, WorkflowExecutionStatus
from tests.integration.multipart_upload.conftest import (
    MultipartStoreHarness,
    SeededMultipartOperation,
)
from workflow_worker.multipart_cleanup_workflow import (
    MULTIPART_CLEANUP_CONTRACT,
    MultipartCleanupDispatchRuntime,
    TemporalMultipartCleanupStarter,
    build_multipart_cleanup_executor,
    build_multipart_cleanup_process,
)

from personal_os.diagnostics.context import DiagnosticContext, TraceContext
from personal_os.diagnostics.trace_context import SpanId, TraceId
from personal_os.error_contracts.codes import ErrorCode
from personal_os.multipart_upload.cleanup import MultipartCleanupBatchInput
from personal_os.multipart_upload.errors import MultipartUploadError
from personal_os.multipart_upload.ports import MultipartProviderUploadId
from personal_os.multipart_upload.service import MultipartUploadService
from personal_os.small_file_sync.contracts import SmallFileDeviceContext
from postgresql_source_store.tables import multipart_parts, multipart_uploads

pytestmark = pytest.mark.local_stack

_TEMPORAL_NAMESPACE = "knowledge"
_CONVERGENCE_TIMEOUT_SECONDS = 90.0

_TRACE = TraceContext(
    trace_id=TraceId("0123456789abcdef0123456789abcdef"),
    remote_parent_span_id=None,
    local_span_id=SpanId("0123456789abcdef"),
    trace_flags=0,
)


def _context() -> DiagnosticContext:
    return DiagnosticContext(request_id=uuid4(), client_request_id=None, trace=_TRACE)


def _temporal_target() -> str:
    return f"127.0.0.1:{os.environ.get('TEMPORAL_GRPC_PORT', '7233')}"


@dataclass
class RecordingCleanupProvider:
    """The staging-provider seam recording every exact resource it touches.

    The gate holds one in-flight abort so a cancellation test can stop the
    sweep mid-row; ``abort_error`` injects the typed dependency outage.
    """

    aborted: list[tuple[str, str]] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    abort_error: Exception | None = None
    abort_gate: asyncio.Event = field(default_factory=asyncio.Event)
    gate_enabled: bool = False
    abort_entries: int = 0

    async def abort_upload(self, staging_key: str, upload_id: MultipartProviderUploadId) -> None:
        self.abort_entries += 1
        if self.gate_enabled:
            await self.abort_gate.wait()
        if self.abort_error is not None:
            raise self.abort_error
        self.aborted.append((staging_key, upload_id.value))

    async def delete_staging_object(self, staging_key: str) -> None:
        self.deleted.append(staging_key)

    async def create_upload(self, staging_key: str) -> MultipartProviderUploadId:
        raise AssertionError("cleanup never creates provider work")

    async def presign_part(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("cleanup never presigns a part URL")

    async def list_parts(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("cleanup never lists parts")

    async def complete_upload(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("cleanup never completes a staging upload")


class CleanupWorkflowHarness:
    """One test's store harness, provider, executor, worker and starter."""

    def __init__(
        self,
        base: MultipartStoreHarness,
        provider: RecordingCleanupProvider,
        executor: MultipartUploadService,
        process: Any,
        client: Client,
    ) -> None:
        self.base = base
        self.provider = provider
        self.executor = executor
        self.process = process
        self.client = client

    def starter(self) -> TemporalMultipartCleanupStarter:
        return TemporalMultipartCleanupStarter(self.client)

    def dispatcher(self, batch_token: UUID) -> MultipartCleanupDispatchRuntime:
        return MultipartCleanupDispatchRuntime(
            starter=self.starter(), batch_token_generator=lambda: batch_token
        )

    async def seed_identity_session(
        self, *, hours_expired: float = 25.0
    ) -> tuple[SeededMultipartOperation, SmallFileDeviceContext]:
        device = await self.base.seed_device()
        seeded = await self.base.seed_operation(device, now=self.base.clock.now)
        await self.base.reserve(seeded, device)
        self.base.clock.advance(timedelta(hours=hours_expired))
        return seeded, device

    async def run_sweep(self, batch_token: UUID) -> Any:
        dispatcher = self.dispatcher(batch_token)
        await dispatcher.dispatch_cleanup_once()
        handle = self.client.get_workflow_handle(f"multipart_cleanup/{batch_token}")
        return await asyncio.wait_for(handle.result(), timeout=_CONVERGENCE_TIMEOUT_SECONDS)

    async def cleanup_row(self, seeded: SeededMultipartOperation) -> dict[str, Any]:
        async with self.base.engine.connect() as connection:
            result = await connection.execute(
                sa.select(
                    multipart_uploads.c.state,
                    multipart_uploads.c.cleanup_state,
                    multipart_uploads.c.cleanup_attempt_count,
                    multipart_uploads.c.cleanup_next_retry_at,
                    multipart_uploads.c.cleanup_reason_code,
                ).where(multipart_uploads.c.staging_key == seeded.staging_key)
            )
            rows = [dict(row) for row in result.mappings()]
        assert len(rows) == 1, "the seeded staging key addresses exactly one session row"
        return rows[0]


@pytest_asyncio.fixture
async def cleanup_workflow(
    multipart_store_harness: MultipartStoreHarness,
) -> Any:
    # The cleanup claim is global over due obligations, so each test starts
    # from an empty obligation table: leftovers of earlier cases (their
    # failed rows become due again under a fresh clock) must never leak
    # into this test's exact provider-touch assertions.
    async with multipart_store_harness.engine.begin() as connection:
        await connection.execute(multipart_parts.delete())
        await connection.execute(multipart_uploads.delete())
    provider = RecordingCleanupProvider()
    executor = build_multipart_cleanup_executor(
        session_store=multipart_store_harness.store,
        staging_provider=provider,  # type: ignore[arg-type]
        clock=multipart_store_harness.clock.now,
    )
    client = await Client.connect(_temporal_target(), namespace=_TEMPORAL_NAMESPACE)
    process = build_multipart_cleanup_process(executor=executor, temporal_client=client)
    harness = CleanupWorkflowHarness(multipart_store_harness, provider, executor, process, client)
    async with process.worker:
        yield harness


@pytest.mark.asyncio
async def test_expired_session_cleans_only_its_own_provider_resources(
    cleanup_workflow: CleanupWorkflowHarness,
) -> None:
    expired, _device = await cleanup_workflow.seed_identity_session()
    fresh_device = await cleanup_workflow.base.seed_device()
    fresh_operation = await cleanup_workflow.base.seed_operation(
        fresh_device, now=cleanup_workflow.base.clock.now
    )
    await cleanup_workflow.base.reserve(fresh_operation, fresh_device)

    result = await cleanup_workflow.run_sweep(UUID("018f47a0-7b00-7000-8000-000000000301"))

    assert result == "completed"
    assert cleanup_workflow.provider.aborted == [
        (expired.staging_key, expired.provider_upload_id.value)
    ]
    assert cleanup_workflow.provider.deleted == [expired.staging_key]
    expired_row = await cleanup_workflow.cleanup_row(expired)
    assert expired_row["state"] == "cleaned"
    assert expired_row["cleanup_state"] == "succeeded"
    fresh_row = await cleanup_workflow.cleanup_row(fresh_operation)
    assert fresh_row["state"] == "created"
    assert fresh_row["cleanup_state"] == "none"


@pytest.mark.asyncio
async def test_cleanup_failure_persists_closed_reason_and_next_retry(
    cleanup_workflow: CleanupWorkflowHarness,
) -> None:
    expired, _device = await cleanup_workflow.seed_identity_session()
    cleanup_workflow.provider.abort_error = MultipartUploadError(
        ErrorCode.MULTIPART_DEPENDENCY_UNAVAILABLE
    )

    result = await cleanup_workflow.run_sweep(UUID("018f47a0-7b00-7000-8000-000000000302"))

    assert result == "completed"
    row = await cleanup_workflow.cleanup_row(expired)
    assert row["state"] == "cleanup_pending"
    assert row["cleanup_state"] == "failed"
    assert row["cleanup_reason_code"] == "multipart_dependency_unavailable"
    # The expiry strike itself is attempt 1; the first failed cleanup is 2.
    assert row["cleanup_attempt_count"] == 2
    assert row["cleanup_next_retry_at"] is not None
    assert cleanup_workflow.provider.aborted == []


@pytest.mark.asyncio
async def test_cancelled_sweep_never_loses_the_cleanup_obligation(
    cleanup_workflow: CleanupWorkflowHarness,
) -> None:
    expired, _device = await cleanup_workflow.seed_identity_session()
    provider = cleanup_workflow.provider
    provider.gate_enabled = True
    batch_token = UUID("018f47a0-7b00-7000-8000-000000000303")

    dispatcher = cleanup_workflow.dispatcher(batch_token)
    await dispatcher.dispatch_cleanup_once()
    handle = cleanup_workflow.client.get_workflow_handle(f"multipart_cleanup/{batch_token}")
    deadline = asyncio.get_running_loop().time() + _CONVERGENCE_TIMEOUT_SECONDS
    while provider.abort_entries == 0:
        assert asyncio.get_running_loop().time() < deadline, "activity never reached the row"
        await asyncio.sleep(0.1)
    await handle.cancel()

    deadline = asyncio.get_running_loop().time() + _CONVERGENCE_TIMEOUT_SECONDS
    while True:
        description = await handle.describe()
        if description.status is not WorkflowExecutionStatus.RUNNING:
            break
        assert asyncio.get_running_loop().time() < deadline, "workflow never terminated"
        await asyncio.sleep(0.2)
    assert description.status in (
        WorkflowExecutionStatus.CANCELED,
        WorkflowExecutionStatus.COMPLETED,
    )

    # The obligation is still durable: the claimed row keeps its pending
    # obligation (the in-flight outcome never landed) and no other state.
    row = await cleanup_workflow.cleanup_row(expired)
    assert row["state"] == "cleanup_pending"
    assert row["cleanup_state"] == "pending"

    # Lease expiry returns the row to the next sweep, which finishes the
    # exact cleanup: no untracked staging resource survives the cancellation.
    provider.gate_enabled = False
    provider.abort_gate.set()
    cleanup_workflow.base.clock.advance(timedelta(minutes=16))
    second_result = await cleanup_workflow.run_sweep(UUID("018f47a0-7b00-7000-8000-000000000304"))
    assert second_result == "completed"
    final_row = await cleanup_workflow.cleanup_row(expired)
    assert final_row["state"] == "cleaned"
    assert final_row["cleanup_state"] == "succeeded"
    # Every provider touch addresses exactly this session's own identities.
    # The cancelled sweep's in-flight call may finish after the gate opens —
    # its late lease-fenced record fails closed while the idempotent exact
    # abort/delete is harmless — so repeats are admitted, foreign keys are
    # not.
    assert set(cleanup_workflow.provider.deleted) == {expired.staging_key}
    assert set(cleanup_workflow.provider.aborted) == {
        (expired.staging_key, expired.provider_upload_id.value)
    }


@pytest.mark.asyncio
async def test_workflow_history_holds_only_opaque_values(
    cleanup_workflow: CleanupWorkflowHarness,
) -> None:
    expired, _device = await cleanup_workflow.seed_identity_session()
    batch_token = UUID("018f47a0-7b00-7000-8000-000000000305")

    await cleanup_workflow.run_sweep(batch_token)

    handle = cleanup_workflow.client.get_workflow_handle(f"multipart_cleanup/{batch_token}")
    history = await handle.fetch_history()
    serialized_history = temporalio.api.history.v1.History(
        events=history.events
    ).SerializeToString()
    started_event = next(
        event
        for event in history.events
        if event.HasField("workflow_execution_started_event_attributes")
    )
    (input_payload,) = started_event.workflow_execution_started_event_attributes.input.payloads
    decoded_input = json.loads(input_payload.data)
    assert decoded_input == {
        "contract": MULTIPART_CLEANUP_CONTRACT,
        "batch_token": str(batch_token),
    }
    activity_inputs: list[dict[str, Any]] = []
    for event in history.events:
        if event.HasField("activity_task_scheduled_event_attributes"):
            payload = event.activity_task_scheduled_event_attributes.input.payloads[0]
            activity_inputs.append(json.loads(payload.data))
    assert activity_inputs
    for reference in activity_inputs:
        assert set(reference) == {"contract", "batch_token", "batch_limit"}
        assert reference["batch_token"] == str(batch_token)
    assert expired.staging_key.encode() not in serialized_history
    assert expired.provider_upload_id.value.encode() not in serialized_history


@pytest.mark.asyncio
async def test_lost_start_acknowledgement_converges_on_one_execution(
    cleanup_workflow: CleanupWorkflowHarness,
) -> None:
    await cleanup_workflow.seed_identity_session()
    batch_token = UUID("018f47a0-7b00-7000-8000-000000000306")
    workflow_id = f"multipart_cleanup/{batch_token}"

    await cleanup_workflow.run_sweep(batch_token)

    outcome = await cleanup_workflow.starter().start_cleanup(
        MultipartCleanupBatchInput(contract=MULTIPART_CLEANUP_CONTRACT, batch_token=batch_token)
    )
    assert outcome.value == "existing"

    executions = [
        execution
        async for execution in cleanup_workflow.client.list_workflows(f"WorkflowId='{workflow_id}'")
    ]
    assert len(executions) == 1
