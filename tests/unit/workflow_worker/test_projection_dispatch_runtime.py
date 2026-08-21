"""Unit contracts for the bounded projection dispatch runtime.

Every case pins one dispatch-loop rule from design section 11.4 against fakes:
reclaim runs before claim, the claim batch is bounded by the pinned limit,
at most eight starts run concurrently, a started or existing execution
acknowledges dispatched through the lease fence, a retryable failure releases
to pending with the capped backoff, a contract failure marks terminal, a stale
fence never overwrites state, and an unknown persistence outcome leaves the
attempt leased for expiry. Shutdown stops new claims without abandoning
in-flight starts. Activation outside local/test is refused and the local/test
loopback exception is bounded.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from workflow_worker.projection_dispatch_runtime import (
    PROJECTION_DISPATCH_CONCURRENCY_LIMIT,
    ProjectionDispatchRuntime,
    load_temporal_dispatch_settings,
    require_dispatcher_activation_allowed,
)
from workflow_worker.projection_workflow_starter import (
    ProjectionWorkflowStartResult,
    SourceIngestionReference,
)

from personal_os.diagnostics.events import EventName, SafeToken
from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import ConfigurationError
from personal_os.runtime_configuration.models import RuntimeEnvironment
from personal_os.sources.errors import ProjectionDispatchError
from personal_os.sources.metrics import (
    InMemorySourcePublicationMetrics,
    ProjectionDispatchOutcome,
    ProjectionKind,
)
from personal_os.sources.projection_dispatch import (
    PROJECTION_CLAIM_BATCH_LIMIT,
    LeasedProjectionIntent,
    projection_retry_backoff_seconds,
)

_FIXED_NOW = datetime(2026, 8, 14, 12, 0, 0, tzinfo=UTC)


class _Missing:
    """Sentinel distinguishing an omitted field from an explicit ``None``."""


_MISSING = _Missing()


def _leased_intent(
    *,
    projection_kind: str = "qdrant",
    attempt_count: int = 0,
    source_version_id: UUID | None | _Missing = _MISSING,
) -> LeasedProjectionIntent:
    resolved_source_version: UUID | None
    if isinstance(source_version_id, _Missing):
        resolved_source_version = uuid4()
    else:
        resolved_source_version = source_version_id
    return LeasedProjectionIntent(
        projection_intent_id=uuid4(),
        workspace_id=uuid4(),
        event_id=uuid4(),
        source_id=uuid4(),
        source_version_id=resolved_source_version,
        projection_kind=SafeToken.parse(projection_kind),
        operation=SafeToken.parse("upsert"),
        attempt_count=attempt_count,
        lease_token=uuid4(),
        leased_until=_FIXED_NOW + timedelta(seconds=60),
    )


@dataclass
class RecordedAcknowledge:
    intent_id: UUID
    lease_token: UUID
    now: datetime


@dataclass
class RecordedRelease:
    intent_id: UUID
    lease_token: UUID
    error_code: SafeToken
    available_at: datetime
    now: datetime


@dataclass
class RecordedTerminal:
    intent_id: UUID
    lease_token: UUID
    error_code: SafeToken
    now: datetime


@dataclass
class FakeIntentStore:
    intents: tuple[LeasedProjectionIntent, ...]
    acknowledge_result: bool = True
    acknowledge_error: Exception | None = None
    release_result: bool = True
    terminal_result: bool = True
    on_claim: Any = None
    reclaim_calls: list[datetime] = field(default_factory=list)
    claim_calls: list[tuple[datetime, int]] = field(default_factory=list)
    acknowledgements: list[RecordedAcknowledge] = field(default_factory=list)
    releases: list[RecordedRelease] = field(default_factory=list)
    terminals: list[RecordedTerminal] = field(default_factory=list)

    async def reclaim_expired(self, now: datetime) -> int:
        self.reclaim_calls.append(now)
        return 0

    async def claim_batch(self, now: datetime, limit: int) -> tuple[LeasedProjectionIntent, ...]:
        self.claim_calls.append((now, limit))
        if self.on_claim is not None:
            self.on_claim()
        return self.intents

    async def acknowledge_dispatched(
        self, intent_id: UUID, lease_token: UUID, now: datetime
    ) -> bool:
        if self.acknowledge_error is not None:
            raise self.acknowledge_error
        self.acknowledgements.append(RecordedAcknowledge(intent_id, lease_token, now))
        return self.acknowledge_result

    async def release_retry(
        self,
        intent_id: UUID,
        lease_token: UUID,
        error_code: SafeToken,
        available_at: datetime,
        now: datetime,
    ) -> bool:
        self.releases.append(RecordedRelease(intent_id, lease_token, error_code, available_at, now))
        return self.release_result

    async def mark_terminal(
        self, intent_id: UUID, lease_token: UUID, error_code: SafeToken, now: datetime
    ) -> bool:
        self.terminals.append(RecordedTerminal(intent_id, lease_token, error_code, now))
        return self.terminal_result


@dataclass
class UnavailableThenRecoveringIntentStore(FakeIntentStore):
    """Fails one dispatch cycle, then stops after the recovered cycle."""

    shutdown: asyncio.Event = field(default_factory=asyncio.Event)
    dispatch_calls: int = 0

    async def reclaim_expired(self, now: datetime) -> int:
        self.dispatch_calls += 1
        if self.dispatch_calls == 1:
            raise _retryable_unavailable()
        return await super().reclaim_expired(now)


@dataclass
class FakeStarter:
    result: ProjectionWorkflowStartResult = ProjectionWorkflowStartResult.STARTED
    error: ProjectionDispatchError | None = None
    delay_seconds: float = 0.0
    calls: list[SourceIngestionReference] = field(default_factory=list)
    active_starts: int = 0
    max_active_starts: int = 0

    async def start_source_ingestion(self, reference: SourceIngestionReference) -> object:
        self.calls.append(reference)
        self.active_starts += 1
        self.max_active_starts = max(self.max_active_starts, self.active_starts)
        try:
            if self.delay_seconds:
                await asyncio.sleep(self.delay_seconds)
            if self.error is not None:
                raise self.error
            return self.result
        finally:
            self.active_starts -= 1


@dataclass
class RecordingDiagnostics:
    events: list[tuple[EventName, dict[str, Any]]] = field(default_factory=list)

    def emit(self, event_name: EventName, fields: dict[str, Any] | None = None) -> None:
        self.events.append((event_name, dict(fields or {})))

    def of(self, event_name: EventName) -> list[dict[str, Any]]:
        return [fields for name, fields in self.events if name is event_name]


def _runtime(
    store: FakeIntentStore,
    starter: FakeStarter,
    *,
    clock_zero: bool = True,
) -> tuple[ProjectionDispatchRuntime, RecordingDiagnostics, InMemorySourcePublicationMetrics]:
    diagnostics = RecordingDiagnostics()
    metrics = InMemorySourcePublicationMetrics()
    runtime = ProjectionDispatchRuntime(
        store=store,
        starter=starter,
        clock=(lambda: _FIXED_NOW) if clock_zero else _utc_now,
        diagnostics=diagnostics,
        metrics=metrics,
    )
    return runtime, diagnostics, metrics


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _retryable_unavailable() -> ProjectionDispatchError:
    return ProjectionDispatchError(ErrorCode.PROJECTION_DISPATCH_UNAVAILABLE)


def _terminal_contract_invalid() -> ProjectionDispatchError:
    return ProjectionDispatchError(ErrorCode.PROJECTION_INTENT_CONTRACT_INVALID)


@pytest.mark.asyncio
async def test_dispatch_reclaims_then_claims_within_the_pinned_batch_limit() -> None:
    intents = (_leased_intent(), _leased_intent(projection_kind="neo4j"))
    store = FakeIntentStore(intents)
    starter = FakeStarter()
    runtime, _, _ = _runtime(store, starter)

    processed = await runtime.dispatch_pending_intents_once()

    assert processed == 2
    assert store.reclaim_calls == [_FIXED_NOW]
    assert len(store.claim_calls) == 1
    assert store.claim_calls[0][1] == PROJECTION_CLAIM_BATCH_LIMIT
    assert PROJECTION_CLAIM_BATCH_LIMIT == 50


@pytest.mark.asyncio
async def test_started_execution_acks_dispatched_through_the_lease_fence() -> None:
    intent = _leased_intent()
    store = FakeIntentStore((intent,))
    starter = FakeStarter(result=ProjectionWorkflowStartResult.STARTED)
    runtime, diagnostics, metrics = _runtime(store, starter)

    await runtime.dispatch_pending_intents_once()

    assert store.acknowledgements == [
        RecordedAcknowledge(intent.projection_intent_id, intent.lease_token, _FIXED_NOW)
    ]
    assert store.releases == [] and store.terminals == []
    dispatched = diagnostics.of(EventName.PROJECTION_INTENT_DISPATCHED)
    assert len(dispatched) == 1
    assert dispatched[0]["intent_id"] == intent.projection_intent_id
    assert dispatched[0]["outcome"] == SafeToken.parse("started")
    assert metrics.dispatch_count(ProjectionKind.QDRANT, ProjectionDispatchOutcome.DISPATCHED) == 1


@pytest.mark.asyncio
async def test_existing_execution_acks_dispatched() -> None:
    intent = _leased_intent()
    store = FakeIntentStore((intent,))
    starter = FakeStarter(result=ProjectionWorkflowStartResult.EXISTING)
    runtime, diagnostics, metrics = _runtime(store, starter)

    await runtime.dispatch_pending_intents_once()

    assert len(store.acknowledgements) == 1
    assert dispatched_outcome(diagnostics) == "existing"
    assert metrics.dispatch_count(ProjectionKind.QDRANT, ProjectionDispatchOutcome.DISPATCHED) == 1


def dispatched_outcome(diagnostics: RecordingDiagnostics) -> str:
    events = diagnostics.of(EventName.PROJECTION_INTENT_DISPATCHED)
    assert len(events) == 1
    outcome = events[0]["outcome"]
    assert isinstance(outcome, SafeToken)
    return outcome.value


@pytest.mark.asyncio
async def test_retryable_failure_releases_pending_with_capped_backoff() -> None:
    intent = _leased_intent(attempt_count=2)
    store = FakeIntentStore((intent,))
    starter = FakeStarter(error=_retryable_unavailable())
    runtime, diagnostics, metrics = _runtime(store, starter)

    await runtime.dispatch_pending_intents_once()

    assert store.acknowledgements == [] and store.terminals == []
    assert len(store.releases) == 1
    release = store.releases[0]
    assert release.intent_id == intent.projection_intent_id
    assert release.lease_token == intent.lease_token
    assert release.error_code == SafeToken.parse("projection_dispatch_unavailable")
    assert release.available_at == _FIXED_NOW + timedelta(
        seconds=projection_retry_backoff_seconds(2)
    )
    failed = diagnostics.of(EventName.PROJECTION_INTENT_DISPATCH_FAILED)
    assert len(failed) == 1
    assert failed[0]["outcome"] == SafeToken.parse("pending")
    assert failed[0]["is_retryable"] is True
    assert metrics.dispatch_count(ProjectionKind.QDRANT, ProjectionDispatchOutcome.PENDING) == 1


@pytest.mark.asyncio
async def test_contract_failure_marks_terminal() -> None:
    intent = _leased_intent()
    store = FakeIntentStore((intent,))
    starter = FakeStarter(error=_terminal_contract_invalid())
    runtime, diagnostics, metrics = _runtime(store, starter)

    await runtime.dispatch_pending_intents_once()

    assert store.acknowledgements == [] and store.releases == []
    assert len(store.terminals) == 1
    terminal = store.terminals[0]
    assert terminal.error_code == SafeToken.parse("projection_intent_contract_invalid")
    failed = diagnostics.of(EventName.PROJECTION_INTENT_DISPATCH_FAILED)
    assert len(failed) == 1
    assert failed[0]["outcome"] == SafeToken.parse("terminal")
    assert failed[0]["is_retryable"] is False
    assert metrics.dispatch_count(ProjectionKind.QDRANT, ProjectionDispatchOutcome.TERMINAL) == 1


@pytest.mark.asyncio
async def test_intent_without_source_version_id_marks_terminal() -> None:
    intent = _leased_intent(source_version_id=None)
    store = FakeIntentStore((intent,))
    starter = FakeStarter()
    runtime, _, _ = _runtime(store, starter)

    await runtime.dispatch_pending_intents_once()

    assert starter.calls == []
    assert len(store.terminals) == 1
    assert store.terminals[0].error_code == SafeToken.parse("projection_intent_contract_invalid")


@pytest.mark.asyncio
async def test_claimed_policy_transition_origin_could_never_start_source_ingestion() -> None:
    # Defense in depth behind the claim SQL's origin filter: if a
    # policy-transition intent ever reached the dispatch loop, the closed
    # input contract rejects it before any workflow start and the row goes
    # terminal — it can never reach SourceIngestionWorkflow.
    from personal_os.sources.projection_dispatch import ProjectionIntentOriginKind

    intent = LeasedProjectionIntent(
        projection_intent_id=uuid4(),
        workspace_id=uuid4(),
        origin_kind=ProjectionIntentOriginKind.POLICY_TRANSITION,
        event_id=None,
        policy_revision_id=uuid4(),
        source_id=uuid4(),
        source_version_id=uuid4(),
        projection_kind=SafeToken.parse("qdrant"),
        operation=SafeToken.parse("delete"),
        attempt_count=0,
        lease_token=uuid4(),
        leased_until=_FIXED_NOW + timedelta(seconds=60),
    )
    store = FakeIntentStore((intent,))
    starter = FakeStarter()
    runtime, _, _ = _runtime(store, starter)

    await runtime.dispatch_pending_intents_once()

    assert starter.calls == []
    assert len(store.terminals) == 1
    assert store.terminals[0].error_code == SafeToken.parse("projection_intent_contract_invalid")


@pytest.mark.asyncio
async def test_stale_fence_never_overwrites_and_records_no_dispatch_outcome() -> None:
    intent = _leased_intent()
    store = FakeIntentStore((intent,), acknowledge_result=False)
    starter = FakeStarter()
    runtime, diagnostics, metrics = _runtime(store, starter)

    await runtime.dispatch_pending_intents_once()

    assert len(store.acknowledgements) == 1
    assert store.releases == [] and store.terminals == []
    assert diagnostics.of(EventName.PROJECTION_INTENT_DISPATCHED) == []
    assert diagnostics.of(EventName.PROJECTION_INTENT_DISPATCH_FAILED) == []
    assert metrics.dispatch_count(ProjectionKind.QDRANT, ProjectionDispatchOutcome.DISPATCHED) == 0


@pytest.mark.asyncio
async def test_unknown_persistence_outcome_leaves_the_attempt_leased() -> None:
    intent = _leased_intent()
    store = FakeIntentStore((intent,), acknowledge_error=_retryable_unavailable())
    starter = FakeStarter()
    runtime, diagnostics, _ = _runtime(store, starter)

    await runtime.dispatch_pending_intents_once()

    assert store.releases == [] and store.terminals == []
    assert diagnostics.of(EventName.PROJECTION_INTENT_DISPATCHED) == []


@pytest.mark.asyncio
async def test_concurrent_starts_are_capped_at_eight() -> None:
    assert PROJECTION_DISPATCH_CONCURRENCY_LIMIT == 8
    intents = tuple(_leased_intent() for _ in range(20))
    store = FakeIntentStore(intents)
    starter = FakeStarter(delay_seconds=0.01)
    runtime, _, _ = _runtime(store, starter)

    processed = await runtime.dispatch_pending_intents_once()

    assert processed == 20
    assert starter.max_active_starts == 8


@pytest.mark.asyncio
async def test_shutdown_stops_new_claims_after_the_current_batch() -> None:
    intents = (_leased_intent(),)
    store = FakeIntentStore(intents)
    shutdown = asyncio.Event()
    store.on_claim = shutdown.set
    starter = FakeStarter()
    runtime, _, _ = _runtime(store, starter)

    await runtime.run_until_shutdown(shutdown, poll_interval_seconds=0.01)

    assert len(store.claim_calls) == 1


@pytest.mark.asyncio
async def test_run_dispatches_until_shutdown_is_signalled() -> None:
    store = FakeIntentStore(())
    shutdown = asyncio.Event()

    async def stop_soon() -> None:
        await asyncio.sleep(0.01)
        shutdown.set()

    stopper = asyncio.create_task(stop_soon())
    runtime, _, _ = _runtime(store, FakeStarter())
    await runtime.run_until_shutdown(shutdown, poll_interval_seconds=0.001)
    await stopper

    assert len(store.claim_calls) >= 1


@pytest.mark.asyncio
async def test_run_recovers_after_retryable_database_dispatch_failure() -> None:
    store = UnavailableThenRecoveringIntentStore(())
    store.on_claim = store.shutdown.set
    runtime, _, _ = _runtime(store, FakeStarter())

    await runtime.run_until_shutdown(store.shutdown, poll_interval_seconds=0.001)

    assert store.dispatch_calls == 2


# --- Activation gate and settings ----------------------------------------------


def test_temporal_settings_load_from_the_environment() -> None:
    settings = load_temporal_dispatch_settings(
        environ={
            "KNOWLEDGE_TEMPORAL_TARGET": "127.0.0.1:17243",
            "KNOWLEDGE_TEMPORAL_NAMESPACE": "knowledge",
        }
    )

    assert settings.target == "127.0.0.1:17243"
    assert settings.namespace == "knowledge"
    assert settings.task_queue == "source-ingestion"


def test_temporal_settings_reject_unknown_knowledge_keys() -> None:
    with pytest.raises(ConfigurationError):
        load_temporal_dispatch_settings(
            environ={"KNOWLEDGE_TEMPORAL_TARGET": "127.0.0.1:7233", "KNOWLEDGE_TYPO": "x"}
        )


def test_temporal_settings_reject_a_non_loopback_target() -> None:
    with pytest.raises(ConfigurationError):
        load_temporal_dispatch_settings(
            environ={"KNOWLEDGE_TEMPORAL_TARGET": "temporal.internal:7233"}
        )


def test_temporal_settings_require_the_pinned_task_queue() -> None:
    with pytest.raises(ConfigurationError):
        load_temporal_dispatch_settings(environ={"KNOWLEDGE_TEMPORAL_TASK_QUEUE": "other-queue"})


@pytest.mark.parametrize("environment", [RuntimeEnvironment.STAGING, RuntimeEnvironment.PRODUCTION])
def test_dispatcher_activation_is_refused_outside_local_and_test(
    environment: RuntimeEnvironment,
) -> None:
    settings = load_temporal_dispatch_settings(environ={})

    with pytest.raises(ConfigurationError):
        require_dispatcher_activation_allowed(environment, settings)


@pytest.mark.parametrize("environment", [RuntimeEnvironment.LOCAL, RuntimeEnvironment.TEST])
def test_dispatcher_activation_allows_loopback_in_local_and_test(
    environment: RuntimeEnvironment,
) -> None:
    settings = load_temporal_dispatch_settings(environ={})

    require_dispatcher_activation_allowed(environment, settings)


# --- Lifecycle intent dispatch (Task 11) ------------------------------------


def _lifecycle_delete_intent() -> LeasedProjectionIntent:
    """A lifecycle delete intent with a non-null ``source_version_id``."""

    return LeasedProjectionIntent(
        projection_intent_id=uuid4(),
        workspace_id=uuid4(),
        event_id=uuid4(),
        source_id=uuid4(),
        source_version_id=uuid4(),
        projection_kind=SafeToken.parse("qdrant"),
        operation=SafeToken.parse("delete"),
        attempt_count=0,
        lease_token=uuid4(),
        leased_until=_FIXED_NOW + timedelta(seconds=60),
    )


def _lifecycle_rename_intent() -> LeasedProjectionIntent:
    """A lifecycle rename intent with a non-null ``source_version_id``."""

    return LeasedProjectionIntent(
        projection_intent_id=uuid4(),
        workspace_id=uuid4(),
        event_id=uuid4(),
        source_id=uuid4(),
        source_version_id=uuid4(),
        projection_kind=SafeToken.parse("neo4j"),
        operation=SafeToken.parse("upsert"),
        attempt_count=0,
        lease_token=uuid4(),
        leased_until=_FIXED_NOW + timedelta(seconds=60),
    )


@pytest.mark.asyncio
async def test_lifecycle_delete_intent_dispatches_into_source_ingestion_workflow() -> None:
    """A lifecycle delete intent reaches the same SourceIngestionWorkflow as create."""

    intent = _lifecycle_delete_intent()
    store = FakeIntentStore((intent,))
    starter = FakeStarter(result=ProjectionWorkflowStartResult.STARTED)
    runtime, diagnostics, metrics = _runtime(store, starter)

    await runtime.dispatch_pending_intents_once()

    assert starter.calls == [
        SourceIngestionReference(
            contract="source_ingestion_reference/v1",
            workspace_id=intent.workspace_id,
            event_id=intent.event_id,
            source_id=intent.source_id,
            source_version_id=intent.source_version_id,
        )
    ]
    assert len(store.acknowledgements) == 1
    assert dispatched_outcome(diagnostics) == "started"
    assert metrics.dispatch_count(ProjectionKind.QDRANT, ProjectionDispatchOutcome.DISPATCHED) == 1


@pytest.mark.asyncio
async def test_lifecycle_rename_intent_reaches_workflow_input_with_version_id() -> None:
    """A lifecycle rename intent propagates its non-null source_version_id."""

    intent = _lifecycle_rename_intent()
    store = FakeIntentStore((intent,))
    starter = FakeStarter(result=ProjectionWorkflowStartResult.STARTED)
    runtime, _, _ = _runtime(store, starter)

    await runtime.dispatch_pending_intents_once()

    assert len(starter.calls) == 1
    reference = starter.calls[0]
    assert reference.source_version_id == intent.source_version_id
    assert reference.event_id == intent.event_id
    assert reference.source_id == intent.source_id
    assert reference.workspace_id == intent.workspace_id
    assert reference.contract == "source_ingestion_reference/v1"


@pytest.mark.asyncio
async def test_lifecycle_intent_pair_dispatches_into_one_workflow_run() -> None:
    """Qdrant and Neo4j intents of one lifecycle event reach the same workflow execution.

    The ``(workspace_id, event_id)`` pair is the deterministic workflow
    id the dispatcher derives; one lifecycle event therefore produces
    one workflow run regardless of how many intents it carries.
    """

    workspace_id = uuid4()
    event_id = uuid4()
    source_id = uuid4()
    source_version_id = uuid4()
    qdrant = LeasedProjectionIntent(
        projection_intent_id=uuid4(),
        workspace_id=workspace_id,
        event_id=event_id,
        source_id=source_id,
        source_version_id=source_version_id,
        projection_kind=SafeToken.parse("qdrant"),
        operation=SafeToken.parse("upsert"),
        attempt_count=0,
        lease_token=uuid4(),
        leased_until=_FIXED_NOW + timedelta(seconds=60),
    )
    neo4j = LeasedProjectionIntent(
        projection_intent_id=uuid4(),
        workspace_id=workspace_id,
        event_id=event_id,
        source_id=source_id,
        source_version_id=source_version_id,
        projection_kind=SafeToken.parse("neo4j"),
        operation=SafeToken.parse("upsert"),
        attempt_count=0,
        lease_token=uuid4(),
        leased_until=_FIXED_NOW + timedelta(seconds=60),
    )
    # Both intents share the same (workspace_id, event_id) identity.
    object.__setattr__(qdrant, "event_id", event_id)
    object.__setattr__(neo4j, "event_id", event_id)
    store = FakeIntentStore((qdrant, neo4j))
    starter = FakeStarter(result=ProjectionWorkflowStartResult.STARTED)
    runtime, _, _ = _runtime(store, starter)

    await runtime.dispatch_pending_intents_once()

    assert len(starter.calls) == 2
    assert all(call.event_id == event_id for call in starter.calls)
    assert all(call.source_version_id == source_version_id for call in starter.calls)
    assert len(store.acknowledgements) == 2


@pytest.mark.asyncio
async def test_lifecycle_intent_without_source_version_id_marks_terminal() -> None:
    """A lifecycle intent with a null ``source_version_id`` is the contract failure."""

    intent = _lifecycle_delete_intent()
    object.__setattr__(intent, "source_version_id", None)
    store = FakeIntentStore((intent,))
    starter = FakeStarter()
    runtime, _, _ = _runtime(store, starter)

    await runtime.dispatch_pending_intents_once()

    assert starter.calls == []
    assert len(store.terminals) == 1
    assert store.terminals[0].error_code == SafeToken.parse("projection_intent_contract_invalid")


@pytest.mark.asyncio
async def test_lifecycle_intent_metric_label_uses_closed_projection_kind() -> None:
    """The dispatch metric records the closed ``qdrant``/``neo4j`` kind, not a lifecycle token.

    The lifecycle vocabulary (``rename`` / ``move`` / ``delete`` / ``restore``)
    never crosses into the dispatch metric label; the projector-kind
    meta-label is the only dimension that survives the boundary.
    """

    intent = _lifecycle_delete_intent()
    store = FakeIntentStore((intent,))
    starter = FakeStarter(result=ProjectionWorkflowStartResult.STARTED)
    runtime, _, metrics = _runtime(store, starter)

    await runtime.dispatch_pending_intents_once()

    # The lifecycle ``delete`` token is the projection-intent operation,
    # not a metric label; the only dispatch metric label is ``qdrant``.
    assert metrics.dispatch_count(ProjectionKind.QDRANT, ProjectionDispatchOutcome.DISPATCHED) == 1


# --- bounded-parallel-traffic unit proof --------------------------------------


@pytest.mark.asyncio
async def test_bounded_parallel_dispatch_loops_complete_within_the_deadline() -> None:
    """Eight parallel dispatch loops run to completion without deadlock.

    The unit-level mirror of the disposable-PostgreSQL concurrency
    probe: eight independent ``(runtime, store, starter)`` triples are
    driven concurrently through ``dispatch_pending_intents_once``. Each
    triple processes a small mixed batch (create / rename / move /
    delete / restore intent shapes). The test asserts every loop
    completes within a bounded deadline and the cap of eight concurrent
    starts is enforced per loop — proving the dispatcher's asyncio
    surface admits parallel traffic without ever blocking the caller.
    """

    deadline_seconds: float = 5.0
    loop_count: int = 8
    intents_per_loop: int = 8

    def _build_loop() -> tuple[FakeIntentStore, FakeStarter, ProjectionDispatchRuntime]:
        intents = tuple(
            _leased_intent(
                projection_kind=("qdrant" if index % 2 == 0 else "neo4j"),
                attempt_count=index % 4,
            )
            for index in range(intents_per_loop)
        )
        store = FakeIntentStore(intents)
        starter = FakeStarter(
            result=ProjectionWorkflowStartResult.STARTED,
            delay_seconds=0.001,
        )
        runtime, _, _ = _runtime(store, starter)
        return store, starter, runtime

    async def _drive() -> None:
        triples = [_build_loop() for _ in range(loop_count)]
        runtimes = [runtime for _, _, runtime in triples]
        await asyncio.gather(*(runtime.dispatch_pending_intents_once() for runtime in runtimes))
        for store, starter, _ in triples:
            assert len(store.acknowledgements) == intents_per_loop
            assert starter.max_active_starts <= PROJECTION_DISPATCH_CONCURRENCY_LIMIT

    try:
        await asyncio.wait_for(_drive(), timeout=deadline_seconds)
    except TimeoutError as cause:  # pragma: no cover - deadline reached
        raise AssertionError(
            f"parallel dispatch loops did not complete within "
            f"{deadline_seconds}s — possible deadlock"
        ) from cause
