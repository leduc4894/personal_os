"""Recovery service consistent backup creation orchestration (spec 9.1-9.3).

The fakes prove the binding flow: the environment gate fires before any port,
client or path is touched; the snapshot stays open through finalize; object
copies are bounded to four concurrent verified readers; every failure and
cancellation path abandons staging, closes readers and re-raises; and the
registered created/failed events plus closed metrics are recorded. The fakes
record only ids, digests, manifests and counters — never paths, tokens or raw
bytes.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest

from personal_os.diagnostics.events import (
    DiagnosticEvent,
    EventName,
    RejectedDiagnosticPayload,
)
from personal_os.error_contracts.codes import ErrorCode
from personal_os.object_storage import (
    CanonicalMediaType,
    ContentDigest,
    ExpectedObject,
    derive_canonical_object_key,
)
from personal_os.object_storage.errors import ObjectStorageError
from personal_os.recovery import service as service_module
from personal_os.recovery.contracts import (
    CANONICAL_COUNT_TABLES,
    POSTGRESQL_SCHEMA_REVISION,
    POSTGRESQL_SERVER_VERSION,
    InMemoryCanonicalBackupMetrics,
    ManifestDumpEntry,
    ManifestObjectEntry,
    RecoveryConfigurationReason,
    RecoveryDependency,
    RecoveryEnvironment,
    RecoveryError,
    RecoveryManifest,
    RecoveryMetricOutcome,
    RecoveryOperation,
)
from personal_os.recovery.ports import (
    CanonicalBackupSnapshot,
    DumpReceipt,
    PostgresqlConnectionTarget,
    RecoveryBundleWriter,
)
from personal_os.recovery.service import (
    BACKUP_OBJECT_READ_CONCURRENCY,
    BackupCreateCommand,
    BackupCreationResult,
    RecoveryService,
)

#: Shared ledger entry constants: one string per observed port event.
SNAPSHOT_OPEN: str = "snapshot.open"
SNAPSHOT_EXIT: str = "snapshot.exit"
STAGING_ENTERED: str = "bundle.create_staging_entered"
WRITER_FINALIZE: str = "writer.finalize"
WRITER_ABANDON: str = "writer.abandon"

_SNAPSHOT_TOKEN: str = "opaque-snapshot-token"
_SNAPSHOT_NOW: datetime = datetime(2026, 8, 15, 12, 0, 0, tzinfo=UTC)
_MANIFEST_CREATED_AT: datetime = datetime(2026, 8, 15, 12, 0, 5, tzinfo=UTC)
_DUMP_RECEIPT: DumpReceipt = DumpReceipt(
    size_bytes=4096, sha256=hashlib.sha256(b"fake pg_dump archive").hexdigest()
)


@dataclass
class SequencedUtcClock:
    """Injectable aware UTC clock returning queued moments, then the last one."""

    moments: list[datetime]

    def __call__(self) -> datetime:
        if len(self.moments) > 1:
            return self.moments.pop(0)
        return self.moments[0]


def build_counts() -> dict[str, int]:
    """Non-zero counts for exactly the closed canonical count tables."""

    return {table: index + 1 for index, table in enumerate(CANONICAL_COUNT_TABLES)}


def build_object_fixture(index: int) -> tuple[ExpectedObject, bytes]:
    """One expected-object claim plus the canonical bytes it describes."""

    payload = f"canonical recovery object {index}".encode()
    media_type = "text/markdown" if index % 2 == 0 else "application/json"
    return (
        ExpectedObject(
            content_digest=ContentDigest.parse(hashlib.sha256(payload).hexdigest()),
            size_bytes=len(payload),
            media_type=CanonicalMediaType.parse(media_type),
        ),
        payload,
    )


def build_object_fixtures(count: int) -> tuple[tuple[ExpectedObject, bytes], ...]:
    return tuple(build_object_fixture(index) for index in range(count))


def build_snapshot(
    referenced_objects: tuple[ExpectedObject, ...],
    *,
    schema_head: str = POSTGRESQL_SCHEMA_REVISION,
) -> CanonicalBackupSnapshot:
    return CanonicalBackupSnapshot(
        snapshot_token=_SNAPSHOT_TOKEN,
        server_version=POSTGRESQL_SERVER_VERSION,
        schema_head=schema_head,
        table_counts=build_counts(),
        referenced_objects=referenced_objects,
    )


def build_command() -> BackupCreateCommand:
    return BackupCreateCommand(
        environment=RecoveryEnvironment.TEST,
        target=PostgresqlConnectionTarget(
            host="localhost", port=5432, database="knowledge", user="knowledge"
        ),
    )


def build_expected_manifest(
    bundle_id: UUID,
    objects: tuple[ExpectedObject, ...],
) -> RecoveryManifest:
    entries = tuple(
        sorted(
            (
                ManifestObjectEntry(
                    content_sha256=expected.content_digest.hexadecimal,
                    object_key=derive_canonical_object_key(expected.content_digest).value,
                    size_bytes=expected.size_bytes,
                    media_type=expected.media_type.value,
                    relative_path=derive_canonical_object_key(expected.content_digest).value,
                )
                for expected in objects
            ),
            key=lambda entry: entry.content_sha256,
        )
    )
    return RecoveryManifest(
        bundle_id=bundle_id,
        created_at=_MANIFEST_CREATED_AT,
        source_environment=RecoveryEnvironment.TEST.value,
        postgresql_server_version=POSTGRESQL_SERVER_VERSION,
        postgresql_schema_revision=POSTGRESQL_SCHEMA_REVISION,
        postgres_dump=ManifestDumpEntry(
            relative_path="postgres.dump",
            size_bytes=_DUMP_RECEIPT.size_bytes,
            sha256=_DUMP_RECEIPT.sha256,
        ),
        canonical_counts=build_counts(),
        objects=entries,
    )


class FakeSnapshotStore:
    """Snapshot-store fake recording open/exit ordering and observations."""

    def __init__(
        self,
        snapshot: CanonicalBackupSnapshot,
        *,
        pending_writers: int = 0,
        ledger: list[str] | None = None,
    ) -> None:
        self.snapshot = snapshot
        self.pending_writers = pending_writers
        self.ledger: list[str] = ledger if ledger is not None else []
        self.open_calls: list[datetime] = []
        self.observe_calls = 0

    def open_quiesced_snapshot(
        self, now: datetime
    ) -> AbstractAsyncContextManager[CanonicalBackupSnapshot]:
        return self._open_quiesced_snapshot(now)

    @asynccontextmanager
    async def _open_quiesced_snapshot(
        self, now: datetime
    ) -> AsyncIterator[CanonicalBackupSnapshot]:
        self.open_calls.append(now)
        self.ledger.append(SNAPSHOT_OPEN)
        try:
            yield self.snapshot
        finally:
            self.ledger.append(SNAPSHOT_EXIT)

    async def observe_pending_writers(self) -> int:
        self.observe_calls += 1
        return self.pending_writers


class BufferedRecordingWriter:
    """Staging-writer fake exposing the buffered ``write_object`` extension.

    Records object bytes, finalized manifests and abandon calls in memory;
    ``object_path`` is never used because the buffered method is offered.
    """

    def __init__(self, ledger: list[str] | None = None) -> None:
        self.dump_path = Path("buffered-staging-postgres.dump")
        self.written_objects: dict[str, bytes] = {}
        self.finalized_manifests: list[RecoveryManifest] = []
        self.abandon_calls = 0
        self.ledger: list[str] = ledger if ledger is not None else []

    def object_path(self, content_sha256: str) -> Path:
        return Path(f"buffered-object-{content_sha256}")

    async def write_object(self, content_sha256: str, object_bytes: bytes) -> None:
        self.written_objects[content_sha256] = object_bytes

    async def finalize(self, manifest: RecoveryManifest) -> None:
        self.ledger.append(WRITER_FINALIZE)
        self.finalized_manifests.append(manifest)

    async def abandon(self) -> None:
        self.ledger.append(WRITER_ABANDON)
        self.abandon_calls += 1


class StreamingRecordingWriter:
    """Port-only staging-writer fake: the service must stream into object_path."""

    def __init__(self, object_root: Path, ledger: list[str] | None = None) -> None:
        self.dump_path = object_root / "postgres.dump"
        self.object_root = object_root
        self.finalized_manifests: list[RecoveryManifest] = []
        self.abandon_calls = 0
        self.ledger: list[str] = ledger if ledger is not None else []

    def object_path(self, content_sha256: str) -> Path:
        return self.object_root / f"object-{content_sha256}.bin"

    async def finalize(self, manifest: RecoveryManifest) -> None:
        self.ledger.append(WRITER_FINALIZE)
        self.finalized_manifests.append(manifest)

    async def abandon(self) -> None:
        self.ledger.append(WRITER_ABANDON)
        self.abandon_calls += 1


@dataclass
class FakeBundleStore:
    """Bundle-store fake with a fixed writer and scripted existence."""

    writer: RecoveryBundleWriter
    exists_for_any: bool = False
    existing_bundle_ids: set[UUID] = field(default_factory=set)
    ledger: list[str] = field(default_factory=list)
    exists_calls: list[UUID] = field(default_factory=list)
    staging_calls: list[UUID] = field(default_factory=list)

    def bundle_exists(self, bundle_id: UUID) -> bool:
        self.exists_calls.append(bundle_id)
        return self.exists_for_any or bundle_id in self.existing_bundle_ids

    def create_staging(self, bundle_id: UUID) -> AbstractAsyncContextManager[RecoveryBundleWriter]:
        self.staging_calls.append(bundle_id)
        return self._create_staging(bundle_id)

    @asynccontextmanager
    async def _create_staging(self, bundle_id: UUID) -> AsyncIterator[RecoveryBundleWriter]:
        self.ledger.append(STAGING_ENTERED)
        yield self.writer


@dataclass
class RecordedDumpCall:
    snapshot_token: str
    output_file: Path
    target: PostgresqlConnectionTarget
    timeout_seconds: float


@dataclass
class FakeDumpProcess:
    """Dump-process fake issuing a fixed receipt or a scripted typed failure."""

    receipt: DumpReceipt = _DUMP_RECEIPT
    error: RecoveryError | None = None
    calls: list[RecordedDumpCall] = field(default_factory=list)

    async def create_dump(
        self,
        snapshot_token: str,
        output_file: Path,
        target: PostgresqlConnectionTarget,
        *,
        timeout_seconds: float = 600.0,
    ) -> DumpReceipt:
        self.calls.append(
            RecordedDumpCall(
                snapshot_token=snapshot_token,
                output_file=output_file,
                target=target,
                timeout_seconds=timeout_seconds,
            )
        )
        if self.error is not None:
            raise self.error
        return self.receipt

    async def restore_dump(
        self,
        input_file: Path,
        target: PostgresqlConnectionTarget,
        *,
        timeout_seconds: float = 600.0,
    ) -> object:
        raise AssertionError("backup creation must never restore")


class ChunkedVerifiedReader:
    """Reader fake serving fixed bytes, optionally blocking on an event."""

    def __init__(
        self,
        payload: bytes,
        *,
        block_event: asyncio.Event | None = None,
        read_entered: asyncio.Event | None = None,
    ) -> None:
        self._remaining = payload
        self._block_event = block_event
        self._read_entered = read_entered

    async def read(self, size_bytes: int = 1_048_576) -> bytes:
        if self._read_entered is not None:
            self._read_entered.set()
        if self._block_event is not None:
            await self._block_event.wait()
        # Yield to the event loop so concurrent copies genuinely overlap.
        await asyncio.sleep(0)
        if not self._remaining:
            return b""
        chunk = self._remaining[: max(size_bytes, 0)]
        self._remaining = self._remaining[len(chunk) :]
        return chunk

    def __aiter__(self) -> ChunkedVerifiedReader:
        return self

    async def __anext__(self) -> bytes:
        chunk = await self.read()
        if not chunk:
            raise StopAsyncIteration
        return chunk


class ConcurrencyRecordingObjectStore:
    """Object-store fake measuring peak simultaneous verified-reader bodies.

    Serves the configured payloads digest-keyed; an unknown digest models a
    missing object. Every mutation-port method records itself and fails: the
    backup path must never mutate canonical object storage.
    """

    def __init__(
        self,
        payloads: Mapping[str, bytes],
        *,
        block_event: asyncio.Event | None = None,
        read_entered: asyncio.Event | None = None,
    ) -> None:
        self._payloads = dict(payloads)
        self._block_event = block_event
        self._read_entered = read_entered
        self.opened: list[str] = []
        self.closed = 0
        self.current_open = 0
        self.peak_open = 0
        self.mutation_calls: list[str] = []

    def open_verified_reader(
        self, expected: ExpectedObject
    ) -> AbstractAsyncContextManager[ChunkedVerifiedReader]:
        return self._open_verified_reader(expected)

    @asynccontextmanager
    async def _open_verified_reader(
        self, expected: ExpectedObject
    ) -> AsyncIterator[ChunkedVerifiedReader]:
        digest = expected.content_digest.hexadecimal
        self.opened.append(digest)
        if digest not in self._payloads:
            raise ObjectStorageError(ErrorCode.OBJECT_STORAGE_OBJECT_MISSING)
        self.current_open += 1
        self.peak_open = max(self.peak_open, self.current_open)
        try:
            yield ChunkedVerifiedReader(
                self._payloads[digest],
                block_event=self._block_event,
                read_entered=self._read_entered,
            )
        finally:
            self.current_open -= 1
            self.closed += 1

    def _reject_mutation(self, entry: str) -> object:
        self.mutation_calls.append(entry)
        raise AssertionError(f"backup creation must never mutate object storage: {entry}")

    async def resolve_verified_object(self, expected: ExpectedObject) -> object:
        return self._reject_mutation("object_store.resolve_verified_object")

    async def store_stream(
        self,
        stream: AsyncIterator[bytes],
        expected_size_bytes: int,
        media_type: str,
        claimed_sha256: str | None = None,
    ) -> object:
        return self._reject_mutation("object_store.store_stream")

    async def verify_existing_object(self, expected: ExpectedObject) -> object:
        return self._reject_mutation("object_store.verify_existing_object")


@dataclass
class BackupHarness:
    """One fully wired service with recording fakes and a shared ledger."""

    service: RecoveryService
    snapshot_store: FakeSnapshotStore
    bundle_store: FakeBundleStore
    writer: BufferedRecordingWriter
    dump_process: FakeDumpProcess
    object_store: ConcurrencyRecordingObjectStore
    metrics: InMemoryCanonicalBackupMetrics
    clock: SequencedUtcClock
    ledger: list[str]


def build_harness(
    fixtures: tuple[tuple[ExpectedObject, bytes], ...],
    *,
    schema_head: str = POSTGRESQL_SCHEMA_REVISION,
    pending_writers: int = 0,
    dump_error: RecoveryError | None = None,
    object_payloads: Mapping[str, bytes] | None = None,
) -> BackupHarness:
    ledger: list[str] = []
    objects = tuple(expected for expected, _ in fixtures)
    if object_payloads is None:
        object_payloads = {
            expected.content_digest.hexadecimal: payload for expected, payload in fixtures
        }
    snapshot_store = FakeSnapshotStore(
        build_snapshot(objects, schema_head=schema_head),
        pending_writers=pending_writers,
        ledger=ledger,
    )
    writer = BufferedRecordingWriter(ledger=ledger)
    bundle_store = FakeBundleStore(writer=writer, ledger=ledger)
    dump_process = FakeDumpProcess(error=dump_error)
    object_store = ConcurrencyRecordingObjectStore(object_payloads)
    metrics = InMemoryCanonicalBackupMetrics()
    clock = SequencedUtcClock([_SNAPSHOT_NOW, _MANIFEST_CREATED_AT])
    service = RecoveryService(
        snapshot_store=snapshot_store,
        bundle_store=bundle_store,
        dump_process=dump_process,
        object_store=object_store,
        metrics=metrics,
        clock=clock,
    )
    return BackupHarness(
        service=service,
        snapshot_store=snapshot_store,
        bundle_store=bundle_store,
        writer=writer,
        dump_process=dump_process,
        object_store=object_store,
        metrics=metrics,
        clock=clock,
        ledger=ledger,
    )


def install_event_spy(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[EventName, dict[str, object]]]:
    """Spy on the service module's event registry, recording every emission."""

    registry_calls: list[tuple[EventName, dict[str, object]]] = []
    original_registry = service_module.build_registered_event

    def recording_registry(
        event_name: EventName, fields: Mapping[str, object]
    ) -> DiagnosticEvent | RejectedDiagnosticPayload:
        registry_calls.append((event_name, dict(fields)))
        return original_registry(event_name, fields)

    monkeypatch.setattr(service_module, "build_registered_event", recording_registry)
    return registry_calls


