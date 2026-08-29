"""Bounded Temporal exact-cleanup workflow, activities and worker process.

This is the worker-owned composition boundary over the Temporal SDK (spec
6.4): :class:`MultipartCleanupWorkflow` drains the due exact-cleanup
obligations in bounded single-activity batches — each activity drives one
:func:`MultipartUploadService.run_exact_cleanup` execution, which claims at
most :data:`MULTIPART_CLEANUP_BATCH_LIMIT` rows through the store's
skip-locked lease, touches only each row's persisted exact resource
identities (provider abort for its exact upload ID, object removal for its
exact staging key) and records every per-row outcome through the claim's
lease token — heartbeats only opaque counts, continues as new at the pinned
bound so one history never grows unboundedly, and on cancellation returns
the closed ``cancelled`` outcome while every unrecorded obligation stays
durably leased: lease expiry, never guesswork, returns the row to the next
sweep, so a cancelled workflow can never become an untracked staging
resource.

The closed ``multipart_cleanup/v1`` input carries only the contract tag and
one opaque batch token, so no session ID, staging key, provider upload ID,
ETag, URL or reason text can ever enter Temporal history. The starter
derives the deterministic workflow identity
``multipart_cleanup/{batch_token}`` and starts with
``ALLOW_DUPLICATE_FAILED_ONLY`` plus ``USE_EXISTING``: a re-driven failed run
starts a fresh deterministic execution while a running or completed one
converges. The dispatch runtime starts one sweep per poll interval and
surfaces every failed start through the closed internal-error diagnostic
event — the reason token is never swallowed. The process composition wires
the real engine, the durable session store, the R2 staging adapter behind
the validated str seam and the registered worker; Temporal workers and
clients are created here only — never at import time.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Final, Protocol, cast
from uuid import UUID, uuid4

from temporalio import activity, workflow
from temporalio.client import Client as TemporalClient
from temporalio.client import WorkflowExecutionStatus
from temporalio.common import (
    RetryPolicy,
    WorkflowIDConflictPolicy,
    WorkflowIDReusePolicy,
)
from temporalio.exceptions import ApplicationError as TemporalApplicationError
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError
from temporalio.worker import Worker

from personal_os.diagnostics.context import DiagnosticContext
from personal_os.diagnostics.events import EventName, SafeToken
from personal_os.diagnostics.logging import configure_diagnostics
from personal_os.diagnostics.redaction import fingerprint_stack, normalize_exception_type
from personal_os.diagnostics.trace_context import SpanId, TraceContext, TraceId
from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import ApplicationError
from personal_os.multipart_upload.cleanup import (
    MULTIPART_CLEANUP_BATCH_LIMIT,
    MULTIPART_CLEANUP_CONTRACT,
    MultipartCleanupBatchInput,
    MultipartCleanupContinuation,
    MultipartCleanupCounters,
    MultipartCleanupExecutionOutcome,
    accumulate_cleanup_counters,
    is_drain_complete,
    multipart_cleanup_workflow_id,
    should_continue_as_new,
)
from personal_os.multipart_upload.contracts import MultipartPartRange, MultipartPartUrl
from personal_os.multipart_upload.errors import MultipartUploadError
from personal_os.multipart_upload.metrics import InMemoryMultipartUploadMetrics
from personal_os.multipart_upload.ports import (
    MultipartProviderUploadId,
    MultipartSessionStore,
)
from personal_os.multipart_upload.service import (
    MultipartCleanupBatchOutcome,
    MultipartObservedPart,
    MultipartSessionEvidenceStore,
    MultipartStagingByteSource,
    MultipartStagingProvider,
    MultipartUploadService,
)
from personal_os.object_storage import CanonicalObjectStore
from personal_os.runtime_configuration.loading import load_runtime_settings
from personal_os.runtime_configuration.models import ServiceName
from personal_os.small_file_sync.ports import (
    AwareUtcClock,
    SmallFilePolicyGuard,
    SmallFilePublicationGateway,
    SmallFileUploadOperationStore,
)
from personal_os.sources.reading import CanonicalSourceReadStore

with workflow.unsafe.imports_passed_through():
    from postgresql_source_store.engine import (
        create_source_store_engine,
        dispose_source_store_engine,
    )
    from postgresql_source_store.multipart_upload_store import (
        PostgresqlMultipartUploadStore,
    )
    from postgresql_source_store.settings import (
        load_database_runtime_settings,
        read_database_runtime_password,
    )
    from r2_object_storage.client import R2ClientManager
    from r2_object_storage.multipart import (
        MultipartProviderPart,
        MultipartStagingKey,
        R2MultipartStagingProvider,
    )
    from r2_object_storage.settings import load_object_storage_settings
    from workflow_worker.policy_workflow_runtime import load_policy_temporal_settings
    from workflow_worker.projection_dispatch_runtime import (
        TemporalDispatchSettings,
        require_dispatcher_activation_allowed,
    )

#: Workflow/activity identity pins (spec 6.4 and the pinned queue).
MULTIPART_CLEANUP_WORKFLOW_TYPE_NAME: Final[str] = "MultipartCleanupWorkflow"
MULTIPART_CLEANUP_TASK_QUEUE: Final[str] = "multipart-cleanup"
MULTIPART_CLEANUP_BATCH_ACTIVITY_NAME: Final[str] = "run_cleanup_batch_activity"
MULTIPART_CLEANUP_ACTIVITY_NAMES: Final[tuple[str, ...]] = (MULTIPART_CLEANUP_BATCH_ACTIVITY_NAME,)

#: The workflow input contract tag is the domain cleanup contract.
MULTIPART_CLEANUP_REFERENCE_CONTRACT: Final[str] = MULTIPART_CLEANUP_CONTRACT

#: The bounded activity retry: infrastructure failures (dependency outage)
#: retry and claim a fresh bounded batch — every attempt is idempotent
#: because each per-row outcome write is fenced by the claim's lease token —
#: while the closed contract/state guard codes never retry.
MULTIPART_CLEANUP_ACTIVITY_MAXIMUM_ATTEMPTS: Final[int] = 5

#: One batch is at most 100 exact rows behind the store's own bounded
#: provider-call timeouts. The bound is not derived from a pinned per-row
#: budget: a slow batch that hits the cap fails the attempt and retries with
#: a fresh claim (idempotent), so the cap trades a throughput retry for
#: safety rather than risking a half-recorded batch.
MULTIPART_CLEANUP_ACTIVITY_START_TO_CLOSE_TIMEOUT: Final[timedelta] = timedelta(minutes=10)

#: The caller-side bound for every Temporal RPC the starter issues.
MULTIPART_CLEANUP_START_TIMEOUT: Final[timedelta] = timedelta(seconds=10)

#: The closed non-retryable error types (typed registry code values). The
#: dependency-outage family stays retryable so Temporal re-claims a fresh
#: bounded batch with bounded backoff; the store owns each row's own
#: closed reason and exact next retry.
MULTIPART_CLEANUP_NON_RETRYABLE_ERROR_TYPES: Final[tuple[str, ...]] = (
    "multipart_session_not_found",
    "multipart_session_state_invalid",
    "multipart_provider_state_invalid",
    "internal_error",
)

#: One dispatch sweep per minute: each sweep starts one bounded drain
#: workflow that itself completes as soon as no due row remains.
MULTIPART_CLEANUP_DISPATCH_POLL_INTERVAL_SECONDS: Final[float] = 60.0


class MultipartCleanupStartOutcome(StrEnum):
    """The closed dispatch outcomes of one sweep start attempt."""

    STARTED = "started"
    EXISTING = "existing"


def multipart_cleanup_retry_policy() -> RetryPolicy:
    """The bounded retry policy of the cleanup batch activity."""

    return RetryPolicy(
        initial_interval=timedelta(seconds=1),
        maximum_interval=timedelta(seconds=30),
        maximum_attempts=MULTIPART_CLEANUP_ACTIVITY_MAXIMUM_ATTEMPTS,
        non_retryable_error_types=list(MULTIPART_CLEANUP_NON_RETRYABLE_ERROR_TYPES),
    )


@dataclass(frozen=True, slots=True)
class MultipartCleanupBatchReference:
    """The closed batch-activity input: the token plus the claim bound."""

    contract: str
    batch_token: UUID
    batch_limit: int


@dataclass(frozen=True, slots=True)
class MultipartCleanupHeartbeatPayload:
    """The closed heartbeat details: opaque counts only."""

    batch_limit: int
    cleaned_count: int
    failed_count: int


class MultipartCleanupExecutor(Protocol):
    """The exact-cleanup entry the activity drives (spec 6.4).

    The production binding is the real multipart orchestration service's
    ``run_exact_cleanup``; the protocol exists so the worker composes
    against the single cleanup entry instead of the whole service surface.
    """

    async def run_exact_cleanup(
        self, *, batch_limit: int, diagnostic_context: DiagnosticContext
    ) -> MultipartCleanupBatchOutcome: ...


def _multipart_dependency_unavailable() -> MultipartUploadError:
    return MultipartUploadError(ErrorCode.MULTIPART_DEPENDENCY_UNAVAILABLE)


def _multipart_state_invalid() -> MultipartUploadError:
    return MultipartUploadError(ErrorCode.MULTIPART_SESSION_STATE_INVALID)


def _activity_context() -> DiagnosticContext:
    """A correlation context for the executor call; no content crosses it."""

    return DiagnosticContext(
        request_id=uuid4(),
        client_request_id=None,
        trace=TraceContext(
            trace_id=TraceId("0123456789abcdef0123456789abcdef"),
            remote_parent_span_id=None,
            local_span_id=SpanId("0123456789abcdef"),
            trace_flags=0,
        ),
        workflow_id=SafeToken.parse("multipart_cleanup_workflow"),
    )


class MultipartCleanupActivities:
    """The batch activity over the exact-cleanup executor.

    The activity forwards only the pinned claim bound and a fresh
    correlation context; the executor owns the claim, the exact provider
    calls and every per-row lease-fenced outcome write, so a retried
    activity is one more idempotent bounded claim. Typed failures map onto
    the closed Temporal error registry — registry retryability alone
    decides whether Temporal retries — and unexpected errors keep
    propagating with their cause chained, never swallowed.
    """

    def __init__(
        self,
        *,
        executor: MultipartCleanupExecutor,
        heartbeat_sink: Callable[[MultipartCleanupHeartbeatPayload], None] | None = None,
    ) -> None:
        self._executor = executor
        self._heartbeat_sink = heartbeat_sink

    def _heartbeat(self, payload: MultipartCleanupHeartbeatPayload) -> None:
        """Emit one opaque-count heartbeat through the injected sink."""

        if self._heartbeat_sink is not None:
            self._heartbeat_sink(payload)
            return
        try:
            activity.heartbeat(payload)
        except RuntimeError:
            # Outside a Temporal worker (unit tests) there is no heartbeat
            # channel; the activity's outcome contract is unchanged.
            return

    @activity.defn(name=MULTIPART_CLEANUP_BATCH_ACTIVITY_NAME)
    async def run_cleanup_batch_activity(
        self, reference: MultipartCleanupBatchReference
    ) -> MultipartCleanupBatchOutcome:
        if reference.contract != MULTIPART_CLEANUP_REFERENCE_CONTRACT:
            raise TemporalApplicationError("multipart_session_state_invalid", non_retryable=True)
        if reference.batch_limit < 1:
            raise TemporalApplicationError("multipart_session_state_invalid", non_retryable=True)
        self._heartbeat(
            MultipartCleanupHeartbeatPayload(
                batch_limit=reference.batch_limit, cleaned_count=0, failed_count=0
            )
        )
        try:
            outcome = await self._executor.run_exact_cleanup(
                batch_limit=reference.batch_limit,
                diagnostic_context=_activity_context(),
            )
        except ApplicationError as error:
            raise TemporalApplicationError(
                error.error_code.value, non_retryable=not error.is_retryable
            ) from error
        self._heartbeat(
            MultipartCleanupHeartbeatPayload(
                batch_limit=reference.batch_limit,
                cleaned_count=outcome.cleaned_count,
                failed_count=outcome.failed_count,
            )
        )
        return outcome


@workflow.defn(name=MULTIPART_CLEANUP_WORKFLOW_TYPE_NAME)
class MultipartCleanupWorkflow:
    """The bounded drain workflow of the exact cleanup (spec 6.4).

    The workflow owns no I/O and no private value: it schedules one batch
    activity per bounded claim, folds the closed counters, continues as new
    at the pinned bound with the cumulative totals, and completes once a
    batch claims nothing. Cancellation of the sweep returns the closed
    ``cancelled`` outcome — every unrecorded row keeps its durable leased
    obligation, and lease expiry returns it to the next sweep, so a
    cancelled workflow never becomes an untracked staging resource.
    """

    @workflow.run
    async def run(
        self, reference: MultipartCleanupBatchInput | MultipartCleanupContinuation
    ) -> str:
        continuation = reference if isinstance(reference, MultipartCleanupContinuation) else None
        batch_token = reference.batch_token
        counters = continuation.counters if continuation is not None else MultipartCleanupCounters()
        run_batch_count = 0
        try:
            while True:
                outcome: MultipartCleanupBatchOutcome = await workflow.execute_activity(
                    MULTIPART_CLEANUP_BATCH_ACTIVITY_NAME,
                    MultipartCleanupBatchReference(
                        contract=MULTIPART_CLEANUP_REFERENCE_CONTRACT,
                        batch_token=batch_token,
                        batch_limit=MULTIPART_CLEANUP_BATCH_LIMIT,
                    ),
                    start_to_close_timeout=MULTIPART_CLEANUP_ACTIVITY_START_TO_CLOSE_TIMEOUT,
                    retry_policy=multipart_cleanup_retry_policy(),
                    result_type=MultipartCleanupBatchOutcome,
                )
                counters = accumulate_cleanup_counters(
                    counters,
                    cleaned_count=outcome.cleaned_count,
                    failed_count=outcome.failed_count,
                )
                run_batch_count += 1
                if is_drain_complete(
                    cleaned_count=outcome.cleaned_count, failed_count=outcome.failed_count
                ):
                    return MultipartCleanupExecutionOutcome.COMPLETED.value
                if should_continue_as_new(run_batch_count=run_batch_count):
                    workflow.continue_as_new(
                        MultipartCleanupContinuation(
                            contract=MULTIPART_CLEANUP_REFERENCE_CONTRACT,
                            batch_token=batch_token,
                            counters=counters,
                        )
                    )
        except asyncio.CancelledError:
            # The in-flight batch keeps its per-row lease fencing: rows it
            # already recorded are durably cleaned or failed with their
            # closed reason and exact next retry; rows it never recorded
            # stay leased in ``cleanup_pending`` and re-enter the next
            # sweep after lease expiry. Nothing here may start provider
            # work that the durable obligation does not already cover.
            return MultipartCleanupExecutionOutcome.CANCELLED.value


class MultipartCleanupStarterPort(Protocol):
    """The start port the dispatch runtime consumes."""

    async def start_cleanup(
        self, reference: MultipartCleanupBatchInput
    ) -> MultipartCleanupStartOutcome: ...


class TemporalMultipartCleanupStarter:
    """Start one deterministic cleanup drain workflow per sweep token.

    Holds only the composition-owned Temporal client and the pinned
    queue/timeout bounds; every Temporal RPC it issues carries the pinned
    caller-side timeout. ``ALLOW_DUPLICATE_FAILED_ONLY`` plus
    ``USE_EXISTING`` implement the re-drive semantics: a failed closed run
    starts a fresh deterministic execution, while a running, completed or
    continued-as-new execution of the same token converges — one sweep can
    never produce a second concurrent drain of the same batch, and even a
    divergent duplicate stays safe because the store's skip-locked lease
    hands the two runs disjoint rows. SDK exceptions map onto the closed
    dependency-unavailable token with the provider error chained
    internally only.
    """

    def __init__(
        self,
        client: TemporalClient,
        *,
        task_queue: str = MULTIPART_CLEANUP_TASK_QUEUE,
        start_timeout: timedelta = MULTIPART_CLEANUP_START_TIMEOUT,
    ) -> None:
        if task_queue != MULTIPART_CLEANUP_TASK_QUEUE:
            raise ValueError("task queue must be the pinned multipart cleanup queue")
        self._client = client
        self._task_queue = task_queue
        self._start_timeout = start_timeout

    async def start_cleanup(
        self, reference: MultipartCleanupBatchInput
    ) -> MultipartCleanupStartOutcome:
        workflow_id = multipart_cleanup_workflow_id(reference.batch_token)
        try:
            await self._client.start_workflow(
                MULTIPART_CLEANUP_WORKFLOW_TYPE_NAME,
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
            raise _multipart_dependency_unavailable() from cause
        except Exception as cause:
            raise _multipart_dependency_unavailable() from cause
        return MultipartCleanupStartOutcome.STARTED

    async def _resolve_existing_execution(
        self, workflow_id: str, cause: WorkflowAlreadyStartedError
    ) -> MultipartCleanupStartOutcome:
        """Resolve a rejected duplicate run by describing the execution.

        The exact deterministic execution — same pinned type and task
        queue, running, completed or continued-as-new — is accepted and
        never terminated or replaced: it IS this sweep's drain. Any other
        shape is the closed terminal contract failure.
        """

        try:
            description = await self._client.get_workflow_handle(workflow_id).describe(
                rpc_timeout=self._start_timeout
            )
        except Exception as describe_cause:
            raise _multipart_dependency_unavailable() from describe_cause
        if (
            description.workflow_type != MULTIPART_CLEANUP_WORKFLOW_TYPE_NAME
            or description.task_queue != self._task_queue
        ):
            raise _multipart_state_invalid() from cause
        if description.status in (
            WorkflowExecutionStatus.RUNNING,
            WorkflowExecutionStatus.COMPLETED,
            WorkflowExecutionStatus.CONTINUED_AS_NEW,
        ):
            return MultipartCleanupStartOutcome.EXISTING
        raise _multipart_state_invalid() from cause


class MultipartCleanupDiagnosticSink(Protocol):
    """Structural sink the worker composition satisfies with its logger.

    The composition root injects the validating
    :class:`~personal_os.diagnostics.logging.DiagnosticLogger`, so a failed
    sweep start rides the structured logging boundary (and the rotating
    file sink) through the closed internal-error event; without a sink the
    runtime keeps the previous build-only behavior.
    """

    def emit(self, event_name: EventName, fields: Mapping[str, object] | None = None) -> None: ...


class MultipartCleanupDispatchRuntime:
    """One dispatcher's bounded sweep loop (spec 6.4).

    Every poll interval mints one fresh opaque batch token and starts one
    bounded drain workflow; the workflow itself completes as soon as no
    due row remains, so a quiet system runs one trivial sweep per minute.
    A failed start never kills the worker and never swallows its reason:
    the closed internal-error event carries the typed code, retryability,
    exception type and stack fingerprint of the failed sweep.
    """

    def __init__(
        self,
        *,
        starter: MultipartCleanupStarterPort,
        batch_token_generator: Callable[[], UUID] = uuid4,
        diagnostics: MultipartCleanupDiagnosticSink | None = None,
    ) -> None:
        self._starter = starter
        self._batch_token_generator = batch_token_generator
        self._diagnostics = diagnostics

    async def run_until_shutdown(
        self,
        shutdown: asyncio.Event,
        *,
        poll_interval_seconds: float = MULTIPART_CLEANUP_DISPATCH_POLL_INTERVAL_SECONDS,
    ) -> None:
        """Sweep until shutdown is signalled."""

        while not shutdown.is_set():
            await self.dispatch_cleanup_once()
            if shutdown.is_set():
                break
            try:
                await asyncio.wait_for(shutdown.wait(), timeout=poll_interval_seconds)
            except TimeoutError:
                continue

    async def dispatch_cleanup_once(self) -> MultipartCleanupStartOutcome | None:
        """Start one sweep; a surfaced start failure returns ``None``."""

        reference = MultipartCleanupBatchInput(
            contract=MULTIPART_CLEANUP_CONTRACT,
            batch_token=self._batch_token_generator(),
        )
        try:
            return await self._starter.start_cleanup(reference)
        except asyncio.CancelledError:
            raise
        except Exception as cause:
            self._emit_sweep_unavailable(cause)
            return None

    def _emit_sweep_unavailable(self, cause: BaseException) -> None:
        """Emit the closed sweep-unavailable event of one failed start."""

        if self._diagnostics is None:
            return
        if isinstance(cause, ApplicationError):
            error_code = SafeToken.parse(cause.error_code.value)
            category: object = cause.category
            is_retryable = bool(cause.is_retryable)
        else:
            error_code = SafeToken.parse(ErrorCode.MULTIPART_CLEANUP_FAILED.value)
            category = SafeToken.parse("internal")
            is_retryable = False
        self._diagnostics.emit(
            EventName.INTERNAL_ERROR,
            {
                "error_code": error_code,
                "error_category": category,
                "is_retryable": is_retryable,
                "exception_type": normalize_exception_type(cause),
                "stack_fingerprint": fingerprint_stack(cause),
            },
        )


# --- the exact-cleanup executor composition -------------------------------------


class _UnboundServicePort:
    """Typed fail-closed placeholder for service ports cleanup never touches.

    The exact-cleanup executor needs only the durable session store, the
    staging provider, the metrics sink and the clock. Every other
    orchestration port of the full service is bound to this placeholder so
    an accidental call fails closed with the typed dependency-unavailable
    token instead of silently doing nothing.
    """

    def __getattr__(self, attribute: str) -> Any:
        del attribute
        raise _multipart_dependency_unavailable()


_CLOSED_UNBOUND_SERVICE_PORT: Final[Any] = _UnboundServicePort()


def build_multipart_cleanup_executor(
    *,
    session_store: MultipartSessionStore,
    staging_provider: MultipartStagingProvider,
    clock: AwareUtcClock | None = None,
) -> MultipartUploadService:
    """Compose the exact-cleanup executor over the durable session store.

    Pure composition: nothing is opened here — the caller owns the engine
    behind the store and the provider's SDK client. The five orchestration
    ports a cleanup sweep never touches stay bound to the typed fail-closed
    placeholder, and the bounded in-memory metrics sink keeps every label
    inside its closed vocabulary.
    """

    return MultipartUploadService(
        session_store=session_store,
        evidence_store=cast("MultipartSessionEvidenceStore", _CLOSED_UNBOUND_SERVICE_PORT),
        operation_store=cast("SmallFileUploadOperationStore", _CLOSED_UNBOUND_SERVICE_PORT),
        policy_guard=cast("SmallFilePolicyGuard", _CLOSED_UNBOUND_SERVICE_PORT),
        current_sources=cast("CanonicalSourceReadStore", _CLOSED_UNBOUND_SERVICE_PORT),
        publication_gateway=cast("SmallFilePublicationGateway", _CLOSED_UNBOUND_SERVICE_PORT),
        object_store=cast("CanonicalObjectStore", _CLOSED_UNBOUND_SERVICE_PORT),
        staging_provider=staging_provider,
        staging_byte_source=cast("MultipartStagingByteSource", _CLOSED_UNBOUND_SERVICE_PORT),
        metrics=InMemoryMultipartUploadMetrics(),
        clock=clock if clock is not None else _utc_now,
    )


class _StagingKeyValidatingProvider(Protocol):
    """The R2 multipart adapter's six-method shape over validated keys."""

    async def create_upload(
        self, staging_key: MultipartStagingKey
    ) -> MultipartProviderUploadId: ...

    async def presign_part(
        self,
        staging_key: MultipartStagingKey,
        upload_id: MultipartProviderUploadId,
        part_range: MultipartPartRange,
    ) -> MultipartPartUrl: ...

    async def list_parts(
        self, staging_key: MultipartStagingKey, upload_id: MultipartProviderUploadId
    ) -> tuple[MultipartProviderPart, ...]: ...

    async def complete_upload(
        self,
        staging_key: MultipartStagingKey,
        upload_id: MultipartProviderUploadId,
        parts: Sequence[MultipartProviderPart],
    ) -> None: ...

    async def abort_upload(
        self, staging_key: MultipartStagingKey, upload_id: MultipartProviderUploadId
    ) -> None: ...

    async def delete_staging_object(self, staging_key: MultipartStagingKey) -> None: ...


