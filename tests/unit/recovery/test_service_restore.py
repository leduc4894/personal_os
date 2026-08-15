"""Recovery service empty-target restore orchestration (spec 11.1-11.3).

The fakes prove the binding flow with one ordered call ledger: environment and
confirmation gates fire before any port; a fresh complete offline verification
gates an otherwise untouched target; R2 objects are restored before
``pg_restore`` at a bounded concurrency of four; the restored graph, every
referenced object and the acceptance smoke read are verified before the safe
success receipt; and every failure path fails closed without overwriting,
deleting or compensating already restored objects.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from personal_os.diagnostics.context import create_diagnostic_context
from personal_os.diagnostics.events import EventName
from personal_os.error_contracts.codes import ErrorCode
from personal_os.object_storage import (
    CanonicalMediaType,
    ContentDigest,
    ExpectedObject,
    VerificationMethod,
    VerifiedObjectReceipt,
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
    RecoveryBundleInvalidReason,
    RecoveryComponent,
    RecoveryDependency,
    RecoveryEnvironment,
    RecoveryError,
    RecoveryManifest,
    RecoveryMetricOutcome,
    RecoveryOperation,
)
from personal_os.recovery.ports import (
    PostgresqlConnectionTarget,
    RestoreReceipt,
)
from personal_os.recovery.service import (
    RESTORE_OBJECT_WRITE_CONCURRENCY,
    AcceptanceSmokeProbe,
    RecoveryService,
    RestoreEmptyCommand,
    RestoreEmptyResult,
)
from personal_os.sources.reading import ReadCurrentSourceCommand

#: Shared ledger entry constants: one string per observed port event.
VERIFY_OFFLINE: str = "verify_offline"
OBJECT_RESTORE: str = "r2.object_restore"
OBJECT_VERIFY_PRE: str = "r2.verify_existing_pre"
OBJECT_VERIFY_POST: str = "r2.post_verify"
RESTORE_DUMP: str = "restore_dump"
TARGET_EMPTINESS: str = "target.is_application_empty"
TARGET_VERSION: str = "target.server_version"
TARGET_SCHEMA_HEAD_PRE: str = "target.schema_head_pre"
TARGET_SCHEMA_HEAD_POST: str = "target.schema_head_post"
TARGET_COUNTS: str = "target.read_canonical_counts"
TARGET_POINTER_RESOLUTION: str = "target.read_current_pointer_resolution"
SMOKE_READ: str = "smoke.read"

_MANIFEST_CREATED_AT: datetime = datetime(2026, 8, 15, 12, 0, 5, tzinfo=UTC)
_RESTORE_COMPLETED_AT: datetime = datetime(2026, 8, 15, 12, 2, 0, tzinfo=UTC)
_STORE_VERIFIED_AT: datetime = datetime(2026, 8, 15, 12, 1, 30, tzinfo=UTC)
_DUMP_SHA256: str = hashlib.sha256(b"fake pg_dump archive").hexdigest()
_BUNDLE_ID: UUID = UUID("018f5b7d-21c0-7c2e-9a4f-3b6d8e5a7c91")


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


def build_manifest(objects: tuple[ExpectedObject, ...]) -> RecoveryManifest:
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
        bundle_id=_BUNDLE_ID,
        created_at=_MANIFEST_CREATED_AT,
        source_environment=RecoveryEnvironment.TEST.value,
        postgresql_server_version=POSTGRESQL_SERVER_VERSION,
        postgresql_schema_revision=POSTGRESQL_SCHEMA_REVISION,
        postgres_dump=ManifestDumpEntry(
            relative_path="postgres.dump", size_bytes=4096, sha256=_DUMP_SHA256
        ),
        canonical_counts=build_counts(),
        objects=entries,
    )


@dataclass
class FakeVerifiedBundle:
    """Opened-bundle fake serving manifest, dump path and object files."""

    manifest: RecoveryManifest
    dump_path: Path
    object_root: Path

    def object_path(self, content_sha256: str) -> Path:
        return self.object_root / f"{content_sha256}.bin"


@dataclass
class RecordedRestoreCall:
    input_file: Path
    target: PostgresqlConnectionTarget
    timeout_seconds: float


class FakeRestoreBundleStore:
    """Bundle-store fake performing verification when the bundle is opened."""

    def __init__(
        self,
        bundle: FakeVerifiedBundle,
        *,
        error: RecoveryError | None = None,
        ledger: list[str] | None = None,
    ) -> None:
        self._bundle = bundle
        self._error = error
        self.ledger: list[str] = ledger if ledger is not None else []
        self.verify_calls: list[UUID] = []

    def verify_offline(self, bundle_id: UUID) -> RecoveryManifest:
        self.verify_calls.append(bundle_id)
        if self._error is not None:
            raise self._error
        return self._bundle.manifest

    def open_verified(self, bundle_id: UUID) -> object:
        return self._open_verified(bundle_id)

    @asynccontextmanager
    async def _open_verified(self, bundle_id: UUID) -> AsyncIterator[FakeVerifiedBundle]:
        self.verify_calls.append(bundle_id)
        self.ledger.append(VERIFY_OFFLINE)
        if self._error is not None:
            raise self._error
        yield self._bundle

    def create_staging(self, bundle_id: UUID) -> object:
        raise AssertionError("restore must never stage bundles")

    def bundle_exists(self, bundle_id: UUID) -> bool:
        raise AssertionError("restore must never probe bundle existence")


@dataclass
class FakeRestoreTarget:
    """Restore-target fake scripting admission and post-restore probes."""

    ledger: list[str]
    is_empty: bool = True
    version: str = POSTGRESQL_SERVER_VERSION
    pre_schema_head: str | None = None
    post_schema_head: str | None = POSTGRESQL_SCHEMA_REVISION
    counts: Mapping[str, int] = field(default_factory=build_counts)
    pointer_resolution: int = 0
    is_restored: bool = False
    emptiness_calls: int = 0
    version_calls: int = 0
    schema_head_calls: int = 0
    counts_calls: int = 0
    pointer_calls: int = 0

    async def is_application_empty(self) -> bool:
        self.emptiness_calls += 1
        self.ledger.append(TARGET_EMPTINESS)
        return self.is_empty

    async def server_version(self) -> str:
        self.version_calls += 1
        self.ledger.append(TARGET_VERSION)
        return self.version

    async def read_schema_head(self) -> str | None:
        self.schema_head_calls += 1
        if self.is_restored:
            self.ledger.append(TARGET_SCHEMA_HEAD_POST)
            return self.post_schema_head
        self.ledger.append(TARGET_SCHEMA_HEAD_PRE)
        return self.pre_schema_head

    async def read_canonical_counts(self) -> Mapping[str, int]:
        self.counts_calls += 1
        self.ledger.append(TARGET_COUNTS)
        return self.counts

    async def read_current_pointer_resolution(self) -> int:
        self.pointer_calls += 1
        self.ledger.append(TARGET_POINTER_RESOLUTION)
        return self.pointer_resolution


@dataclass
class RecordedStoreStreamCall:
    chunks: tuple[bytes, ...]
    expected_size_bytes: int
    media_type: str
    claimed_sha256: str | None


class FakeRestoreObjectStore:
    """Object-store fake modeling conditional store, full verify and reuse.

    Serves payloads digest-keyed; a digest absent from ``existing`` models a
    missing key that must be restored through ``store_stream``. Restores are
    observable for concurrency; the deleted-keys list stays empty forever
    because compensation deletes are forbidden (spec 11.2).
    """

    def __init__(
        self,
        existing: Mapping[str, bytes],
        *,
        ledger: list[str] | None = None,
        is_post_restore_flag: list[bool] | None = None,
    ) -> None:
        self._existing = dict(existing)
        self.ledger: list[str] = ledger if ledger is not None else []
        self._post_restore_flag = (
            is_post_restore_flag if is_post_restore_flag is not None else [False]
        )
        self.resolve_calls: list[ExpectedObject] = []
        self.store_stream_calls: list[RecordedStoreStreamCall] = []
        self.verify_calls: list[ExpectedObject] = []
        self.deleted_keys: list[str] = []
        self.current_open = 0
        self.peak_open = 0

    def _receipt(self, expected: ExpectedObject) -> VerifiedObjectReceipt:
        return VerifiedObjectReceipt(
            content_digest=expected.content_digest,
            object_key=derive_canonical_object_key(expected.content_digest),
            size_bytes=expected.size_bytes,
            media_type=expected.media_type,
            verified_at=_STORE_VERIFIED_AT,
            verification_method=VerificationMethod.EXISTING_FULL_READ,
        )

    async def resolve_verified_object(
        self, expected: ExpectedObject
    ) -> VerifiedObjectReceipt | None:
        self.resolve_calls.append(expected)
        if expected.content_digest.hexadecimal in self._existing:
            return self._receipt(expected)
        return None

    async def store_stream(
        self,
        stream: AsyncIterator[bytes],
        expected_size_bytes: int,
        media_type: str,
        claimed_sha256: str | None = None,
    ) -> VerifiedObjectReceipt:
        self.current_open += 1
        self.peak_open = max(self.peak_open, self.current_open)
        self.ledger.append(OBJECT_RESTORE)
        try:
            # Yield to the event loop so concurrent restores genuinely overlap.
            await asyncio.sleep(0)
            chunks: list[bytes] = []
            async for chunk in stream:
                chunks.append(chunk)
            await asyncio.sleep(0)
        finally:
            self.current_open -= 1
        self.store_stream_calls.append(
            RecordedStoreStreamCall(
                chunks=tuple(chunks),
                expected_size_bytes=expected_size_bytes,
                media_type=media_type,
                claimed_sha256=claimed_sha256,
            )
        )
        payload = b"".join(chunks)
        if (
            claimed_sha256 is None
            or hashlib.sha256(payload).hexdigest() != claimed_sha256
            or len(payload) != expected_size_bytes
        ):
            raise ObjectStorageError(ErrorCode.OBJECT_STORAGE_INTEGRITY_FAILED)
        assert claimed_sha256 is not None
        self._existing[claimed_sha256] = payload
        expected = ExpectedObject(
            content_digest=ContentDigest.parse(claimed_sha256),
            size_bytes=expected_size_bytes,
            media_type=CanonicalMediaType.parse(media_type),
        )
        return self._receipt(expected)

    async def verify_existing_object(self, expected: ExpectedObject) -> VerifiedObjectReceipt:
        self.verify_calls.append(expected)
        self.ledger.append(OBJECT_VERIFY_POST if self._post_restore_flag[0] else OBJECT_VERIFY_PRE)
        payload = self._existing.get(expected.content_digest.hexadecimal)
        if payload is None:
            raise ObjectStorageError(ErrorCode.OBJECT_STORAGE_OBJECT_MISSING)
        if (
            hashlib.sha256(payload).hexdigest() != expected.content_digest.hexadecimal
            or len(payload) != expected.size_bytes
        ):
            raise ObjectStorageError(ErrorCode.OBJECT_STORAGE_INTEGRITY_FAILED)
        return self._receipt(expected)

    def delete_object_for_test(self, object_key: str) -> None:
        self.deleted_keys.append(object_key)
        raise AssertionError("restore must never delete restored objects")

    def stored_payloads(self) -> dict[str, bytes]:
        return dict(self._existing)


class FakeRestoreDumpProcess:
    """Dump-process fake recording the single restore call, or failing it."""

    def __init__(
        self,
        target: FakeRestoreTarget,
        object_store: FakeRestoreObjectStore,
        *,
        error: RecoveryError | None = None,
        ledger: list[str] | None = None,
    ) -> None:
        self._target = target
        self._object_store = object_store
        self._error = error
        self.ledger: list[str] = ledger if ledger is not None else []
        self.restore_calls: list[RecordedRestoreCall] = []

    async def create_dump(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("restore must never dump")

    async def restore_dump(
        self,
        input_file: Path,
        target: PostgresqlConnectionTarget,
        *,
        timeout_seconds: float = 600.0,
    ) -> RestoreReceipt:
        self.ledger.append(RESTORE_DUMP)
        self.restore_calls.append(
            RecordedRestoreCall(
                input_file=input_file, target=target, timeout_seconds=timeout_seconds
            )
        )
        if self._error is not None:
            raise self._error
        self._target.is_restored = True
        self._object_store._post_restore_flag[0] = True
        return RestoreReceipt(completed_at=_RESTORE_COMPLETED_AT)


class FakeAcceptanceReadService:
    """Canonical-read fake serving scripted bytes for the acceptance smoke."""

    def __init__(self, payload: bytes, *, ledger: list[str] | None = None) -> None:
        self._payload = payload
        self.ledger: list[str] = ledger if ledger is not None else []
        self.calls: list[tuple[ReadCurrentSourceCommand, object]] = []

    async def read_current_source_bytes(
        self, command: ReadCurrentSourceCommand, diagnostic_context: object
    ) -> bytes:
        self.ledger.append(SMOKE_READ)
        self.calls.append((command, diagnostic_context))
        return self._payload


@dataclass
class RestoreHarness:
    """One fully wired service with recording fakes and a shared ledger."""

    service: RecoveryService
    bundle_store: FakeRestoreBundleStore
    object_store: FakeRestoreObjectStore
    dump_process: FakeRestoreDumpProcess
    restore_target: FakeRestoreTarget
    read_service: FakeAcceptanceReadService
    metrics: InMemoryCanonicalBackupMetrics
    ledger: list[str]
    bundle: FakeVerifiedBundle
    manifest: RecoveryManifest
    fixtures: tuple[tuple[ExpectedObject, bytes], ...]


def build_restore_harness(
    tmp_path: Path,
    *,
    object_count: int = 3,
    existing_digests: frozenset[str] = frozenset(),
    bundle_error: RecoveryError | None = None,
    dump_error: RecoveryError | None = None,
    smoke_payload: bytes | None = None,
) -> RestoreHarness:
    fixtures = build_object_fixtures(object_count)
    objects = tuple(expected for expected, _ in fixtures)
    manifest = build_manifest(objects)
    object_root = tmp_path / "objects"
    object_root.mkdir()
    for expected, payload in fixtures:
        (object_root / f"{expected.content_digest.hexadecimal}.bin").write_bytes(payload)
    dump_path = tmp_path / "postgres.dump"
    dump_path.write_bytes(b"fake pg_dump archive")
    bundle = FakeVerifiedBundle(manifest=manifest, dump_path=dump_path, object_root=object_root)
    ledger: list[str] = []
    existing = {
        expected.content_digest.hexadecimal: payload
        for expected, payload in fixtures
        if expected.content_digest.hexadecimal in existing_digests
    }
    post_restore_flag = [False]
    object_store = FakeRestoreObjectStore(
        existing, ledger=ledger, is_post_restore_flag=post_restore_flag
    )
    restore_target = FakeRestoreTarget(ledger=ledger)
    dump_process = FakeRestoreDumpProcess(
        restore_target, object_store, error=dump_error, ledger=ledger
    )
    smoke_payload = smoke_payload if smoke_payload is not None else fixtures[0][1]
    read_service = FakeAcceptanceReadService(smoke_payload, ledger=ledger)
    metrics = InMemoryCanonicalBackupMetrics()
    bundle_store = FakeRestoreBundleStore(bundle, error=bundle_error, ledger=ledger)
    service = RecoveryService(
        snapshot_store=_RefusingSnapshotStore(),
        bundle_store=bundle_store,
        dump_process=dump_process,
        object_store=object_store,
        metrics=metrics,
        clock=lambda: _MANIFEST_CREATED_AT,
    )
    return RestoreHarness(
        service=service,
        bundle_store=bundle_store,
        object_store=object_store,
        dump_process=dump_process,
        restore_target=restore_target,
        read_service=read_service,
        metrics=metrics,
        ledger=ledger,
        bundle=bundle,
        manifest=manifest,
        fixtures=fixtures,
    )


class _RefusingSnapshotStore:
    """Snapshot-store stand-in proving restore never opens a backup snapshot."""

    def open_quiesced_snapshot(self, now: datetime) -> object:
        raise AssertionError("restore must never open a snapshot")

    async def observe_pending_writers(self) -> int:
        raise AssertionError("restore must never observe writers")


def build_restore_command(
    *,
    environment: RecoveryEnvironment = RecoveryEnvironment.TEST,
    confirmation: str = "knowledge",
    probe: AcceptanceSmokeProbe | None = None,
) -> RestoreEmptyCommand:
    return RestoreEmptyCommand(
        environment=environment,
        bundle_id=_BUNDLE_ID,
        target=PostgresqlConnectionTarget(
            host="localhost", port=5432, database="knowledge", user="knowledge"
        ),
        target_confirmation=confirmation,
        acceptance_probe=probe,
    )


def build_probe(fixtures: tuple[tuple[ExpectedObject, bytes], ...]) -> AcceptanceSmokeProbe:
    expected, _ = fixtures[0]
    return AcceptanceSmokeProbe(
        workspace_id=UUID("018f5b7d-3aa1-7b0c-8e2d-1f4a6c8e0d21"),
        source_id=UUID("018f5b7d-4bb2-7c1d-9f3e-2a5b7d9f1e32"),
        expected_sha256=expected.content_digest.hexadecimal,
        expected_size_bytes=expected.size_bytes,
        expected_media_type=expected.media_type,
    )


def install_event_spy(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[EventName, dict[str, object]]]:
    """Spy on the service module's event registry, recording every emission."""

    registry_calls: list[tuple[EventName, dict[str, object]]] = []
    original_registry = service_module.build_registered_event

    def recording_registry(event_name: EventName, fields: dict[str, object]) -> object:
        registry_calls.append((event_name, dict(fields)))
        return original_registry(event_name, fields)

    monkeypatch.setattr(service_module, "build_registered_event", recording_registry)
    return registry_calls


