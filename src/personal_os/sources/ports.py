"""Provider-neutral ports for source-version publication orchestration.

:class:`SourcePublicationStore` is the durable publication store port (the
PostgreSQL adapter implements it in its own package). It exposes no SQLAlchemy
row, database exception, Temporal handle or provider payload, and receives
the server-owned :class:`~personal_os.diagnostics.context.DiagnosticContext` for
correlation. :data:`AwareUtcClock` is the injectable aware UTC clock seam every
time-dependent rule (such as the verified-receipt age rule) reads.
:class:`ProjectionIntentStore` is the leased projection-outbox port (design
section 11): claim, expired-lease reclaim and the fenced
acknowledge/retry/terminal transitions over committed intent rows.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Protocol
from uuid import UUID

from personal_os.diagnostics.context import DiagnosticContext
from personal_os.diagnostics.events import SafeToken
from personal_os.object_storage import VerifiedObjectReceipt
from personal_os.sources.commands import CreateSourceVersion, UpdateSourceVersion
from personal_os.sources.fingerprint import RequestFingerprint, SourceVersionCommand
from personal_os.sources.projection_dispatch import LeasedProjectionIntent
from personal_os.sources.results import SourceVersionPublicationResult

#: Injectable clock returning the current aware UTC moment.
type AwareUtcClock = Callable[[], datetime]


class SourcePublicationStore(Protocol):
    """Durable publication store port: idempotent preflight and commit.

    ``resolve_committed`` performs the indexed preflight lookups without a
    source lock: it returns the committed result for an exact
    command/fingerprint replay, ``None`` on a miss, and raises the typed
    identity-mismatch error when the key or event was reused by another
    request. The commit methods run the transaction, rechecking idempotency
    under lock, and may internally perform the bounded database retry reusing
    the one receipt passed in.
    """

    async def resolve_committed(
        self,
        command: SourceVersionCommand,
        request_fingerprint: RequestFingerprint,
        diagnostic_context: DiagnosticContext,
    ) -> SourceVersionPublicationResult | None: ...

    async def commit_create(
        self,
        command: CreateSourceVersion,
        request_fingerprint: RequestFingerprint,
        receipt: VerifiedObjectReceipt,
        diagnostic_context: DiagnosticContext,
    ) -> SourceVersionPublicationResult: ...

    async def commit_update(
        self,
        command: UpdateSourceVersion,
        request_fingerprint: RequestFingerprint,
        receipt: VerifiedObjectReceipt,
        diagnostic_context: DiagnosticContext,
    ) -> SourceVersionPublicationResult: ...


class ProjectionIntentStore(Protocol):
    """Durable projection-intent lease port: claim, reclaim, fenced transitions.

    ``claim_batch`` leases due ``pending`` rows (``FOR UPDATE SKIP LOCKED``,
    ordered by ``(available_at, created_at, projection_intent_id)``, bounded by
    the pinned batch limit) and commits before the caller performs any network
    I/O; concurrent claimers never own one intent. ``reclaim_expired`` returns
    overdue leases to ``pending``, incrementing the attempt count, recording
    the lease-expired error code and applying the bounded backoff. The three
    fenced transitions affect a row only when the exact intent ID,
    ``status='leased'`` and lease token match: a stale token affects zero rows
    and must emit a diagnostic without overwriting state. Every persisted
    state timestamp and lease expiry is database time; the injected ``now``
    reading is used only for due/expiry comparisons and availability
    computation, and the attempt count changes only on a known dispatch
    outcome or a lease expiry.
    """

    async def reclaim_expired(self, now: datetime) -> int: ...

    async def claim_batch(
        self, now: datetime, limit: int
    ) -> tuple[LeasedProjectionIntent, ...]: ...

    async def acknowledge_dispatched(
        self, intent_id: UUID, lease_token: UUID, now: datetime
    ) -> bool: ...

    async def release_retry(
        self,
        intent_id: UUID,
        lease_token: UUID,
        error_code: SafeToken,
        available_at: datetime,
        now: datetime,
    ) -> bool: ...

    async def mark_terminal(
        self,
        intent_id: UUID,
        lease_token: UUID,
        error_code: SafeToken,
        now: datetime,
    ) -> bool: ...