def _validated_staging_key(staging_key: str) -> MultipartStagingKey:
    """Validate one persisted staging key against the closed grammar.

    The store only persists keys the creation boundary validated, so a key
    that fails the grammar here is a corrupted row surfaced as the closed
    provider-state-invalid rejection — never a value that could address a
    canonical object.
    """

    try:
        return MultipartStagingKey.parse(staging_key)
    except ValueError as cause:
        raise MultipartUploadError(ErrorCode.MULTIPART_PROVIDER_STATE_INVALID) from cause


class R2StagingKeyBoundProvider:
    """Bind the R2 multipart adapter onto the service's validated str seam.

    Every method validates the str staging key against the closed grammar
    before it crosses to the adapter, and translates the provider part
    facts between the adapter's and the domain's value objects. The exact
    identities — never a listing, prefix or wildcard — are the only thing
    that crosses.
    """

    def __init__(self, provider: _StagingKeyValidatingProvider) -> None:
        self._provider = provider

    async def create_upload(self, staging_key: str) -> MultipartProviderUploadId:
        return await self._provider.create_upload(_validated_staging_key(staging_key))

    async def presign_part(
        self,
        staging_key: str,
        upload_id: MultipartProviderUploadId,
        part_range: MultipartPartRange,
    ) -> MultipartPartUrl:
        return await self._provider.presign_part(
            _validated_staging_key(staging_key), upload_id, part_range
        )

    async def list_parts(
        self, staging_key: str, upload_id: MultipartProviderUploadId
    ) -> tuple[MultipartObservedPart, ...]:
        parts = await self._provider.list_parts(_validated_staging_key(staging_key), upload_id)
        return tuple(
            MultipartObservedPart(
                part_number=part.part_number,
                etag=part.etag,
                size_bytes=part.size_bytes,
            )
            for part in parts
        )

    async def complete_upload(
        self,
        staging_key: str,
        upload_id: MultipartProviderUploadId,
        parts: Sequence[MultipartObservedPart],
    ) -> None:
        await self._provider.complete_upload(
            _validated_staging_key(staging_key),
            upload_id,
            [
                MultipartProviderPart(
                    part_number=part.part_number,
                    etag=part.etag,
                    size_bytes=part.size_bytes,
                )
                for part in parts
            ],
        )

    async def abort_upload(self, staging_key: str, upload_id: MultipartProviderUploadId) -> None:
        await self._provider.abort_upload(_validated_staging_key(staging_key), upload_id)

    async def delete_staging_object(self, staging_key: str) -> None:
        await self._provider.delete_staging_object(_validated_staging_key(staging_key))


