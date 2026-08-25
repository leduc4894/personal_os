"""Framework-neutral device sync operation orchestration (spec 5.1/14).

:class:`DeviceSyncService` wraps the event and manifest store ports with the
closed low-cardinality metrics and the registered structured diagnostic
events: every operation records its outcome and duration, a typed
:class:`~personal_os.device_sync.errors.DeviceSyncError` additionally records
its closed reason code before the error re-raises unchanged, and caller
cancellation is never caught or metered. Without a composition-provided sink
the built payload is validated and discarded (build-only behavior). The
module imports no FastAPI, SQLAlchemy, database driver, R2 SDK or Obsidian
type, and no identifier, locator, digest or provider detail ever becomes a
metric label or an event field.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Final

from personal_os.device_sync.contracts import (
    MAX_PULL_EVENTS,
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
from personal_os.device_sync.errors import DeviceSyncError
from personal_os.device_sync.metrics import (
    DeviceSyncMetrics,
    DeviceSyncOperation,
    DeviceSyncOutcome,
)
from personal_os.device_sync.ports import DeviceEventStore, DeviceManifestStore
from personal_os.diagnostics.context import DiagnosticContext
from personal_os.diagnostics.events import (
    DiagnosticEventSink,
    EventName,
    RejectedDiagnosticPayload,
    build_registered_event,
)
from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import InternalApplicationError

_MILLISECONDS_PER_SECOND: Final[float] = 1_000.0


class DeviceSyncService:
    """Instrumented facade over the device sync event and manifest ports.

    Depends only on the provider-neutral store ports, the closed metrics
    sink, the optional structured-event sink and the injectable monotonic
    clock. Pull always asks for :data:`MAX_PULL_EVENTS` events; durable
    semantics stay the stores' own authority and their typed errors surface
    unchanged after their outcome and reason are recorded.
    """

    def __init__(
        self,
        *,
        events: DeviceEventStore,
        manifests: DeviceManifestStore,
        metrics: DeviceSyncMetrics,
        diagnostics: DiagnosticEventSink | None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._events = events
        self._manifests = manifests
        self._metrics = metrics
        self._diagnostics = diagnostics
        self._monotonic = monotonic

    async def pull_events(
        self,
        *,
        context: DeviceSyncContext,
        diagnostic_context: DiagnosticContext,
    ) -> DeviceEventPage:
        started = self._monotonic()
        try:
            page = await self._events.pull_events(
                context, limit=MAX_PULL_EVENTS, diagnostic_context=diagnostic_context
            )
        except DeviceSyncError as error:
            self._record_failure(DeviceSyncOperation.PULL, error, started)
            raise
        self._record_success(DeviceSyncOperation.PULL, started)
        return page

    async def acknowledge_cursor(
        self,
        *,
        context: DeviceSyncContext,
        expected_previous_sequence: int,
        applied_through_sequence: int,
        diagnostic_context: DiagnosticContext,
    ) -> DeviceCursorReceipt:
        started = self._monotonic()
        try:
            receipt = await self._events.acknowledge_cursor(
                context,
                expected_previous_sequence=expected_previous_sequence,
                applied_through_sequence=applied_through_sequence,
                diagnostic_context=diagnostic_context,
            )
        except DeviceSyncError as error:
            self._record_failure(DeviceSyncOperation.ACKNOWLEDGE, error, started)
            raise
        self._record_success(DeviceSyncOperation.ACKNOWLEDGE, started)
        return receipt

    async def start_manifest(self, command: StartManifestCommand) -> ManifestRunReceipt:
        started = self._monotonic()
        try:
            receipt = await self._manifests.start_manifest(command)
        except DeviceSyncError as error:
            self._record_failure(DeviceSyncOperation.MANIFEST_START, error, started)
            raise
        self._record_success(DeviceSyncOperation.MANIFEST_START, started)
        return receipt

    async def append_manifest_page(self, command: AppendManifestPageCommand) -> ManifestPageReceipt:
        started = self._monotonic()
        try:
            receipt = await self._manifests.append_manifest_page(command)
        except DeviceSyncError as error:
            self._record_failure(DeviceSyncOperation.MANIFEST_PAGE, error, started)
            raise
        self._record_success(DeviceSyncOperation.MANIFEST_PAGE, started)
        return receipt

    async def finalize_manifest(self, command: FinalizeManifestCommand) -> ManifestRunReceipt:
        started = self._monotonic()
        try:
            receipt = await self._manifests.finalize_manifest(command)
        except DeviceSyncError as error:
            self._record_failure(DeviceSyncOperation.MANIFEST_FINALIZE, error, started)
            raise
        self._record_success(DeviceSyncOperation.MANIFEST_FINALIZE, started)
        return receipt

    async def read_manifest_actions(self, query: ManifestActionsQuery) -> ManifestActionPage:
        started = self._monotonic()
        try:
            page = await self._manifests.read_manifest_actions(query)
        except DeviceSyncError as error:
            self._record_failure(DeviceSyncOperation.MANIFEST_ACTIONS, error, started)
            raise
        self._record_success(DeviceSyncOperation.MANIFEST_ACTIONS, started)
        return page

    async def complete_manifest(self, command: CompleteManifestCommand) -> DeviceCursorReceipt:
        started = self._monotonic()
        try:
            receipt = await self._manifests.complete_manifest(command)
        except DeviceSyncError as error:
            self._record_failure(DeviceSyncOperation.MANIFEST_COMPLETE, error, started)
            raise
        self._record_success(DeviceSyncOperation.MANIFEST_COMPLETE, started)
        return receipt

    def _record_success(self, operation: DeviceSyncOperation, started: float) -> None:
        duration_ms = self._elapsed_ms_since(started)
        self._metrics.record_operation(
            operation=operation,
            outcome=DeviceSyncOutcome.SUCCEEDED,
            reason=None,
            duration_ms=duration_ms,
        )
        self._emit_registered_event(
            EventName.DEVICE_SYNC_OPERATION_COMPLETED,
            {"operation": operation, "duration_ms": duration_ms},
        )

    def _record_failure(
        self, operation: DeviceSyncOperation, error: DeviceSyncError, started: float
    ) -> None:
        duration_ms = self._elapsed_ms_since(started)
        outcome = DeviceSyncOutcome.REJECTED if not error.is_retryable else DeviceSyncOutcome.FAILED
        event_name = (
            EventName.DEVICE_SYNC_OPERATION_REJECTED
            if outcome is DeviceSyncOutcome.REJECTED
            else EventName.DEVICE_SYNC_OPERATION_FAILED
        )
        self._metrics.record_operation(
            operation=operation,
            outcome=outcome,
            reason=error.code,
            duration_ms=duration_ms,
        )
        self._emit_registered_event(
            event_name,
            {"operation": operation, "reason": error.code, "duration_ms": duration_ms},
        )

    def _emit_registered_event(self, event_name: EventName, fields: Mapping[str, object]) -> None:
        """Validate the registered event; deliver it when a sink is bound.

        Without a composition-provided sink the validated payload is discarded
        (build-and-validate only); a rejected payload is registry drift, a
        programming error rather than untrusted input, and raises regardless
        of sink presence.
        """

        built = build_registered_event(event_name, fields)
        if isinstance(built, RejectedDiagnosticPayload):
            raise InternalApplicationError(ErrorCode.INTERNAL_ERROR)
        if self._diagnostics is not None:
            self._diagnostics.emit(event_name, dict(fields))

    def _elapsed_ms_since(self, started: float) -> int:
        # Clamped at zero so a clock seam that repeats or drifts backwards can
        # never turn a recorded duration negative.
        return max(int((self._monotonic() - started) * _MILLISECONDS_PER_SECOND), 0)


__all__ = [
    "DeviceSyncService",
]