def assert_single_failed_event(
    registry_calls: list[tuple[EventName, dict[str, object]]],
    error_code: ErrorCode,
) -> None:
    """Exactly one registered failure event with the closed error code."""

    assert len(registry_calls) == 1
    event_name, fields = registry_calls[0]
    assert event_name == EventName.CANONICAL_BACKUP_FAILED
    assert fields["operation"] is RecoveryOperation.CREATE
    assert fields["outcome"] is RecoveryMetricOutcome.FAILED
    assert fields["error_code"] == error_code
    duration_ms = fields["duration_ms"]
    assert isinstance(duration_ms, int)
    assert duration_ms >= 0


@pytest.mark.asyncio
async def test_backup_creates_verified_bundle_with_manifest_from_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixtures = build_object_fixtures(3)
    objects = tuple(expected for expected, _ in fixtures)
    harness = build_harness(fixtures)
    registry_calls = install_event_spy(monkeypatch)

    result = await harness.service.create_backup(build_command())

    assert isinstance(result, BackupCreationResult)
    assert result.bundle_id.version == 7
    assert result.object_count == 3
    assert result.byte_total == sum(expected.size_bytes for expected in objects)
    assert result.duration_seconds >= 0.0
    assert harness.writer.finalized_manifests == [
        build_expected_manifest(result.bundle_id, objects)
    ]
    (manifest,) = harness.writer.finalized_manifests
    assert [entry.content_sha256 for entry in manifest.objects] == sorted(
        entry.content_sha256 for entry in manifest.objects
    )
    assert manifest.canonical_counts == build_counts()
    assert manifest.source_environment == RecoveryEnvironment.TEST.value
    assert harness.writer.abandon_calls == 0
    assert harness.writer.written_objects == {
        expected.content_digest.hexadecimal: payload for expected, payload in fixtures
    }
    # The snapshot token flows only into the dump process.
    assert len(harness.dump_process.calls) == 1
    (dump_call,) = harness.dump_process.calls
    assert dump_call.snapshot_token == _SNAPSHOT_TOKEN
    assert dump_call.output_file == harness.writer.dump_path
    assert dump_call.target == build_command().target
    assert dump_call.timeout_seconds == 600.0
    assert harness.object_store.opened == [
        expected.content_digest.hexadecimal for expected in objects
    ]
    assert harness.object_store.mutation_calls == []
    assert registry_calls[0][0] == EventName.CANONICAL_BACKUP_CREATED