# --- the worker process composition ---------------------------------------------


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class MultipartCleanupProcess:
    """The composed worker pieces one process run owns."""

    worker: Worker
    dispatch_runtime: MultipartCleanupDispatchRuntime
    shutdown: asyncio.Event


def build_multipart_cleanup_process(
    *,
    executor: MultipartCleanupExecutor,
    temporal_client: TemporalClient,
    batch_token_generator: Callable[[], UUID] | None = None,
    diagnostics: MultipartCleanupDiagnosticSink | None = None,
) -> MultipartCleanupProcess:
    """Compose the cleanup activities, worker and dispatch runtime.

    Pure composition: no connection is opened here beyond what the caller
    already owns (the executor's store/engine and the connected Temporal
    client), so tests build the same graph against disposable stacks.
    """

    activities = MultipartCleanupActivities(executor=executor)
    worker = Worker(
        temporal_client,
        task_queue=MULTIPART_CLEANUP_TASK_QUEUE,
        workflows=[MultipartCleanupWorkflow],
        activities=[activities.run_cleanup_batch_activity],
    )
    starter = TemporalMultipartCleanupStarter(temporal_client)
    dispatch_runtime = MultipartCleanupDispatchRuntime(
        starter=starter,
        batch_token_generator=(
            batch_token_generator if batch_token_generator is not None else uuid4
        ),
        diagnostics=diagnostics,
    )
    return MultipartCleanupProcess(
        worker=worker, dispatch_runtime=dispatch_runtime, shutdown=asyncio.Event()
    )