def assert_single_restore_failed_event(
    registry_calls: list[tuple[EventName, dict[str, object]]],
    error_code: ErrorCode,
) -> None:
    """Exactly one registered restore-failure event with the closed code."""

    assert len(registry_calls) == 1
    event_name, fields = registry_calls[0]
    assert event_name == EventName.CANONICAL_RESTORE_FAILED
    assert fields["operation"] is RecoveryOperation.RESTORE
    assert fields["outcome"] is RecoveryMetricOutcome.FAILED
    assert fields["bundle_id"] == _BUNDLE_ID
    assert fields["error_code"] == error_code
    duration_ms = fields["duration_ms"]
    assert isinstance(duration_ms, int)
    assert duration_ms >= 0


@pytest.mark.asyncio
async def test_restore_order_is_verify_r2_pgrestore_graph_smoke_receipt(
    tmp_path: Path,
) -> None:
    harness = build_restore_harness(tmp_path, object_count=3)
    command = build_restore_command(probe=build_probe(harness.fixtures))

    result = await harness.service.restore_empty(
        command,
        read_service=harness.read_service,
        restore_target=harness.restore_target,
    )

    assert isinstance(result, RestoreEmptyResult)
    ledger = harness.ledger
    assert ledger[0] == VERIFY_OFFLINE
    object_restore_positions = [
        position for position, token in enumerate(ledger) if token == OBJECT_RESTORE
    ]
    assert len(object_restore_positions) == 3
    restore_dump_position = ledger.index(RESTORE_DUMP)
    assert all(position < restore_dump_position for position in object_restore_positions)
    schema_head_post_position = ledger.index(TARGET_SCHEMA_HEAD_POST)
    counts_position = ledger.index(TARGET_COUNTS)
    pointer_position = ledger.index(TARGET_POINTER_RESOLUTION)
    assert restore_dump_position < schema_head_post_position < counts_position < pointer_position
    post_verify_positions = [
        position for position, token in enumerate(ledger) if token == OBJECT_VERIFY_POST
    ]
    assert len(post_verify_positions) == 3
    assert all(position > pointer_position for position in post_verify_positions)
    smoke_position = ledger.index(SMOKE_READ)
    assert smoke_position > max(post_verify_positions)
    # Admission probes run after verification and before any R2 restore.
    admission_positions = [
        ledger.index(TARGET_EMPTINESS),
        ledger.index(TARGET_VERSION),
        ledger.index(TARGET_SCHEMA_HEAD_PRE),
    ]
    verify_position = ledger.index(VERIFY_OFFLINE)
    assert all(
        verify_position < position < min(object_restore_positions)
        for position in admission_positions
    )
    assert result.bundle_id == _BUNDLE_ID
    assert result.completed_at == _RESTORE_COMPLETED_AT
    assert result.table_counts == build_counts()
    assert result.object_count == 3