@pytest.mark.asyncio
async def test_environment_refusal_happens_before_any_io() -> None:
    harness = build_harness(build_object_fixtures(1))
    command = BackupCreateCommand(
        environment=cast(RecoveryEnvironment, "production"),
        target=build_command().target,
    )

    with pytest.raises(RecoveryError) as excinfo:
        await harness.service.create_backup(command)

    error = excinfo.value
    assert error.error_code == ErrorCode.CANONICAL_RECOVERY_ENVIRONMENT_REFUSED
    assert dict(error.safe_details) == {"operation": RecoveryOperation.CREATE}
    assert error.is_retryable is False
    # Zero interactions: no snapshot, no staging, no dump, no reader, no metric.
    assert harness.snapshot_store.open_calls == []
    assert harness.snapshot_store.observe_calls == 0
    assert harness.bundle_store.exists_calls == []
    assert harness.bundle_store.staging_calls == []
    assert harness.dump_process.calls == []
    assert harness.object_store.opened == []
    assert harness.object_store.mutation_calls == []
    assert (
        harness.metrics.backup_count(RecoveryOperation.CREATE, RecoveryMetricOutcome.SUCCEEDED) == 0
    )
    assert harness.metrics.backup_count(RecoveryOperation.CREATE, RecoveryMetricOutcome.FAILED) == 0


