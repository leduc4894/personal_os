"""Temporal Server 1.31.2 integration contracts for projection dispatch.

The disposable local stack (already provisioning Temporal with the
``knowledge`` namespace) backs every case: Temporal accepts a deterministic
start with no workflow poller registered; two intents of one event produce
exactly one workflow run and both rows reach ``dispatched``; a crash before
start loses nothing because lease expiry reclaims and re-dispatches; a crash
after Temporal accepted the start but before the database acknowledgement
resolves the existing execution under ``USE_EXISTING`` and acknowledges
dispatched, while the stale holder's fenced transition affects zero rows; and
a closed execution rejects the duplicate run, resolving as terminal integrity
failure instead of terminating or replacing anything. The serialized workflow
input and history are scanned for the seeded title, object key, content hash
and idempotency key sentinels.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
import temporalio.api.history.v1
from temporalio.client import Client, WorkflowExecutionStatus
from tests.integration.projection_dispatch.conftest import (
    ProjectionDispatchStack,
    RecordingProjectionDiagnostics,
    SeededIntent,
    SeededWorkspace,
)
from workflow_worker.projection_dispatch_runtime import ProjectionDispatchRuntime
from workflow_worker.projection_workflow_starter import (
    SOURCE_INGESTION_REFERENCE_CONTRACT,
    ProjectionWorkflowStartResult,
    SourceIngestionReference,
    TemporalProjectionWorkflowStarter,
    projection_workflow_id,
    source_ingestion_reference_for_intent,
)

from personal_os.diagnostics.events import EventName, SafeToken
from personal_os.sources.metrics import InMemorySourcePublicationMetrics
from personal_os.sources.projection_dispatch import PROJECTION_CLAIM_BATCH_LIMIT
from postgresql_source_store.engine import create_source_store_engine, dispose_source_store_engine
from postgresql_source_store.projection_intents import PostgresqlProjectionIntentStore
from postgresql_source_store.tables import (
    content_objects,
    projection_intents,
    source_versions,
    sources,
    sync_events,
    users,
    workspaces,
)

pytestmark = pytest.mark.local_stack

_TEMPORAL_NAMESPACE = "knowledge"
_DISPATCH_CONVERGENCE_TIMEOUT_SECONDS = 30.0


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _temporal_target() -> str:
    return f"127.0.0.1:{os.environ.get('TEMPORAL_GRPC_PORT', '7233')}"


class TemporalDispatchHarness:
    """One test's engine, fenced store, Temporal starter and dispatch runtime."""

    def __init__(
        self,
        stack: ProjectionDispatchStack,
        engine: Any,
        store: PostgresqlProjectionIntentStore,
        starter: TemporalProjectionWorkflowStarter,
        client: Client,
        runtime: ProjectionDispatchRuntime,
        diagnostics: RecordingProjectionDiagnostics,
        metrics: InMemorySourcePublicationMetrics,
    ) -> None:
        self._stack = stack
        self._engine = engine
        self.store = store
        self.starter = starter
        self.client = client
        self.runtime = runtime
        self.diagnostics = diagnostics
        self.metrics = metrics

    async def seed_workspace(self) -> SeededWorkspace:
        """Seed the owner user and workspace graph for one test."""
        owner_user_id = uuid4()
        workspace_id = uuid4()
        nonce = uuid4().hex
        async with self._engine.begin() as connection:
            await connection.execute(
                sa.insert(users).values(
                    user_id=owner_user_id,
                    username=f"owner-{nonce}",
                    display_name="Temporal Dispatch Owner",
                )
            )
            await connection.execute(
                sa.insert(workspaces).values(
                    workspace_id=workspace_id,
                    owner_user_id=owner_user_id,
                    workspace_key=f"ws-{nonce[:12]}",
                    display_name="Temporal Dispatch Workspace",
                )
            )
        return SeededWorkspace(owner_user_id=owner_user_id, workspace_id=workspace_id)

    async def seed_due_intent(
        self, workspace: SeededWorkspace, *, projection_kind: str = "qdrant"
    ) -> SeededIntent:
        """Seed one due pending intent plus its full canonical graph."""
        import hashlib
        from uuid import uuid4

        source_id = uuid4()
        event_id = uuid4()
        source_version_id = uuid4()
        content_object_id = uuid4()
        projection_intent_id = uuid4()
        nonce = uuid4().hex
        content_hash = hashlib.sha256(nonce.encode("utf-8")).hexdigest()
        async with self._engine.begin() as connection:
            await connection.execute(
                sa.insert(content_objects).values(
                    content_object_id=content_object_id,
                    content_hash=content_hash,
                    object_key=f"objects/sha256/{content_hash[:2]}/{content_hash[2:4]}/{content_hash}",
                    byte_size=len(nonce),
                    media_type="text/markdown",
                    verified_at=sa.text("CURRENT_TIMESTAMP - interval '1 second'"),
                )
            )
            await connection.execute(
                sa.insert(sources).values(
                    source_id=source_id,
                    workspace_id=workspace.workspace_id,
                    source_type="markdown",
                    title=f"Temporal Source {nonce[:8]}",
                )
            )
            await connection.execute(
                sa.insert(source_versions).values(
                    source_version_id=source_version_id,
                    workspace_id=workspace.workspace_id,
                    source_id=source_id,
                    content_object_id=content_object_id,
                    content_version=1,
                    author_kind="user",
                    author_id=workspace.owner_user_id,
                )
            )
            await connection.execute(
                sa.insert(sync_events).values(
                    event_id=event_id,
                    workspace_id=workspace.workspace_id,
                    source_id=source_id,
                    committed_version_id=source_version_id,
                    idempotency_key=f"temporal-{nonce}",
                    request_fingerprint=content_hash,
                    event_type="create",
                )
            )
            await connection.execute(
                sa.insert(projection_intents).values(
                    projection_intent_id=projection_intent_id,
                    workspace_id=workspace.workspace_id,
                    event_id=event_id,
                    source_id=source_id,
                    source_version_id=source_version_id,
                    projection_kind=projection_kind,
                    operation="upsert",
                    status="pending",
                    attempt_count=0,
                    available_at=sa.text("CURRENT_TIMESTAMP - interval '5 seconds'"),
                    created_at=sa.text("CURRENT_TIMESTAMP - interval '5 seconds'"),
                )
            )
        return SeededIntent(
            projection_intent_id=projection_intent_id,
            workspace_id=workspace.workspace_id,
            source_id=source_id,
            event_id=event_id,
            source_version_id=source_version_id,
        )

    async def duplicate_intent_for_event(
        self, seeded: SeededIntent, *, projection_kind: str = "neo4j"
    ) -> UUID:
        """Insert a second intent for the same event/version graph."""
        from uuid import uuid4

        duplicate_intent_id = uuid4()
        async with self._engine.begin() as connection:
            await connection.execute(
                sa.insert(projection_intents).values(
                    projection_intent_id=duplicate_intent_id,
                    workspace_id=seeded.workspace_id,
                    event_id=seeded.event_id,
                    source_id=seeded.source_id,
                    source_version_id=seeded.source_version_id,
                    projection_kind=projection_kind,
                    operation="upsert",
                    status="pending",
                    attempt_count=0,
                    available_at=sa.text("CURRENT_TIMESTAMP - interval '5 seconds'"),
                    created_at=sa.text("CURRENT_TIMESTAMP - interval '5 seconds'"),
                )
            )
        return duplicate_intent_id

    async def expire_lease(self, projection_intent_id: UUID) -> None:
        """Backdate one leased row's expiry beyond the reclaim horizon."""
        async with self._engine.begin() as connection:
            await connection.execute(
                sa.update(projection_intents)
                .values(
                    created_at=sa.text("CURRENT_TIMESTAMP - interval '80 seconds'"),
                    available_at=sa.text("CURRENT_TIMESTAMP - interval '80 seconds'"),
                    leased_until=sa.text("CURRENT_TIMESTAMP - interval '5 seconds'"),
                    updated_at=sa.text("CURRENT_TIMESTAMP - interval '70 seconds'"),
                )
                .where(projection_intents.c.projection_intent_id == projection_intent_id)
            )

    async def fetch_intent(self, projection_intent_id: UUID) -> dict[str, Any]:
        statement = sa.select(
            projection_intents.c.status,
            projection_intents.c.attempt_count,
            projection_intents.c.lease_token,
            projection_intents.c.dispatched_at,
            projection_intents.c.last_error_code,
        ).where(projection_intents.c.projection_intent_id == projection_intent_id)
        async with self._engine.connect() as connection:
            row = (await connection.execute(statement)).one()
        return dict(row._mapping)

    async def sensitive_sentinels(self, seeded: SeededIntent) -> list[str]:
        """The seeded title, object key, content hash and idempotency key."""
        title_statement = sa.select(sources.c.title).where(sources.c.source_id == seeded.source_id)
        graph_statement = (
            sa.select(
                content_objects.c.object_key,
                content_objects.c.content_hash,
                sync_events.c.idempotency_key,
            )
            .select_from(source_versions)
            .join(
                content_objects,
                content_objects.c.content_object_id == source_versions.c.content_object_id,
            )
            .join(sync_events, sync_events.c.event_id == seeded.event_id)
            .where(source_versions.c.source_version_id == seeded.source_version_id)
        )
        async with self._engine.connect() as connection:
            title = (await connection.execute(title_statement)).scalar_one()
            graph_row = (await connection.execute(graph_statement)).one()
        return [
            str(title),
            str(graph_row.object_key),
            str(graph_row.content_hash),
            str(graph_row.idempotency_key),
        ]

    async def claim(self, seeded: SeededIntent) -> Any:
        """Claim due intents and return this seed's leased view."""
        claimed = await self.store.claim_batch(_utc_now(), PROJECTION_CLAIM_BATCH_LIMIT)
        matching = [
            intent
            for intent in claimed
            if intent.projection_intent_id == seeded.projection_intent_id
        ]
        assert len(matching) == 1
        return matching[0]

    async def dispatch_until(
        self,
        predicate: Callable[[], Awaitable[bool]],
        *,
        timeout_seconds: float = _DISPATCH_CONVERGENCE_TIMEOUT_SECONDS,
    ) -> None:
        """Run bounded dispatch cycles until the async predicate holds."""
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            await self.runtime.dispatch_pending_intents_once()
            if await predicate():
                return
            await asyncio.sleep(0.25)
        pytest.fail("dispatch cycles did not converge before the bounded deadline")

    async def workflow_run_count(self, workflow_id: str) -> int:
        executions = [
            execution
            async for execution in self.client.list_workflows(f"WorkflowId='{workflow_id}'")
        ]
        return len(executions)


