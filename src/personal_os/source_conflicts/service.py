"""Source-conflict orchestration: policy recheck, then the atomic store.

:class:`SourceConflictService` is the one shared domain surface the syncing
domains and the HTTP composition drive conflict evidence through. It owns
no state machine of its own: ``capture_conflict`` evaluates the exclusion
policy before any store work and hands the command to the store's own
idempotent capture transaction; ``resolve_conflict`` performs the row-locked
resolution read, re-evaluates the policy over exactly that read, then hands
the command to the store's atomic resolve transaction, which replays by
resolution event identity, rechecks conflict state, the reviewed remote
version and the current source pointer, and commits the winner or the
stale-successor supersession. Verified-object admission stays outside this
service: commands carry only opaque references, and the result surfaces
only the frozen read models.

Policy recheck boundary (documented honestly): the store port carries no
policy evidence, so the active exclusion policy cannot be re-evaluated
inside the store's transaction. The guard therefore runs at this service
boundary — for capture over the command, for resolution over the
row-locked read — while ownership, conflict state, the reviewed remote
version and the current source/locator state are rechecked atomically
inside the store transaction. A policy revision committed between the
guard's allow and the store's commit is not caught on this path; it is
caught at the next boundary that re-evaluates policy (the evidence-read
boundary before any byte streams, the next capture, the next resolution).
Handing the store a policy-revision token captured before its transaction
would close that window but requires widening the Task 1 store port and
the Task 2 store; that tradeoff is deliberately not taken here.

Every completed or rejected branch records exactly one closed metric
outcome — capture: ``captured``/``replayed``/``rejected``; resolution:
``resolved``/``stale_successor``/``replayed``/``rejected`` — and a typed
``SourceConflictError`` additionally records its closed reason code
(``SourceConflictRejectionReason`` mirrors the domain error registry
one-to-one, so the conversion is total). A typed
:class:`~personal_os.exclusion_policy.errors.ExclusionPolicyError`
propagates unchanged with the ``rejected`` outcome label and no
source-conflict reason code: the closed reason lives in the policy
domain's own evaluation metric, and this domain's registries carry no
policy labels by contract. Locators, keys, digests and object references
never become labels, messages or safe details. The replay labels are
derived without weakening the store's idempotency verdicts: capture asks
the event-identity lookup after the policy allow (a found conflict plus a
successful store call can only be the store's own exact replay, because
the originating-event identity is unique), and resolution compares the
locked read against the command's event identity (a terminal conflict
bound to this resolution event can only replay). A duplicate delivery
racing between the label lookup and the store call can label that rare
replay with the fresh outcome; the store's returned value and its
idempotency guarantees are unaffected.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from personal_os.diagnostics.context import DiagnosticContext
from personal_os.exclusion_policy.errors import ExclusionPolicyError
from personal_os.source_conflicts.commands import (
    CaptureConflictCommand,
    ConflictResolutionResult,
    ResolveConflictCommand,
)
from personal_os.source_conflicts.contracts import (
    TERMINAL_CONFLICT_STATUSES,
    ConflictKind,
    ConflictResolutionKind,
    ConflictResolutionOutcome,
    SourceConflict,
)
from personal_os.source_conflicts.errors import SourceConflictError
from personal_os.source_conflicts.metrics import (
    ConflictCaptureOutcome,
    ConflictResolutionMetricOutcome,
    SourceConflictMetrics,
    SourceConflictOperation,
    SourceConflictRejectionReason,
)
from personal_os.source_conflicts.ports import (
    SourceConflictPolicyGuard,
    SourceConflictStore,
)

type _ResolutionOutcomeLabel = ConflictResolutionOutcome | ConflictResolutionMetricOutcome


def _default_clock() -> datetime:
    """Default aware UTC clock seam used when the composition root injects none.

    The difference between two reads is the only value the service feeds
    into the metrics sink, so monotonic drift on either side does not turn
    a recorded duration negative (the elapsed helper clamps as well).
    """

    return datetime.now(UTC)


def _validate_finite_non_negative(field_name: str, value: float) -> None:
    """Reject non-finite or negative durations before they cross into a metric."""

    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{field_name} must be finite and non-negative")


@dataclass(slots=True)
class SourceConflictService:
    """Orchestrates idempotent conflict capture and resolution over ports.

    Depends only on the provider-neutral
    :class:`~personal_os.source_conflicts.ports.SourceConflictStore`, the
    :class:`~personal_os.source_conflicts.ports.SourceConflictPolicyGuard`
    rechecked before every store mutation and the closed low-cardinality
    :class:`SourceConflictMetrics` sink, plus the injectable aware-UTC
    clock. The store remains the sole owner of the state machine, the
    idempotency verdicts and the atomic transitions; this service never
    inspects evidence, never admits verified objects and never retries a
    store call.
    """

    store: SourceConflictStore
    policy_guard: SourceConflictPolicyGuard
    metrics: SourceConflictMetrics
    clock: Callable[[], datetime] = _default_clock

    async def capture_conflict(
        self,
        command: CaptureConflictCommand,
        diagnostic_context: DiagnosticContext,
    ) -> SourceConflict:
        """Capture one conflict behind the policy recheck, idempotently.

        The policy guard evaluates the exclusion policy before any store
        access; the event-identity replay lookup then derives the replay
        label, and the store's own transaction replays by idempotency key,
        validates the references and inserts the accepted event, evidence
        and audit rows without touching the source current pointer.
        """

        started_at = self.clock()
        try:
            await self.policy_guard.authorize_capture(command, diagnostic_context)
            replayed = (
                await self.store.find_captured_conflict(
                    command.originating_event_id,
                    command.workspace_id,
                    diagnostic_context,
                )
                is not None
            )
            conflict = await self.store.capture(command, diagnostic_context)
        except SourceConflictError as error:
            self._record_rejection(SourceConflictOperation.CAPTURE, error)
            self._record_capture(command.conflict_kind, ConflictCaptureOutcome.REJECTED, started_at)
            raise
        except ExclusionPolicyError:
            self._record_capture(command.conflict_kind, ConflictCaptureOutcome.REJECTED, started_at)
            raise
        outcome = ConflictCaptureOutcome.REPLAYED if replayed else ConflictCaptureOutcome.CAPTURED
        self._record_capture(command.conflict_kind, outcome, started_at)
        return conflict

    async def resolve_conflict(
        self,
        command: ResolveConflictCommand,
        workspace_id: UUID,
        diagnostic_context: DiagnosticContext,
    ) -> ConflictResolutionResult:
        """Resolve one conflict behind the policy recheck, atomically.

        The row-locked resolution read scopes the conflict to the
        credential-derived ``workspace_id``; the policy guard re-evaluates
        over exactly that read; the store's transaction then replays by
        resolution event identity, rechecks the reviewed remote version
        against the locked current pointer and commits the winner or the
        stale-successor supersession. A stale reviewed remote is not an
        error: it returns the typed ``STALE_SUCCESSOR`` outcome.
        """

        started_at = self.clock()
        try:
            conflict = await self.store.read_for_resolution(
                command.conflict_id, workspace_id, diagnostic_context
            )
            await self.policy_guard.authorize_resolution(conflict, diagnostic_context)
            replayed = self._is_replay_of_resolution(conflict, command)
            result = await self.store.resolve(command, workspace_id, diagnostic_context)
        except SourceConflictError as error:
            self._record_rejection(SourceConflictOperation.RESOLVE, error)
            self._record_resolution(
                command.resolution_kind,
                ConflictResolutionMetricOutcome.REJECTED,
                started_at,
            )
            raise
        except ExclusionPolicyError:
            self._record_resolution(
                command.resolution_kind,
                ConflictResolutionMetricOutcome.REJECTED,
                started_at,
            )
            raise
        outcome: _ResolutionOutcomeLabel = (
            ConflictResolutionMetricOutcome.REPLAYED if replayed else result.kind
        )
        self._record_resolution(command.resolution_kind, outcome, started_at)
        return result

    @staticmethod
    def _is_replay_of_resolution(conflict: SourceConflict, command: ResolveConflictCommand) -> bool:
        """Report whether the locked read shows this event's frozen outcome.

        A terminal conflict bound to the command's resolution event
        identity can only make the store's transaction replay that stored
        outcome (a divergent idempotency key takes the typed mismatch
        rejection instead); anything else is a fresh attempt.
        """

        return (
            conflict.status in TERMINAL_CONFLICT_STATUSES
            and conflict.resolution_event_id == command.resolution_event_id
        )

    def _record_capture(
        self,
        conflict_kind: ConflictKind,
        outcome: ConflictCaptureOutcome,
        started_at: datetime,
    ) -> None:
        """Record one closed capture-path outcome and its duration."""

        self.metrics.record_capture(
            conflict_kind=conflict_kind,
            outcome=outcome,
            duration_seconds=self._elapsed_seconds_since(started_at),
        )

    def _record_resolution(
        self,
        resolution_kind: ConflictResolutionKind,
        outcome: _ResolutionOutcomeLabel,
        started_at: datetime,
    ) -> None:
        """Record one closed resolution-path outcome and its duration."""

        self.metrics.record_resolution(
            resolution_kind=resolution_kind,
            outcome=outcome,
            duration_seconds=self._elapsed_seconds_since(started_at),
        )

    def _record_rejection(
        self,
        operation: SourceConflictOperation,
        error: SourceConflictError,
    ) -> None:
        """Record one closed rejection reason for a typed conflict error.

        ``SourceConflictRejectionReason`` mirrors the domain error registry
        one-to-one (pinned by the metric contract tests), so the
        conversion of the error's registry code is total.
        """

        self.metrics.record_rejection(
            operation=operation,
            reason_code=SourceConflictRejectionReason(error.error_code.value),
        )

    def _elapsed_seconds_since(self, started_at: datetime) -> float:
        # Clamped at zero so a clock seam that repeats or drifts backwards can
        # never turn a recorded duration negative.
        duration_seconds = max((self.clock() - started_at).total_seconds(), 0.0)
        _validate_finite_non_negative("duration_seconds", duration_seconds)
        return duration_seconds


__all__ = ["SourceConflictService"]
