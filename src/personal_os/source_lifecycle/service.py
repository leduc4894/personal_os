"""Source lifecycle orchestration: replay → policy → store.

:class:`SourceLifecycleService` orchestrates one source lifecycle command
deterministically: compute the request fingerprint, run the lock-free
indexed exact-replay lookup, and only on a miss consult the policy port
outside the transaction before handing the verdict through to the atomic
store commit. The store owns the locked-policy verification, the
projection-intent selection between ``upsert`` and ``delete``, the
at-most-three cancellable deadlock/serialization retry and the ambiguous
commit recovery; the service never retries a failed commit (that would
double-retry and break exact replay) and never inspects the policy
decision to mutate canonical state.

When the store rejects with the two race codes the shared conflict
contract can represent without content bytes — a delete's version
conflict and a rename/move/restore's locator conflict on a target it
carries — the service hands the losing command's evidence to the
:class:`LifecycleConflictCaptureGateway` port before re-raising the
original typed error, so the durable conflict receipt exists while the
retrying device sees the unchanged rejection. Capture is best-effort
evidence retention: it mutates no pointer, locator or tombstone, and a
typed capture failure is swallowed only because the conflict domain's own
metrics sink already records its closed reason token.

Metric labels come only from the closed
:mod:`personal_os.source_lifecycle.metrics` vocabulary; raw locators,
titles, fingerprints, tokens and content never become labels, messages or
safe details. The service emits ``committed`` on a fresh successful
commit, ``replayed`` on an exact replay, and ``rejected`` on a typed
``SourceLifecycleError`` raised by either port (a captured race is still
a rejection; the conflict domain's sink counts the capture itself).

The constructor injects only the provider-neutral ports the service
depends on; no FastAPI, database driver or provider SDK is imported. The
diagnostic context is reused from :mod:`personal_os.diagnostics.context`
without inventing a parallel structure, and the optional injected clock
follows the canonical aware-UTC seam of the source-publication service.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from personal_os.diagnostics.context import DiagnosticContext
from personal_os.error_contracts.exceptions import ApplicationError
from personal_os.source_lifecycle.commands import (
    LifecycleOperation,
    SourceLifecycleCommand,
    SourceLifecycleCommitResult,
)
from personal_os.source_lifecycle.errors import (
    SourceLifecycleError,
    SourceLifecycleErrorCode,
)
from personal_os.source_lifecycle.fingerprint import (
    LifecycleRequestFingerprint,
    fingerprint_lifecycle_command,
)
from personal_os.source_lifecycle.metrics import (
    LifecycleMetricOutcome,
    SourceLifecycleMetrics,
)
from personal_os.source_lifecycle.ports import (
    LifecycleConflictCaptureGateway,
    LifecycleDeviceContext,
    SourceLifecyclePolicy,
    SourceLifecycleStore,
)


def _default_clock() -> datetime:
    """Default aware UTC clock seam used when the composition root injects none.

    Mirrors the ``AwareUtcClock`` seam of the source-publication service:
    the difference between two reads is the only value the service feeds
    into the metrics sink, so monotonic drift on either side does not
    turn a recorded duration negative.
    """

    return datetime.now(UTC)


def _validate_finite_non_negative(field_name: str, value: float) -> None:
    """Reject non-finite or negative durations before they cross into a metric label."""

    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{field_name} must be finite and non-negative")


@dataclass(slots=True)
class SourceLifecycleService:
    """Orchestrates one idempotent source lifecycle commit over injected ports.

    The service depends only on the provider-neutral
    :class:`SourceLifecycleStore`, :class:`SourceLifecyclePolicy` and
    :class:`LifecycleConflictCaptureGateway` ports and the closed
    low-cardinality :class:`SourceLifecycleMetrics` sink. An exact replay
    returns the canonical committed result without any policy or network
    work; a miss consults the policy port outside the transaction and hands
    the resulting verdict through unchanged to the atomic store commit. The
    store is the sole owner of locked-policy re-verification, the
    projection-intent selection and the bounded database retry, so the
    service never retries a failed commit and never inspects the verdict.

    When the store rejects because a competing canonical lifecycle
    transition won the race, the service additionally hands the losing
    command's evidence to the conflict-capture gateway before re-raising the
    original typed error: a delete that lost to a remote edit becomes a
    byteless ``delete_remote_edit`` conflict and a rename/move/restore onto
    a locator another active source holds becomes a ``locator_collision``
    conflict preserving the locator snapshot. Only those two race shapes are
    captured — every other typed rejection (byteless rename/move version
    races, remote-delete state conflicts, tombstone races, idempotency
    drift) keeps its existing locks, audit and typed error untouched, and
    capture mutates no current pointer, locator or tombstone.
    """

    store: SourceLifecycleStore
    policy: SourceLifecyclePolicy
    conflict_capture: LifecycleConflictCaptureGateway
    metrics: SourceLifecycleMetrics
    clock: Callable[[], datetime] = _default_clock

    async def commit(
        self,
        command: SourceLifecycleCommand,
        device_context: LifecycleDeviceContext,
        diagnostic_context: DiagnosticContext,
    ) -> SourceLifecycleCommitResult:
        """Commit one source lifecycle command idempotently."""

        started_at = self.clock()
        request_fingerprint = fingerprint_lifecycle_command(command)
        replay = await self.store.resolve_committed(
            command,
            device_context,
            request_fingerprint,
            diagnostic_context,
        )
        if replay is not None:
            self._record_replay(command=command, started_at=started_at)
            return replay
        try:
            decision = await self.policy.evaluate_lifecycle(command, device_context)
            result = await self.store.commit(
                command,
                device_context,
                request_fingerprint,
                decision,
                diagnostic_context,
            )
        except SourceLifecycleError as error:
            self._record_rejection(command=command, error=error)
            await self._capture_race_conflict(
                command=command,
                device_context=device_context,
                request_fingerprint=request_fingerprint,
                error=error,
                diagnostic_context=diagnostic_context,
            )
            raise
        self._record_commit(command=command, started_at=started_at)
        return result

    async def _capture_race_conflict(
        self,
        *,
        command: SourceLifecycleCommand,
        device_context: LifecycleDeviceContext,
        request_fingerprint: LifecycleRequestFingerprint,
        error: SourceLifecycleError,
        diagnostic_context: DiagnosticContext,
    ) -> None:
        """Delegate one capturable race to the shared conflict-capture port.

        The classification is deliberately narrow: only the two race shapes
        the shared conflict contract can represent without content bytes.
        A delete rejected by a version conflict is a ``delete_remote_edit``
        race (the gateway re-validates that canonical state really moved);
        a rename/move/restore rejected by a locator conflict carries a
        target locator and is a ``locator_collision`` race (the gateway
        re-validates the holder is another active source, so idempotency
        drift and misclassification never capture). Everything else —
        including the locator conflicts of a delete, which has no target
        locator — keeps its typed rejection alone.

        A typed capture failure (the shared service's policy recheck or
        idempotency verdict) never masks the lifecycle rejection: it is
        swallowed here only because the conflict domain's own metrics sink
        already records its closed reason token at the capture boundary,
        leaving a readable trail without replacing the error the retrying
        device must see. A ``None`` answer means the race was no longer
        confirmed against capture-time canonical state, so nothing is
        retained and the typed rejection stands.
        """

        if error.code is SourceLifecycleErrorCode.VERSION_CONFLICT and (
            command.operation is LifecycleOperation.DELETE
        ):
            try:
                await self.conflict_capture.capture_delete_remote_edit(
                    command=command,
                    device_context=device_context,
                    request_fingerprint=request_fingerprint,
                    diagnostic_context=diagnostic_context,
                )
            except ApplicationError:
                return
            return
        if (
            error.code is SourceLifecycleErrorCode.LOCATOR_CONFLICT
            and command.target_locator is not None
            and command.operation
            in {
                LifecycleOperation.RENAME,
                LifecycleOperation.MOVE,
                LifecycleOperation.RESTORE,
            }
        ):
            try:
                await self.conflict_capture.capture_locator_collision(
                    command=command,
                    device_context=device_context,
                    request_fingerprint=request_fingerprint,
                    diagnostic_context=diagnostic_context,
                )
            except ApplicationError:
                return

    def _record_replay(
        self,
        *,
        command: SourceLifecycleCommand,
        started_at: datetime,
    ) -> None:
        """Record the closed ``replayed`` outcome for an exact replay."""

        duration_seconds = max((self.clock() - started_at).total_seconds(), 0.0)
        _validate_finite_non_negative("duration_seconds", duration_seconds)
        self.metrics.record_commit(
            operation=command.operation,
            outcome=LifecycleMetricOutcome.REPLAYED,
            duration_seconds=duration_seconds,
        )

    def _record_commit(
        self,
        *,
        command: SourceLifecycleCommand,
        started_at: datetime,
    ) -> None:
        """Record the closed ``committed`` outcome for one fresh successful commit."""

        duration_seconds = max((self.clock() - started_at).total_seconds(), 0.0)
        _validate_finite_non_negative("duration_seconds", duration_seconds)
        self.metrics.record_commit(
            operation=command.operation,
            outcome=LifecycleMetricOutcome.COMMITTED,
            duration_seconds=duration_seconds,
        )

    def _record_rejection(
        self,
        *,
        command: SourceLifecycleCommand,
        error: SourceLifecycleError,
    ) -> None:
        """Record the closed rejection label for one typed lifecycle error."""

        self.metrics.record_rejection(
            operation=command.operation,
            error_code=error.code,
        )


__all__ = ["SourceLifecycleService"]