@pytest_asyncio.fixture
async def temporal_dispatch(
    projection_dispatch_stack: ProjectionDispatchStack,
) -> Any:
    from uuid import uuid4

    engine = create_source_store_engine(
        projection_dispatch_stack.settings, projection_dispatch_stack.password
    )
    diagnostics = RecordingProjectionDiagnostics()
    metrics = InMemorySourcePublicationMetrics()
    store = PostgresqlProjectionIntentStore(
        engine,
        lease_token_generator=uuid4,
        diagnostics=diagnostics,
        metrics=metrics,
    )
    client = await Client.connect(_temporal_target(), namespace=_TEMPORAL_NAMESPACE)
    starter = TemporalProjectionWorkflowStarter(client)
    runtime = ProjectionDispatchRuntime(
        store=store,
        starter=starter,
        clock=_utc_now,
        diagnostics=diagnostics,
        metrics=metrics,
    )
    harness = TemporalDispatchHarness(
        projection_dispatch_stack,
        engine,
        store,
        starter,
        client,
        runtime,
        diagnostics,
        metrics,
    )
    try:
        async with engine.begin() as connection:
            await connection.execute(projection_intents.delete())
        yield harness
    finally:
        await dispose_source_store_engine(engine)


@pytest.mark.asyncio
async def test_temporal_accepts_start_without_poller_and_input_stays_closed(
    temporal_dispatch: TemporalDispatchHarness,
) -> None:
    workspace = await temporal_dispatch.seed_workspace()
    seeded = await temporal_dispatch.seed_due_intent(workspace)

    reference = SourceIngestionReference(
        contract=SOURCE_INGESTION_REFERENCE_CONTRACT,
        workspace_id=seeded.workspace_id,
        event_id=seeded.event_id,
        source_id=seeded.source_id,
        source_version_id=seeded.source_version_id,
    )
    workflow_id = projection_workflow_id(reference.workspace_id, reference.event_id)

    result = await temporal_dispatch.starter.start_source_ingestion(reference)

    assert result is ProjectionWorkflowStartResult.STARTED
    handle = temporal_dispatch.client.get_workflow_handle(workflow_id)
    description = await handle.describe()
    # No workflow poller is registered: the durable start waits on the queue.
    assert description.status is WorkflowExecutionStatus.RUNNING
    assert description.workflow_type == "SourceIngestionWorkflow"
    assert description.task_queue == "source-ingestion"

    history = await handle.fetch_history()
    full_history = temporalio.api.history.v1.History(events=history.events)
    serialized_history = full_history.SerializeToString()
    started_event = next(
        event
        for event in history.events
        if event.HasField("workflow_execution_started_event_attributes")
    )
    input_payloads = started_event.workflow_execution_started_event_attributes.input
    assert len(input_payloads.payloads) == 1
    input_payload = input_payloads.payloads[0]
    decoded_input = json.loads(input_payload.data)
    assert decoded_input == {
        "contract": SOURCE_INGESTION_REFERENCE_CONTRACT,
        "workspace_id": str(seeded.workspace_id),
        "event_id": str(seeded.event_id),
        "source_id": str(seeded.source_id),
        "source_version_id": str(seeded.source_version_id),
    }
    for sentinel in await temporal_dispatch.sensitive_sentinels(seeded):
        assert sentinel.encode() not in serialized_history, (
            "sensitive sentinel leaked into Temporal history"
        )
        assert sentinel.encode() not in input_payload.data


