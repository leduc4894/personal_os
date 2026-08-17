"""Temporal integration contracts of the asynchronous preview (spec 10/21).

The disposable stack's Temporal service (``knowledge`` namespace) backs every
case with the real worker registration: the leased dispatcher starts the
deterministic ``exclusion-policy-preview/{workspace_id}/{policy_preview_id}``
workflow, the registered single activity executes the store's repeatable-read
snapshot against the same database, and the durable row reaches ``ready``
with its complete evidence while the workflow returns the closed
``ready`` outcome. The serialized workflow input carries only the contract
tag, the two opaque UUIDs and the checkpoint; the full history is scanned
for the seeded title, locator-shaped and operand sentinels. A lost start
acknowledgement converges: starting the same deterministic workflow again
resolves the completed execution as ``existing`` without a second run.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
import temporalio.api.history.v1
from temporalio.client import Client, WorkflowExecutionStatus
from tests.integration.exclusion_policy.conftest import PolicyMigrationHarness
from workflow_worker.policy_preview_workflow import (
    POLICY_PREVIEW_REFERENCE_CONTRACT,
    PolicyPreviewReference,
    PolicyPreviewStartOutcome,
    TemporalPolicyPreviewStarter,
    policy_preview_workflow_id,
)
from workflow_worker.policy_workflow_runtime import (
    PolicyPreviewDispatchRuntime,
    build_policy_preview_process,
)

from personal_os.diagnostics.context import DiagnosticContext, TraceContext
from personal_os.diagnostics.trace_context import SpanId, TraceId
from personal_os.exclusion_policy.ports import PolicyActor, PolicyActorKind
from personal_os.exclusion_policy.previews import PreviewStatus
from postgresql_source_store.policy_previews import PostgresqlPolicyPreviewStore
from postgresql_source_store.tables import (
    policy_preview_results,
    policy_previews,
    sources,
    sync_events,
)

pytestmark = pytest.mark.local_stack

_TEMPORAL_NAMESPACE = "knowledge"
_CONVERGENCE_TIMEOUT_SECONDS = 60.0

_SENTINEL_TITLE_FRAGMENT = "Temporal Preview Sentinel"

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


class TemporalPreviewHarness:
    """One test's engine, store, starter, dispatcher and inline worker."""

    def __init__(self, base: PolicyMigrationHarness, process: Any, client: Client) -> None:
        self.base = base
        self.engine = base.engine
        self.process = process
        self.client = client
        self.store: PostgresqlPolicyPreviewStore = (
            process.dispatch_runtime._preview_store
        )
        self.runtime: PolicyPreviewDispatchRuntime = process.dispatch_runtime

    async def reset_previews(self) -> None:
        async with self.engine.begin() as connection:
            # Audit rows are append-only; preview-requested history stays.
            await connection.execute(policy_preview_results.delete())
            await connection.execute(policy_previews.delete())
            await connection.execute(
                sa.delete(sources).where(
                    sources.c.workspace_id == self.base.stack.workspace_id,
                    sources.c.source_id.not_in(
                        sa.select(sync_events.c.source_id).where(
                            sync_events.c.workspace_id == self.base.stack.workspace_id
                        )
                    ),
                )
            )

    async def seed_sources(self, count: int, *, source_type: str = "markdown") -> int:
        async with self.engine.begin() as connection:
            await connection.execute(
                sa.insert(sources).values(
                    [
                        {
                            "source_id": uuid4(),
                            "workspace_id": self.base.stack.workspace_id,
                            "source_type": source_type,
                            "title": f"{_SENTINEL_TITLE_FRAGMENT} {uuid4().hex[:8]}",
                        }
                        for _ in range(count)
                    ]
                )
            )
            total = await connection.execute(
                sa.select(sa.func.count())
                .select_from(sources)
                .where(sources.c.workspace_id == self.base.stack.workspace_id)
            )
            return int(total.scalar_one())

    async def request_preview(self) -> UUID:
        record = await self.store.request_preview(
            self.base.stack.workspace_id,
            PolicyActor(
                actor_kind=PolicyActorKind.USER, user_id=self.base.stack.owner_user_id
            ),
            _context(),
        )
        return record.policy_preview_id

    async def dispatch_once(self) -> int:
        return await self.runtime.dispatch_pending_previews_once()

    async def wait_until_ready(self, preview_id: UUID) -> None:
        deadline = asyncio.get_running_loop().time() + _CONVERGENCE_TIMEOUT_SECONDS
        while asyncio.get_running_loop().time() < deadline:
            record = await self.store.get_preview(preview_id, _context())
            if record.status is PreviewStatus.READY:
                return
            await asyncio.sleep(0.25)
        pytest.fail("preview did not reach ready before the bounded deadline")


