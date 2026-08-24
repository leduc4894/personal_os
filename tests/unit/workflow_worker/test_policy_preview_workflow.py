"""Unit contracts for the Temporal policy-preview workflow and dispatcher.

Every case pins one rule of the preview orchestration (spec 10/21): the
deterministic workflow identity and pinned queue, the closed
``exclusion_policy_preview_reference/v1`` input serializing only the contract
tag, the two opaque UUIDs and the checkpoint, the single-activity shape with
its bounded retry policy and heartbeat wiring, the activity's typed stale
failures marking the durable row failed before surfacing the closed
non-retryable error, the final-attempt failure marking, and the leased
dispatcher's outcomes — converged start, retryable release with bounded
backoff and terminal contract failure — and the closed dispatch-unavailable
event an injected diagnostic sink receives when an unexpected start failure
would otherwise be swallowed. Sensitive sentinels must never appear
in the serialized input, error surfaces or metric labels.
"""

from __future__ import annotations

import inspect
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from temporalio.common import (
    RetryPolicy,
    WorkflowIDConflictPolicy,
    WorkflowIDReusePolicy,
)
from temporalio.converter import DataConverter
from temporalio.exceptions import WorkflowAlreadyStartedError
from workflow_worker.policy_preview_workflow import (
    POLICY_PREVIEW_ACTIVITY_MAXIMUM_ATTEMPTS,
    POLICY_PREVIEW_ACTIVITY_NAME,
    POLICY_PREVIEW_HEARTBEAT_TIMEOUT,
    POLICY_PREVIEW_REFERENCE_CONTRACT,
    POLICY_PREVIEW_START_TO_CLOSE_TIMEOUT,
    POLICY_PREVIEW_TASK_QUEUE,
    POLICY_PREVIEW_WORKFLOW_TYPE_NAME,
    PolicyPreviewActivities,
    PolicyPreviewExecutionOutcome,
    PolicyPreviewReference,
    PolicyPreviewStartOutcome,
    PolicyPreviewWorkflow,
    TemporalPolicyPreviewStarter,
    policy_preview_workflow_id,
    preview_reference_for_lease,
    preview_retry_policy,
)
from workflow_worker.policy_workflow_runtime import (
    LeasedPolicyPreview,
    PolicyPreviewDispatchRuntime,
    run_policy_preview_process,
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
from personal_os.exclusion_policy.previews import (
    PolicyPreviewRecord,
    PreviewProgress,
    PreviewStatus,
)

WORKSPACE_ID = UUID("018f47a0-7b00-7000-8000-0000000000b1")
PREVIEW_ID = UUID("018f47a0-7b00-7000-8000-0000000000b2")
USER_ID = UUID("018f47a0-7b00-7000-8000-0000000000b3")
CHECKPOINT = 12

_LEAKAGE_SENTINELS: tuple[str, ...] = (
    "sentinel-title",
    "private/notes/sentinel-locator.md",
    "sentinel operand",
    "sentinel provider exception detail",
)


def _reference(
    *,
    workspace_id: UUID = WORKSPACE_ID,
    policy_preview_id: UUID = PREVIEW_ID,
    source_event_checkpoint: int = CHECKPOINT,
) -> PolicyPreviewReference:
    return PolicyPreviewReference(
        contract=POLICY_PREVIEW_REFERENCE_CONTRACT,
        workspace_id=workspace_id,
        policy_preview_id=policy_preview_id,
        source_event_checkpoint=source_event_checkpoint,
    )


def _serialize_input(reference: PolicyPreviewReference) -> bytes:
    (payload,) = DataConverter.default.payload_converter.to_payloads([reference])
    return payload.data


def _record(status: PreviewStatus = PreviewStatus.READY) -> PolicyPreviewRecord:
    return PolicyPreviewRecord(
        policy_preview_id=PREVIEW_ID,
        workspace_id=WORKSPACE_ID,
        policy_draft_id=uuid4(),
        draft_version=1,
        draft_sha256="a" * 64,
        base_policy_revision_id=None,
        source_checkpoint_event_sequence=CHECKPOINT,
        status=status,
        impact_digest=None,
        safe_error_code=None,
        created_by_user_id=USER_ID,
        created_at=datetime(2026, 8, 17, 10, 0, 0, tzinfo=UTC),
        ready_at=None,
        expires_at=None,
        consumed_at=None,
        newly_excluded_count=0,
        still_excluded_count=0,
        newly_allowed_count=0,
        still_allowed_count=0,
        indeterminate_count=0,
    )


def _leased_preview(
    *,
    checkpoint: int = CHECKPOINT,
    attempt_count: int = 1,
) -> LeasedPolicyPreview:
    return LeasedPolicyPreview(
        policy_preview_id=PREVIEW_ID,
        workspace_id=WORKSPACE_ID,
        source_event_checkpoint=checkpoint,
        attempt_count=attempt_count,
        lease_token=uuid4(),
        leased_until=datetime.now(UTC) + timedelta(seconds=60),
    )


# --- deterministic workflow identity and closed input ----------------------------


def test_workflow_identity_contract_is_pinned() -> None:
    assert POLICY_PREVIEW_WORKFLOW_TYPE_NAME == "PolicyPreviewWorkflow"
    assert POLICY_PREVIEW_TASK_QUEUE == "exclusion-policy-preview"
    assert POLICY_PREVIEW_REFERENCE_CONTRACT == "exclusion_policy_preview_reference/v1"
    assert POLICY_PREVIEW_ACTIVITY_NAME == "run_policy_preview_activity"
    assert (
        policy_preview_workflow_id(WORKSPACE_ID, PREVIEW_ID)
        == f"exclusion-policy-preview/{WORKSPACE_ID}/{PREVIEW_ID}"
    )
    assert POLICY_PREVIEW_ACTIVITY_MAXIMUM_ATTEMPTS >= 2
    assert timedelta(minutes=15) < POLICY_PREVIEW_START_TO_CLOSE_TIMEOUT
    assert timedelta(seconds=30) <= POLICY_PREVIEW_HEARTBEAT_TIMEOUT


def test_reference_serializes_to_only_contract_ids_and_checkpoint() -> None:
    serialized = _serialize_input(_reference())
    decoded = json.loads(serialized)
    assert decoded == {
        "contract": POLICY_PREVIEW_REFERENCE_CONTRACT,
        "workspace_id": str(WORKSPACE_ID),
        "policy_preview_id": str(PREVIEW_ID),
        "source_event_checkpoint": CHECKPOINT,
    }
    for sentinel in _LEAKAGE_SENTINELS:
        assert sentinel.encode() not in serialized


def test_workflow_exposes_exactly_one_activity_and_no_database_io() -> None:
    source = inspect.getsource(PolicyPreviewWorkflow)
    assert source.count("execute_activity") == 1
    assert "sqlalchemy" not in source
    assert "postgres" not in source.lower()


def test_retry_policy_is_bounded_with_the_pinned_non_retryable_set() -> None:
    policy = preview_retry_policy()
    assert isinstance(policy, RetryPolicy)
    assert policy.maximum_attempts == POLICY_PREVIEW_ACTIVITY_MAXIMUM_ATTEMPTS
    assert "exclusion_policy_preview_stale" in (policy.non_retryable_error_types or ())
    assert "exclusion_policy_not_initialized" in (policy.non_retryable_error_types or ())
    assert "exclusion_policy_preview_expired" in (policy.non_retryable_error_types or ())
    assert "exclusion_policy_preview_failed" in (policy.non_retryable_error_types or ())


def test_preview_reference_for_lease_carries_the_leased_checkpoint() -> None:
    reference = preview_reference_for_lease(_leased_preview())
    assert reference.contract == POLICY_PREVIEW_REFERENCE_CONTRACT
    assert reference.workspace_id == WORKSPACE_ID
    assert reference.policy_preview_id == PREVIEW_ID
    assert reference.source_event_checkpoint == CHECKPOINT


# --- activity behavior over a fake store -----------------------------------------


@dataclass
class FakePreviewStore:
    """Preview port double recording activity calls and heartbeats."""

    records: dict[UUID, PolicyPreviewRecord] = field(default_factory=dict)
    run_calls: list[UUID] = field(default_factory=list)
    heartbeats: list[PreviewProgress] = field(default_factory=list)
    run_error: Exception | None = None
    failed: list[tuple[UUID, SafeToken]] = field(default_factory=list)

    async def run_preview_activity(
        self,
        preview_id: UUID,
        context: Any,
        heartbeat: Any = None,
    ) -> PolicyPreviewRecord:
        self.run_calls.append(preview_id)
        if heartbeat is not None:
            progress = PreviewProgress(evaluated_subjects=500, batch_count=1)
            await heartbeat(progress)
            self.heartbeats.append(progress)
        if self.run_error is not None:
            raise self.run_error
        return self.records.get(preview_id, _record())

    async def mark_preview_failed(self, preview_id: UUID, error_code: SafeToken) -> bool:
        self.failed.append((preview_id, error_code))
        return True


def _activities(store: FakePreviewStore, *, attempt: int | None = None) -> PolicyPreviewActivities:
    return PolicyPreviewActivities(
        preview_store=store,
        attempt_reader=(lambda: attempt) if attempt is not None else None,
    )


@pytest.mark.asyncio
async def test_activity_runs_the_store_and_heartbeats_between_pages() -> None:
    store = FakePreviewStore()
    activities = _activities(store)

    outcome = await activities.run_policy_preview_activity(_reference())

    assert outcome == PolicyPreviewExecutionOutcome.READY.value
    assert store.run_calls == [PREVIEW_ID]
    assert [progress.evaluated_subjects for progress in store.heartbeats] == [500]


@pytest.mark.asyncio
async def test_activity_rejects_references_outside_the_contract() -> None:
    from temporalio.exceptions import ApplicationError as TemporalApplicationError

    store = FakePreviewStore()
    activities = _activities(store)
    with pytest.raises(TemporalApplicationError) as raised:
        await activities.run_policy_preview_activity(
            PolicyPreviewReference(
                contract="not_the_preview_contract/v1",
                workspace_id=WORKSPACE_ID,
                policy_preview_id=PREVIEW_ID,
                source_event_checkpoint=CHECKPOINT,
            )
        )
    assert raised.value.non_retryable is True
    assert store.run_calls == []


@pytest.mark.asyncio
async def test_activity_marks_stale_bindings_failed_and_surfaces_non_retryable() -> None:
    from temporalio.exceptions import ApplicationError as TemporalApplicationError

    store = FakePreviewStore()
    store.run_error = ExclusionPolicyError(ErrorCode.EXCLUSION_POLICY_PREVIEW_STALE)
    activities = _activities(store)

    with pytest.raises(TemporalApplicationError) as raised:
        await activities.run_policy_preview_activity(_reference())

    assert raised.value.non_retryable is True
    assert store.failed and store.failed[0][0] == PREVIEW_ID
    assert raised.value.message == "exclusion_policy_preview_stale"


@pytest.mark.asyncio
async def test_activity_marks_final_attempt_failed_and_reraises() -> None:
    from temporalio.exceptions import ApplicationError as TemporalApplicationError

    store = FakePreviewStore()
    store.run_error = ExclusionPolicyError(ErrorCode.EXCLUSION_POLICY_COMMIT_OUTCOME_UNKNOWN)
    activities = _activities(store, attempt=POLICY_PREVIEW_ACTIVITY_MAXIMUM_ATTEMPTS)

    with pytest.raises(TemporalApplicationError):
        await activities.run_policy_preview_activity(_reference())

    assert store.failed and store.failed[0][1].value == "preview_execution_failed"


@pytest.mark.asyncio
async def test_activity_keeps_retrying_before_the_final_attempt() -> None:
    from temporalio.exceptions import ApplicationError as TemporalApplicationError

    store = FakePreviewStore()
    store.run_error = ExclusionPolicyError(ErrorCode.EXCLUSION_POLICY_COMMIT_OUTCOME_UNKNOWN)
    activities = _activities(store, attempt=1)

    with pytest.raises(TemporalApplicationError) as raised:
        await activities.run_policy_preview_activity(_reference())

    assert raised.value.non_retryable is False
    assert store.failed == []


@pytest.mark.asyncio
async def test_activity_returns_ready_for_already_ready_replays() -> None:
    store = FakePreviewStore()
    store.records[PREVIEW_ID] = _record(status=PreviewStatus.READY)
    activities = _activities(store)

    outcome = await activities.run_policy_preview_activity(_reference())

    assert outcome == PolicyPreviewExecutionOutcome.READY.value


# --- Temporal starter convergence --------------------------------------------------


@dataclass
class _FakeWorkflowHandle:
    """Handle double whose describe reports the exact deterministic run."""

    async def describe(self, rpc_timeout: object = None) -> Any:
        from types import SimpleNamespace

        from temporalio.client import WorkflowExecutionStatus

        return SimpleNamespace(
            workflow_type=POLICY_PREVIEW_WORKFLOW_TYPE_NAME,
            task_queue=POLICY_PREVIEW_TASK_QUEUE,
            status=WorkflowExecutionStatus.COMPLETED,
        )


@dataclass
class FakeTemporalClient:
    """Temporal client double recording the start call."""

    started: list[dict[str, Any]] = field(default_factory=list)
    raise_already_started: bool = False

    def get_workflow_handle(self, workflow_id: str) -> _FakeWorkflowHandle:
        del workflow_id
        return _FakeWorkflowHandle()

    async def start_workflow(self, *args: Any, **kwargs: Any) -> None:
        self.started.append({"args": args, "kwargs": kwargs})
        if self.raise_already_started:
            workflow_id = str(kwargs.get("id"))
            raise WorkflowAlreadyStartedError(POLICY_PREVIEW_WORKFLOW_TYPE_NAME, workflow_id)


@pytest.mark.asyncio
async def test_starter_starts_the_deterministic_workflow_with_convergence() -> None:
    client = FakeTemporalClient()
    starter = TemporalPolicyPreviewStarter(client)  # type: ignore[arg-type]

    outcome = await starter.start_policy_preview(_reference())

    assert outcome is PolicyPreviewStartOutcome.STARTED
    assert len(client.started) == 1
    call = client.started[0]
    assert call["kwargs"]["id"] == policy_preview_workflow_id(WORKSPACE_ID, PREVIEW_ID)
    assert call["kwargs"]["task_queue"] == POLICY_PREVIEW_TASK_QUEUE
    assert call["kwargs"]["id_reuse_policy"] == WorkflowIDReusePolicy.REJECT_DUPLICATE
    assert call["kwargs"]["id_conflict_policy"] == WorkflowIDConflictPolicy.USE_EXISTING
    assert call["args"][0] == POLICY_PREVIEW_WORKFLOW_TYPE_NAME
    serialized = json.dumps(json.loads(_serialize_input(_reference())))
    for sentinel in _LEAKAGE_SENTINELS:
        assert sentinel not in serialized


@pytest.mark.asyncio
async def test_starter_resolves_lost_acknowledgement_as_existing() -> None:
    client = FakeTemporalClient(raise_already_started=True)
    starter = TemporalPolicyPreviewStarter(client)  # type: ignore[arg-type]

    outcome = await starter.start_policy_preview(_reference())

    assert outcome is PolicyPreviewStartOutcome.EXISTING
    assert len(client.started) == 1


def test_starter_rejects_foreign_task_queues() -> None:
    client = FakeTemporalClient()
    with pytest.raises(ValueError):
        TemporalPolicyPreviewStarter(client, task_queue="other-queue")  # type: ignore[arg-type]


# --- leased dispatcher ------------------------------------------------------------


@dataclass
class FakeDispatchStore:
    """Outbox double recording the dispatcher's fenced transitions."""

    reclaimed: int = 0
    claimed: list[tuple[Any, int]] = field(default_factory=list)
    claim_result: list[Any] = field(default_factory=list)
    released: list[tuple[UUID, SafeToken]] = field(default_factory=list)
    failed: list[tuple[UUID, SafeToken]] = field(default_factory=list)
    swept: bool = False

    async def reclaim_expired_leases(self, now: Any) -> int:
        self.reclaimed += 1
        return 0

    async def expire_overdue_previews(self, now: Any) -> Any:
        self.swept = True
        return None

    async def claim_pending_previews(self, now: Any, limit: int) -> list[Any]:
        self.claimed.append((now, limit))
        return list(self.claim_result)

    async def release_retry(
        self, preview_id: UUID, lease_token: UUID, error_code: SafeToken, now: Any
    ) -> bool:
        self.released.append((preview_id, error_code))
        return True

    async def mark_preview_failed(self, preview_id: UUID, error_code: SafeToken) -> bool:
        self.failed.append((preview_id, error_code))
        return True


@dataclass
class FakeStarter:
    outcome: PolicyPreviewStartOutcome = PolicyPreviewStartOutcome.STARTED
    error: Exception | None = None
    calls: list[PolicyPreviewReference] = field(default_factory=list)

    async def start_policy_preview(
        self, reference: PolicyPreviewReference
    ) -> PolicyPreviewStartOutcome:
        self.calls.append(reference)
        if self.error is not None:
            raise self.error
        return self.outcome


def _runtime(store: FakeDispatchStore, starter: FakeStarter) -> PolicyPreviewDispatchRuntime:
    return PolicyPreviewDispatchRuntime(
        preview_store=store,  # type: ignore[arg-type]
        starter=starter,  # type: ignore[arg-type]
        clock=lambda: datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_dispatcher_sweeps_reclaims_claims_and_starts() -> None:
    store = FakeDispatchStore()
    store.claim_result = [_leased_preview()]
    starter = FakeStarter()
    runtime = _runtime(store, starter)

    claimed = await runtime.dispatch_pending_previews_once()

    assert claimed == 1
    assert store.swept and store.reclaimed == 1
    assert [call.policy_preview_id for call in starter.calls] == [PREVIEW_ID]


@pytest.mark.asyncio
async def test_dispatcher_releases_retryable_start_failures_with_backoff() -> None:
    store = FakeDispatchStore()
    store.claim_result = [_leased_preview()]
    starter = FakeStarter(
        error=ExclusionPolicyError(ErrorCode.EXCLUSION_POLICY_COMMIT_OUTCOME_UNKNOWN)
    )
    runtime = _runtime(store, starter)

    claimed = await runtime.dispatch_pending_previews_once()

    assert claimed == 1
    assert len(store.released) == 1
    assert store.released[0][0] == PREVIEW_ID
    assert store.failed == []


@pytest.mark.asyncio
async def test_dispatcher_marks_terminal_start_failures_failed() -> None:
    store = FakeDispatchStore()
    store.claim_result = [_leased_preview()]
    starter = FakeStarter(error=ExclusionPolicyError(ErrorCode.EXCLUSION_POLICY_INPUT_INVALID))
    runtime = _runtime(store, starter)

    claimed = await runtime.dispatch_pending_previews_once()

    assert claimed == 1
    assert store.released == []
    assert len(store.failed) == 1
    assert store.failed[0][0] == PREVIEW_ID


# --- dispatch diagnostics -----------------------------------------------------------------


@dataclass
class RecordingDiagnosticSink:
    """Diagnostic sink double recording the closed events it receives."""

    events: list[tuple[EventName, dict[str, object]]] = field(default_factory=list)

    def emit(self, event_name: EventName, fields: Mapping[str, object] | None = None) -> None:
        self.events.append((event_name, dict(fields or {})))


@pytest.mark.asyncio
async def test_dispatcher_emits_dispatch_unavailable_when_start_raises_unexpectedly() -> None:
    store = FakeDispatchStore()
    store.claim_result = [_leased_preview(attempt_count=2)]
    starter = FakeStarter(error=RuntimeError("sentinel provider detail"))
    sink = RecordingDiagnosticSink()
    runtime = PolicyPreviewDispatchRuntime(
        preview_store=store,  # type: ignore[arg-type]
        starter=starter,  # type: ignore[arg-type]
        clock=lambda: datetime.now(UTC),
        diagnostics=sink,
    )

    claimed = await runtime.dispatch_pending_previews_once()

    assert claimed == 1
    assert store.released == [] and store.failed == []
    assert len(sink.events) == 1
    event_name, fields = sink.events[0]
    assert event_name is EventName.PREVIEW_DISPATCH_UNAVAILABLE
    assert event_name.value == "preview_dispatch_unavailable"
    assert fields["policy_preview_id"] == PREVIEW_ID
    assert fields["attempt_count"] == 2
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
        ExclusionPolicyError(ErrorCode.EXCLUSION_POLICY_COMMIT_OUTCOME_UNKNOWN),
        ExclusionPolicyError(ErrorCode.EXCLUSION_POLICY_INPUT_INVALID),
    ],
    ids=["converged-start", "retryable-release", "terminal-failure"],
)
async def test_dispatcher_emits_no_events_on_typed_start_outcomes(
    error: Exception | None,
) -> None:
    store = FakeDispatchStore()
    store.claim_result = [_leased_preview()]
    starter = FakeStarter(error=error)
    sink = RecordingDiagnosticSink()
    runtime = PolicyPreviewDispatchRuntime(
        preview_store=store,  # type: ignore[arg-type]
        starter=starter,  # type: ignore[arg-type]
        clock=lambda: datetime.now(UTC),
        diagnostics=sink,
    )

    claimed = await runtime.dispatch_pending_previews_once()

    assert claimed == 1
    assert sink.events == []


def test_preview_process_composition_wires_the_configured_diagnostic_sink() -> None:
    """The process runner injects the configured logger; no hardcoded sink."""

    source = inspect.getsource(run_policy_preview_process)
    assert "configure_diagnostics(runtime_settings)" in source
    assert "diagnostics=diagnostics" in source
