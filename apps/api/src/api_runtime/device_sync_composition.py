"""Composition of the device sync runtime: the serve graph and its offline double.

:func:`compose_device_sync` builds the real serve graph the API process
runs: the durable PostgreSQL device event store, manifest store and content
catalog of the schema child over the shared engine, the real R2 verified
object reader behind a lazy per-process client (no connection opens at
composition — the first store call does), the real
:class:`~personal_os.device_sync.service.DeviceSyncService` and the real
:class:`VerifiedDeviceContentService` binding them with the shared in-memory
low-cardinality metrics sink.

:func:`compose_offline_device_sync` builds the deterministic offline graph
used by the OpenAPI export and by route tests: knob-seeded in-memory event,
manifest and catalog doubles mirroring the port contracts (a seeded page or
receipt, a scripted typed error, a policy denial, verified bytes with an
optional mid-stream failure) and an object-store double performing the
reader contract over one fixed buffer. It reads no environment value, no
secret file, no database and no provider client, so the offline contract
document stays byte-deterministic while route tests seed behavior through
the public knobs of :class:`OfflineDeviceSyncState` and observe safety
through the typed domain errors and the captured call evidence.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncEngine

from api_runtime.device_sync_content import VerifiedDeviceContentService
from api_runtime.small_file_sync_composition import LazyR2ClientSource
from personal_os.device_sync.contracts import (
    MANIFEST_RUN_LIFETIME,
    AppendManifestPageCommand,
    CompleteManifestCommand,
    DeviceContentDescriptor,
    DeviceCursorReceipt,
    DeviceEventPage,
    DeviceSyncContext,
    FinalizeManifestCommand,
    ManifestActionPage,
    ManifestActionsQuery,
    ManifestPageReceipt,
    ManifestRunReceipt,
    ManifestRunState,
    StartManifestCommand,
)
from personal_os.device_sync.errors import DeviceSyncError
from personal_os.device_sync.metrics import InMemoryDeviceSyncMetrics
from personal_os.device_sync.service import DeviceSyncService
from personal_os.diagnostics.context import DiagnosticContext
from personal_os.diagnostics.logging import DiagnosticLogger
from personal_os.error_contracts.codes import ErrorCode
from personal_os.exclusion_policy.errors import ExclusionPolicyError
from personal_os.object_storage import CanonicalMediaType, ContentDigest, ExpectedObject
from personal_os.object_storage.errors import ObjectStorageError
from postgresql_source_store.device_content_catalog import PostgresqlDeviceContentCatalog
from postgresql_source_store.device_event_store import PostgresqlDeviceEventStore
from postgresql_source_store.device_manifest_store import PostgresqlDeviceManifestStore
from r2_object_storage.adapter import R2S3ObjectStore
from r2_object_storage.client import R2ClientManager
from r2_object_storage.error_mapping import RetryPolicy
from r2_object_storage.metrics import InMemoryObjectStorageMetrics
from r2_object_storage.settings import LoadedR2Credentials, ObjectStorageSettings
from r2_object_storage.spool import SpoolManager

#: The offline double's canonical media type of its verified bytes.
_OFFLINE_MEDIA_TYPE: Final[CanonicalMediaType] = CanonicalMediaType.parse("text/markdown")

_OFFLINE_ZERO_CURSOR: Final[DeviceCursorReceipt] = DeviceCursorReceipt(0, 0)


@dataclass(frozen=True, slots=True)
class DeviceSyncRuntime:
    """One composed device sync runtime the device sync routes consume.

    ``service`` orchestrates the event and manifest operations; ``content``
    resolves and streams the verified exact-version bytes; ``aclose`` is the
    serve graph's disposal hook — closing the R2 client and its spool
    reservations on shutdown — while the offline graph owns no resource and
    leaves it unset.
    """

    service: DeviceSyncService
    content: VerifiedDeviceContentService
    aclose: Callable[[], Awaitable[None]] | None = None


def compose_device_sync(
    *,
    engine: AsyncEngine,
    object_storage_settings: ObjectStorageSettings,
    object_storage_credentials: LoadedR2Credentials,
    logger: DiagnosticLogger,
) -> DeviceSyncRuntime:
    """Build the real serve runtime of one API process.

    Follows the small-file-sync serve precedent's shape: the shared engine,
    the durable device stores that connect lazily per transaction and the
    provider adapters that open no connection at construction — the R2
    client opens at the first verified download inside the serving loop. The
    graph is therefore composable before the socket exists while every
    adapter is the production one.
    """

    object_store = R2S3ObjectStore(
        LazyR2ClientSource(R2ClientManager(object_storage_settings, object_storage_credentials)),
        spools=SpoolManager(object_storage_settings.object_storage_spool_root),
        retry=RetryPolicy(),
        metrics=InMemoryObjectStorageMetrics(),
        logger=logger,
    )
    metrics = InMemoryDeviceSyncMetrics()
    service = DeviceSyncService(
        events=PostgresqlDeviceEventStore(engine),
        manifests=PostgresqlDeviceManifestStore(engine),
        metrics=metrics,
        diagnostics=logger,
    )
    content = VerifiedDeviceContentService(
        catalog=PostgresqlDeviceContentCatalog(engine),
        objects=object_store,
        metrics=metrics,
        diagnostics=logger,
    )
    return DeviceSyncRuntime(service=service, content=content, aclose=object_store.close)


@dataclass
class OfflineDeviceSyncState:
    """Public knobs and captured evidence of the offline device sync graph.

    Tests seed behavior through the typed error knobs (``None`` keeps the
    happy default), the seeded pages and receipts, the content knobs
    (``content_bytes``, ``is_policy_denied``, ``mid_stream_error``) and read
    safety back through the captured call evidence — the received device
    contexts, acknowledge watermarks and manifest commands. The doubles never
    retain a locator, digest or credential beyond what the captured commands
    already own.
    """

    pull_page: DeviceEventPage | None = None
    pull_error: DeviceSyncError | None = None
    pull_contexts: list[DeviceSyncContext] = field(default_factory=list)

    acknowledge_receipt: DeviceCursorReceipt | None = None
    acknowledge_error: DeviceSyncError | None = None
    acknowledge_calls: list[tuple[int, int]] = field(default_factory=list)

    start_error: DeviceSyncError | None = None
    start_calls: list[tuple[DeviceSyncContext, int]] = field(default_factory=list)

    append_receipt: ManifestPageReceipt | None = None
    append_error: DeviceSyncError | None = None
    append_calls: list[tuple[DeviceSyncContext, AppendManifestPageCommand]] = field(
        default_factory=list
    )

    finalize_error: DeviceSyncError | None = None
    finalize_calls: list[tuple[DeviceSyncContext, FinalizeManifestCommand]] = field(
        default_factory=list
    )

    actions_page: ManifestActionPage | None = None
    actions_error: DeviceSyncError | None = None
    actions_calls: list[ManifestActionsQuery] = field(default_factory=list)

    complete_receipt: DeviceCursorReceipt | None = None
    complete_error: DeviceSyncError | None = None
    complete_calls: list[tuple[DeviceSyncContext, UUID]] = field(default_factory=list)

    descriptor: DeviceContentDescriptor | None = None
    content_error: DeviceSyncError | None = None
    is_policy_denied: bool = False
    content_bytes: bytes = b"offline device sync content"
    mid_stream_error: Exception | None = None
    reader_closed: bool = False
    descriptor_contexts: list[DeviceSyncContext] = field(default_factory=list)


class OfflineDeviceEventStore:
    """Event double honoring the seeded page, error and capture knobs."""

    def __init__(self, state: OfflineDeviceSyncState) -> None:
        self._state = state

    async def pull_events(
        self,
        context: DeviceSyncContext,
        *,
        limit: int,
        diagnostic_context: DiagnosticContext,
    ) -> DeviceEventPage:
        del limit, diagnostic_context
        self._state.pull_contexts.append(context)
        if self._state.pull_error is not None:
            raise self._state.pull_error
        if self._state.pull_page is not None:
            return self._state.pull_page
        return DeviceEventPage(
            acknowledged_sequence=0,
            page_checkpoint_sequence=0,
            delivered_through_sequence=0,
            events=(),
            has_more=False,
        )

    async def acknowledge_cursor(
        self,
        context: DeviceSyncContext,
        *,
        expected_previous_sequence: int,
        applied_through_sequence: int,
        diagnostic_context: DiagnosticContext,
    ) -> DeviceCursorReceipt:
        del context, diagnostic_context
        self._state.acknowledge_calls.append(
            (expected_previous_sequence, applied_through_sequence),
        )
        if self._state.acknowledge_error is not None:
            raise self._state.acknowledge_error
        if self._state.acknowledge_receipt is not None:
            return self._state.acknowledge_receipt
        return _OFFLINE_ZERO_CURSOR


class OfflineDeviceManifestStore:
    """Manifest double honoring the seeded receipts, errors and captures."""

    def __init__(self, state: OfflineDeviceSyncState) -> None:
        self._state = state

    def _run_receipt(
        self,
        context: DeviceSyncContext,
        *,
        state: ManifestRunState,
        entry_count: int,
        observation_generation: int = 0,
        manifest_run_id: UUID | None = None,
    ) -> ManifestRunReceipt:
        return ManifestRunReceipt(
            manifest_run_id=manifest_run_id if manifest_run_id is not None else uuid4(),
            state=state,
            base_acknowledged_sequence=0,
            checkpoint_sequence=0,
            policy_revision_number=1,
            client_observation_generation=observation_generation,
            next_page_number=0,
            entry_count=entry_count,
            expires_at=datetime.now(UTC) + MANIFEST_RUN_LIFETIME,
        )

    async def start_manifest(self, command: StartManifestCommand) -> ManifestRunReceipt:
        self._state.start_calls.append(
            (command.context, command.client_observation_generation)
        )
        if self._state.start_error is not None:
            raise self._state.start_error
        return self._run_receipt(
            command.context,
            state=ManifestRunState.COLLECTING,
            entry_count=0,
            observation_generation=command.client_observation_generation,
        )

    async def append_manifest_page(
        self, command: AppendManifestPageCommand
    ) -> ManifestPageReceipt:
        self._state.append_calls.append((command.context, command))
        if self._state.append_error is not None:
            raise self._state.append_error
        if self._state.append_receipt is not None:
            return self._state.append_receipt
        return ManifestPageReceipt(
            manifest_run_id=command.manifest_run_id,
            page_number=command.page_number,
            accepted_entry_count=len(command.entries),
            next_page_number=command.page_number + 1,
        )

    async def finalize_manifest(self, command: FinalizeManifestCommand) -> ManifestRunReceipt:
        self._state.finalize_calls.append((command.context, command))
        if self._state.finalize_error is not None:
            raise self._state.finalize_error
        return self._run_receipt(
            command.context,
            state=ManifestRunState.PLANNED,
            entry_count=command.total_entry_count,
            manifest_run_id=command.manifest_run_id,
        )

    async def read_manifest_actions(self, query: ManifestActionsQuery) -> ManifestActionPage:
        self._state.actions_calls.append(query)
        if self._state.actions_error is not None:
            raise self._state.actions_error
        if self._state.actions_page is not None:
            return self._state.actions_page
        return ManifestActionPage(
            manifest_run_id=query.manifest_run_id, actions=(), has_more=False
        )

    async def complete_manifest(self, command: CompleteManifestCommand) -> DeviceCursorReceipt:
        self._state.complete_calls.append((command.context, command.manifest_run_id))
        if self._state.complete_error is not None:
            raise self._state.complete_error
        if self._state.complete_receipt is not None:
            return self._state.complete_receipt
        return _OFFLINE_ZERO_CURSOR


class OfflineDeviceContentCatalog:
    """Catalog double resolving the seeded descriptor or typed failure."""

    def __init__(self, state: OfflineDeviceSyncState) -> None:
        self._state = state

    async def resolve_descriptor(
        self,
        context: DeviceSyncContext,
        *,
        source_id: UUID,
        source_version_id: UUID,
        diagnostic_context: DiagnosticContext,
    ) -> DeviceContentDescriptor:
        del diagnostic_context
        self._state.descriptor_contexts.append(context)
        if self._state.is_policy_denied:
            raise ExclusionPolicyError(ErrorCode.EXCLUSION_POLICY_DENIED)
        if self._state.content_error is not None:
            raise self._state.content_error
        if self._state.descriptor is not None:
            return self._state.descriptor
        return DeviceContentDescriptor(
            source_id=source_id,
            source_version_id=source_version_id,
            content_digest=ContentDigest.parse(
                hashlib.sha256(self._state.content_bytes).hexdigest()
            ),
            size_bytes=len(self._state.content_bytes),
            media_type=_OFFLINE_MEDIA_TYPE,
        )


class _OfflineVerifiedReader:
    """Bounded async reader over the state's verified buffer."""

    def __init__(self, state: OfflineDeviceSyncState) -> None:
        self._state = state
        self._remaining = state.content_bytes
        self._failed = False

    async def read(self, size_bytes: int = 1_048_576) -> bytes:
        return await self._next_chunk(max(size_bytes, 0))

    def __aiter__(self) -> _OfflineVerifiedReader:
        return self

    async def __anext__(self) -> bytes:
        chunk = await self._next_chunk(65536)
        if not chunk:
            raise StopAsyncIteration
        return chunk

    async def _next_chunk(self, size_bytes: int) -> bytes:
        is_past_first_chunk = self._remaining != self._state.content_bytes
        if self._state.mid_stream_error is not None and is_past_first_chunk:
            # The first chunk already shipped; the scripted failure now
            # terminates the transport mid-stream.
            raise self._state.mid_stream_error
        chunk = self._remaining[:size_bytes]
        self._remaining = self._remaining[len(chunk) :]
        return chunk