@pytest.mark.asyncio
async def test_schema_head_mismatch_refuses_backup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = build_harness(build_object_fixtures(1), schema_head="20260701_99")
    registry_calls = install_event_spy(monkeypatch)

    with pytest.raises(RecoveryError) as excinfo:
        await harness.service.create_backup(build_command())

    error = excinfo.value
    assert error.error_code == ErrorCode.CANONICAL_RECOVERY_CONFIGURATION_INVALID
    assert dict(error.safe_details) == {"reason": RecoveryConfigurationReason.SCHEMA_HEAD_MISMATCH}
    # The bundle store is never opened: no existence probe, no staging.
    assert harness.bundle_store.exists_calls == []
    assert harness.bundle_store.staging_calls == []
    assert harness.writer.finalized_manifests == []
    assert harness.writer.abandon_calls == 0
    assert harness.metrics.backup_count(RecoveryOperation.CREATE, RecoveryMetricOutcome.FAILED) == 1
    assert_single_failed_event(registry_calls, ErrorCode.CANONICAL_RECOVERY_CONFIGURATION_INVALID)


@pytest.mark.asyncio
async def test_existing_bundle_id_refuses_without_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = build_harness(build_object_fixtures(1))
    harness.bundle_store.exists_for_any = True
    registry_calls = install_event_spy(monkeypatch)

    with pytest.raises(RecoveryError) as excinfo:
        await harness.service.create_backup(build_command())

    error = excinfo.value
    assert error.error_code == ErrorCode.CANONICAL_RECOVERY_BUNDLE_EXISTS
    refused_bundle_id = error.safe_details["bundle_id"]
    assert isinstance(refused_bundle_id, UUID)
    assert refused_bundle_id.version == 7
    # No staging is created and nothing else is touched.
    assert harness.bundle_store.staging_calls == []
    assert harness.dump_process.calls == []
    assert harness.object_store.opened == []
    assert harness.object_store.mutation_calls == []
    assert harness.writer.finalized_manifests == []
    assert harness.writer.abandon_calls == 0
    assert harness.metrics.backup_count(RecoveryOperation.CREATE, RecoveryMetricOutcome.FAILED) == 1
    assert_single_failed_event(registry_calls, ErrorCode.CANONICAL_RECOVERY_BUNDLE_EXISTS)


