"""Composition of the source lifecycle runtime: serve graph and offline graph.

:func:`compose_source_lifecycle_runtime` builds the real service graph the
serve process runs: the PostgreSQL lifecycle store from Task 4, the
exclusion-policy lifecycle evaluation guard (the same
:class:Composition :class:PolicyDecisionSurface bound to the workspace's
locked active policy revision), the lifecycle conflict-capture gateway
(:class:`PostgresqlLifecycleConflictCaptureGateway`, built by
:func:`compose_lifecycle_conflict_capture_gateway` over the shared
conflict service so losing lifecycle races retain durable conflict
evidence) and the in-memory low-cardinality
:class:`SourceLifecycleMetrics` sink. The composition mirrors the
exclusion-policy and small-file-sync patterns exactly so the application
factory can wire it without bespoke ports.

:func:`compose_offline_source_lifecycle` builds the deterministic offline
graph used by OpenAPI export and by unit and contract tests: fixed
identity namespace, fixed ``obsidian`` device kind, deterministic
in-memory ``OfflineSourceLifecycleStore``,
``OfflineSourceLifecyclePolicy`` and ``OfflineLifecycleConflictCaptureGateway``
doubles and the real :class:`SourceLifecycleService` over them with the
bounded :class:`InMemorySourceLifecycleMetrics` recorder. It reads no
environment value, no secret file, no database and no provider client, so
the offline contract document stays byte-deterministic while route tests
observe behavior through the typed domain errors and the service contract.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Final
from uuid import UUID, uuid5

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from api_runtime.authentication_composition import WebAuthenticationRuntime
from api_runtime.small_file_sync_composition import PolicyEnforcementConflictCaptureGuard
from personal_os.diagnostics.context import (
    DiagnosticContext,
    create_diagnostic_context,
    current_diagnostic_context,
)
from personal_os.exclusion_policy.contracts import PolicySubject, RawPolicyDecision
from personal_os.exclusion_policy.enforcement import (
    PolicyEnforcementService,
    PolicyTrustAnchorVerifier,
    default_utc_clock,
    evaluate_policy_decision,
    parse_verified_policy_revision,
    policy_not_initialized_error,
)
from personal_os.source_conflicts.commands import CaptureConflictCommand
from personal_os.source_conflicts.contracts import (
    ConflictCandidate,
    ConflictIdempotencyKey,
    ConflictKind,
    SourceConflict,
)
from personal_os.source_conflicts.metrics import InMemorySourceConflictMetrics
from personal_os.source_conflicts.service import SourceConflictService
from personal_os.source_lifecycle.commands import (
    LifecycleConflictCaptureReceipt,
    LifecycleConflictKind,
    SourceLifecycleCommand,
    SourceLifecycleCommitResult,
)
from personal_os.source_lifecycle.errors import SourceLifecycleError
from personal_os.source_lifecycle.fingerprint import LifecycleRequestFingerprint
from personal_os.source_lifecycle.metrics import (
    InMemorySourceLifecycleMetrics,
    SourceLifecycleDiagnosticsSource,
    SourceLifecycleMetricsWithDiagnostics,
)
from personal_os.source_lifecycle.ports import (
    LifecycleConflictCaptureGateway,
    LifecycleDeviceContext,
    LifecyclePolicyDecision,
    LifecyclePolicyOutcome,
    SourceLifecyclePolicy,
    SourceLifecycleStore,
)
from personal_os.source_lifecycle.service import SourceLifecycleService
from personal_os.source_lifecycle.service import _default_clock as _lifecycle_default_clock
from postgresql_source_store.conflict_store import PostgresqlSourceConflictStore
from postgresql_source_store.policy_enforcement import (
    PostgresqlActivePolicySnapshotSource,
    PostgresqlPolicySubjectEvidenceSource,
)
from postgresql_source_store.tables import (
    source_locators,
    sources,
    sync_events,
)

#: Deterministic offline identity namespace and timestamp; tests and the
#: OpenAPI export use the same values so every render is byte-stable.
_OFFLINE_IDENTITY_NAMESPACE: Final[UUID] = UUID("6a0e7a1e-0000-7000-8000-0000000000f3")
OFFLINE_LIFECYCLE_WORKSPACE_ID: Final[UUID] = UUID("00000000-0000-7000-8000-000000000002")
OFFLINE_LIFECYCLE_DEVICE_ID: Final[UUID] = UUID("00000000-0000-7000-8000-000000000003")
OFFLINE_LIFECYCLE_USER_ID: Final[UUID] = UUID("00000000-0000-7000-8000-000000000004")
OFFLINE_LIFECYCLE_DEVICE_KIND: Final[str] = "obsidian"
OFFLINE_LIFECYCLE_FIXED_NOW: Final[datetime] = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)

#: Derivation namespace for the conflict idempotency key of a captured
#: lifecycle race. The lifecycle command's own idempotency key is an opaque
#: printable-ASCII string, while the conflict grammar accepts only canonical
#: UUID text, so the capture key is derived deterministically from the race
#: kind and the command's request fingerprint: a same-identity redelivery
#: derives the same key and replays the stored conflict, and any payload
#: drift under the same event identity lands on the conflict domain's typed
#: idempotency-mismatch rejection instead of a silent second conflict.
_LIFECYCLE_CONFLICT_CAPTURE_NAMESPACE: Final[UUID] = UUID("6a0e7a1e-0000-7000-8000-0000000000f5")


@dataclass(frozen=True, slots=True)
class SourceLifecycleRuntime:
    """One composed source lifecycle runtime the lifecycle routes consume.

    ``service`` is the :class:`SourceLifecycleService` the route hands a
    command to; ``store``, ``policy`` and ``conflict_capture`` mirror the
    injected ports so composition-time introspection remains cheap;
    ``metrics`` is the shared write sink; ``lifecycle_diagnostics`` exposes
    the metrics sink's read side — the closed commit counters and the
    bounded rejection ring — for the Web Admin lifecycle diagnostics route;
    ``web_authentication`` is the web-authentication runtime that resolves
    the access Bearer credential to the device context. The offline runtime
    carries its ``state`` container for tests; the serve graph leaves it
    unset.
    """

    service: SourceLifecycleService
    store: SourceLifecycleStore
    policy: SourceLifecyclePolicy
    conflict_capture: LifecycleConflictCaptureGateway
    metrics: SourceLifecycleMetricsWithDiagnostics
    lifecycle_diagnostics: SourceLifecycleDiagnosticsSource
    web_authentication: WebAuthenticationRuntime
    state: OfflineSourceLifecycleState | None = None


# --- the serve composition --------------------------------------------------------------


def compose_source_lifecycle_runtime(
    *,
    store: SourceLifecycleStore,
    policy: SourceLifecyclePolicy,
    conflict_capture: LifecycleConflictCaptureGateway,
    metrics: SourceLifecycleMetricsWithDiagnostics,
    web_authentication: WebAuthenticationRuntime,
    clock: Callable[[], datetime] = _lifecycle_default_clock,
) -> SourceLifecycleRuntime:
    """Build the real source lifecycle runtime of one serve process.

    The composition owns no FastAPI or database driver; the PostgreSQL
    store from Task 4, the lifecycle policy guard (a thin wrapper over
    the composed exclusion-policy lifecycle evaluation) and the
    conflict-capture gateway (built by
    :func:`compose_lifecycle_conflict_capture_gateway` over the shared
    conflict service) come pre-bound.
    """

    service = SourceLifecycleService(
        store=store,
        policy=policy,
        conflict_capture=conflict_capture,
        metrics=metrics,
        clock=clock,
    )
    return SourceLifecycleRuntime(
        service=service,
        store=store,
        policy=policy,
        conflict_capture=conflict_capture,
        metrics=metrics,
        lifecycle_diagnostics=metrics,
        web_authentication=web_authentication,
    )


def _lifecycle_conflict_capture_key(
    kind: LifecycleConflictKind, request_fingerprint: LifecycleRequestFingerprint
) -> ConflictIdempotencyKey:
    """Derive the deterministic, event-scoped capture key of one race."""

    return ConflictIdempotencyKey(
        str(
            uuid5(
                _LIFECYCLE_CONFLICT_CAPTURE_NAMESPACE,
                f"source-lifecycle-conflict/{kind.value}/{request_fingerprint.hexadecimal}",
            )
        )
    )


def _lifecycle_capture_receipt(
    conflict: SourceConflict,
) -> LifecycleConflictCaptureReceipt:
    """Render the opaque lifecycle receipt of one captured conflict."""

    return LifecycleConflictCaptureReceipt(
        conflict_id=conflict.conflict_id,
        workspace_id=conflict.workspace_id,
        source_id=conflict.source_id,
        conflict_kind=LifecycleConflictKind(conflict.conflict_kind.value),
        verified_candidate_object_id=conflict.candidate.verified_candidate_object_id,
        captured_at=conflict.captured_at,
    )


class PostgresqlLifecycleConflictCaptureGateway:
    """Capture losing lifecycle races through the shared conflict service.

    The composition root binds this adapter behind the lifecycle domain's
    :class:`LifecycleConflictCaptureGateway` port: each member re-validates
    the race against capture-time canonical state in one short read
    transaction — the observed remote version of an active source for a
    lost delete, the active holder of the contested target locator for a
    lost rename/move/restore, and in both cases that the lifecycle command's
    idempotency key is not already bound to a committed event (an
    idempotency drift must never masquerade as a race) — and only a
    confirmed race issues one
    :class:`~personal_os.source_conflicts.commands.CaptureConflictCommand`
    through the shared
    :class:`~personal_os.source_conflicts.service.SourceConflictService`
    (policy recheck, idempotent store transaction, no current-pointer
    mutation). An unconfirmed race answers ``None`` and retains nothing.
    No byte, digest, locator or object key is logged or echoed.
    """

    _ACTIVE_SOURCE_STATE: Final[str] = "active"

    def __init__(
        self,
        *,
        engine: AsyncEngine,
        conflict_service: SourceConflictService,
    ) -> None:
        self._engine = engine
        self._conflict_service = conflict_service

    async def capture_delete_remote_edit(
        self,
        *,
        command: SourceLifecycleCommand,
        device_context: LifecycleDeviceContext,
        request_fingerprint: LifecycleRequestFingerprint,
        diagnostic_context: DiagnosticContext,
    ) -> LifecycleConflictCaptureReceipt | None:
        if await self._lifecycle_key_already_committed(
            workspace_id=device_context.workspace_id, idempotency_key=command.idempotency_key
        ):
            return None
        sync_state, observed_remote_version_id = await self._read_active_source_version(
            workspace_id=device_context.workspace_id, source_id=command.source_id
        )
        if (
            sync_state != self._ACTIVE_SOURCE_STATE
            or observed_remote_version_id is None
            or observed_remote_version_id == command.expected_version_id
        ):
            # The race is not confirmed at capture time: the source is gone,
            # already deleted, or its pointer no longer differs from the
            # delete's base, so nothing may be retained.
            return None
        capture_command = CaptureConflictCommand(
            workspace_id=device_context.workspace_id,
            source_id=command.source_id,
            conflict_kind=ConflictKind.DELETE_REMOTE_EDIT,
            originating_event_id=command.event_id,
            originating_device_id=device_context.device_id,
            idempotency_key=_lifecycle_conflict_capture_key(
                LifecycleConflictKind.DELETE_REMOTE_EDIT, request_fingerprint
            ),
            base_version_id=command.expected_version_id,
            observed_remote_version_id=observed_remote_version_id,
            candidate=ConflictCandidate.delete(),
            normalized_locator=None,
        )
        conflict = await self._conflict_service.capture_conflict(
            capture_command, diagnostic_context
        )
        return _lifecycle_capture_receipt(conflict)

    async def capture_locator_collision(
        self,
        *,
        command: SourceLifecycleCommand,
        device_context: LifecycleDeviceContext,
        request_fingerprint: LifecycleRequestFingerprint,
        diagnostic_context: DiagnosticContext,
    ) -> LifecycleConflictCaptureReceipt | None:
        target_locator = command.target_locator
        if target_locator is None:
            return None
        if await self._lifecycle_key_already_committed(
            workspace_id=device_context.workspace_id, idempotency_key=command.idempotency_key
        ):
            return None
        holder_source_id = await self._read_active_target_holder(
            workspace_id=device_context.workspace_id, target_locator=target_locator.value
        )
        if holder_source_id is None or holder_source_id == command.source_id:
            # No other active source holds the target at capture time, so
            # the rejection was drift or a same-source rebinding, never a
            # collision worth retaining.
            return None
        capture_command = CaptureConflictCommand(
            workspace_id=device_context.workspace_id,
            source_id=command.source_id,
            conflict_kind=ConflictKind.LOCATOR_COLLISION,
            originating_event_id=command.event_id,
            originating_device_id=device_context.device_id,
            idempotency_key=_lifecycle_conflict_capture_key(
                LifecycleConflictKind.LOCATOR_COLLISION, request_fingerprint
            ),
            base_version_id=command.expected_version_id,
            observed_remote_version_id=None,
            candidate=ConflictCandidate.delete(),
            normalized_locator=target_locator,
        )
        conflict = await self._conflict_service.capture_conflict(
            capture_command, diagnostic_context
        )
        return _lifecycle_capture_receipt(conflict)

    async def _lifecycle_key_already_committed(
        self,
        *,
        workspace_id: UUID,
        idempotency_key: str,
    ) -> bool:
        async with self._engine.connect() as connection:
            result = await connection.execute(
                sa.select(sync_events.c.event_id).where(
                    sync_events.c.workspace_id == workspace_id,
                    sync_events.c.idempotency_key == idempotency_key,
                )
            )
            return result.scalar_one_or_none() is not None

    async def _read_active_source_version(
        self,
        *,
        workspace_id: UUID,
        source_id: UUID,
    ) -> tuple[str | None, UUID | None]:
        async with self._engine.connect() as connection:
            result = await connection.execute(
                sa.select(
                    sources.c.sync_state,
                    sources.c.current_version_id,
                ).where(
                    sources.c.workspace_id == workspace_id,
                    sources.c.source_id == source_id,
                )
            )
            row = result.one_or_none()
        if row is None:
            return None, None
        return str(row.sync_state), row.current_version_id

    async def _read_active_target_holder(
        self,
        *,
        workspace_id: UUID,
        target_locator: str,
    ) -> UUID | None:
        async with self._engine.connect() as connection:
            result = await connection.execute(
                sa.select(source_locators.c.source_id)
                .select_from(source_locators)
                .join(
                    sources,
                    sa.and_(
                        sources.c.source_id == source_locators.c.source_id,
                        sources.c.workspace_id == source_locators.c.workspace_id,
                        sources.c.sync_state == self._ACTIVE_SOURCE_STATE,
                    ),
                )
                .where(
                    source_locators.c.workspace_id == workspace_id,
                    source_locators.c.normalized_locator == target_locator,
                    source_locators.c.closed_event_id.is_(None),
                )
            )
            return result.scalar_one_or_none()


def compose_lifecycle_conflict_capture_gateway(
    *,
    engine: AsyncEngine,
    enforcement: PolicyEnforcementService,
    clock: Callable[[], datetime] = default_utc_clock,
) -> LifecycleConflictCaptureGateway:
    """Build the serve graph's lifecycle conflict-capture gateway.

    Binds the shared
    :class:`~personal_os.source_conflicts.service.SourceConflictService`
    over the durable conflict store with the real exclusion-policy capture
    guard and the in-memory low-cardinality conflict metrics sink, exactly
    like the small-file composition's gateway: every lifecycle capture
    re-evaluates the active signed policy at the ``conflict_capture``
    boundary before any conflict row is written.
    """

    return PostgresqlLifecycleConflictCaptureGateway(
        engine=engine,
        conflict_service=SourceConflictService(
            store=PostgresqlSourceConflictStore(engine, clock=clock),
            policy_guard=PolicyEnforcementConflictCaptureGuard(enforcement=enforcement),
            metrics=InMemorySourceConflictMetrics(),
            clock=clock,
        ),
    )


class PostgresqlSourceLifecyclePolicy:
    """Evaluate the advisory lifecycle verdict from canonical policy state.

    The lifecycle store performs the transaction-final locked verification.
    This adapter supplies the pre-transaction verdict that selects projection
    upserts versus deletes, using the same persisted signed revision and
    canonical source evidence as the publication policy boundary.
    """

    def __init__(
        self,
        *,
        snapshot_source: PostgresqlActivePolicySnapshotSource,
        evidence_source: PostgresqlPolicySubjectEvidenceSource,
        verifier: PolicyTrustAnchorVerifier,
        clock: Callable[[], datetime] = _lifecycle_default_clock,
    ) -> None:
        self._snapshot_source = snapshot_source
        self._evidence_source = evidence_source
        self._verifier = verifier
        self._clock = clock

    async def evaluate_lifecycle(
        self,
        command: SourceLifecycleCommand,
        device_context: LifecycleDeviceContext,
    ) -> LifecyclePolicyDecision:
        context = current_diagnostic_context() or create_diagnostic_context().context
        material = await self._snapshot_source.load_active_snapshot(
            device_context.workspace_id, context
        )
        if material is None:
            raise policy_not_initialized_error()
        canonical_subject = await self._evidence_source.load_subject_evidence(
            device_context.workspace_id, command.source_id, context
        )
        locator = command.target_locator or command.expected_locator
        subject = replace(
            canonical_subject
            if canonical_subject is not None
            else PolicySubject(
                workspace_id=device_context.workspace_id,
                source_id=command.source_id,
            ),
            normalized_locator=locator.value if locator is not None else None,
        )
        revision = parse_verified_policy_revision(material, verifier=self._verifier)
        decision = evaluate_policy_decision(
            revision=revision,
            subject=subject,
            evaluated_at=self._clock(),
        )
        outcome = {
            RawPolicyDecision.ALLOWED: LifecyclePolicyOutcome.ALLOWED,
            RawPolicyDecision.EXCLUDED: LifecyclePolicyOutcome.DENIED,
            RawPolicyDecision.INDETERMINATE: LifecyclePolicyOutcome.INDETERMINATE,
        }[decision.raw_decision]
        return LifecyclePolicyDecision(
            workspace_id=device_context.workspace_id,
            outcome=outcome,
            policy_revision_number=decision.revision_number,
            subject=subject,
            expected_locator=command.expected_locator,
            target_locator=command.target_locator,
        )


# --- the offline composition ------------------------------------------------------------


class OfflineSourceLifecycleState:
    """In-memory state of the offline graph: only closed identities and clocks.

    Tests read the deterministic workspace/device/user identities and the
    fixed device kind; the public containers are intentionally minimal so
    every offline render stays byte-deterministic.
    ``captured_conflicts`` freezes one opaque capture receipt per
    ``(event_id, conflict_kind)`` identity — the offline mirror of the
    durable conflict store's event-identity replay map.
    """

    def __init__(self) -> None:
        self.workspace_id: UUID = OFFLINE_LIFECYCLE_WORKSPACE_ID
        self.device_id: UUID = OFFLINE_LIFECYCLE_DEVICE_ID
        self.user_id: UUID = OFFLINE_LIFECYCLE_USER_ID
        self.device_kind: str = OFFLINE_LIFECYCLE_DEVICE_KIND
        self.committed_results: dict[str, SourceLifecycleCommitResult] = {}
        self.captured_conflicts: dict[
            tuple[UUID, LifecycleConflictKind], LifecycleConflictCaptureReceipt
        ] = {}

    @property
    def now(self) -> datetime:
        return OFFLINE_LIFECYCLE_FIXED_NOW

    @property
    def conflict_capture_count(self) -> int:
        return len(self.captured_conflicts)

    def device_context(self) -> LifecycleDeviceContext:
        return LifecycleDeviceContext(
            workspace_id=self.workspace_id,
            device_id=self.device_id,
            user_id=self.user_id,
            device_kind=self.device_kind,
        )


class OfflineLifecycleConflictCaptureGateway:
    """In-memory conflict-capture double replaying by event identity.

    Mirrors the real gateway's contract without canonical state: the first
    capture of one ``(event_id, conflict_kind)`` identity freezes one
    deterministic opaque receipt into the offline state; an exact replay of
    that identity returns the stored receipt unchanged. The double never
    sees or retains bytes — only the opaque receipt crosses, keyed exactly
    like the real gateway's derived-capture replay identity.
    """

    def __init__(self, state: OfflineSourceLifecycleState) -> None:
        self._state = state

    async def capture_delete_remote_edit(
        self,
        *,
        command: SourceLifecycleCommand,
        device_context: LifecycleDeviceContext,
        request_fingerprint: LifecycleRequestFingerprint,
        diagnostic_context: DiagnosticContext,
    ) -> LifecycleConflictCaptureReceipt:
        del request_fingerprint, diagnostic_context
        return self._capture(
            command=command,
            device_context=device_context,
            conflict_kind=LifecycleConflictKind.DELETE_REMOTE_EDIT,
        )

    async def capture_locator_collision(
        self,
        *,
        command: SourceLifecycleCommand,
        device_context: LifecycleDeviceContext,
        request_fingerprint: LifecycleRequestFingerprint,
        diagnostic_context: DiagnosticContext,
    ) -> LifecycleConflictCaptureReceipt:
        del request_fingerprint, diagnostic_context
        return self._capture(
            command=command,
            device_context=device_context,
            conflict_kind=LifecycleConflictKind.LOCATOR_COLLISION,
        )

    def _capture(
        self,
        *,
        command: SourceLifecycleCommand,
        device_context: LifecycleDeviceContext,
        conflict_kind: LifecycleConflictKind,
    ) -> LifecycleConflictCaptureReceipt:
        identity = (command.event_id, conflict_kind)
        stored = self._state.captured_conflicts.get(identity)
        if stored is not None:
            return stored
        receipt = LifecycleConflictCaptureReceipt(
            conflict_id=uuid5(
                _OFFLINE_IDENTITY_NAMESPACE,
                f"conflict:{command.event_id}:{conflict_kind.value}",
            ),
            workspace_id=device_context.workspace_id,
            source_id=command.source_id,
            conflict_kind=conflict_kind,
            verified_candidate_object_id=None,
            captured_at=self._state.now,
        )
        self._state.captured_conflicts[identity] = receipt
        return receipt


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
        lifecycle_decision: LifecyclePolicyDecision,
        diagnostic_context: DiagnosticContext,
    ) -> SourceLifecycleCommitResult:
        del device_context, request_fingerprint, lifecycle_decision, diagnostic_context
        if self._error is not None:
            raise self._error
        result = self._build_result(command)
        self._state.committed_results[_replay_key(command)] = result
        # The store double records no metric: the service is the sole
        # emitter of the closed ``committed`` outcome per successful commit.
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
    metrics: SourceLifecycleMetricsWithDiagnostics | None = None,
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
    conflict_capture: LifecycleConflictCaptureGateway = OfflineLifecycleConflictCaptureGateway(
        offline_state
    )
    recorder = metrics if metrics is not None else InMemorySourceLifecycleMetrics()
    service = SourceLifecycleService(
        store=store,
        policy=policy,
        conflict_capture=conflict_capture,
        metrics=recorder,
    )
    return SourceLifecycleRuntime(
        service=service,
        store=store,
        policy=policy,
        conflict_capture=conflict_capture,
        metrics=recorder,
        lifecycle_diagnostics=recorder,
        web_authentication=compose_offline_web_authentication(),
        state=offline_state,
    )


__all__ = [
    "OFFLINE_LIFECYCLE_DEVICE_ID",
    "OFFLINE_LIFECYCLE_DEVICE_KIND",
    "OFFLINE_LIFECYCLE_USER_ID",
    "OFFLINE_LIFECYCLE_WORKSPACE_ID",
    "OfflineLifecycleConflictCaptureGateway",
    "OfflineSourceLifecyclePolicy",
    "OfflineSourceLifecycleState",
    "OfflineSourceLifecycleStore",
    "PostgresqlLifecycleConflictCaptureGateway",
    "PostgresqlSourceLifecyclePolicy",
    "SourceLifecycleRuntime",
    "compose_lifecycle_conflict_capture_gateway",
    "compose_offline_source_lifecycle",
    "compose_source_lifecycle_runtime",
]