class OfflineVerifiedObjectStore:
    """Verified reader double over the state's fixed verified buffer."""

    def __init__(self, state: OfflineDeviceSyncState) -> None:
        self._state = state

    def open_verified_reader(
        self, expected: ExpectedObject
    ) -> AbstractAsyncContextManager[_OfflineVerifiedReader]:
        state = self._state

        @asynccontextmanager
        async def _reader() -> AsyncIterator[_OfflineVerifiedReader]:
            expected_digest = expected.content_digest.hexadecimal
            actual_digest = hashlib.sha256(state.content_bytes).hexdigest()
            if expected_digest != actual_digest or expected.size_bytes != len(state.content_bytes):
                raise ObjectStorageError(ErrorCode.OBJECT_STORAGE_OBJECT_MISSING)
            try:
                yield _OfflineVerifiedReader(state)
            finally:
                state.reader_closed = True

        return _reader()


def compose_offline_device_sync(
    *,
    state: OfflineDeviceSyncState | None = None,
) -> DeviceSyncRuntime:
    """Build the deterministic offline device sync runtime."""

    offline_state = state if state is not None else OfflineDeviceSyncState()
    metrics = InMemoryDeviceSyncMetrics()
    service = DeviceSyncService(
        events=OfflineDeviceEventStore(offline_state),
        manifests=OfflineDeviceManifestStore(offline_state),
        metrics=metrics,
        diagnostics=None,
    )
    content = VerifiedDeviceContentService(
        catalog=OfflineDeviceContentCatalog(offline_state),
        objects=OfflineVerifiedObjectStore(offline_state),
        metrics=metrics,
        diagnostics=None,
    )
    return DeviceSyncRuntime(service=service, content=content)


__all__ = [
    "DeviceSyncRuntime",
    "OfflineDeviceContentCatalog",
    "OfflineDeviceEventStore",
    "OfflineDeviceManifestStore",
    "OfflineDeviceSyncState",
    "OfflineVerifiedObjectStore",
    "compose_device_sync",
    "compose_offline_device_sync",
]
