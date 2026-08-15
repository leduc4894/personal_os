"""Recovery service offline bundle verification (spec 10.5-10.8).

The fakes prove the binding flow: the environment gate fires before any port
call; verification touches only the bundle store's offline verifier (never a
snapshot, dump process or object-storage port); the returned result carries
only safe counts and identifiers — never a path, key or hash; and the
registered verified/failed events plus the closed verify metrics are recorded.
"""

from __future__ import annotations

import dataclasses
import hashlib
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

import pytest

from personal_os.diagnostics.events import EventName
from personal_os.error_contracts.codes import ErrorCode
from personal_os.object_storage import ExpectedObject
from personal_os.recovery import service as service_module
from personal_os.recovery.contracts import (
    CANONICAL_COUNT_TABLES,
    MANIFEST_CONTRACT,
    POSTGRESQL_SCHEMA_REVISION,
    POSTGRESQL_SERVER_VERSION,
    InMemoryCanonicalBackupMetrics,
    ManifestDumpEntry,
    ManifestObjectEntry,
    RecoveryBundleInvalidReason,
    RecoveryEnvironment,
    RecoveryError,
    RecoveryManifest,
    RecoveryMetricOutcome,
    RecoveryOperation,
)
from personal_os.recovery.service import (
    BundleVerificationResult,
    RecoveryService,
    VerifyBundleCommand,
)

_MANIFEST_CREATED_AT: datetime = datetime(2026, 8, 15, 12, 0, 5, tzinfo=UTC)
_DUMP_SHA256: str = hashlib.sha256(b"fake pg_dump archive").hexdigest()
_BUNDLE_ID: UUID = UUID("018f5b7d-21c0-7c2e-9a4f-3b6d8e5a7c91")


class RefusingSnapshotStore:
    """Snapshot-store stand-in proving verification never opens a snapshot."""

    def open_quiesced_snapshot(self, now: datetime) -> object:
        raise AssertionError("verification must never open a snapshot")


