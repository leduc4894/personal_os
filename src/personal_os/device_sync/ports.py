"""Provider-neutral store ports for device sync orchestration (spec 5.1).

The seams the later PostgreSQL adapters build on: the device event store
(server pull hydration plus monotonic cursor acknowledgement) and the device
manifest store (run, page, action and completion state). The ports expose no
SQLAlchemy row, database exception, R2 key, receipt or provider payload, and
receive the server-owned
:class:`~personal_os.diagnostics.context.DiagnosticContext` for correlation
through the command or call. Durable semantics — locking, expiry, replay
freezing and policy rechecks — are the adapters' own authority.
"""

from __future__ import annotations

from typing import Protocol

from personal_os.device_sync.contracts import (
    AppendManifestPageCommand,
    CompleteManifestCommand,
    DeviceCursorReceipt,
    DeviceEventPage,
    DeviceSyncContext,
    FinalizeManifestCommand,
    ManifestActionPage,
    ManifestActionsQuery,
    ManifestPageReceipt,
    ManifestRunReceipt,
    StartManifestCommand,
)
from personal_os.diagnostics.context import DiagnosticContext


class DeviceEventStore(Protocol):
    """Server event pull and monotonic cursor acknowledgement (spec 6.1/7.1)."""

    async def pull_events(
        self,
        context: DeviceSyncContext,
        *,
        limit: int,
        diagnostic_context: DiagnosticContext,
    ) -> DeviceEventPage: ...

    async def acknowledge_cursor(
        self,
        context: DeviceSyncContext,
        *,
        expected_previous_sequence: int,
        applied_through_sequence: int,
        diagnostic_context: DiagnosticContext,
    ) -> DeviceCursorReceipt: ...


class DeviceManifestStore(Protocol):
    """Manifest run, page, action and completion state (spec 6.2-6.5/7.3)."""

    async def start_manifest(self, command: StartManifestCommand) -> ManifestRunReceipt: ...

    async def append_manifest_page(
        self, command: AppendManifestPageCommand
    ) -> ManifestPageReceipt: ...

    async def finalize_manifest(self, command: FinalizeManifestCommand) -> ManifestRunReceipt: ...

    async def read_manifest_actions(self, query: ManifestActionsQuery) -> ManifestActionPage: ...

    async def complete_manifest(self, command: CompleteManifestCommand) -> DeviceCursorReceipt: ...


__all__ = [
    "DeviceEventStore",
    "DeviceManifestStore",
]