@pytest_asyncio.fixture
async def temporal_preview(
    policy_migration_harness: PolicyMigrationHarness,
) -> Any:
    engine = policy_migration_harness.engine
    client = await Client.connect(_temporal_target(), namespace=_TEMPORAL_NAMESPACE)
    process = build_policy_preview_process(engine=engine, temporal_client=client)
    harness = TemporalPreviewHarness(policy_migration_harness, process, client)
    await harness.reset_previews()
    async with process.worker:
        yield harness


@pytest.mark.asyncio
async def test_dispatched_workflow_executes_the_snapshot_and_reaches_ready(
    temporal_preview: TemporalPreviewHarness,
) -> None:
    total_sources = await temporal_preview.seed_sources(6, source_type="markdown")
    preview_id = await temporal_preview.request_preview()

    assert await temporal_preview.dispatch_once() == 1
    await temporal_preview.wait_until_ready(preview_id)

    record = await temporal_preview.store.get_preview(preview_id, _context())
    assert record.status is PreviewStatus.READY
    assert record.newly_allowed_count == total_sources
    assert await temporal_preview.store.count_results(preview_id) == total_sources
    workflow_id = policy_preview_workflow_id(
        temporal_preview.base.stack.workspace_id, preview_id
    )
    description = await temporal_preview.client.get_workflow_handle(workflow_id).describe()
    assert description.status is WorkflowExecutionStatus.COMPLETED


@pytest.mark.asyncio
async def test_workflow_input_and_history_stay_closed(
    temporal_preview: TemporalPreviewHarness,
) -> None:
    await temporal_preview.seed_sources(2)
    preview_id = await temporal_preview.request_preview()
    workflow_id = policy_preview_workflow_id(
        temporal_preview.base.stack.workspace_id, preview_id
    )

    await temporal_preview.dispatch_once()
    await temporal_preview.wait_until_ready(preview_id)

    handle = temporal_preview.client.get_workflow_handle(workflow_id)
    # The durable row is ready once the activity commits; the workflow run
    # itself closes a moment later.
    result = await asyncio.wait_for(handle.result(), timeout=_CONVERGENCE_TIMEOUT_SECONDS)
    assert result == "ready"
    description = await handle.describe()
    assert description.workflow_type == "PolicyPreviewWorkflow"
    assert description.status is WorkflowExecutionStatus.COMPLETED
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
    async with temporal_preview.engine.connect() as connection:
        checkpoint = await connection.execute(
            sa.select(sa.func.max(sync_events.c.event_sequence)).where(
                sync_events.c.workspace_id == temporal_preview.base.stack.workspace_id
            )
        )
        expected_checkpoint = int(checkpoint.scalar_one() or 0)
    assert decoded_input == {
        "contract": POLICY_PREVIEW_REFERENCE_CONTRACT,
        "workspace_id": str(temporal_preview.base.stack.workspace_id),
        "policy_preview_id": str(preview_id),
        "source_event_checkpoint": expected_checkpoint,
    }
    assert _SENTINEL_TITLE_FRAGMENT.encode() not in serialized_history
    assert _SENTINEL_TITLE_FRAGMENT.encode() not in input_payload.data


@pytest.mark.asyncio
async def test_lost_start_acknowledgement_converges_on_one_execution(
    temporal_preview: TemporalPreviewHarness,
) -> None:
    await temporal_preview.seed_sources(1)
    preview_id = await temporal_preview.request_preview()
    workflow_id = policy_preview_workflow_id(
        temporal_preview.base.stack.workspace_id, preview_id
    )

    assert await temporal_preview.dispatch_once() == 1
    await temporal_preview.wait_until_ready(preview_id)

    # A lost start acknowledgement re-dispatches the same deterministic
    # identity: the closed completed execution resolves as existing — never
    # a second run.
    ready_record = await temporal_preview.store.get_preview(preview_id, _context())
    starter = TemporalPolicyPreviewStarter(temporal_preview.client)
    outcome = await starter.start_policy_preview(
        PolicyPreviewReference(
            contract=POLICY_PREVIEW_REFERENCE_CONTRACT,
            workspace_id=temporal_preview.base.stack.workspace_id,
            policy_preview_id=preview_id,
            source_event_checkpoint=ready_record.source_checkpoint_event_sequence,
        )
    )
    assert outcome is PolicyPreviewStartOutcome.EXISTING

    executions = [
        execution
        async for execution in temporal_preview.client.list_workflows(
            f"WorkflowId='{workflow_id}'"
        )
    ]
    assert len(executions) == 1
    record = await temporal_preview.store.get_preview(preview_id, _context())
    assert record.status is PreviewStatus.READY