@pytest.mark.asyncio
async def test_object_reads_bounded_to_four_concurrent() -> None:
    fixtures = build_object_fixtures(10)
    payloads = {expected.content_digest.hexadecimal: payload for expected, payload in fixtures}
    harness = build_harness(fixtures, object_payloads=payloads)

    result = await harness.service.create_backup(build_command())

    assert result.object_count == 10
    assert len(harness.object_store.opened) == 10
    assert harness.object_store.peak_open == BACKUP_OBJECT_READ_CONCURRENCY
    assert harness.object_store.closed == 10


@pytest.mark.asyncio
async def test_pending_writer_before_finalize_aborts_bundle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = build_harness(build_object_fixtures(2), pending_writers=1)
    registry_calls = install_event_spy(monkeypatch)

    with pytest.raises(RecoveryError) as excinfo:
        await harness.service.create_backup(build_command())

    error = excinfo.value
    assert error.error_code == ErrorCode.CANONICAL_RECOVERY_SNAPSHOT_BUSY
    assert error.is_retryable is True
    assert harness.snapshot_store.observe_calls == 1
    assert harness.writer.abandon_calls == 1
    assert harness.writer.finalized_manifests == []
    assert harness.metrics.backup_count(RecoveryOperation.CREATE, RecoveryMetricOutcome.FAILED) == 1
    assert_single_failed_event(registry_calls, ErrorCode.CANONICAL_RECOVERY_SNAPSHOT_BUSY)