@pytest.mark.asyncio
async def test_two_intents_for_one_event_dispatch_once_into_one_workflow_run(
    temporal_dispatch: TemporalDispatchHarness,
) -> None:
    workspace = await temporal_dispatch.seed_workspace()
    seeded = await temporal_dispatch.seed_due_intent(workspace, projection_kind="qdrant")
    duplicate_intent_id = await temporal_dispatch.duplicate_intent_for_event(
        seeded, projection_kind="neo4j"
    )
    workflow_id = projection_workflow_id(seeded.workspace_id, seeded.event_id)

    await temporal_dispatch.runtime.dispatch_pending_intents_once()

    first = await temporal_dispatch.fetch_intent(seeded.projection_intent_id)
    second = await temporal_dispatch.fetch_intent(duplicate_intent_id)
    assert first["status"] == "dispatched"
    assert second["status"] == "dispatched"
    assert first["dispatched_at"] is not None
    assert second["dispatched_at"] is not None
    assert await temporal_dispatch.workflow_run_count(workflow_id) == 1


@pytest.mark.asyncio
async def test_crash_before_start_is_reclaimed_and_dispatched(
    temporal_dispatch: TemporalDispatchHarness,
) -> None:
    workspace = await temporal_dispatch.seed_workspace()
    seeded = await temporal_dispatch.seed_due_intent(workspace)
    workflow_id = projection_workflow_id(seeded.workspace_id, seeded.event_id)
    # The claimer crashed after claiming but before any Temporal start.
    leased = await temporal_dispatch.claim(seeded)
    await temporal_dispatch.expire_lease(seeded.projection_intent_id)
    assert leased.lease_token is not None

    async def dispatched() -> bool:
        row = await temporal_dispatch.fetch_intent(seeded.projection_intent_id)
        return row["status"] == "dispatched"

    await temporal_dispatch.dispatch_until(dispatched)

    row = await temporal_dispatch.fetch_intent(seeded.projection_intent_id)
    assert row["status"] == "dispatched"
    # One lease expiry plus one known dispatched outcome.
    assert row["attempt_count"] == 2
    assert await temporal_dispatch.workflow_run_count(workflow_id) == 1


