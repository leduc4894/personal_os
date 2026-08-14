"""Provider-neutral ports for source-version publication orchestration.

:class:`SourcePublicationStore` is the durable publication store port (the
PostgreSQL adapter implements it in its own package). It exposes no SQLAlchemy
row, database exception, Temporal handle or provider payload, and receives the
server-owned :class:`~personal_os.diagnostics.context.DiagnosticContext` for
correlation. :data:`AwareUtcClock` is the injectable aware UTC clock seam every
time-dependent rule (such as the verified-receipt age rule) reads.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from personal_os.diagnostics.context import DiagnosticContext
from personal_os.object_storage import VerifiedObjectReceipt
from personal_os.sources.commands import CreateSourceVersion, UpdateSourceVersion
from personal_os.sources.fingerprint import RequestFingerprint, SourceVersionCommand
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