@pytest.mark.asyncio
async def test_dump_failure_abandons_staging_and_never_touches_canonical_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = build_harness(
        build_object_fixtures(2),
        dump_error=RecoveryError(
            ErrorCode.CANONICAL_RECOVERY_DEPENDENCY_UNAVAILABLE,
            safe_details={"dependency": RecoveryDependency.PG_CLIENT},
        ),
    )
    registry_calls = install_event_spy(monkeypatch)

    with pytest.raises(RecoveryError) as excinfo:
        await harness.service.create_backup(build_command())

    assert excinfo.value.error_code == ErrorCode.CANONICAL_RECOVERY_DEPENDENCY_UNAVAILABLE
    assert harness.writer.abandon_calls == 1
    assert harness.writer.finalized_manifests == []
    # The dump failed before any object read: canonical state is untouched.
    assert harness.object_store.opened == []
    assert harness.object_store.mutation_calls == []
    assert harness.metrics.backup_count(RecoveryOperation.CREATE, RecoveryMetricOutcome.FAILED) == 1
    assert_single_failed_event(registry_calls, ErrorCode.CANONICAL_RECOVERY_DEPENDENCY_UNAVAILABLE)


@pytest.mark.asyncio
async def test_object_read_failure_abandons_staging_without_r2_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = build_harness(build_object_fixtures(2), object_payloads={})
    registry_calls = install_event_spy(monkeypatch)

    with pytest.raises(ObjectStorageError) as excinfo:
        await harness.service.create_backup(build_command())

    assert excinfo.value.error_code == ErrorCode.OBJECT_STORAGE_OBJECT_MISSING
    assert harness.writer.abandon_calls == 1
    assert harness.writer.finalized_manifests == []
    assert harness.object_store.mutation_calls == []
    assert harness.metrics.backup_count(RecoveryOperation.CREATE, RecoveryMetricOutcome.FAILED) == 1
    assert_single_failed_event(registry_calls, ErrorCode.OBJECT_STORAGE_OBJECT_MISSING)


