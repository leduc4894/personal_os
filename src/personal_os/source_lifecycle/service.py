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

Metric labels come only from the closed
:mod:`personal_os.source_lifecycle.metrics` vocabulary; raw locators,
titles, fingerprints, tokens and content never become labels, messages or
safe details. The service emits ``replayed`` on an exact replay,
``rejected`` on a typed ``SourceLifecycleError`` raised by either port,
and lets the store emit ``committed`` for the atomic transition — the
service does not double-count committed outcomes.

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
from personal_os.source_lifecycle.commands import (
    SourceLifecycleCommand,
    SourceLifecycleCommitResult,
)
from personal_os.source_lifecycle.errors import SourceLifecycleError
from personal_os.source_lifecycle.fingerprint import fingerprint_lifecycle_command
from personal_os.source_lifecycle.metrics import (
    LifecycleMetricOutcome,
    SourceLifecycleMetrics,
)
from personal_os.source_lifecycle.ports import (
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
    :class:`SourceLifecycleStore` and :class:`SourceLifecyclePolicy` ports
    and the closed low-cardinality :class:`SourceLifecycleMetrics` sink.
    An exact replay returns the canonical committed result without any
    policy or network work; a miss consults the policy port outside the
    transaction and hands the resulting verdict through unchanged to the
    atomic store commit. The store is the sole owner of locked-policy
    re-verification, the projection-intent selection and the bounded
    database retry, so the service never retries a failed commit and
    never inspects the verdict.
    """

    store: SourceLifecycleStore
    policy: SourceLifecyclePolicy
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
            raise
        return result

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