def load_multipart_temporal_settings(
    *, environ: Mapping[str, str] | None = None
) -> TemporalDispatchSettings:
    """Load the Temporal target/namespace with the cleanup queue pinned."""

    settings = load_policy_temporal_settings(environ=environ)
    return TemporalDispatchSettings(
        target=settings.target,
        namespace=settings.namespace,
        task_queue=MULTIPART_CLEANUP_TASK_QUEUE,
    )


def _install_shutdown_signals(shutdown: asyncio.Event) -> None:
    """Request graceful shutdown on SIGINT/SIGTERM where handlers can install."""

    def _request_shutdown(signal_number: int, frame: object) -> None:
        del signal_number, frame
        shutdown.set()

    for signal_number in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(ValueError, OSError):
            signal.signal(signal_number, _request_shutdown)


async def run_multipart_cleanup_process() -> None:
    """Compose and run one exact-cleanup worker process until shutdown.

    Loads the validated runtime, database, object-storage and Temporal
    settings, refuses activation outside the loopback local/test exception,
    connects the R2 SDK client and the Temporal client under the pinned
    bounds, then runs the registered worker and the sweep loop
    concurrently. The engine and the R2 client manager are disposed on
    exit.
    """

    runtime_settings = load_runtime_settings(ServiceName.WORKER)
    temporal_settings = load_multipart_temporal_settings()
    require_dispatcher_activation_allowed(runtime_settings.environment, temporal_settings)
    diagnostics = configure_diagnostics(runtime_settings)
    database_settings = load_database_runtime_settings()
    password = read_database_runtime_password(database_settings)
    engine = create_source_store_engine(database_settings, password)
    object_storage_settings, object_storage_credentials = load_object_storage_settings()
    client_manager = R2ClientManager(object_storage_settings, object_storage_credentials)
    try:
        staging_client = await client_manager.get_multipart_staging_client()
        staging_provider = R2MultipartStagingProvider(
            staging_client,
            bucket=object_storage_settings.r2_bucket_name,
            logger=diagnostics,
        )
        session_store = PostgresqlMultipartUploadStore(engine, clock=_utc_now)
        executor = build_multipart_cleanup_executor(
            session_store=session_store,
            staging_provider=R2StagingKeyBoundProvider(staging_provider),
        )
        temporal_client = await asyncio.wait_for(
            TemporalClient.connect(temporal_settings.target, namespace=temporal_settings.namespace),
            timeout=MULTIPART_CLEANUP_START_TIMEOUT.total_seconds(),
        )
        process = build_multipart_cleanup_process(
            executor=executor, temporal_client=temporal_client, diagnostics=diagnostics
        )
        _install_shutdown_signals(process.shutdown)
        async with process.worker:
            await process.dispatch_runtime.run_until_shutdown(process.shutdown)
    finally:
        await client_manager.close()
        await dispose_source_store_engine(engine)


