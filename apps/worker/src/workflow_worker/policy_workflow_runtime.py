"""Policy-preview dispatch runtime and Temporal worker process composition.

The :class:`PolicyPreviewDispatchRuntime` owns the leased-outbox loop of the
asynchronous preview (spec 10): sweep the execution deadline and the ready
expiry, reclaim expired leases, claim at most the pinned batch of due
pending previews, and start each one's deterministic Temporal workflow.
A converged start (started or the exact existing execution) needs no
acknowledgement — the activity itself owns the fenced lifecycle transitions
inside its single transaction. A retryable start failure releases the lease
back to pending with the bounded backoff; a terminal contract failure fails
the row with the closed dispatch-terminal code; an unknown outcome stays
leased for expiry. The deterministic workflow ID makes every re-dispatch
converge on the same execution after a lost start acknowledgement.

The process composition wires the real engine, Temporal client, preview
store, activity and :class:`temporalio.worker.Worker` on the pinned
``exclusion-policy-preview`` queue, reuses the loopback-only local/test
activation gate of the projection dispatcher, and disposes the engine on
shutdown. Temporal workers and clients are created here only — never at
import time.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Protocol, runtime_checkable
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncEngine
from temporalio.client import Client as TemporalClient
from temporalio.worker import Worker

from personal_os.diagnostics.events import SafeToken
from personal_os.error_contracts.exceptions import ApplicationError
from personal_os.runtime_configuration.loading import load_runtime_settings
from personal_os.runtime_configuration.models import ServiceName
from postgresql_source_store.engine import (
    create_source_store_engine,
    dispose_source_store_engine,
)
from postgresql_source_store.policy_previews import (
    POLICY_PREVIEW_CLAIM_BATCH_LIMIT,
    PREVIEW_DISPATCH_TERMINAL_ERROR_CODE,
    LeasedPolicyPreview,
    PostgresqlPolicyPreviewStore,
)
from postgresql_source_store.settings import (
    load_database_runtime_settings,
    read_database_runtime_password,
)
from workflow_worker.policy_preview_workflow import (
    POLICY_PREVIEW_START_TIMEOUT,
    POLICY_PREVIEW_TASK_QUEUE,
    PolicyPreviewActivities,
    PolicyPreviewReference,
    PolicyPreviewStartOutcome,
    PolicyPreviewWorkflow,
    TemporalPolicyPreviewStarter,
    preview_reference_for_lease,
)
from workflow_worker.projection_dispatch_runtime import (
    DEFAULT_TEMPORAL_NAMESPACE,
    DEFAULT_TEMPORAL_TARGET,
    TemporalDispatchSettings,
    _validate_namespace,
    _validate_target,
    require_dispatcher_activation_allowed,
)

#: The pause between dispatch cycles when no claim is outstanding.
POLICY_PREVIEW_DISPATCH_POLL_INTERVAL_SECONDS: Final[float] = 1.0

_ENVIRONMENT_PREFIX: Final[str] = "KNOWLEDGE_"

_POLICY_TEMPORAL_ENVIRONMENT_FIELDS: Final[Mapping[str, str]] = {
    "KNOWLEDGE_TEMPORAL_TARGET": "target",
    "KNOWLEDGE_TEMPORAL_NAMESPACE": "namespace",
}


@runtime_checkable
class PolicyPreviewOutboxStore(Protocol):
    """The leased-outbox slice of the preview store the runtime consumes."""

    async def expire_overdue_previews(self, now: datetime) -> object: ...

    async def reclaim_expired_leases(self, now: datetime) -> int: ...

    async def claim_pending_previews(
        self, now: datetime, limit: int
    ) -> list[LeasedPolicyPreview]: ...

    async def release_retry(
        self,
        preview_id: UUID,
        lease_token: UUID,
        error_code: SafeToken,
        now: datetime,
    ) -> bool: ...

    async def mark_preview_failed(self, preview_id: UUID, error_code: SafeToken) -> bool: ...


class PolicyPreviewStarterPort(Protocol):
    """The start port the runtime consumes."""

    async def start_policy_preview(
        self, reference: PolicyPreviewReference
    ) -> PolicyPreviewStartOutcome: ...


class AwareUtcClock(Protocol):
    """The injected clock port (mirrors the projection dispatcher)."""

    def __call__(self) -> datetime: ...


def load_policy_temporal_settings(
    *, environ: Mapping[str, str] | None = None
) -> TemporalDispatchSettings:
    """Load the Temporal target/namespace for the policy preview worker.

    The target and namespace reuse the projection dispatcher's registered
    environment names and validation rules; the task queue is pinned to the
    preview queue. Any ``KNOWLEDGE_*`` key outside the registry is terminal
    (the projection loader enforces the full registry; here the shared
    validation helpers keep the same grammar).
    """

    from personal_os.error_contracts.codes import ErrorCode
    from personal_os.error_contracts.exceptions import ConfigurationError
    from personal_os.runtime_configuration.environment_names import (
        KNOWN_KNOWLEDGE_ENVIRONMENT_NAMES,
    )

    source = dict(os.environ if environ is None else environ)
    unknown_count = sum(
        1
        for key in source
        if key.startswith(_ENVIRONMENT_PREFIX) and key not in KNOWN_KNOWLEDGE_ENVIRONMENT_NAMES
    )
    if unknown_count:
        raise ConfigurationError(
            ErrorCode.CONFIGURATION_UNKNOWN_KEY, safe_details={"count": unknown_count}
        )
    raw_values = {
        field_name: source[environment_name]
        for environment_name, field_name in _POLICY_TEMPORAL_ENVIRONMENT_FIELDS.items()
        if environment_name in source
    }
    target = _validate_target(raw_values.get("target", DEFAULT_TEMPORAL_TARGET))
    namespace = _validate_namespace(raw_values.get("namespace", DEFAULT_TEMPORAL_NAMESPACE))
    return TemporalDispatchSettings(
        target=target, namespace=namespace, task_queue=POLICY_PREVIEW_TASK_QUEUE
    )


class PolicyPreviewDispatchRuntime:
    """One dispatcher's bounded sweep/reclaim/claim/start loop (spec 10).

    All time arrives through the injected clock; persistence stamps database
    time. The runtime owns no Temporal type: it consumes the starter port and
    the provider-neutral outbox store, so unit tests drive it entirely with
    fakes.
    """

    def __init__(
        self,
        *,
        preview_store: PolicyPreviewOutboxStore,
        starter: PolicyPreviewStarterPort,
        clock: AwareUtcClock,
    ) -> None:
        self._preview_store = preview_store
        self._starter = starter
        self._clock = clock

    async def run_until_shutdown(
        self,
        shutdown: asyncio.Event,
        *,
        poll_interval_seconds: float = POLICY_PREVIEW_DISPATCH_POLL_INTERVAL_SECONDS,
    ) -> None:
        """Sweep, reclaim and dispatch until shutdown is signalled."""

        while not shutdown.is_set():
            await self.dispatch_pending_previews_once()
            if shutdown.is_set():
                break
            try:
                await asyncio.wait_for(shutdown.wait(), timeout=poll_interval_seconds)
            except TimeoutError:
                continue

    async def dispatch_pending_previews_once(self) -> int:
        """Run one sweep/reclaim/claim/dispatch cycle and return the claimed count."""

        now = self._clock()
        await self._preview_store.expire_overdue_previews(now)
        await self._preview_store.reclaim_expired_leases(now)
        claimed = await self._preview_store.claim_pending_previews(
            now, POLICY_PREVIEW_CLAIM_BATCH_LIMIT
        )
        for lease in claimed:
            await self._dispatch_lease(lease, now)
        return len(claimed)

    async def _dispatch_lease(self, lease: LeasedPolicyPreview, now: datetime) -> None:
        reference = preview_reference_for_lease(lease)
        try:
            outcome = await self._starter.start_policy_preview(reference)
        except ApplicationError as error:
            if error.is_retryable:
                await self._preview_store.release_retry(
                    lease.policy_preview_id, lease.lease_token, _safe_code(error), now
                )
            else:
                await self._preview_store.mark_preview_failed(
                    lease.policy_preview_id, PREVIEW_DISPATCH_TERMINAL_ERROR_CODE
                )
            return
        except Exception:
            # An unexpected start failure leaves the outcome unknown: the
            # lease stays held and lease expiry reclaims it.
            return
        del outcome  # Started and existing converge on the same execution.


def _safe_code(error: ApplicationError) -> SafeToken:
    """The closed column-safe error code for a retryable release."""

    value = error.error_code.value
    return SafeToken.parse(value)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _install_shutdown_signals(shutdown: asyncio.Event) -> None:
    """Request graceful shutdown on SIGINT/SIGTERM where handlers can install."""

    def _request_shutdown(signal_number: int, frame: object) -> None:
        del signal_number, frame
        shutdown.set()

    for signal_number in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(ValueError, OSError):
            signal.signal(signal_number, _request_shutdown)


@dataclass(frozen=True, slots=True)
class PolicyPreviewProcess:
    """The composed worker pieces one process run owns."""

    worker: Worker
    dispatch_runtime: PolicyPreviewDispatchRuntime
    shutdown: asyncio.Event


def build_policy_preview_process(
    *,
    engine: AsyncEngine,
    temporal_client: TemporalClient,
    lease_token_generator: Callable[[], UUID] | None = None,
    clock: AwareUtcClock = _utc_now,
) -> PolicyPreviewProcess:
    """Compose the preview store, activities, worker and dispatch runtime.

    Pure composition: no connection is opened here beyond what the caller
    already owns (the engine and the connected Temporal client), so tests
    build the same graph against disposable stacks.
    """

    store = PostgresqlPolicyPreviewStore(
        engine,
        lease_token_generator=lease_token_generator if lease_token_generator is not None else uuid4,
    )
    activities = PolicyPreviewActivities(preview_store=store)
    worker = Worker(
        temporal_client,
        task_queue=POLICY_PREVIEW_TASK_QUEUE,
        workflows=[PolicyPreviewWorkflow],
        activities=[activities.run_policy_preview_activity],
    )
    starter = TemporalPolicyPreviewStarter(temporal_client)
    dispatch_runtime = PolicyPreviewDispatchRuntime(
        preview_store=store, starter=starter, clock=clock
    )
    return PolicyPreviewProcess(
        worker=worker, dispatch_runtime=dispatch_runtime, shutdown=asyncio.Event()
    )


async def run_policy_preview_process() -> None:
    """Compose and run one preview worker process until a shutdown signal.

    Loads the validated runtime, database and Temporal settings, refuses
    activation outside the loopback local/test exception, connects the
    Temporal client under the pinned bound, then runs the registered worker
    and the dispatch loop concurrently. The engine is disposed on exit.
    """

    runtime_settings = load_runtime_settings(ServiceName.WORKER)
    database_settings = load_database_runtime_settings()
    temporal_settings = load_policy_temporal_settings()
    require_dispatcher_activation_allowed(runtime_settings.environment, temporal_settings)
    password = read_database_runtime_password(database_settings)
    engine = create_source_store_engine(database_settings, password)
    try:
        temporal_client = await asyncio.wait_for(
            TemporalClient.connect(
                temporal_settings.target, namespace=temporal_settings.namespace
            ),
            timeout=POLICY_PREVIEW_START_TIMEOUT.total_seconds(),
        )
    except TimeoutError as cause:
        from personal_os.error_contracts.codes import ErrorCode
        from personal_os.exclusion_policy.errors import ExclusionPolicyError

        raise ExclusionPolicyError(ErrorCode.EXCLUSION_POLICY_COMMIT_OUTCOME_UNKNOWN) from cause
    process = build_policy_preview_process(engine=engine, temporal_client=temporal_client)
    _install_shutdown_signals(process.shutdown)
    try:
        async with process.worker:
            await process.dispatch_runtime.run_until_shutdown(process.shutdown)
    finally:
        await dispose_source_store_engine(engine)


__all__ = [
    "POLICY_PREVIEW_DISPATCH_POLL_INTERVAL_SECONDS",
    "AwareUtcClock",
    "LeasedPolicyPreview",
    "PolicyPreviewDispatchRuntime",
    "PolicyPreviewOutboxStore",
    "PolicyPreviewProcess",
    "PolicyPreviewStarterPort",
    "build_policy_preview_process",
    "load_policy_temporal_settings",
    "run_policy_preview_process",
]