@pytest.mark.asyncio
async def test_cancellation_removes_exact_staging_and_closes_readers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixtures = build_object_fixtures(1)
    ((expected_object, payload),) = fixtures
    block_event = asyncio.Event()
    read_entered = asyncio.Event()
    ledger: list[str] = []
    snapshot_store = FakeSnapshotStore(build_snapshot((expected_object,)), ledger=ledger)
    writer = BufferedRecordingWriter(ledger=ledger)
    bundle_store = FakeBundleStore(writer=writer, ledger=ledger)
    object_store = ConcurrencyRecordingObjectStore(
        {expected_object.content_digest.hexadecimal: payload},
        block_event=block_event,
        read_entered=read_entered,
    )
    metrics = InMemoryCanonicalBackupMetrics()
    service = RecoveryService(
        snapshot_store=snapshot_store,
        bundle_store=bundle_store,
        dump_process=FakeDumpProcess(),
        object_store=object_store,
        metrics=metrics,
        clock=SequencedUtcClock([_SNAPSHOT_NOW, _MANIFEST_CREATED_AT]),
    )
    registry_calls = install_event_spy(monkeypatch)

    backup_task = asyncio.create_task(service.create_backup(build_command()))
    await asyncio.wait_for(read_entered.wait(), timeout=5.0)
    backup_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await backup_task

    assert writer.abandon_calls == 1
    assert writer.finalized_manifests == []
    assert object_store.opened == [expected_object.content_digest.hexadecimal]
    assert object_store.closed == len(object_store.opened)
    assert object_store.current_open == 0
    # The snapshot transaction was exited: no lock is left open.
    assert ledger[0] == SNAPSHOT_OPEN
    assert ledger[-1] == SNAPSHOT_EXIT
    assert WRITER_FINALIZE not in ledger
    assert registry_calls == []
    assert metrics.backup_count(RecoveryOperation.CREATE, RecoveryMetricOutcome.FAILED) == 0


