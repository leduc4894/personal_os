"""Narrow in-memory fakes proving the device-sync service instrumentation.

Every fake is a minimal closed double for one port: the event-store and
manifest-store fakes record the exact shapes they received and either return
one canned result or raise one configured typed error, the event-sink fake
records the emitted structured events, and the monotonic fake returns one
fixed increasing sequence so duration assertions are deterministic. No fake
retains or echoes locator, digest or credential sentinels; ledger entries are
closed command objects and enum members only.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final
from uuid import UUID, uuid4

from personal_os.device_sync.contracts import (
    AppendManifestPageCommand,
    CompleteManifestCommand,
    DeviceCursorReceipt,
    DeviceEventPage,
    DeviceEventType,
    DeviceSyncContext,
    DeviceSyncEvent,
    FinalizeManifestCommand,
    ManifestAction,
    ManifestActionKind,
    ManifestActionPage,
    ManifestActionsQuery,
    ManifestEntry,
    ManifestPageReceipt,
    ManifestRunReceipt,
    ManifestRunState,
    NormalizedLocator,
    SourceFingerprint,
    StartManifestCommand,
    compute_manifest_run_expiry,
)
from personal_os.device_sync.errors import DeviceSyncError, DeviceSyncErrorCode
from personal_os.device_sync.metrics import DeviceSyncMetrics, InMemoryDeviceSyncMetrics
from personal_os.device_sync.service import DeviceSyncService
from personal_os.diagnostics.context import DiagnosticContext, create_diagnostic_context
from personal_os.diagnostics.events import EventName
from personal_os.object_storage import ContentDigest

#: Fixed canonical evidence shared by the builders. The digest and locator are
#: private values: assertions never echo them.
FINGERPRINT: Final[SourceFingerprint] = SourceFingerprint(
    sha256=hashlib.sha256(b"device-sync fake fingerprint evidence").hexdigest(),
    size_bytes=48,
    media_type="text/markdown",
)
LOCATOR: Final[NormalizedLocator] = NormalizedLocator("notes/device-sync.md")
COMMITTED_AT: Final[datetime] = datetime(2026, 8, 26, 9, 0, 0, tzinfo=UTC)

#: The event types whose operation-shaped operands require each locator or
#: tombstone member, mirrored here so the builders stay independent of the
#: contract's internal matrices.
_RESULTING_LOCATOR_TYPES: Final[frozenset[DeviceEventType]] = frozenset(
    {
        DeviceEventType.CREATED,
        DeviceEventType.RENAMED,
        DeviceEventType.MOVED,
        DeviceEventType.RESTORED,
    }
)
_PRIOR_LOCATOR_TYPES: Final[frozenset[DeviceEventType]] = frozenset(
    {DeviceEventType.RENAMED, DeviceEventType.MOVED, DeviceEventType.DELETED}
)
_TOMBSTONE_TYPES: Final[frozenset[DeviceEventType]] = frozenset(
    {DeviceEventType.DELETED, DeviceEventType.RESTORED}
)


def build_diagnostic_context() -> DiagnosticContext:
    """A fresh server-owned diagnostic context for one request-bound unit of work."""

    return create_diagnostic_context().context


def build_device_sync_context() -> DeviceSyncContext:
    """A fresh credential-derived device sync context."""

    return DeviceSyncContext(workspace_id=uuid4(), device_id=uuid4(), user_id=uuid4())


def build_device_sync_event(event_type: DeviceEventType) -> DeviceSyncEvent:
    """One valid operation-shaped sync event of ``event_type``."""

    return DeviceSyncEvent(
        event_id=uuid4(),
        event_sequence=1,
        event_type=event_type,
        source_id=uuid4(),
        origin_device_id=None,
        base_version_id=None,
        current_version_id=uuid4(),
        base_fingerprint=None,
        current_fingerprint=FINGERPRINT,
        prior_locator=LOCATOR if event_type in _PRIOR_LOCATOR_TYPES else None,
        resulting_locator=LOCATOR if event_type in _RESULTING_LOCATOR_TYPES else None,
        tombstone_id=uuid4() if event_type in _TOMBSTONE_TYPES else None,
        committed_at=COMMITTED_AT,
    )


def build_event_page(events: tuple[DeviceSyncEvent, ...] = ()) -> DeviceEventPage:
    """A valid empty-or-filled event page over consecutive watermarks."""

    return DeviceEventPage(
        acknowledged_sequence=0,
        page_checkpoint_sequence=len(events),
        delivered_through_sequence=len(events),
        events=events,
        has_more=False,
    )


def build_cursor_receipt() -> DeviceCursorReceipt:
    """A valid cursor receipt."""

    return DeviceCursorReceipt(acknowledged_sequence=1, delivered_through_sequence=1)


def build_manifest_entry() -> ManifestEntry:
    """One valid manifest entry."""

    return ManifestEntry(
        local_entry_id="entry-1",
        known_source_id=None,
        known_version_id=None,
        normalized_locator=LOCATOR,
        fingerprint=FINGERPRINT,
        observation_generation=4,
    )


def build_start_command(context: DeviceSyncContext) -> StartManifestCommand:
    """One valid manifest start command."""

    return StartManifestCommand(
        context=context,
        client_observation_generation=3,
        diagnostic_context=build_diagnostic_context(),
    )


def build_run_receipt() -> ManifestRunReceipt:
    """One valid collecting run receipt."""

    return ManifestRunReceipt(
        manifest_run_id=uuid4(),
        state=ManifestRunState.COLLECTING,
        base_acknowledged_sequence=0,
        checkpoint_sequence=5,
        policy_revision_number=2,
        client_observation_generation=3,
        next_page_number=0,
        entry_count=0,
        expires_at=compute_manifest_run_expiry(COMMITTED_AT),
    )


def build_append_command(context: DeviceSyncContext) -> AppendManifestPageCommand:
    """One valid manifest page command."""

    return AppendManifestPageCommand(
        context=context,
        manifest_run_id=uuid4(),
        page_number=0,
        entries=(build_manifest_entry(),),
        page_digest=ContentDigest.parse(hashlib.sha256(b"page").hexdigest()),
        diagnostic_context=build_diagnostic_context(),
    )


def build_page_receipt() -> ManifestPageReceipt:
    """One valid manifest page receipt."""

    return ManifestPageReceipt(
        manifest_run_id=uuid4(),
        page_number=0,
        accepted_entry_count=1,
        next_page_number=1,
    )


def build_finalize_command(context: DeviceSyncContext) -> FinalizeManifestCommand:
    """One valid manifest finalize command."""

    return FinalizeManifestCommand(
        context=context,
        manifest_run_id=uuid4(),
        total_entry_count=1,
        final_digest=ContentDigest.parse(hashlib.sha256(b"final").hexdigest()),
        diagnostic_context=build_diagnostic_context(),
    )


def build_actions_query(context: DeviceSyncContext) -> ManifestActionsQuery:
    """One valid manifest actions query."""

    return ManifestActionsQuery(
        context=context,
        manifest_run_id=uuid4(),
        after_action_index=0,
        limit=200,
        diagnostic_context=build_diagnostic_context(),
    )


def build_complete_command(context: DeviceSyncContext) -> CompleteManifestCommand:
    """One valid manifest completion command."""

    return CompleteManifestCommand(
        context=context,
        manifest_run_id=uuid4(),
        final_digest=ContentDigest.parse(hashlib.sha256(b"final").hexdigest()),
        diagnostic_context=build_diagnostic_context(),
    )


def build_action_page() -> ManifestActionPage:
    """One valid ordered action page."""

    return ManifestActionPage(
        manifest_run_id=uuid4(),
        actions=(
            ManifestAction(
                action_index=0,
                action_kind=ManifestActionKind.NO_CHANGE,
                local_entry_id="entry-1",
                source_id=uuid4(),
                source_version_id=uuid4(),
                source_locator_id=uuid4(),
                source_tombstone_id=None,
                reason=None,
            ),
            ManifestAction(
                action_index=1,
                action_kind=ManifestActionKind.CONFLICT,
                local_entry_id="entry-2",
                source_id=None,
                source_version_id=None,
                source_locator_id=None,
                source_tombstone_id=None,
                reason=None,
            ),
        ),
        has_more=False,
    )


@dataclass
class SequenceMonotonic:
    """Injectable monotonic clock returning one fixed increasing sequence."""

    moments: list[float]
    index: int = 0

    def __call__(self) -> float:
        moment = self.moments[min(self.index, len(self.moments) - 1)]
        self.index += 1
        return moment


@dataclass
class RecordingEventSink:
    """Diagnostic sink fake recording every emitted structured event."""

    emitted: list[tuple[EventName, dict[str, object]]] = field(default_factory=list)

    def emit(self, event_name: EventName, fields: Mapping[str, object] | None = None) -> None:
        self.emitted.append((event_name, dict(fields or {})))

    def last_event_name(self) -> EventName:
        return self.emitted[-1][0]

    def last_fields(self) -> Mapping[str, object]:
        return self.emitted[-1][1]


@dataclass
class ScriptedDeviceEventStore:
    """Event-store fake returning canned results or raising configured errors."""

    page: DeviceEventPage
    receipt: DeviceCursorReceipt
    pull_error: DeviceSyncError | None = None
    acknowledge_error: DeviceSyncError | None = None
    pull_cancelled: bool = False
    pull_limits: list[int] = field(default_factory=list)
    acknowledge_sequences: list[tuple[int, int]] = field(default_factory=list)

    async def pull_events(
        self,
        context: DeviceSyncContext,
        *,
        limit: int,
        diagnostic_context: DiagnosticContext,
    ) -> DeviceEventPage:
        del context, diagnostic_context
        if self.pull_cancelled:
            raise asyncio.CancelledError
        self.pull_limits.append(limit)
        if self.pull_error is not None:
            raise self.pull_error
        return self.page

    async def acknowledge_cursor(
        self,
        context: DeviceSyncContext,
        *,
        expected_previous_sequence: int,
        applied_through_sequence: int,
        diagnostic_context: DiagnosticContext,
    ) -> DeviceCursorReceipt:
        del context, diagnostic_context
        self.acknowledge_sequences.append((expected_previous_sequence, applied_through_sequence))
        if self.acknowledge_error is not None:
            raise self.acknowledge_error
        return self.receipt


@dataclass
class GapRaisingEventStore:
    """Event-store fake whose pull always hits the closed cursor gap."""

    pull_calls: int = 0

    async def pull_events(
        self,
        context: DeviceSyncContext,
        *,
        limit: int,
        diagnostic_context: DiagnosticContext,
    ) -> DeviceEventPage:
        del context, limit, diagnostic_context
        self.pull_calls += 1
        raise DeviceSyncError(DeviceSyncErrorCode.CURSOR_GAP)

    async def acknowledge_cursor(
        self,
        context: DeviceSyncContext,
        *,
        expected_previous_sequence: int,
        applied_through_sequence: int,
        diagnostic_context: DiagnosticContext,
    ) -> DeviceCursorReceipt:
        del context, expected_previous_sequence, applied_through_sequence, diagnostic_context
        raise AssertionError("acknowledge_cursor must not be called by the pull tests")


@dataclass
class UnusedManifestStore:
    """Manifest-store fake asserting no manifest path runs in event-only tests."""

    def _refuse(self) -> None:
        raise AssertionError("manifest store must not be called by this test")

    async def start_manifest(self, command: StartManifestCommand) -> ManifestRunReceipt:
        del command
        self._refuse()

    async def append_manifest_page(self, command: AppendManifestPageCommand) -> ManifestPageReceipt:
        del command
        self._refuse()

    async def finalize_manifest(self, command: FinalizeManifestCommand) -> ManifestRunReceipt:
        del command
        self._refuse()

    async def read_manifest_actions(self, query: ManifestActionsQuery) -> ManifestActionPage:
        del query
        self._refuse()

    async def complete_manifest(self, command: CompleteManifestCommand) -> DeviceCursorReceipt:
        del command
        self._refuse()


@dataclass
class ScriptedManifestStore:
    """Manifest-store fake returning canned receipts or raising configured errors."""

    run_receipt: ManifestRunReceipt
    page_receipt: ManifestPageReceipt
    action_page: ManifestActionPage
    cursor_receipt: DeviceCursorReceipt
    start_error: DeviceSyncError | None = None
    append_error: DeviceSyncError | None = None
    finalize_error: DeviceSyncError | None = None
    actions_error: DeviceSyncError | None = None
    complete_error: DeviceSyncError | None = None
    start_commands: list[StartManifestCommand] = field(default_factory=list)
    append_commands: list[AppendManifestPageCommand] = field(default_factory=list)
    finalize_commands: list[FinalizeManifestCommand] = field(default_factory=list)
    actions_queries: list[ManifestActionsQuery] = field(default_factory=list)
    complete_commands: list[CompleteManifestCommand] = field(default_factory=list)

    async def start_manifest(self, command: StartManifestCommand) -> ManifestRunReceipt:
        self.start_commands.append(command)
        if self.start_error is not None:
            raise self.start_error
        return self.run_receipt

    async def append_manifest_page(self, command: AppendManifestPageCommand) -> ManifestPageReceipt:
        self.append_commands.append(command)
        if self.append_error is not None:
            raise self.append_error
        return self.page_receipt

    async def finalize_manifest(self, command: FinalizeManifestCommand) -> ManifestRunReceipt:
        self.finalize_commands.append(command)
        if self.finalize_error is not None:
            raise self.finalize_error
        return self.run_receipt

    async def read_manifest_actions(self, query: ManifestActionsQuery) -> ManifestActionPage:
        self.actions_queries.append(query)
        if self.actions_error is not None:
            raise self.actions_error
        return self.action_page

    async def complete_manifest(self, command: CompleteManifestCommand) -> DeviceCursorReceipt:
        self.complete_commands.append(command)
        if self.complete_error is not None:
            raise self.complete_error
        return self.cursor_receipt


@dataclass
class ServiceHarness:
    """The real service wired over the scripted fakes, sink and metrics."""

    service: DeviceSyncService
    events: ScriptedDeviceEventStore
    manifests: ScriptedManifestStore
    metrics: InMemoryDeviceSyncMetrics
    sink: RecordingEventSink
    monotonic: SequenceMonotonic


def build_service_harness() -> ServiceHarness:
    """Wire the real service over the scripted fakes and recording sink."""

    events = ScriptedDeviceEventStore(
        page=build_event_page((build_device_sync_event(DeviceEventType.UPDATED),)),
        receipt=build_cursor_receipt(),
    )
    manifests = ScriptedManifestStore(
        run_receipt=build_run_receipt(),
        page_receipt=build_page_receipt(),
        action_page=build_action_page(),
        cursor_receipt=build_cursor_receipt(),
    )
    metrics = InMemoryDeviceSyncMetrics()
    sink = RecordingEventSink()
    monotonic = SequenceMonotonic(moments=[100.0, 100.5])
    service = DeviceSyncService(
        events=events,
        manifests=manifests,
        metrics=metrics,
        diagnostics=sink,
        monotonic=monotonic,
    )
    return ServiceHarness(
        service=service,
        events=events,
        manifests=manifests,
        metrics=metrics,
        sink=sink,
        monotonic=monotonic,
    )


def build_metrics_protocol_fake() -> DeviceSyncMetrics:
    """The in-memory metrics recorder as the injectable protocol double."""

    return InMemoryDeviceSyncMetrics()


def manifest_run_id_of(receipt: ManifestRunReceipt) -> UUID:
    """Test introspection: the run identity behind one receipt."""

    return receipt.manifest_run_id