@pytest.mark.asyncio
async def test_crash_after_temporal_accept_before_ack_resolves_existing_execution(
    temporal_dispatch: TemporalDispatchHarness,
) -> None:
    workspace = await temporal_dispatch.seed_workspace()
    seeded = await temporal_dispatch.seed_due_intent(workspace)
    workflow_id = projection_workflow_id(seeded.workspace_id, seeded.event_id)
    leased = await temporal_dispatch.claim(seeded)
    # Temporal accepted the start, then the claimer crashed before the ack.
    started = await temporal_dispatch.starter.start_source_ingestion(
        source_ingestion_reference_for_intent(leased)
    )
    assert started is ProjectionWorkflowStartResult.STARTED
    await temporal_dispatch.expire_lease(seeded.projection_intent_id)

    async def dispatched() -> bool:
        row = await temporal_dispatch.fetch_intent(seeded.projection_intent_id)
        return row["status"] == "dispatched"

    await temporal_dispatch.dispatch_until(dispatched)

    row = await temporal_dispatch.fetch_intent(seeded.projection_intent_id)
    assert row["status"] == "dispatched"
    assert row["lease_token"] is None
    # The stale holder's fenced acknowledgement affects zero rows.
    stale_ack = await temporal_dispatch.store.acknowledge_dispatched(
        seeded.projection_intent_id, leased.lease_token, _utc_now()
    )
    assert stale_ack is False
    final = await temporal_dispatch.fetch_intent(seeded.projection_intent_id)
    assert final["status"] == "dispatched"
    assert final["attempt_count"] == 2
    assert await temporal_dispatch.workflow_run_count(workflow_id) == 1


