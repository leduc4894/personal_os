"""Provider-neutral ports for the source-conflict domain (Child 8).

The seams the later tasks build on: the durable conflict store owning
capture, replay lookup, open listing, resolution reads and the atomic
resolve transaction; the server-side policy guard rechecked at capture,
resolution and evidence-read boundaries; and the verified evidence reader
streaming base, remote or candidate bytes. No port exposes a SQLAlchemy
row, database exception, R2 key, presigned URL, digest or provider
payload, and every method receives the server-owned
:class:`~personal_os.diagnostics.context.DiagnosticContext` for
correlation. Workspace scope is always an explicit parameter: it derives
from the authenticated device credential at the composition boundary and
never from wire data, so no call can reach a cross-workspace conflict.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol
from uuid import UUID

from personal_os.diagnostics.context import DiagnosticContext
from personal_os.source_conflicts.commands import (
    CaptureConflictCommand,
    ConflictResolutionResult,
    ResolveConflictCommand,
)
from personal_os.source_conflicts.contracts import (
    ConflictEvidenceRole,
    SourceConflict,
)


class SourceConflictStore(Protocol):
    """Durable conflict evidence and atomic transition boundary (spec 4/5).

    ``capture`` inserts the accepted sync event, the immutable evidence row
    and the audit record in one transaction without touching the source
    current pointer; a same-identity replay returns the stored conflict
    unchanged. ``find_captured_conflict`` is the replay lookup by the
    originating event identity a syncing domain performs before its normal
    published/no-change classifier. ``list_open`` pages the open conflicts
    of one workspace in stable conflict-identity order. ``read`` and
    ``read_for_resolution`` scope one conflict to its workspace;
    ``read_for_resolution`` locks for the resolve transaction. ``resolve``
    replays by resolution event identity, rechecks the reviewed remote
    version, current state and active policy, then either commits the
    winner or supersedes the conflict and creates the open successor.
    """

    async def capture(
        self,
        command: CaptureConflictCommand,
        diagnostic_context: DiagnosticContext,
    ) -> SourceConflict: ...

    async def find_captured_conflict(
        self,
        originating_event_id: UUID,
        workspace_id: UUID,
        diagnostic_context: DiagnosticContext,
    ) -> SourceConflict | None: ...

    async def list_open(
        self,
        workspace_id: UUID,
        *,
        limit: int,
        exclusive_start_conflict_id: UUID | None,
        diagnostic_context: DiagnosticContext,
    ) -> tuple[SourceConflict, ...]: ...

    async def read(
        self,
        conflict_id: UUID,
        workspace_id: UUID,
        diagnostic_context: DiagnosticContext,
    ) -> SourceConflict: ...

    async def read_for_resolution(
        self,
        conflict_id: UUID,
        workspace_id: UUID,
        diagnostic_context: DiagnosticContext,
    ) -> SourceConflict: ...

    async def resolve(
        self,
        command: ResolveConflictCommand,
        workspace_id: UUID,
        diagnostic_context: DiagnosticContext,
    ) -> ConflictResolutionResult: ...


class SourceConflictPolicyGuard(Protocol):
    """Server-side policy recheck for capture, resolution and evidence reads.

    The composition root binds the domain policy enforcement service here.
    ``authorize_capture`` evaluates the exclusion policy before any
    conflict row is written; ``authorize_resolution`` re-evaluates before a
    winner is accepted and again before any evidence stream opens. A
    definite denial, an indeterminate outcome or any fail-closed policy
    failure raises the typed exclusion-policy error; only an allowed
    decision returns. The guard never inspects or returns raw content,
    locators or digests.
    """

    async def authorize_capture(
        self,
        command: CaptureConflictCommand,
        diagnostic_context: DiagnosticContext,
    ) -> None: ...

    async def authorize_resolution(
        self,
        conflict: SourceConflict,
        diagnostic_context: DiagnosticContext,
    ) -> None: ...


class ConflictEvidenceReader(Protocol):
    """Verified streaming read of one conflict's immutable evidence (spec 5.2).

    Callers must complete the policy recheck (and, for resolution, the
    ownership check) before opening a stream; the reader itself is the
    verified-read boundary only. ``role`` selects the exact ``base``,
    ``remote`` or ``candidate`` bytes; a role with no retained evidence —
    the ``candidate`` role of a delete conflict, or a base the canonical
    history no longer serves — fails closed with the typed conflict error
    and never substitutes other bytes. No R2 key, URL, digest or provider
    detail crosses this boundary in either direction.
    """

    def open_evidence_stream(
        self,
        conflict_id: UUID,
        role: ConflictEvidenceRole,
        workspace_id: UUID,
        diagnostic_context: DiagnosticContext,
    ) -> AsyncIterator[bytes]: ...


__all__ = [
    "ConflictEvidenceReader",
    "SourceConflictPolicyGuard",
    "SourceConflictStore",
]
