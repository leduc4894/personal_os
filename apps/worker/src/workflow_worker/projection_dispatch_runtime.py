"""Bounded projection dispatch runtime over the leased intent outbox.

This worker-owned composition module runs the design section 11.4 loop:
reclaim expired leases, claim at most the pinned batch of 50, start at most
eight workflows concurrently, then apply the fenced persistence transition
matching each outcome — started/existing acknowledges dispatched, a retryable
failure releases to pending with the capped backoff and a contract failure
marks terminal. A fenced transition that affects zero rows is a stale lease:
state is never overwritten, the store already emitted the stale-lease
diagnostic pre-commit, and no dispatch metric label is recorded for it.
Graceful shutdown stops new claims, waits for the bounded in-flight start
calls and leaves attempts with unknown outcomes leased for expiry.

The module also owns the Temporal dispatch settings fragment
(``KNOWLEDGE_TEMPORAL_TARGET``/``_NAMESPACE``/``_TASK_QUEUE``) and the
activation gate: local/test permits only loopback unauthenticated Temporal,
and staging/production refuses dispatcher activation until a deployment spec
supplies tested TLS/auth settings.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import os
import re
import signal
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Protocol, runtime_checkable

from temporalio.client import Client as TemporalClient

from personal_os.diagnostics.events import EventName, SafeToken
from personal_os.error_contracts.codes import ErrorCategory, ErrorCode
from personal_os.error_contracts.exceptions import ApplicationError, ConfigurationError
from personal_os.runtime_configuration.environment_names import (
    KNOWN_KNOWLEDGE_ENVIRONMENT_NAMES,
)
from personal_os.runtime_configuration.loading import load_runtime_settings
from personal_os.runtime_configuration.models import RuntimeEnvironment, ServiceName
from personal_os.sources.errors import ProjectionDispatchError
from personal_os.sources.metrics import (
    ProjectionDispatchErrorCode,
    ProjectionDispatchOutcome,
    ProjectionKind,
    SourcePublicationMetrics,
)
from personal_os.sources.ports import AwareUtcClock, ProjectionIntentStore
from personal_os.sources.projection_dispatch import (
    PROJECTION_CLAIM_BATCH_LIMIT,
    LeasedProjectionIntent,
    retry_available_at,
)
from postgresql_source_store.engine import (
    create_source_store_engine,
    dispose_source_store_engine,
)
from postgresql_source_store.projection_intents import PostgresqlProjectionIntentStore
from postgresql_source_store.settings import (
    load_database_runtime_settings,
    read_database_runtime_password,
)
from workflow_worker.projection_workflow_starter import (
    PROJECTION_WORKFLOW_START_TIMEOUT,
    PROJECTION_WORKFLOW_TASK_QUEUE,
    ProjectionWorkflowStarter,
    ProjectionWorkflowStartResult,
    TemporalProjectionWorkflowStarter,
    source_ingestion_reference_for_intent,
)

#: The pinned maximum number of concurrent workflow starts (design 11.2).
PROJECTION_DISPATCH_CONCURRENCY_LIMIT: Final[int] = 8

#: The pause between dispatch cycles when no claim is outstanding.
PROJECTION_DISPATCH_POLL_INTERVAL_SECONDS: Final[float] = 1.0

#: Retryable database failures wait for the existing bounded poll interval.
_RETRY_DELAY_SECONDS: Final[float] = PROJECTION_DISPATCH_POLL_INTERVAL_SECONDS

#: Default Temporal target: the loopback-only unauthenticated local exception.
DEFAULT_TEMPORAL_TARGET: Final[str] = "127.0.0.1:7233"

#: Default Temporal namespace provisioned by the local service stack.
DEFAULT_TEMPORAL_NAMESPACE: Final[str] = "knowledge"

_ENVIRONMENT_PREFIX: Final[str] = "KNOWLEDGE_"

_TEMPORAL_ENVIRONMENT_FIELDS: Final[Mapping[str, str]] = {
    "KNOWLEDGE_TEMPORAL_TARGET": "target",
    "KNOWLEDGE_TEMPORAL_NAMESPACE": "namespace",
    "KNOWLEDGE_TEMPORAL_TASK_QUEUE": "task_queue",
}

#: Loopback host literals accepted for the unauthenticated local exception.
_LOOPBACK_TARGET_HOSTS: Final[frozenset[str]] = frozenset({"127.0.0.1", "localhost", "::1"})

_TARGET_PATTERN: Final[re.Pattern[str]] = re.compile(r"^(.{1,253}):([0-9]{1,5})$")

_NAMESPACE_PATTERN: Final[re.Pattern[str]] = re.compile(r"[a-z][a-z0-9-]{0,62}")

_DISPATCHER_ENVIRONMENTS: Final[frozenset[RuntimeEnvironment]] = frozenset(
    {RuntimeEnvironment.LOCAL, RuntimeEnvironment.TEST}
)


@dataclass(frozen=True, slots=True)
class TemporalDispatchSettings:
    """Frozen snapshot of the Temporal dispatch target, namespace and queue."""

    target: str = DEFAULT_TEMPORAL_TARGET
    namespace: str = DEFAULT_TEMPORAL_NAMESPACE
    task_queue: str = PROJECTION_WORKFLOW_TASK_QUEUE


@runtime_checkable
class ProjectionDiagnosticSink(Protocol):
    """Structural sink the composition root satisfies with its logger."""

    def emit(self, event_name: EventName, fields: Mapping[str, object] | None = None) -> None: ...


def _validate_target(value: str) -> str:
    """Require a loopback ``host:port`` target within the bind-range port set."""
    match = _TARGET_PATTERN.fullmatch(value)
    if match is None:
        raise ConfigurationError(ErrorCode.CONFIGURATION_INVALID)
    host, raw_port = match.groups()
    port = int(raw_port)
    if port < 1 or port > 65535 or host not in _LOOPBACK_TARGET_HOSTS:
        raise ConfigurationError(ErrorCode.CONFIGURATION_INVALID)
    return value


def _validate_namespace(value: str) -> str:
    if _NAMESPACE_PATTERN.fullmatch(value) is None:
        raise ConfigurationError(ErrorCode.CONFIGURATION_INVALID)
    return value


def load_temporal_dispatch_settings(
    *, environ: Mapping[str, str] | None = None
) -> TemporalDispatchSettings:
    """Load the frozen Temporal dispatch settings from an environment snapshot.

    Any ``KNOWLEDGE_*`` key outside the repository-wide registry is terminal
    ``configuration_unknown_key``; the target must be a loopback ``host:port``
    pair (the only approved unauthenticated exception), the namespace a safe
    token, and the task queue exactly the pinned queue.
    """
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
        for environment_name, field_name in _TEMPORAL_ENVIRONMENT_FIELDS.items()
        if environment_name in source
    }
    target = _validate_target(raw_values.get("target", DEFAULT_TEMPORAL_TARGET))
    namespace = _validate_namespace(raw_values.get("namespace", DEFAULT_TEMPORAL_NAMESPACE))
    task_queue = raw_values.get("task_queue", PROJECTION_WORKFLOW_TASK_QUEUE)
    if task_queue != PROJECTION_WORKFLOW_TASK_QUEUE:
        raise ConfigurationError(ErrorCode.CONFIGURATION_INVALID)
    return TemporalDispatchSettings(target=target, namespace=namespace, task_queue=task_queue)


def require_dispatcher_activation_allowed(
    environment: RuntimeEnvironment, settings: TemporalDispatchSettings
) -> None:
    """Refuse dispatcher activation outside the loopback local/test exception.

    Staging and production refuse until a deployment spec supplies tested
    TLS/auth settings; local/test settings have already restricted the target
    to loopback.
    """
    if environment not in _DISPATCHER_ENVIRONMENTS:
        raise ConfigurationError(ErrorCode.CONFIGURATION_INVALID)
    _validate_target(settings.target)


def _dispatch_metric_kind(intent: LeasedProjectionIntent) -> ProjectionKind:
    return ProjectionKind(intent.projection_kind.value)


def _dispatch_error_metric_code(error_code: SafeToken) -> ProjectionDispatchErrorCode:
    return ProjectionDispatchErrorCode(error_code.value)


class ProjectionDispatchRuntime:
    """One dispatcher's bounded reclaim/claim/start/acknowledge loop.

    All time arrives through the injected clock for due/backoff decisions;
    persistence stamps database time. The runtime owns no Temporal type: it
    consumes the :class:`ProjectionWorkflowStarter` port and the
    provider-neutral intent store, so unit tests drive it entirely with fakes.
    """

    def __init__(
        self,
        *,
        store: ProjectionIntentStore,
        starter: ProjectionWorkflowStarter,
        clock: AwareUtcClock,
        diagnostics: ProjectionDiagnosticSink | None = None,
        metrics: SourcePublicationMetrics | None = None,
        concurrency_limit: int = PROJECTION_DISPATCH_CONCURRENCY_LIMIT,
    ) -> None:
        if concurrency_limit < 1 or concurrency_limit > PROJECTION_DISPATCH_CONCURRENCY_LIMIT:
            raise ValueError("concurrency_limit must be between 1 and the pinned limit")
        self._store = store
        self._starter = starter
        self._clock = clock
        self._diagnostics = diagnostics
        self._metrics = metrics
        self._concurrency_limit = concurrency_limit

    async def run_until_shutdown(
        self,
        shutdown: asyncio.Event,
        *,
        poll_interval_seconds: float = PROJECTION_DISPATCH_POLL_INTERVAL_SECONDS,
    ) -> None:
        """Reclaim, claim and dispatch until shutdown is signalled.

        Each cycle processes one bounded batch; shutdown stops new claims and
        lets the in-flight batch finish inside the pinned start-call bounds
        before returning. Attempts whose outcome stays unknown remain leased
        for expiry.
        """
        while not shutdown.is_set():
            try:
                await self.dispatch_pending_intents_once()
            except ProjectionDispatchError as error:
                if error.error_code is not ErrorCode.PROJECTION_DISPATCH_UNAVAILABLE:
                    raise
                await _wait_for_shutdown_or_delay(shutdown, _RETRY_DELAY_SECONDS)
                continue
            if shutdown.is_set():
                break
            await _wait_for_shutdown_or_delay(shutdown, poll_interval_seconds)

    async def dispatch_pending_intents_once(self) -> int:
        """Run one reclaim/claim/dispatch cycle and return the claimed count."""
        await self._store.reclaim_expired(self._clock())
        claimed = await self._store.claim_batch(self._clock(), PROJECTION_CLAIM_BATCH_LIMIT)
        if not claimed:
            return 0
        # Every Temporal call inside the group — the start and any
        # duplicate-run resolution describe — carries the pinned caller-side
        # RPC timeout, so waiting for the group waits at most that bound per
        # attempt.
        semaphore = asyncio.Semaphore(self._concurrency_limit)

        async def dispatch_bounded(intent: LeasedProjectionIntent) -> None:
            async with semaphore:
                await self._dispatch_intent(intent)

        async with asyncio.TaskGroup() as tasks:
            for intent in claimed:
                tasks.create_task(dispatch_bounded(intent))
        return len(claimed)

    async def _dispatch_intent(self, intent: LeasedProjectionIntent) -> None:
        started_monotonic = time.monotonic()
        try:
            reference = source_ingestion_reference_for_intent(intent)
        except ApplicationError as error:
            await self._apply_terminal(intent, error, started_monotonic)
            return
        try:
            result = await self._starter.start_source_ingestion(reference)
        except ApplicationError as error:
            if error.is_retryable:
                await self._apply_retryable(intent, error, started_monotonic)
            else:
                await self._apply_terminal(intent, error, started_monotonic)
            return
        except Exception:
            # An unexpected start failure leaves the outcome unknown: the
            # attempt stays leased and lease expiry reclaims it.
            self._emit_transition_failure(
                intent,
                SafeToken.parse(ErrorCode.PROJECTION_DISPATCH_UNAVAILABLE.value),
                started_monotonic,
            )
            return
        await self._apply_dispatched(intent, result, started_monotonic)

    @staticmethod
    def _elapsed_ms(started_monotonic: float) -> int:
        return max(0, int((time.monotonic() - started_monotonic) * 1000))

    async def _apply_dispatched(
        self,
        intent: LeasedProjectionIntent,
        result: ProjectionWorkflowStartResult,
        started_monotonic: float,
    ) -> None:
        duration_ms = self._elapsed_ms(started_monotonic)
        try:
            acknowledged = await self._store.acknowledge_dispatched(
                intent.projection_intent_id, intent.lease_token, self._clock()
            )
        except ApplicationError as error:
            self._emit_transition_failure(
                intent, SafeToken.parse(error.error_code.value), started_monotonic
            )
            return
        if not acknowledged:
            # Stale lease: the fenced transition affected zero rows and the
            # store emitted the stale-lease diagnostic; nothing is overwritten
            # and no closed metric label exists for this outcome.
            return
        self._emit_dispatched(intent, result, duration_ms)
        if self._metrics is not None:
            self._metrics.record_dispatch(
                projection_kind=_dispatch_metric_kind(intent),
                outcome=ProjectionDispatchOutcome.DISPATCHED,
                duration_seconds=duration_ms / 1000,
            )

    async def _apply_retryable(
        self,
        intent: LeasedProjectionIntent,
        error: ApplicationError,
        started_monotonic: float,
    ) -> None:
        duration_ms = self._elapsed_ms(started_monotonic)
        error_code = SafeToken.parse(error.error_code.value)
        now = self._clock()
        available_at = retry_available_at(now, intent.attempt_count)
        try:
            released = await self._store.release_retry(
                intent.projection_intent_id,
                intent.lease_token,
                error_code,
                available_at,
                now,
            )
        except ApplicationError as transition_error:
            self._emit_transition_failure(
                intent, SafeToken.parse(transition_error.error_code.value), started_monotonic
            )
            return
        if not released:
            return
        self._emit_failed(
            intent,
            outcome=SafeToken.parse("pending"),
            error_code=error_code,
            error_category=error.category,
            is_retryable=True,
            duration_ms=duration_ms,
        )
        if self._metrics is not None:
            self._metrics.record_dispatch(
                projection_kind=_dispatch_metric_kind(intent),
                outcome=ProjectionDispatchOutcome.PENDING,
                duration_seconds=duration_ms / 1000,
                error_code=_dispatch_error_metric_code(error_code),
            )

    async def _apply_terminal(
        self,
        intent: LeasedProjectionIntent,
        error: ApplicationError,
        started_monotonic: float,
    ) -> None:
        duration_ms = self._elapsed_ms(started_monotonic)
        error_code = SafeToken.parse(error.error_code.value)
        try:
            terminated = await self._store.mark_terminal(
                intent.projection_intent_id,
                intent.lease_token,
                error_code,
                self._clock(),
            )
        except ApplicationError as transition_error:
            self._emit_transition_failure(
                intent, SafeToken.parse(transition_error.error_code.value), started_monotonic
            )
            return
        if not terminated:
            return
        self._emit_failed(
            intent,
            outcome=SafeToken.parse("terminal"),
            error_code=error_code,
            error_category=error.category,
            is_retryable=False,
            duration_ms=duration_ms,
        )
        if self._metrics is not None:
            self._metrics.record_dispatch(
                projection_kind=_dispatch_metric_kind(intent),
                outcome=ProjectionDispatchOutcome.TERMINAL,
                duration_seconds=duration_ms / 1000,
                error_code=_dispatch_error_metric_code(error_code),
            )

    def _emit_dispatched(
        self,
        intent: LeasedProjectionIntent,
        result: ProjectionWorkflowStartResult,
        duration_ms: int,
    ) -> None:
        if self._diagnostics is None:
            return
        self._diagnostics.emit(
            EventName.PROJECTION_INTENT_DISPATCHED,
            {
                "projection_kind": intent.projection_kind,
                "outcome": SafeToken.parse(result.value),
                "duration_ms": duration_ms,
                "attempt_count": intent.attempt_count,
                "intent_id": intent.projection_intent_id,
            },
        )

    def _emit_failed(
        self,
        intent: LeasedProjectionIntent,
        *,
        outcome: SafeToken,
        error_code: SafeToken,
        error_category: ErrorCategory,
        is_retryable: bool,
        duration_ms: int,
    ) -> None:
        if self._diagnostics is None:
            return
        self._diagnostics.emit(
            EventName.PROJECTION_INTENT_DISPATCH_FAILED,
            {
                "projection_kind": intent.projection_kind,
                "outcome": outcome,
                "duration_ms": duration_ms,
                "attempt_count": intent.attempt_count,
                "intent_id": intent.projection_intent_id,
                "error_code": error_code,
                "error_category": SafeToken.parse(error_category.value),
                "is_retryable": is_retryable,
            },
        )

    def _emit_transition_failure(
        self,
        intent: LeasedProjectionIntent,
        error_code: SafeToken,
        started_monotonic: float,
    ) -> None:
        """Report a fenced transition that could not record its outcome.

        The attempt stays leased for expiry; only the closed failure surface
        is emitted, with no provider or SQL text.
        """
        if self._diagnostics is None:
            return
        self._diagnostics.emit(
            EventName.PROJECTION_INTENT_DISPATCH_FAILED,
            {
                "projection_kind": intent.projection_kind,
                "outcome": SafeToken.parse("pending"),
                "duration_ms": self._elapsed_ms(started_monotonic),
                "attempt_count": intent.attempt_count,
                "intent_id": intent.projection_intent_id,
                "error_code": error_code,
                "error_category": SafeToken.parse(ErrorCategory.DEPENDENCY.value),
                "is_retryable": True,
            },
        )


async def _wait_for_shutdown_or_delay(shutdown: asyncio.Event, delay_seconds: float) -> None:
    """Wait for shutdown or one bounded dispatch-loop delay."""
    try:
        await asyncio.wait_for(shutdown.wait(), timeout=delay_seconds)
    except TimeoutError:
        return


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


async def close_temporal_client(client: TemporalClient) -> None:
    """Close one dispatcher-owned Temporal client.

    temporalio 1.30.0 exposes no ``Client.close()`` or ``ServiceClient.close()``
    surface — gRPC connections live on the shared Core runtime and are torn
    down with the process — so there is nothing to await today. This seam
    keeps every dispatcher exit path calling the per-client close the moment
    a pinned temporalio bump reintroduces one, and the lifecycle unit tests
    prove the dispatcher reaches the close on every exit path with a
    close-bearing double.
    """
    close = getattr(client, "close", None)
    if callable(close):
        pending_close = close()
        if inspect.isawaitable(pending_close):
            await pending_close


async def run_projection_dispatcher_process() -> None:
    """Compose and run one dispatcher process until a shutdown signal.

    Loads the validated runtime, database and Temporal dispatch settings,
    refuses activation outside the loopback local/test exception, then runs
    the bounded dispatch loop over the composition-owned engine, intent
    store, Temporal client and starter. Clients and pools are created here
    only — never at import time — and the Temporal client is closed and the
    engine disposed on every exit path, while attempts with unknown outcomes
    stay leased for expiry. The Temporal connect carries the pinned
    caller-side bound (``Client.connect`` exposes no timeout keyword, so the
    bound is applied with ``asyncio.wait_for``); a connect failure — the
    timeout or any other refused/TLS/DNS outcome — surfaces as the retryable
    dispatch-unavailable failure rather than hanging startup or escaping as
    a raw traceback.
    """
    runtime_settings = load_runtime_settings(ServiceName.WORKER)
    database_settings = load_database_runtime_settings()
    temporal_settings = load_temporal_dispatch_settings()
    require_dispatcher_activation_allowed(runtime_settings.environment, temporal_settings)
    password = read_database_runtime_password(database_settings)
    engine = create_source_store_engine(database_settings, password)
    temporal_client: TemporalClient | None = None
    try:
        try:
            temporal_client = await asyncio.wait_for(
                TemporalClient.connect(
                    temporal_settings.target, namespace=temporal_settings.namespace
                ),
                timeout=PROJECTION_WORKFLOW_START_TIMEOUT.total_seconds(),
            )
        except Exception as cause:
            # A timeout and any other connect failure (refused endpoint,
            # TLS, DNS) are the same closed-dependency outcome — never a
            # raw traceback out of the process shell.
            raise ProjectionDispatchError(ErrorCode.PROJECTION_DISPATCH_UNAVAILABLE) from cause
        store = PostgresqlProjectionIntentStore(engine)
        starter = TemporalProjectionWorkflowStarter(temporal_client)
        shutdown = asyncio.Event()
        _install_shutdown_signals(shutdown)
        runtime = ProjectionDispatchRuntime(store=store, starter=starter, clock=_utc_now)
        await runtime.run_until_shutdown(shutdown)
    finally:
        if temporal_client is not None:
            await close_temporal_client(temporal_client)
        await dispose_source_store_engine(engine)