@pytest.mark.asyncio
async def test_closed_execution_rejects_duplicate_run_as_terminal(
    temporal_dispatch: TemporalDispatchHarness,
) -> None:
    workspace = await temporal_dispatch.seed_workspace()
    seeded = await temporal_dispatch.seed_due_intent(workspace)
    workflow_id = projection_workflow_id(seeded.workspace_id, seeded.event_id)
    await temporal_dispatch.starter.start_source_ingestion(
        SourceIngestionReference(
            contract=SOURCE_INGESTION_REFERENCE_CONTRACT,
            workspace_id=seeded.workspace_id,
            event_id=seeded.event_id,
            source_id=seeded.source_id,
            source_version_id=seeded.source_version_id,
        )
    )
    # An operator closes the execution; the dispatcher must never replace it.
    await temporal_dispatch.client.get_workflow_handle(workflow_id).terminate()

    async def terminal() -> bool:
        row = await temporal_dispatch.fetch_intent(seeded.projection_intent_id)
        return row["status"] == "terminal"

    await temporal_dispatch.dispatch_until(terminal)

    row = await temporal_dispatch.fetch_intent(seeded.projection_intent_id)
    assert row["status"] == "terminal"
    assert row["last_error_code"] == "projection_intent_contract_invalid"
    assert await temporal_dispatch.workflow_run_count(workflow_id) == 1
    failed = temporal_dispatch.diagnostics.of(EventName.PROJECTION_INTENT_DISPATCH_FAILED)
    assert any(
        event["error_code"] == SafeToken.parse("projection_intent_contract_invalid")
        for event in failed
    )