@pytest.mark.asyncio
async def test_missing_r2_key_restored_via_conditional_store_with_claimed_digest(
    tmp_path: Path,
) -> None:
    harness = build_restore_harness(tmp_path, object_count=3)

    await harness.service.restore_empty(
        build_restore_command(),
        read_service=harness.read_service,
        restore_target=harness.restore_target,
    )

    manifest_entries = harness.manifest.objects
    assert len(harness.object_store.store_stream_calls) == 3
    for entry, call in zip(manifest_entries, harness.object_store.store_stream_calls, strict=True):
        assert call.expected_size_bytes == entry.size_bytes
        assert call.media_type == entry.media_type
        assert call.claimed_sha256 == entry.content_sha256
    for expected, payload in harness.fixtures:
        digest = expected.content_digest.hexadecimal
        assert harness.object_store.stored_payloads()[digest] == payload
    (restore_call,) = harness.dump_process.restore_calls
    assert restore_call.input_file == harness.bundle.dump_path
    assert restore_call.target == build_restore_command().target
    assert restore_call.timeout_seconds == 600.0


@pytest.mark.asyncio
async def test_existing_exact_object_reused_without_store_stream(tmp_path: Path) -> None:
    fixtures = build_object_fixtures(3)
    existing_digests = frozenset(
        expected.content_digest.hexadecimal for expected, _ in fixtures[:1]
    )
    harness = build_restore_harness(tmp_path, object_count=3, existing_digests=existing_digests)

    await harness.service.restore_empty(
        build_restore_command(),
        read_service=harness.read_service,
        restore_target=harness.restore_target,
    )

    # The existing object is full-verified and reused; the missing two stream in.
    assert len(harness.object_store.store_stream_calls) == 2
    streamed_digests = {
        str(call.claimed_sha256) for call in harness.object_store.store_stream_calls
    }
    existing_digest_set = set(existing_digests)
    assert not streamed_digests & existing_digest_set
    assert len(streamed_digests | existing_digest_set) == 3
    assert harness.object_store.resolve_calls == [
        ExpectedObject(
            content_digest=ContentDigest.parse(entry.content_sha256),
            size_bytes=entry.size_bytes,
            media_type=CanonicalMediaType.parse(entry.media_type),
        )
        for entry in harness.manifest.objects
    ]