@pytest.mark.asyncio
async def test_success_emits_created_event_and_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixtures = build_object_fixtures(2)
    objects = tuple(expected for expected, _ in fixtures)
    harness = build_harness(fixtures)
    registry_calls = install_event_spy(monkeypatch)

    result = await harness.service.create_backup(build_command())

    (record,) = harness.metrics.backup_records()
    assert record.operation is RecoveryOperation.CREATE
    assert record.outcome is RecoveryMetricOutcome.SUCCEEDED
    assert record.object_count == 2
    assert record.byte_total == sum(expected.size_bytes for expected in objects)
    assert 0.0 <= record.duration_seconds < 60.0
    assert len(registry_calls) == 1
    event_name, fields = registry_calls[0]
    assert event_name == EventName.CANONICAL_BACKUP_CREATED
    assert fields["operation"] is RecoveryOperation.CREATE
    assert fields["outcome"] is RecoveryMetricOutcome.SUCCEEDED
    assert fields["bundle_id"] == result.bundle_id
    assert fields["object_count"] == result.object_count
    assert fields["byte_total"] == result.byte_total
    duration_ms = fields["duration_ms"]
    assert isinstance(duration_ms, int)
    assert duration_ms >= 0


@pytest.mark.asyncio
async def test_snapshot_transaction_stays_open_through_finalize() -> None:
    harness = build_harness(build_object_fixtures(1))

    await harness.service.create_backup(build_command())

    assert harness.ledger == [
        SNAPSHOT_OPEN,
        STAGING_ENTERED,
        WRITER_FINALIZE,
        SNAPSHOT_EXIT,
    ]


@pytest.mark.asyncio
async def test_port_only_writer_receives_streamed_object_files(tmp_path: Path) -> None:
    """A writer without the buffered method still receives every object byte."""

    fixtures = build_object_fixtures(2)
    objects = tuple(expected for expected, _ in fixtures)
    ledger: list[str] = []
    snapshot_store = FakeSnapshotStore(build_snapshot(objects), ledger=ledger)
    writer = StreamingRecordingWriter(object_root=tmp_path, ledger=ledger)
    bundle_store = FakeBundleStore(writer=writer, ledger=ledger)
    object_store = ConcurrencyRecordingObjectStore(
        {expected.content_digest.hexadecimal: payload for expected, payload in fixtures}
    )
    service = RecoveryService(
        snapshot_store=snapshot_store,
        bundle_store=bundle_store,
        dump_process=FakeDumpProcess(),
        object_store=object_store,
        metrics=InMemoryCanonicalBackupMetrics(),
        clock=SequencedUtcClock([_SNAPSHOT_NOW, _MANIFEST_CREATED_AT]),
    )

    result = await service.create_backup(build_command())

    (manifest,) = writer.finalized_manifests
    assert manifest == build_expected_manifest(result.bundle_id, objects)
    for expected_object, payload in fixtures:
        streamed_file = writer.object_path(expected_object.content_digest.hexadecimal)
        assert streamed_file.exists()
        assert streamed_file.read_bytes() == payload