__all__ = [
    "MULTIPART_CLEANUP_ACTIVITY_MAXIMUM_ATTEMPTS",
    "MULTIPART_CLEANUP_ACTIVITY_NAMES",
    "MULTIPART_CLEANUP_ACTIVITY_START_TO_CLOSE_TIMEOUT",
    "MULTIPART_CLEANUP_BATCH_ACTIVITY_NAME",
    "MULTIPART_CLEANUP_DISPATCH_POLL_INTERVAL_SECONDS",
    "MULTIPART_CLEANUP_NON_RETRYABLE_ERROR_TYPES",
    "MULTIPART_CLEANUP_REFERENCE_CONTRACT",
    "MULTIPART_CLEANUP_START_TIMEOUT",
    "MULTIPART_CLEANUP_TASK_QUEUE",
    "MULTIPART_CLEANUP_WORKFLOW_TYPE_NAME",
    "MultipartCleanupActivities",
    "MultipartCleanupBatchReference",
    "MultipartCleanupDispatchRuntime",
    "MultipartCleanupExecutor",
    "MultipartCleanupHeartbeatPayload",
    "MultipartCleanupProcess",
    "MultipartCleanupStartOutcome",
    "MultipartCleanupWorkflow",
    "R2StagingKeyBoundProvider",
    "TemporalMultipartCleanupStarter",
    "build_multipart_cleanup_executor",
    "build_multipart_cleanup_process",
    "load_multipart_temporal_settings",
    "multipart_cleanup_retry_policy",
    "run_multipart_cleanup_process",
]