@pytest.mark.asyncio
async def test_mismatched_existing_object_fails_closed_without_overwrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixtures = build_object_fixtures(1)
    ((expected, _),) = fixtures
    corrupt_payload = b"same-size corrupt canonical recovery object"
    harness = build_restore_harness(tmp_path, object_count=1)
    harness.object_store._existing[expected.content_digest.hexadecimal] = corrupt_payload
    registry_calls = install_event_spy(monkeypatch)

    with pytest.raises(ObjectStorageError) as excinfo:
        await harness.service.restore_empty(
            build_restore_command(),
            read_service=harness.read_service,
            restore_target=harness.restore_target,
        )

    assert excinfo.value.error_code == ErrorCode.OBJECT_STORAGE_INTEGRITY_FAILED
    assert harness.object_store.store_stream_calls == []
    assert harness.dump_process.restore_calls == []
    assert RESTORE_DUMP not in harness.ledger
    # The corrupt key is neither overwritten nor deleted.
    assert (
        harness.object_store.stored_payloads()[expected.content_digest.hexadecimal]
        == corrupt_payload
    )
    assert harness.object_store.deleted_keys == []
    failed_restores = harness.metrics.backup_count(
        RecoveryOperation.RESTORE, RecoveryMetricOutcome.FAILED
    )
    assert failed_restores == 1
    assert_single_restore_failed_event(registry_calls, ErrorCode.OBJECT_STORAGE_INTEGRITY_FAILED)


