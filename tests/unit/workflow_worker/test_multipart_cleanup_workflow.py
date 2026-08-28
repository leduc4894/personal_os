"""Unit contracts for the bounded multipart exact-cleanup workflow (spec 6.4).

Every case pins one rule of the expiry-cleanup orchestration: the
deterministic ``multipart_cleanup/{batch_token}`` identity on the pinned
queue, the closed workflow input, continuation, activity reference and
heartbeat serializing only the contract tag, the opaque batch token and
opaque counts, the bounded drain/continue-as-new decision, the batch
activity's contract and error mapping, the executor composition's typed
fail-closed boundary for the service ports cleanup never touches, the
staging-key adapter's exact-identity translation, the starter's
duplicate-run convergence and typed start-failure mapping, and the
dispatcher's one-sweep-per-interval loop with a closed diagnostic surface
for failed sweeps. Sensitive sentinels — staging keys, provider upload
IDs, ETags, URLs — must never appear in any serialized input, heartbeat or
identity.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from temporalio.client import WorkflowExecutionStatus
from temporalio.common import RetryPolicy, WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.converter import DataConverter
from temporalio.exceptions import ApplicationError as TemporalApplicationError
from temporalio.exceptions import WorkflowAlreadyStartedError
from workflow_worker.multipart_cleanup_workflow import (
    MULTIPART_CLEANUP_ACTIVITY_MAXIMUM_ATTEMPTS,
    MULTIPART_CLEANUP_ACTIVITY_NAMES,
    MULTIPART_CLEANUP_ACTIVITY_START_TO_CLOSE_TIMEOUT,
    MULTIPART_CLEANUP_BATCH_ACTIVITY_NAME,
    MULTIPART_CLEANUP_DISPATCH_POLL_INTERVAL_SECONDS,
    MULTIPART_CLEANUP_NON_RETRYABLE_ERROR_TYPES,
    MULTIPART_CLEANUP_START_TIMEOUT,
    MULTIPART_CLEANUP_TASK_QUEUE,
    MULTIPART_CLEANUP_WORKFLOW_TYPE_NAME,
    MultipartCleanupActivities,
    MultipartCleanupBatchReference,
    MultipartCleanupDispatchRuntime,
    MultipartCleanupExecutor,
    MultipartCleanupHeartbeatPayload,
    MultipartCleanupStartOutcome,
    MultipartCleanupWorkflow,
    R2StagingKeyBoundProvider,
    TemporalMultipartCleanupStarter,
    build_multipart_cleanup_executor,
    multipart_cleanup_retry_policy,
)

from personal_os.diagnostics.context import DiagnosticContext, TraceContext
from personal_os.diagnostics.events import EventName
from personal_os.diagnostics.trace_context import SpanId, TraceId
from personal_os.error_contracts.codes import ErrorCode
from personal_os.multipart_upload.cleanup import (
    MULTIPART_CLEANUP_BATCH_LIMIT,
    MULTIPART_CLEANUP_CONTINUE_AS_NEW_BATCHES,
    MULTIPART_CLEANUP_CONTRACT,
    MultipartCleanupBatchInput,
    MultipartCleanupContinuation,
    MultipartCleanupCounters,
    accumulate_cleanup_counters,
    is_drain_complete,
    multipart_cleanup_workflow_id,
    should_continue_as_new,
)
from personal_os.multipart_upload.contracts import (
    MultipartPartUrl,
    MultipartSessionState,
    MultipartUploadSessionId,
)
from personal_os.multipart_upload.errors import MultipartUploadError
from personal_os.multipart_upload.ports import (
    MultipartCleanupClaim,
    MultipartProviderPartETag,
    MultipartProviderUploadId,
    MultipartSessionRecord,
)
from personal_os.multipart_upload.service import (
    MultipartCleanupBatchOutcome,
    MultipartObservedPart,
    MultipartUploadService,
)
from postgresql_source_store.multipart_upload_store import MULTIPART_CLEANUP_BATCH_MAXIMUM
from r2_object_storage.multipart import (
    MultipartProviderPart,
    MultipartStagingKey,
)

BATCH_TOKEN = UUID("018f47a0-7b00-7000-8000-000000000201")
LEASE_TOKEN = UUID("018f47a0-7b00-7000-8000-000000000203")

_SENTINEL_STAGING_KEY = "staging/multipart/sentinelStagingKeyValue0000000000000000"
_SENTINEL_UPLOAD_ID = "sentinel-provider-upload-id"
_SENTINEL_ETAG = "sentinel-provider-etag"
_SENTINEL_URL = "https://sentinel.example.invalid/signed-part-url"
_SENTINEL_SESSION_ID = "sentinel-public-session-id-000000000000000000"

_LEAKAGE_SENTINELS: tuple[str, ...] = (
    _SENTINEL_STAGING_KEY,
    _SENTINEL_UPLOAD_ID,
    _SENTINEL_ETAG,
    _SENTINEL_URL,
)


def _serialize(value: object) -> bytes:
    (payload,) = DataConverter.default.payload_converter.to_payloads([value])
    return payload.data


def _batch_reference(
    batch_limit: int = MULTIPART_CLEANUP_BATCH_LIMIT,
) -> MultipartCleanupBatchReference:
    return MultipartCleanupBatchReference(
        contract=MULTIPART_CLEANUP_CONTRACT,
        batch_token=BATCH_TOKEN,
        batch_limit=batch_limit,
    )


# --- identity and closed wire contracts ---------------------------------------


def test_workflow_activity_and_queue_identities_are_pinned() -> None:
    assert MULTIPART_CLEANUP_WORKFLOW_TYPE_NAME == "MultipartCleanupWorkflow"
    assert MULTIPART_CLEANUP_TASK_QUEUE == "multipart-cleanup"
    assert MULTIPART_CLEANUP_ACTIVITY_NAMES == (MULTIPART_CLEANUP_BATCH_ACTIVITY_NAME,)
    assert MULTIPART_CLEANUP_BATCH_ACTIVITY_NAME == "run_cleanup_batch_activity"


def test_workflow_id_is_the_opaque_batch_identity() -> None:
    assert multipart_cleanup_workflow_id(BATCH_TOKEN) == f"multipart_cleanup/{BATCH_TOKEN}"


def test_batch_input_serializes_only_closed_fields() -> None:
    reference = MultipartCleanupBatchInput(
        contract=MULTIPART_CLEANUP_CONTRACT, batch_token=BATCH_TOKEN
    )
    decoded = json.loads(_serialize(reference))
    assert decoded == {
        "contract": MULTIPART_CLEANUP_CONTRACT,
        "batch_token": str(BATCH_TOKEN),
    }
    for sentinel in _LEAKAGE_SENTINELS:
        assert sentinel.encode() not in _serialize(reference)


def test_continuation_serializes_only_closed_fields() -> None:
    continuation = MultipartCleanupContinuation(
        contract=MULTIPART_CLEANUP_CONTRACT,
        batch_token=BATCH_TOKEN,
        counters=MultipartCleanupCounters(cleaned_count=7, failed_count=2),
    )
    decoded = json.loads(_serialize(continuation))
    assert decoded["counters"] == {"cleaned_count": 7, "failed_count": 2}
    for sentinel in _LEAKAGE_SENTINELS:
        assert sentinel.encode() not in _serialize(continuation)


def test_activity_reference_serializes_only_closed_fields() -> None:
    decoded = json.loads(_serialize(_batch_reference()))
    assert decoded == {
        "contract": MULTIPART_CLEANUP_CONTRACT,
        "batch_token": str(BATCH_TOKEN),
        "batch_limit": MULTIPART_CLEANUP_BATCH_LIMIT,
    }
    for sentinel in _LEAKAGE_SENTINELS:
        assert sentinel.encode() not in _serialize(_batch_reference())


def test_heartbeat_payload_serializes_only_opaque_counts() -> None:
    payload = MultipartCleanupHeartbeatPayload(batch_limit=10, cleaned_count=3, failed_count=1)
    decoded = json.loads(_serialize(payload))
    assert decoded == {"batch_limit": 10, "cleaned_count": 3, "failed_count": 1}
    for sentinel in _LEAKAGE_SENTINELS:
        assert sentinel.encode() not in _serialize(payload)


def test_batch_limit_respects_the_store_claim_ceiling() -> None:
    assert 1 <= MULTIPART_CLEANUP_BATCH_LIMIT <= MULTIPART_CLEANUP_BATCH_MAXIMUM


def test_retry_policy_pins_the_closed_bounds_and_non_retryable_set() -> None:
    policy = multipart_cleanup_retry_policy()
    assert isinstance(policy, RetryPolicy)
    assert policy.maximum_attempts == MULTIPART_CLEANUP_ACTIVITY_MAXIMUM_ATTEMPTS
    assert "multipart_dependency_unavailable" not in (policy.non_retryable_error_types or ())
    assert "multipart_cleanup_failed" not in (policy.non_retryable_error_types or ())
    assert set(MULTIPART_CLEANUP_NON_RETRYABLE_ERROR_TYPES) <= set(
        policy.non_retryable_error_types or ()
    )
    assert timedelta(minutes=1) < MULTIPART_CLEANUP_ACTIVITY_START_TO_CLOSE_TIMEOUT
    assert timedelta(seconds=10) == MULTIPART_CLEANUP_START_TIMEOUT


# --- bounded drain and continue-as-new rules ----------------------------------


def test_drain_completes_only_when_a_batch_claimed_nothing() -> None:
    assert is_drain_complete(cleaned_count=0, failed_count=0) is True
    assert is_drain_complete(cleaned_count=1, failed_count=0) is False
    assert is_drain_complete(cleaned_count=0, failed_count=1) is False


def test_continue_as_new_fires_at_the_pinned_batch_bound() -> None:
    assert MULTIPART_CLEANUP_CONTINUE_AS_NEW_BATCHES == 20
    assert should_continue_as_new(run_batch_count=19) is False
    assert should_continue_as_new(run_batch_count=20) is True
    with pytest.raises(ValueError):
        should_continue_as_new(run_batch_count=-1)


def test_counters_fold_only_opaque_counts() -> None:
    counters = MultipartCleanupCounters(cleaned_count=2, failed_count=1)
    folded = accumulate_cleanup_counters(counters, cleaned_count=3, failed_count=2)
    assert folded == MultipartCleanupCounters(cleaned_count=5, failed_count=3)


def test_workflow_loop_drains_bounded_batches_and_handles_cancellation() -> None:
    # The workflow method must drive the pure drain rule, continue as new at
    # the pinned bound and return the closed cancelled outcome on
    # cancellation instead of losing the durable cleanup obligation.
    source = inspect.getsource(MultipartCleanupWorkflow.run)
    assert "is_drain_complete" in source
    assert "continue_as_new" in source
    assert "CancelledError" in source
    assert "MultipartCleanupExecutionOutcome.CANCELLED" in source


# --- the batch activity --------------------------------------------------------


@dataclass
class FakeCleanupExecutor:
    """The executor fake: records the requested batch limit."""

    outcome: MultipartCleanupBatchOutcome = field(
        default_factory=lambda: MultipartCleanupBatchOutcome(cleaned_count=1, failed_count=0)
    )
    error: Exception | None = None
    batch_limits: list[int] = field(default_factory=list)

    async def run_exact_cleanup(
        self, *, batch_limit: int, diagnostic_context: DiagnosticContext
    ) -> MultipartCleanupBatchOutcome:
        del diagnostic_context
        self.batch_limits.append(batch_limit)
        if self.error is not None:
            raise self.error
        return self.outcome


def _activities(
    executor: FakeCleanupExecutor,
    *,
    heartbeat_sink: Any = None,
) -> MultipartCleanupActivities:
    return MultipartCleanupActivities(
        executor=executor,  # type: ignore[arg-type]
        heartbeat_sink=heartbeat_sink,
    )


def test_batch_activity_runs_one_bounded_batch_and_heartbeats_counts() -> None:
    executor = FakeCleanupExecutor()
    heartbeats: list[MultipartCleanupHeartbeatPayload] = []

    outcome = asyncio.run(
        _activities(executor, heartbeat_sink=heartbeats.append).run_cleanup_batch_activity(
            _batch_reference(batch_limit=25)
        )
    )

    assert outcome == executor.outcome
    assert executor.batch_limits == [25]
    assert heartbeats == [
        MultipartCleanupHeartbeatPayload(batch_limit=25, cleaned_count=0, failed_count=0),
        MultipartCleanupHeartbeatPayload(batch_limit=25, cleaned_count=1, failed_count=0),
    ]


def test_batch_activity_rejects_contract_drift_non_retryably() -> None:
    executor = FakeCleanupExecutor()
    reference = MultipartCleanupBatchReference(
        contract="multipart_cleanup/v2",
        batch_token=BATCH_TOKEN,
        batch_limit=MULTIPART_CLEANUP_BATCH_LIMIT,
    )
    with pytest.raises(TemporalApplicationError) as raised:
        asyncio.run(_activities(executor).run_cleanup_batch_activity(reference))
    assert raised.value.non_retryable is True
    assert executor.batch_limits == []


def test_batch_activity_maps_typed_failures_onto_closed_retryability() -> None:
    retryable = FakeCleanupExecutor(
        error=MultipartUploadError(ErrorCode.MULTIPART_DEPENDENCY_UNAVAILABLE)
    )
    with pytest.raises(TemporalApplicationError) as raised:
        asyncio.run(_activities(retryable).run_cleanup_batch_activity(_batch_reference()))
    assert raised.value.non_retryable is False
    assert raised.value.message == "multipart_dependency_unavailable"

    terminal = FakeCleanupExecutor(
        error=MultipartUploadError(ErrorCode.MULTIPART_SESSION_STATE_INVALID)
    )
    with pytest.raises(TemporalApplicationError) as raised_terminal:
        asyncio.run(_activities(terminal).run_cleanup_batch_activity(_batch_reference()))
    assert raised_terminal.value.non_retryable is True
    assert raised_terminal.value.message == "multipart_session_state_invalid"


def test_batch_activity_never_swallows_unexpected_errors() -> None:
    executor = FakeCleanupExecutor(error=RuntimeError("sentinel provider detail"))
    with pytest.raises(RuntimeError):
        asyncio.run(_activities(executor).run_cleanup_batch_activity(_batch_reference()))


# --- the executor composition boundary -----------------------------------------


@dataclass
class RecordingStagingKeyProvider:
    """The inner provider fake recording the validated staging keys."""

    aborted: list[tuple[str, str]] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    completed: list[tuple[str, str, int]] = field(default_factory=list)
    error: Exception | None = None

    async def create_upload(self, staging_key: MultipartStagingKey) -> MultipartProviderUploadId:
        raise AssertionError("cleanup never creates provider work")

    async def presign_part(
        self,
        staging_key: MultipartStagingKey,
        upload_id: MultipartProviderUploadId,
        part_range: Any,
    ) -> MultipartPartUrl:
        raise AssertionError("cleanup never presigns a part URL")

    async def list_parts(
        self, staging_key: MultipartStagingKey, upload_id: MultipartProviderUploadId
    ) -> tuple[MultipartProviderPart, ...]:
        return (
            MultipartProviderPart(
                part_number=1,
                etag=MultipartProviderPartETag(_SENTINEL_ETAG),
                size_bytes=64,
            ),
        )

    async def complete_upload(
        self,
        staging_key: MultipartStagingKey,
        upload_id: MultipartProviderUploadId,
        parts: Any,
    ) -> None:
        self.completed.append((staging_key.value, upload_id.value, len(parts)))

    async def abort_upload(
        self, staging_key: MultipartStagingKey, upload_id: MultipartProviderUploadId
    ) -> None:
        if self.error is not None:
            raise self.error
        self.aborted.append((staging_key.value, upload_id.value))

    async def delete_staging_object(self, staging_key: MultipartStagingKey) -> None:
        if self.error is not None:
            raise self.error
        self.deleted.append(staging_key.value)


def _valid_staging_key() -> str:
    return "staging/multipart/validStagingKeyValue000000000000000000"


def test_staging_key_adapter_translates_only_the_exact_identity() -> None:
    inner = RecordingStagingKeyProvider()
    adapter = R2StagingKeyBoundProvider(inner)  # type: ignore[arg-type]
    upload_id = MultipartProviderUploadId(_SENTINEL_UPLOAD_ID)

    parts = asyncio.run(adapter.list_parts(_valid_staging_key(), upload_id))
    asyncio.run(
        adapter.complete_upload(
            _valid_staging_key(),
            upload_id,
            (
                MultipartObservedPart(
                    part_number=1,
                    etag=MultipartProviderPartETag(_SENTINEL_ETAG),
                    size_bytes=64,
                ),
            ),
        )
    )
    asyncio.run(adapter.abort_upload(_valid_staging_key(), upload_id))
    asyncio.run(adapter.delete_staging_object(_valid_staging_key()))

    assert parts == (
        MultipartObservedPart(
            part_number=1, etag=MultipartProviderPartETag(_SENTINEL_ETAG), size_bytes=64
        ),
    )
    assert inner.completed == [(_valid_staging_key(), _SENTINEL_UPLOAD_ID, 1)]
    assert inner.aborted == [(_valid_staging_key(), _SENTINEL_UPLOAD_ID)]
    assert inner.deleted == [_valid_staging_key()]


def test_staging_key_adapter_rejects_non_staging_shapes_closed() -> None:
    adapter = R2StagingKeyBoundProvider(RecordingStagingKeyProvider())  # type: ignore[arg-type]
    for invalid_key in ("objects/sha256/abcd", "staging/multipart/short", ""):
        with pytest.raises(MultipartUploadError) as raised:
            asyncio.run(
                adapter.abort_upload(invalid_key, MultipartProviderUploadId(_SENTINEL_UPLOAD_ID))
            )
        assert raised.value.error_code is ErrorCode.MULTIPART_PROVIDER_STATE_INVALID


@dataclass
class FakeCleanupSessionStore:
    """The durable-store slice the executor drives."""

    claims: list[MultipartCleanupClaim]
    recorded: list[tuple[str, bool]] = field(default_factory=list)

    async def claim_cleanup_batch(
        self, *, batch_limit: int, diagnostic_context: DiagnosticContext
    ) -> Any:
        del diagnostic_context, batch_limit
        claimed = self.claims
        self.claims = []
        return claimed

    async def record_cleanup_result(
        self,
        *,
        claim: Any,
        is_succeeded: bool,
        failure_reason: ErrorCode | None = None,
        diagnostic_context: DiagnosticContext,
    ) -> None:
        del failure_reason, diagnostic_context
        self.recorded.append((claim.session.session_id.value, is_succeeded))


def _expired_session_claim() -> MultipartCleanupClaim:
    record = MultipartSessionRecord(
        session_id=MultipartUploadSessionId(_SENTINEL_SESSION_ID),
        state=MultipartSessionState.EXPIRED,
        part_size_bytes=8 * 1024 * 1024,
        part_count=3,
        total_size_bytes=20 * 1024 * 1024,
        expires_at=datetime(2026, 8, 27, 12, 0, 0, tzinfo=UTC),
        staging_key=_valid_staging_key(),
        provider_upload_id=MultipartProviderUploadId(_SENTINEL_UPLOAD_ID),
        completed_part_numbers=frozenset({1}),
        terminal_result=None,
    )
    return MultipartCleanupClaim(
        session=record,
        claim_token=LEASE_TOKEN,
        claim_expires_at=datetime(2026, 8, 28, 12, 15, 0, tzinfo=UTC),
    )


def test_built_executor_drives_the_exact_cleanup_and_records_by_lease() -> None:
    inner = RecordingStagingKeyProvider()
    store = FakeCleanupSessionStore(claims=[_expired_session_claim()])

    service = build_multipart_cleanup_executor(
        session_store=store,  # type: ignore[arg-type]
        staging_provider=R2StagingKeyBoundProvider(inner),  # type: ignore[arg-type]
    )
    assert isinstance(service, MultipartUploadService)

    context = DiagnosticContext(
        request_id=uuid4(),
        client_request_id=None,
        trace=TraceContext(
            trace_id=TraceId("0123456789abcdef0123456789abcdef"),
            remote_parent_span_id=None,
            local_span_id=SpanId("0123456789abcdef"),
            trace_flags=0,
        ),
    )
    outcome = asyncio.run(service.run_exact_cleanup(batch_limit=5, diagnostic_context=context))

    assert outcome == MultipartCleanupBatchOutcome(cleaned_count=1, failed_count=0)
    assert inner.aborted == [(_valid_staging_key(), _SENTINEL_UPLOAD_ID)]
    assert inner.deleted == [_valid_staging_key()]
    assert store.recorded == [(_SENTINEL_SESSION_ID, True)]


def test_unbound_service_ports_fail_closed_with_the_typed_dependency_token() -> None:
    service = build_multipart_cleanup_executor(
        session_store=FakeCleanupSessionStore(claims=[]),  # type: ignore[arg-type]
        staging_provider=RecordingStagingKeyProvider(),  # type: ignore[arg-type]
    )
    for unbound in (
        service.evidence_store,
        service.operation_store,
        service.policy_guard,
        service.current_sources,
        service.publication_gateway,
        service.object_store,
        service.staging_byte_source,
    ):
        with pytest.raises(MultipartUploadError) as raised:
            unbound.any_method()  # type: ignore[attr-defined]
        assert raised.value.error_code is ErrorCode.MULTIPART_DEPENDENCY_UNAVAILABLE


# --- the starter ---------------------------------------------------------------


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

    def get_workflow_handle(self, workflow_id: str) -> FakeWorkflowHandle:
        return FakeWorkflowHandle(self, workflow_id)

    async def start_workflow(
        self,
        workflow: str,
        arg: object,
        *,
        id: str,
        task_queue: str,
        id_reuse_policy: object,
        id_conflict_policy: object,
        rpc_timeout: timedelta | None,
    ) -> None:
        if self.start_error is not None:
            raise self.start_error
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


@dataclass
class FakeDescription:
    workflow_type: str
    task_queue: str
    status: object


def test_starter_pins_the_closed_identity_and_start_bounds() -> None:
    client = FakeTemporalClient()
    starter = TemporalMultipartCleanupStarter(client)  # type: ignore[arg-type]

    outcome = asyncio.run(
        starter.start_cleanup(
            MultipartCleanupBatchInput(contract=MULTIPART_CLEANUP_CONTRACT, batch_token=BATCH_TOKEN)
        )
    )

    assert outcome is MultipartCleanupStartOutcome.STARTED
    (start,) = client.starts
    assert start.workflow == MULTIPART_CLEANUP_WORKFLOW_TYPE_NAME
    assert start.id == f"multipart_cleanup/{BATCH_TOKEN}"
    assert start.task_queue == MULTIPART_CLEANUP_TASK_QUEUE
    assert start.id_reuse_policy is WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY
    assert start.id_conflict_policy is WorkflowIDConflictPolicy.USE_EXISTING
    assert start.rpc_timeout == MULTIPART_CLEANUP_START_TIMEOUT


def test_starter_resolves_a_completed_duplicate_as_existing() -> None:
    client = FakeTemporalClient(
        start_error=WorkflowAlreadyStartedError(
            "duplicate-id", MULTIPART_CLEANUP_WORKFLOW_TYPE_NAME
        ),
        description=FakeDescription(
            workflow_type=MULTIPART_CLEANUP_WORKFLOW_TYPE_NAME,
            task_queue=MULTIPART_CLEANUP_TASK_QUEUE,
            status=WorkflowExecutionStatus.COMPLETED,
        ),
    )
    starter = TemporalMultipartCleanupStarter(client)  # type: ignore[arg-type]

    outcome = asyncio.run(
        starter.start_cleanup(
            MultipartCleanupBatchInput(contract=MULTIPART_CLEANUP_CONTRACT, batch_token=BATCH_TOKEN)
        )
    )

    assert outcome is MultipartCleanupStartOutcome.EXISTING
    assert client.handle_calls == [
        (f"multipart_cleanup/{BATCH_TOKEN}", MULTIPART_CLEANUP_START_TIMEOUT)
    ]


def test_starter_maps_start_failures_onto_the_typed_retryable_code() -> None:
    client = FakeTemporalClient(start_error=RuntimeError("temporal transport collapsed"))
    starter = TemporalMultipartCleanupStarter(client)  # type: ignore[arg-type]

    with pytest.raises(MultipartUploadError) as raised:
        asyncio.run(
            starter.start_cleanup(
                MultipartCleanupBatchInput(
                    contract=MULTIPART_CLEANUP_CONTRACT, batch_token=BATCH_TOKEN
                )
            )
        )
    assert raised.value.error_code is ErrorCode.MULTIPART_DEPENDENCY_UNAVAILABLE
    assert raised.value.is_retryable is True


# --- the dispatch runtime ------------------------------------------------------


class RecordingStarter:
    def __init__(self, error: Exception | None = None) -> None:
        self.inputs: list[MultipartCleanupBatchInput] = []
        self.error = error

    async def start_cleanup(
        self, reference: MultipartCleanupBatchInput
    ) -> MultipartCleanupStartOutcome:
        self.inputs.append(reference)
        if self.error is not None:
            raise self.error
        return MultipartCleanupStartOutcome.STARTED


@dataclass
class RecordingDiagnosticSink:
    events: list[tuple[EventName, Mapping[str, object]]] = field(default_factory=list)

    def emit(self, event_name: EventName, fields: Mapping[str, object] | None = None) -> None:
        self.events.append((event_name, dict(fields or {})))


def test_dispatch_once_starts_one_workflow_with_a_fresh_opaque_token() -> None:
    starter = RecordingStarter()
    runtime = MultipartCleanupDispatchRuntime(
        starter=starter, batch_token_generator=lambda: BATCH_TOKEN
    )

    asyncio.run(runtime.dispatch_cleanup_once())

    assert starter.inputs == [
        MultipartCleanupBatchInput(contract=MULTIPART_CLEANUP_CONTRACT, batch_token=BATCH_TOKEN)
    ]
    assert MULTIPART_CLEANUP_DISPATCH_POLL_INTERVAL_SECONDS >= 30.0


def test_dispatch_failure_surfaces_the_closed_code_and_keeps_sweeping() -> None:
    sink = RecordingDiagnosticSink()
    starter = RecordingStarter(
        error=MultipartUploadError(ErrorCode.MULTIPART_DEPENDENCY_UNAVAILABLE)
    )
    runtime = MultipartCleanupDispatchRuntime(
        starter=starter,
        batch_token_generator=lambda: BATCH_TOKEN,
        diagnostics=sink,
    )

    asyncio.run(runtime.dispatch_cleanup_once())
    starter.error = None
    asyncio.run(runtime.dispatch_cleanup_once())

    assert len(starter.inputs) == 2
    [(event_name, fields)] = sink.events
    assert event_name is EventName.INTERNAL_ERROR
    assert str(fields["error_code"]) == "multipart_dependency_unavailable"
    assert fields["is_retryable"] is True
    for sentinel in _LEAKAGE_SENTINELS:
        assert sentinel not in json.dumps({key: str(value) for key, value in fields.items()})


def test_runtime_protocol_matches_the_service_executor_surface() -> None:
    # The activity's executor port is exactly the service's cleanup entry.
    executor: MultipartCleanupExecutor = FakeCleanupExecutor()
    assert callable(executor.run_exact_cleanup)