class RefusingDumpProcess:
    """Dump-process stand-in proving verification never spawns a subprocess."""

    async def create_dump(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("verification must never dump")

    async def restore_dump(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("verification must never restore")


class RefusingObjectStore:
    """Object-store stand-in proving verification never touches object storage."""

    async def resolve_verified_object(self, expected: ExpectedObject) -> object:
        raise AssertionError("verification must never resolve objects")

    async def store_stream(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("verification must never store objects")

    async def verify_existing_object(self, expected: ExpectedObject) -> object:
        raise AssertionError("verification must never verify objects")

    def open_verified_reader(self, expected: ExpectedObject) -> object:
        raise AssertionError("verification must never read objects")


class OfflineVerifyingBundleStore:
    """Bundle-store fake returning one fixed manifest or a scripted failure."""

    def __init__(self, manifest: RecoveryManifest, error: RecoveryError | None) -> None:
        self._manifest = manifest
        self._error = error
        self.verify_calls: list[UUID] = []

    def verify_offline(self, bundle_id: UUID) -> RecoveryManifest:
        self.verify_calls.append(bundle_id)
        if self._error is not None:
            raise self._error
        return self._manifest

    def open_verified(self, bundle_id: UUID) -> object:
        raise AssertionError("offline verification must not open bundle handles")

    def create_staging(self, bundle_id: UUID) -> object:
        raise AssertionError("verification must never stage bundles")

    def bundle_exists(self, bundle_id: UUID) -> bool:
        raise AssertionError("verification must never probe bundle existence")


def build_counts() -> dict[str, int]:
    return {table: index + 1 for index, table in enumerate(CANONICAL_COUNT_TABLES)}


def build_object_entry(index: int) -> tuple[ManifestObjectEntry, bytes]:
    payload = f"canonical recovery object {index}".encode()
    digest = hashlib.sha256(payload).hexdigest()
    media_type = "text/markdown" if index % 2 == 0 else "application/json"
    entry = ManifestObjectEntry(
        content_sha256=digest,
        object_key=f"objects/sha256/{digest[0:2]}/{digest[2:4]}/{digest}",
        size_bytes=len(payload),
        media_type=media_type,
        relative_path=f"objects/sha256/{digest[0:2]}/{digest[2:4]}/{digest}",
    )
    return entry, payload


def build_manifest(object_count: int) -> tuple[RecoveryManifest, tuple[bytes, ...]]:
    entries_and_payloads = [build_object_entry(index) for index in range(object_count)]
    entries = tuple(
        sorted((entry for entry, _ in entries_and_payloads), key=lambda entry: entry.content_sha256)
    )
    payloads = tuple(payload for _, payload in entries_and_payloads)
    return (
        RecoveryManifest(
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
        ),
        payloads,
    )


def build_service(
    manifest: RecoveryManifest, error: RecoveryError | None = None
) -> tuple[RecoveryService, OfflineVerifyingBundleStore, InMemoryCanonicalBackupMetrics]:
    bundle_store = OfflineVerifyingBundleStore(manifest, error)
    metrics = InMemoryCanonicalBackupMetrics()
    service = RecoveryService(
        snapshot_store=RefusingSnapshotStore(),
        bundle_store=bundle_store,
        dump_process=RefusingDumpProcess(),
        object_store=RefusingObjectStore(),
        metrics=metrics,
        clock=lambda: _MANIFEST_CREATED_AT,
    )
    return service, bundle_store, metrics


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


def build_command(
    environment: RecoveryEnvironment = RecoveryEnvironment.TEST,
) -> VerifyBundleCommand:
    return VerifyBundleCommand(environment=environment, bundle_id=_BUNDLE_ID)


@pytest.mark.asyncio
async def test_verification_makes_no_postgresql_r2_or_temporal_call() -> None:
    manifest, _ = build_manifest(object_count=3)
    service, bundle_store, metrics = build_service(manifest)

    result = await service.verify_bundle(build_command())

    assert isinstance(result, BundleVerificationResult)
    assert bundle_store.verify_calls == [_BUNDLE_ID]
    # The only interaction with any port is the offline bundle verification.
    assert metrics.backup_count(RecoveryOperation.VERIFY, RecoveryMetricOutcome.SUCCEEDED) == 1


@pytest.mark.asyncio
async def test_verification_returns_only_safe_counts() -> None:
    manifest, payloads = build_manifest(object_count=3)
    service, _, _ = build_service(manifest)

    result = await service.verify_bundle(build_command())

    field_names = {field.name for field in dataclasses.fields(BundleVerificationResult)}
    assert field_names == {"bundle_id", "contract", "object_count", "byte_total", "table_counts"}
    assert result.bundle_id == _BUNDLE_ID
    assert result.contract == MANIFEST_CONTRACT
    assert result.object_count == 3
    assert result.byte_total == sum(len(payload) for payload in payloads)
    assert result.table_counts == build_counts()
    # No dump hash, object digest, key or path fragment ever reaches the result.
    result_text = repr(result)
    assert _DUMP_SHA256 not in result_text
    for payload in payloads:
        assert hashlib.sha256(payload).hexdigest() not in result_text
    assert "objects/sha256" not in result_text


@pytest.mark.asyncio
async def test_invalid_bundle_raises_bundle_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, _ = build_manifest(object_count=1)
    error = RecoveryError(
        ErrorCode.CANONICAL_RECOVERY_BUNDLE_INVALID,
        safe_details={"reason": RecoveryBundleInvalidReason.CHECKSUM_MISMATCH},
    )
    service, _, metrics = build_service(manifest, error)
    registry_calls = install_event_spy(monkeypatch)

    with pytest.raises(RecoveryError) as excinfo:
        await service.verify_bundle(build_command())

    failure = excinfo.value
    assert failure.error_code == ErrorCode.CANONICAL_RECOVERY_BUNDLE_INVALID
    assert dict(failure.safe_details) == {"reason": RecoveryBundleInvalidReason.CHECKSUM_MISMATCH}
    assert metrics.backup_count(RecoveryOperation.VERIFY, RecoveryMetricOutcome.FAILED) == 1
    assert len(registry_calls) == 1
    event_name, fields = registry_calls[0]
    assert event_name == EventName.CANONICAL_BACKUP_FAILED
    assert fields["operation"] is RecoveryOperation.VERIFY
    assert fields["outcome"] is RecoveryMetricOutcome.FAILED
    assert fields["error_code"] == ErrorCode.CANONICAL_RECOVERY_BUNDLE_INVALID
    assert fields["bundle_id"] == _BUNDLE_ID


@pytest.mark.asyncio
async def test_verification_emits_verified_event_and_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, payloads = build_manifest(object_count=2)
    service, _, metrics = build_service(manifest)
    registry_calls = install_event_spy(monkeypatch)

    result = await service.verify_bundle(build_command())

    (record,) = metrics.backup_records()
    assert record.operation is RecoveryOperation.VERIFY
    assert record.outcome is RecoveryMetricOutcome.SUCCEEDED
    assert record.object_count == 2
    assert record.byte_total == sum(len(payload) for payload in payloads)
    assert 0.0 <= record.duration_seconds < 60.0
    assert len(registry_calls) == 1
    event_name, fields = registry_calls[0]
    assert event_name == EventName.CANONICAL_BACKUP_VERIFIED
    assert fields["operation"] is RecoveryOperation.VERIFY
    assert fields["outcome"] is RecoveryMetricOutcome.SUCCEEDED
    assert fields["bundle_id"] == _BUNDLE_ID
    assert fields["object_count"] == result.object_count
    assert fields["byte_total"] == result.byte_total
    duration_ms = fields["duration_ms"]
    assert isinstance(duration_ms, int)
    assert duration_ms >= 0


@pytest.mark.asyncio
async def test_environment_refused_for_verification_before_any_port_call() -> None:
    manifest, _ = build_manifest(object_count=1)
    service, bundle_store, metrics = build_service(manifest)

    command = VerifyBundleCommand(
        environment=cast(RecoveryEnvironment, "production"), bundle_id=_BUNDLE_ID
    )
    with pytest.raises(RecoveryError) as excinfo:
        await service.verify_bundle(command)

    error = excinfo.value
    assert error.error_code == ErrorCode.CANONICAL_RECOVERY_ENVIRONMENT_REFUSED
    assert dict(error.safe_details) == {"operation": RecoveryOperation.VERIFY}
    assert error.is_retryable is False
    assert bundle_store.verify_calls == []
    assert metrics.backup_records() == []