@pytest.mark.asyncio
async def test_restore_object_writes_bounded_to_four_concurrent(tmp_path: Path) -> None:
    harness = build_restore_harness(tmp_path, object_count=10)

    result = await harness.service.restore_empty(
        build_restore_command(),
        read_service=harness.read_service,
        restore_target=harness.restore_target,
    )

    assert result.object_count == 10
    assert len(harness.object_store.store_stream_calls) == 10
    assert harness.object_store.peak_open == RESTORE_OBJECT_WRITE_CONCURRENCY
    assert harness.object_store.current_open == 0


@pytest.mark.asyncio
async def test_pg_restore_failure_maps_restore_failed_and_leaves_no_success_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = build_restore_harness(
        tmp_path,
        object_count=2,
        dump_error=RecoveryError(
            ErrorCode.CANONICAL_RECOVERY_RESTORE_FAILED,
            safe_details={"component": RecoveryComponent.POSTGRES_RESTORE},
        ),
    )
    registry_calls = install_event_spy(monkeypatch)

    with pytest.raises(RecoveryError) as excinfo:
        await harness.service.restore_empty(
            build_restore_command(),
            read_service=harness.read_service,
            restore_target=harness.restore_target,
        )

    error = excinfo.value
    assert error.error_code == ErrorCode.CANONICAL_RECOVERY_RESTORE_FAILED
    assert error.safe_details["component"] == "postgres_restore"
    # Already restored objects stay as safe unreferenced CAS bytes.
    assert len(harness.object_store.store_stream_calls) == 2
    assert harness.object_store.deleted_keys == []
    assert harness.restore_target.is_restored is False
    assert TARGET_COUNTS not in harness.ledger
    assert SMOKE_READ not in harness.ledger
    assert (
        harness.metrics.backup_count(RecoveryOperation.RESTORE, RecoveryMetricOutcome.SUCCEEDED)
        == 0
    )
    failed_restores = harness.metrics.backup_count(
        RecoveryOperation.RESTORE, RecoveryMetricOutcome.FAILED
    )
    assert failed_restores == 1
    assert_single_restore_failed_event(registry_calls, ErrorCode.CANONICAL_RECOVERY_RESTORE_FAILED)


