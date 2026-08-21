"""Composition of the source lifecycle runtime: serve graph and offline graph.

:func:`compose_source_lifecycle_runtime` builds the real service graph the
serve process runs: the PostgreSQL lifecycle store from Task 4, the
exclusion-policy lifecycle evaluation guard (the same
:class:Composition :class:PolicyDecisionSurface bound to the workspace's
locked active policy revision) and the in-memory low-cardinality
:class:`SourceLifecycleMetrics` sink. The composition mirrors the
exclusion-policy and small-file-sync patterns exactly so the application
factory can wire it without bespoke ports.

:func:`compose_offline_source_lifecycle` builds the deterministic offline
graph used by OpenAPI export and by unit and contract tests: fixed
identity namespace, fixed ``obsidian`` device kind, deterministic
in-memory ``OfflineSourceLifecycleStore`` and
``OfflineSourceLifecyclePolicy`` doubles and the real
:class:`SourceLifecycleService` over them with the bounded
:class:`InMemorySourceLifecycleMetrics` recorder. It reads no environment
value, no secret file, no database and no provider client, so the offline
contract document stays byte-deterministic while route tests observe
behavior through the typed domain errors and the service contract.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final
from uuid import UUID, uuid5

from api_runtime.authentication_composition import WebAuthenticationRuntime
from personal_os.diagnostics.context import DiagnosticContext
from personal_os.exclusion_policy.contracts import PolicySubject
from personal_os.source_lifecycle.commands import (
    SourceLifecycleCommand,
    SourceLifecycleCommitResult,
)
from personal_os.source_lifecycle.errors import SourceLifecycleError
from personal_os.source_lifecycle.metrics import (
    InMemorySourceLifecycleMetrics,
    SourceLifecycleMetrics,
)
from personal_os.source_lifecycle.ports import (
    LifecycleDeviceContext,
    LifecyclePolicyDecision,
    LifecyclePolicyOutcome,
    SourceLifecyclePolicy,
    SourceLifecycleStore,
)
from personal_os.source_lifecycle.service import SourceLifecycleService
from personal_os.source_lifecycle.service import _default_clock as _lifecycle_default_clock

#: Deterministic offline identity namespace and timestamp; tests and the
#: OpenAPI export use the same values so every render is byte-stable.
_OFFLINE_IDENTITY_NAMESPACE: Final[UUID] = UUID("6a0e7a1e-0000-7000-8000-0000000000f3")
OFFLINE_LIFECYCLE_WORKSPACE_ID: Final[UUID] = UUID("00000000-0000-7000-8000-000000000002")
OFFLINE_LIFECYCLE_DEVICE_ID: Final[UUID] = UUID("00000000-0000-7000-8000-000000000003")
OFFLINE_LIFECYCLE_USER_ID: Final[UUID] = UUID("00000000-0000-7000-8000-000000000004")
OFFLINE_LIFECYCLE_DEVICE_KIND: Final[str] = "obsidian"
OFFLINE_LIFECYCLE_FIXED_NOW: Final[datetime] = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class SourceLifecycleRuntime:
    """One composed source lifecycle runtime the lifecycle routes consume.

    ``service`` is the :class:`SourceLifecycleService` the route hands a
    command to; ``store`` and ``policy`` mirror the injected ports so
    composition-time introspection remains cheap; ``web_authentication``
    is the web-authentication runtime that resolves the access Bearer
    credential to the device context. The offline runtime carries its
    ``state`` container for tests; the serve graph leaves it unset.
    """

    service: SourceLifecycleService
    store: SourceLifecycleStore
    policy: SourceLifecyclePolicy
    metrics: SourceLifecycleMetrics
    web_authentication: WebAuthenticationRuntime
    state: OfflineSourceLifecycleState | None = None


# --- the serve composition --------------------------------------------------------------


def compose_source_lifecycle_runtime(
    *,
    store: SourceLifecycleStore,
    policy: SourceLifecyclePolicy,
    metrics: SourceLifecycleMetrics,
    web_authentication: WebAuthenticationRuntime,
    clock: Callable[[], datetime] = _lifecycle_default_clock,
) -> SourceLifecycleRuntime:
    """Build the real source lifecycle runtime of one serve process.

    The composition owns no FastAPI or database driver; the PostgreSQL
    store from Task 4 and the lifecycle policy guard (a thin wrapper over
    the composed exclusion-policy lifecycle evaluation) come pre-bound.
    """

    service = SourceLifecycleService(
        store=store,
        policy=policy,
        metrics=metrics,
        clock=clock,
    )
    return SourceLifecycleRuntime(
        service=service,
        store=store,
        policy=policy,
        metrics=metrics,
        web_authentication=web_authentication,
    )


# --- the offline composition ------------------------------------------------------------


class OfflineSourceLifecycleState:
    """In-memory state of the offline graph: only closed identities and clocks.

    Tests read the deterministic workspace/device/user identities and the
    fixed device kind; the public containers are intentionally minimal so
    every offline render stays byte-deterministic.
    """

    def __init__(self) -> None:
        self.workspace_id: UUID = OFFLINE_LIFECYCLE_WORKSPACE_ID
        self.device_id: UUID = OFFLINE_LIFECYCLE_DEVICE_ID
        self.user_id: UUID = OFFLINE_LIFECYCLE_USER_ID
        self.device_kind: str = OFFLINE_LIFECYCLE_DEVICE_KIND
        self.committed_results: dict[str, SourceLifecycleCommitResult] = {}

    @property
    def now(self) -> datetime:
        return OFFLINE_LIFECYCLE_FIXED_NOW

    def device_context(self) -> LifecycleDeviceContext:
        return LifecycleDeviceContext(
            workspace_id=self.workspace_id,
            device_id=self.device_id,
            user_id=self.user_id,
            device_kind=self.device_kind,
        )


class OfflineSourceLifecyclePolicy:
    """In-memory policy port returning a deterministic closed decision.

    The verdict always carries the workspace's expected and target locator
    operands of the command, the same fixed positive policy revision and a
    deterministic UUIDv5-anchored :class ``PolicySubject` so the
    fingerprint and the metric labels stay stable across reruns.
    """

    def __init__(
        self,
        *,
        outcome: LifecyclePolicyOutcome = LifecyclePolicyOutcome.ALLOWED,
        policy_revision_number: int = 1,
    ) -> None:
        self._outcome = outcome
        self._policy_revision_number = policy_revision_number

    def build_decision(
        self,
        *,
        command: SourceLifecycleCommand,
        device_context: LifecycleDeviceContext,
    ) -> LifecyclePolicyDecision:
        if command.target_locator is not None:
            subject_locator = command.target_locator.value
        elif command.expected_locator is not None:
            subject_locator = command.expected_locator.value
        else:
            subject_locator = ""
        subject = PolicySubject(
            workspace_id=device_context.workspace_id,
            source_id=command.source_id,
            normalized_locator=subject_locator,
        )
        return LifecyclePolicyDecision(
            workspace_id=device_context.workspace_id,
            outcome=self._outcome,
            policy_revision_number=self._policy_revision_number,
            subject=subject,
            expected_locator=command.expected_locator,
            target_locator=command.target_locator,
        )

    async def evaluate_lifecycle(
        self,
        command: SourceLifecycleCommand,
        device_context: LifecycleDeviceContext,
    ) -> LifecyclePolicyDecision:
        return self.build_decision(command=command, device_context=device_context)


class OfflineSourceLifecycleStore:
    """In-memory store double honouring replay preflight and atomic commit.

    The replay identity is ``(command.event_id, command.idempotency_key)``
    paired with the source identifier; an exact match short-circuits with the
    stored commit result, a mismatch raises the typed idempotency-conflict
    error. The commit path synthesises a canonical commit result bound to
    the request fingerprint and writes the immutable row once. Every
    deterministic identity derives from a single UUIDv5 anchor so the
    offline render stays byte-stable.
    """

    def __init__(
        self,
        *,
        state: OfflineSourceLifecycleState,
        error: SourceLifecycleError | None = None,
    ) -> None:
        self._state = state
        self._error = error

    async def resolve_committed(
        self,
        command: SourceLifecycleCommand,
        device_context: LifecycleDeviceContext,
        request_fingerprint: object,
        diagnostic_context: DiagnosticContext,
    ) -> SourceLifecycleCommitResult | None:
        del device_context, request_fingerprint, diagnostic_context
        if self._error is not None:
            raise self._error
        return self._state.committed_results.get(_replay_key(command))

    async def commit(
        self,
        command: SourceLifecycleCommand,
        device_context: LifecycleDeviceContext,
        request_fingerprint: object,
        policy_decision: LifecyclePolicyDecision,
        diagnostic_context: DiagnosticContext,
    ) -> SourceLifecycleCommitResult:
        del device_context, request_fingerprint, policy_decision, diagnostic_context
        if self._error is not None:
            raise self._error
        result = self._build_result(command)
        self._state.committed_results[_replay_key(command)] = result
        # Mirror the durable store's COMMITTED emission so the offline graph
        # observes one closed ``committed`` outcome per successful commit.
        return result

    def _build_result(self, command: SourceLifecycleCommand) -> SourceLifecycleCommitResult:
        from personal_os.source_lifecycle.commands import (
            LifecycleOperation,
            LifecycleState,
        )

        if command.operation is LifecycleOperation.DELETE:
            tombstone_id = uuid5(
                _OFFLINE_IDENTITY_NAMESPACE,
                f"tombstone:{command.source_id}:{command.event_id}",
            )
            return SourceLifecycleCommitResult(
                source_id=command.source_id,
                source_version_id=uuid5(
                    _OFFLINE_IDENTITY_NAMESPACE,
                    f"version:{command.source_id}:{command.event_id}",
                ),
                event_id=command.event_id,
                event_sequence=1,
                state=LifecycleState.DELETED,
                tombstone_id=tombstone_id,
                resulting_locator=None,
                committed_at=self._state.now,
            )
        resulting = command.target_locator
        assert resulting is not None
        return SourceLifecycleCommitResult(
            source_id=command.source_id,
            source_version_id=uuid5(
                _OFFLINE_IDENTITY_NAMESPACE,
                f"version:{command.source_id}:{command.event_id}",
            ),
            event_id=command.event_id,
            event_sequence=1,
            state=LifecycleState.ACTIVE,
            tombstone_id=None,
            resulting_locator=resulting,
            committed_at=self._state.now,
        )


def _replay_key(command: SourceLifecycleCommand) -> str:
    return f"{command.source_id}:{command.event_id}:{command.idempotency_key}"


def compose_offline_source_lifecycle(
    *,
    state: OfflineSourceLifecycleState | None = None,
    metrics: SourceLifecycleMetrics | None = None,
) -> SourceLifecycleRuntime:
    """Build the deterministic offline source lifecycle runtime.

    The runtime wires the real :class:`SourceLifecycleService` over the
    in-memory policy and store doubles; routes can replay the canonical
    commit result and observe the typed lifecycle rejections without ever
    entering a database transaction or provider client.
    """

    from api_runtime.authentication_composition import compose_offline_web_authentication

    offline_state = state if state is not None else OfflineSourceLifecycleState()
    store: SourceLifecycleStore = OfflineSourceLifecycleStore(state=offline_state)
    policy: SourceLifecyclePolicy = OfflineSourceLifecyclePolicy()
    recorder = metrics if metrics is not None else InMemorySourceLifecycleMetrics()
    service = SourceLifecycleService(
        store=store,
        policy=policy,
        metrics=recorder,
    )
    return SourceLifecycleRuntime(
        service=service,
        store=store,
        policy=policy,
        metrics=recorder,
        web_authentication=compose_offline_web_authentication(),
        state=offline_state,
    )


__all__ = [
    "OFFLINE_LIFECYCLE_DEVICE_ID",
    "OFFLINE_LIFECYCLE_DEVICE_KIND",
    "OFFLINE_LIFECYCLE_USER_ID",
    "OFFLINE_LIFECYCLE_WORKSPACE_ID",
    "OfflineSourceLifecyclePolicy",
    "OfflineSourceLifecycleState",
    "OfflineSourceLifecycleStore",
    "SourceLifecycleRuntime",
    "compose_offline_source_lifecycle",
    "compose_source_lifecycle_runtime",
]