@pytest.mark.asyncio
async def test_target_not_empty_refused_before_any_io(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = build_restore_harness(tmp_path, object_count=2)
    harness.restore_target.is_empty = False
    registry_calls = install_event_spy(monkeypatch)

    with pytest.raises(RecoveryError) as excinfo:
        await harness.service.restore_empty(
            build_restore_command(),
            read_service=harness.read_service,
            restore_target=harness.restore_target,
        )

    error = excinfo.value
    assert error.error_code == ErrorCode.CANONICAL_RECOVERY_TARGET_NOT_EMPTY
    assert dict(error.safe_details) == {}
    assert harness.object_store.resolve_calls == []
    assert harness.object_store.store_stream_calls == []
    assert harness.dump_process.restore_calls == []
    assert harness.restore_target.version_calls == 0
    assert TARGET_COUNTS not in harness.ledger
    assert_single_restore_failed_event(
        registry_calls, ErrorCode.CANONICAL_RECOVERY_TARGET_NOT_EMPTY
    )


@pytest.mark.asyncio
async def test_environment_refused_for_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typing import cast

    harness = build_restore_harness(tmp_path, object_count=1)
    registry_calls = install_event_spy(monkeypatch)

    with pytest.raises(RecoveryError) as excinfo:
        await harness.service.restore_empty(
            build_restore_command(environment=cast(RecoveryEnvironment, "production")),
            read_service=harness.read_service,
            restore_target=harness.restore_target,
        )

    error = excinfo.value
    assert error.error_code == ErrorCode.CANONICAL_RECOVERY_ENVIRONMENT_REFUSED
    assert dict(error.safe_details) == {"operation": RecoveryOperation.RESTORE}
    assert error.is_retryable is False
    assert harness.ledger == []
    assert harness.metrics.backup_records() == []
    assert registry_calls == []


@pytest.mark.asyncio
async def test_target_confirmation_mismatch_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = build_restore_harness(tmp_path, object_count=1)
    registry_calls = install_event_spy(monkeypatch)

    with pytest.raises(RecoveryError) as excinfo:
        await harness.service.restore_empty(
            build_restore_command(confirmation="other-database"),
            read_service=harness.read_service,
            restore_target=harness.restore_target,
        )

    error = excinfo.value
    assert error.error_code == ErrorCode.CANONICAL_RECOVERY_ENVIRONMENT_REFUSED
    assert dict(error.safe_details) == {"operation": RecoveryOperation.RESTORE}
    assert harness.ledger == []
    assert harness.metrics.backup_records() == []
    assert registry_calls == []


@pytest.mark.asyncio
async def test_invalid_bundle_never_reaches_postgresql_or_r2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = build_restore_harness(
        tmp_path,
        object_count=2,
        bundle_error=RecoveryError(
            ErrorCode.CANONICAL_RECOVERY_BUNDLE_INVALID,
            safe_details={"reason": RecoveryBundleInvalidReason.FILE_TREE_MISMATCH},
        ),
    )
    registry_calls = install_event_spy(monkeypatch)

    with pytest.raises(RecoveryError) as excinfo:
        await harness.service.restore_empty(
            build_restore_command(),
            read_service=harness.read_service,
            restore_target=harness.restore_target,
        )

    error = excinfo.value
    assert error.error_code == ErrorCode.CANONICAL_RECOVERY_BUNDLE_INVALID
    assert dict(error.safe_details) == {"reason": RecoveryBundleInvalidReason.FILE_TREE_MISMATCH}
    assert harness.object_store.resolve_calls == []
    assert harness.object_store.store_stream_calls == []
    assert harness.dump_process.restore_calls == []
    assert harness.restore_target.emptiness_calls == 0
    assert_single_restore_failed_event(registry_calls, ErrorCode.CANONICAL_RECOVERY_BUNDLE_INVALID)


@pytest.mark.asyncio
async def test_wrong_server_version_refuses_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = build_restore_harness(tmp_path, object_count=1)
    harness.restore_target.version = "17.9"
    registry_calls = install_event_spy(monkeypatch)

    with pytest.raises(RecoveryError) as excinfo:
        await harness.service.restore_empty(
            build_restore_command(),
            read_service=harness.read_service,
            restore_target=harness.restore_target,
        )

    error = excinfo.value
    assert error.error_code == ErrorCode.CANONICAL_RECOVERY_DEPENDENCY_UNAVAILABLE
    assert dict(error.safe_details) == {"dependency": RecoveryDependency.POSTGRESQL}
    assert harness.object_store.resolve_calls == []
    assert harness.dump_process.restore_calls == []
    assert_single_restore_failed_event(
        registry_calls, ErrorCode.CANONICAL_RECOVERY_DEPENDENCY_UNAVAILABLE
    )


@pytest.mark.asyncio
async def test_pre_existing_schema_head_refuses_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = build_restore_harness(tmp_path, object_count=1)
    harness.restore_target.pre_schema_head = "20260701_99"
    registry_calls = install_event_spy(monkeypatch)

    with pytest.raises(RecoveryError) as excinfo:
        await harness.service.restore_empty(
            build_restore_command(),
            read_service=harness.read_service,
            restore_target=harness.restore_target,
        )

    error = excinfo.value
    assert error.error_code == ErrorCode.CANONICAL_RECOVERY_TARGET_NOT_EMPTY
    assert harness.object_store.resolve_calls == []
    assert harness.dump_process.restore_calls == []
    assert_single_restore_failed_event(
        registry_calls, ErrorCode.CANONICAL_RECOVERY_TARGET_NOT_EMPTY
    )


@pytest.mark.asyncio
async def test_post_restore_count_mismatch_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = build_restore_harness(tmp_path, object_count=2)
    mismatched_counts = build_counts()
    mismatched_counts["sources"] = 99
    harness.restore_target.counts = mismatched_counts
    registry_calls = install_event_spy(monkeypatch)

    with pytest.raises(RecoveryError) as excinfo:
        await harness.service.restore_empty(
            build_restore_command(),
            read_service=harness.read_service,
            restore_target=harness.restore_target,
        )

    error = excinfo.value
    assert error.error_code == ErrorCode.CANONICAL_RECOVERY_RESTORE_FAILED
    assert error.safe_details["component"] == RecoveryComponent.CANONICAL_GRAPH
    assert len(harness.dump_process.restore_calls) == 1
    assert SMOKE_READ not in harness.ledger
    assert harness.object_store.deleted_keys == []
    assert_single_restore_failed_event(registry_calls, ErrorCode.CANONICAL_RECOVERY_RESTORE_FAILED)


@pytest.mark.asyncio
async def test_post_restore_schema_head_must_be_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = build_restore_harness(tmp_path, object_count=1)
    harness.restore_target.post_schema_head = "20260901_02"
    registry_calls = install_event_spy(monkeypatch)

    with pytest.raises(RecoveryError) as excinfo:
        await harness.service.restore_empty(
            build_restore_command(),
            read_service=harness.read_service,
            restore_target=harness.restore_target,
        )

    error = excinfo.value
    assert error.error_code == ErrorCode.CANONICAL_RECOVERY_RESTORE_FAILED
    assert error.safe_details["component"] == RecoveryComponent.CANONICAL_GRAPH
    assert TARGET_COUNTS not in harness.ledger
    assert_single_restore_failed_event(registry_calls, ErrorCode.CANONICAL_RECOVERY_RESTORE_FAILED)


@pytest.mark.asyncio
async def test_post_restore_current_pointer_resolution_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = build_restore_harness(tmp_path, object_count=1)
    harness.restore_target.pointer_resolution = 2
    registry_calls = install_event_spy(monkeypatch)

    with pytest.raises(RecoveryError) as excinfo:
        await harness.service.restore_empty(
            build_restore_command(),
            read_service=harness.read_service,
            restore_target=harness.restore_target,
        )

    error = excinfo.value
    assert error.error_code == ErrorCode.CANONICAL_RECOVERY_RESTORE_FAILED
    assert error.safe_details["component"] == RecoveryComponent.CANONICAL_GRAPH
    assert OBJECT_VERIFY_POST not in harness.ledger
    assert SMOKE_READ not in harness.ledger
    assert_single_restore_failed_event(registry_calls, ErrorCode.CANONICAL_RECOVERY_RESTORE_FAILED)


@pytest.mark.asyncio
async def test_post_restore_referenced_objects_full_verified(tmp_path: Path) -> None:
    harness = build_restore_harness(tmp_path, object_count=3)

    await harness.service.restore_empty(
        build_restore_command(),
        read_service=harness.read_service,
        restore_target=harness.restore_target,
    )

    # Every manifest object is full-verified from R2 again after pg_restore.
    assert len(harness.object_store.verify_calls) == 3
    verified_digests = [
        expected.content_digest.hexadecimal for expected in harness.object_store.verify_calls
    ]
    assert verified_digests == [entry.content_sha256 for entry in harness.manifest.objects]
    restore_dump_position = harness.ledger.index(RESTORE_DUMP)
    post_verify_positions = [
        position for position, token in enumerate(harness.ledger) if token == OBJECT_VERIFY_POST
    ]
    assert len(post_verify_positions) == 3
    assert all(position > restore_dump_position for position in post_verify_positions)


@pytest.mark.asyncio
async def test_acceptance_smoke_read_returns_exact_restored_bytes(
    tmp_path: Path,
) -> None:
    harness = build_restore_harness(tmp_path, object_count=2)
    probe = build_probe(harness.fixtures)
    command = build_restore_command(probe=probe)
    diagnostic_context = create_diagnostic_context().context

    result = await harness.service.restore_empty(
        command,
        read_service=harness.read_service,
        restore_target=harness.restore_target,
        diagnostic_context=diagnostic_context,
    )

    assert isinstance(result, RestoreEmptyResult)
    (read_command, seen_context) = harness.read_service.calls[0]
    assert read_command == ReadCurrentSourceCommand(
        workspace_id=probe.workspace_id, source_id=probe.source_id
    )
    assert seen_context is diagnostic_context


@pytest.mark.asyncio
async def test_acceptance_smoke_mismatch_fails_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = build_restore_harness(
        tmp_path,
        object_count=1,
        smoke_payload=b"different bytes entirely",
    )
    registry_calls = install_event_spy(monkeypatch)

    with pytest.raises(RecoveryError) as excinfo:
        await harness.service.restore_empty(
            build_restore_command(probe=build_probe(harness.fixtures)),
            read_service=harness.read_service,
            restore_target=harness.restore_target,
        )

    error = excinfo.value
    assert error.error_code == ErrorCode.CANONICAL_RECOVERY_RESTORE_FAILED
    assert error.safe_details["component"] == RecoveryComponent.CANONICAL_READ
    assert (
        harness.metrics.backup_count(RecoveryOperation.RESTORE, RecoveryMetricOutcome.SUCCEEDED)
        == 0
    )
    assert_single_restore_failed_event(registry_calls, ErrorCode.CANONICAL_RECOVERY_RESTORE_FAILED)


@pytest.mark.asyncio
async def test_success_emits_restore_succeeded_event_with_no_keys_or_hashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = build_restore_harness(tmp_path, object_count=2)
    registry_calls = install_event_spy(monkeypatch)

    result = await harness.service.restore_empty(
        build_restore_command(),
        read_service=harness.read_service,
        restore_target=harness.restore_target,
    )

    (record,) = harness.metrics.backup_records()
    assert record.operation is RecoveryOperation.RESTORE
    assert record.outcome is RecoveryMetricOutcome.SUCCEEDED
    assert record.object_count == 2
    assert record.byte_total == sum(entry.size_bytes for entry in harness.manifest.objects)
    assert len(registry_calls) == 1
    event_name, fields = registry_calls[0]
    assert event_name == EventName.CANONICAL_RESTORE_SUCCEEDED
    assert fields["operation"] is RecoveryOperation.RESTORE
    assert fields["outcome"] is RecoveryMetricOutcome.SUCCEEDED
    assert fields["bundle_id"] == _BUNDLE_ID
    assert fields["object_count"] == result.object_count
    assert fields["byte_total"] == sum(entry.size_bytes for entry in harness.manifest.objects)
    duration_ms = fields["duration_ms"]
    assert isinstance(duration_ms, int)
    assert duration_ms >= 0
    field_names = {field.name for field in dataclasses.fields(RestoreEmptyResult)}
    assert field_names == {"bundle_id", "completed_at", "table_counts", "object_count"}
    result_text = repr(result) + repr(fields)
    assert _DUMP_SHA256 not in result_text
    for expected, _ in harness.fixtures:
        assert expected.content_digest.hexadecimal not in result_text
    assert "objects/sha256" not in result_text
    assert harness.object_store.deleted_keys == []
