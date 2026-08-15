"""Recovery service: consistent backup creation orchestrating the ports.

:class:`RecoveryService.create_backup` composes the quiesced snapshot store,
the bundle store, the ``pg_dump`` process boundary and the verified object
reader exactly as design spec 9.1-9.3 binds it: the environment gate fires
before any client, connection or path is opened; the schema head and bundle
identity are re-checked inside the snapshot; object copies run at most four
concurrently; finalization happens while the snapshot transaction is still
open; and every failure or cancellation path abandons staging, closes readers
and re-raises. The snapshot token flows only into ``create_dump`` — never into
a manifest, event, metric or error detail. Events carry only safe scalars and
metrics only the closed operation/outcome enums; no path, key, hash, digest or
raw content is ever disclosed.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Protocol, runtime_checkable
from uuid import UUID

from personal_os.diagnostics.events import (
    EventName,
    RejectedDiagnosticPayload,
    build_registered_event,
)
from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import ApplicationError, InternalApplicationError
from personal_os.object_storage import (
    CanonicalObjectStore,
    ExpectedObject,
    derive_canonical_object_key,
)
from personal_os.recovery.contracts import (
    POSTGRESQL_SCHEMA_REVISION,
    CanonicalBackupMetrics,
    ManifestDumpEntry,
    ManifestObjectEntry,
    RecoveryComponent,
    RecoveryConfigurationReason,
    RecoveryEnvironment,
    RecoveryError,
    RecoveryManifest,
    RecoveryMetricOutcome,
    RecoveryOperation,
)
from personal_os.recovery.ports import (
    CanonicalBackupSnapshotStore,
    PostgresqlConnectionTarget,
    PostgresqlDumpProcess,
    RecoveryBundleStore,
    RecoveryBundleWriter,
)

__all__ = [
    "BACKUP_OBJECT_READ_CONCURRENCY",
    "POSTGRESQL_DUMP_TIMEOUT_SECONDS",
    "RECOVERY_COMMAND_TIMEOUT_SECONDS",
    "BackupCreateCommand",
    "BackupCreationResult",
    "BufferedObjectWriter",
    "RecoveryService",
    "canonical_backup_created_event_fields",
    "canonical_backup_failed_event_fields",
]

#: Peak number of concurrent verified object reads during one backup (spec 9.2).
BACKUP_OBJECT_READ_CONCURRENCY: Final[int] = 4

#: Whole-command bound applied by the composition layer (spec 9.2, Task 12).
RECOVERY_COMMAND_TIMEOUT_SECONDS: Final[float] = 30 * 60.0

#: The ``pg_dump`` subprocess bound (spec 9.2: ten minutes).
POSTGRESQL_DUMP_TIMEOUT_SECONDS: Final[float] = 600.0

#: Verified object copies stream in bounded chunks of this size.
OBJECT_COPY_CHUNK_SIZE_BYTES: Final[int] = 1024 * 1024

#: The single manifest-registered dump sidecar relative path.
POSTGRES_DUMP_RELATIVE_PATH: Final[str] = "postgres.dump"

_ALLOWED_ENVIRONMENTS: Final[frozenset[RecoveryEnvironment]] = frozenset(
    {RecoveryEnvironment.LOCAL, RecoveryEnvironment.TEST}
)


@dataclass(frozen=True, slots=True)
class BackupCreateCommand:
    """One consistent backup-creation request for a canonical database."""

    environment: RecoveryEnvironment
    target: PostgresqlConnectionTarget


@dataclass(frozen=True, slots=True)
class BackupCreationResult:
    """Observed outcome of one finalized backup bundle.

    ``byte_total`` counts only the content objects admitted into the bundle,
    matching the manifest totals the offline verifier re-checks (spec 10.8).
    """

    bundle_id: UUID
    object_count: int
    byte_total: int
    duration_seconds: float


@runtime_checkable
class BufferedObjectWriter(Protocol):
    """Optional buffered object-write extension of :class:`RecoveryBundleWriter`.

    The filesystem staging writer offers it, so its exclusive-create, digest
    and fsync behavior is reused instead of being reimplemented here. A
    port-only writer falls back to streamed writes through ``object_path``.
    """

    async def write_object(self, content_sha256: str, object_bytes: bytes) -> None: ...


@dataclass(frozen=True, slots=True)
class RecoveryService:
    """Backup creation, offline verification and empty-target restore (spec 9-11).

    Composes existing production ports; never reimplements publication,
    object verification or transaction behavior.
    """

    snapshot_store: CanonicalBackupSnapshotStore
    bundle_store: RecoveryBundleStore
    dump_process: PostgresqlDumpProcess
    object_store: CanonicalObjectStore
    metrics: CanonicalBackupMetrics
    clock: Callable[[], datetime]

    async def create_backup(self, command: BackupCreateCommand) -> BackupCreationResult:
        """Create one consistent backup bundle under a quiesced snapshot."""

        if command.environment not in _ALLOWED_ENVIRONMENTS:
            # Before any client, connection or path is opened: no metric or
            # event is recorded for a refused gate (spec 9.1).
            raise RecoveryError(
                ErrorCode.CANONICAL_RECOVERY_ENVIRONMENT_REFUSED,
                safe_details={"operation": RecoveryOperation.CREATE},
            )
        started = time.monotonic()
        bundle_id = uuid.uuid7()
        try:
            return await self._create_within_gates(command, bundle_id, started)
        except ApplicationError as error:
            duration_seconds = max(time.monotonic() - started, 0.0)
            self.metrics.record_backup(
                operation=RecoveryOperation.CREATE,
                outcome=RecoveryMetricOutcome.FAILED,
                duration_seconds=duration_seconds,
                object_count=0,
                byte_total=0,
            )
            _emit_registered_event(
                *canonical_backup_failed_event_fields(error, _duration_ms(duration_seconds))
            )
            raise

    async def _create_within_gates(
        self,
        command: BackupCreateCommand,
        bundle_id: UUID,
        started: float,
    ) -> BackupCreationResult:
        async with self.snapshot_store.open_quiesced_snapshot(now=self.clock()) as snapshot:
            if snapshot.schema_head != POSTGRESQL_SCHEMA_REVISION:
                raise RecoveryError(
                    ErrorCode.CANONICAL_RECOVERY_CONFIGURATION_INVALID,
                    safe_details={"reason": RecoveryConfigurationReason.SCHEMA_HEAD_MISMATCH},
                )
            if self.bundle_store.bundle_exists(bundle_id):
                raise RecoveryError(
                    ErrorCode.CANONICAL_RECOVERY_BUNDLE_EXISTS,
                    safe_details={"bundle_id": bundle_id},
                )
            async with self.bundle_store.create_staging(bundle_id) as writer:
                is_finalized = False
                try:
                    dump_receipt = await self.dump_process.create_dump(
                        snapshot_token=snapshot.snapshot_token,
                        output_file=writer.dump_path,
                        target=command.target,
                        timeout_seconds=POSTGRESQL_DUMP_TIMEOUT_SECONDS,
                    )
                    object_entries = await self._copy_referenced_objects(
                        writer, snapshot.referenced_objects
                    )
                    if await self.snapshot_store.observe_pending_writers() != 0:
                        raise RecoveryError(ErrorCode.CANONICAL_RECOVERY_SNAPSHOT_BUSY)
                    manifest = RecoveryManifest(
                        bundle_id=bundle_id,
                        created_at=self.clock(),
                        source_environment=command.environment.value,
                        postgresql_server_version=snapshot.server_version,
                        postgresql_schema_revision=snapshot.schema_head,
                        postgres_dump=ManifestDumpEntry(
                            relative_path=POSTGRES_DUMP_RELATIVE_PATH,
                            size_bytes=dump_receipt.size_bytes,
                            sha256=dump_receipt.sha256,
                        ),
                        canonical_counts=dict(snapshot.table_counts),
                        objects=object_entries,
                    )
                    # Finalize while the snapshot transaction is still open
                    # (spec 9.2 step 9); locks release only after it (step 10).
                    await writer.finalize(manifest)
                    is_finalized = True
                finally:
                    if not is_finalized:
                        await writer.abandon()
        duration_seconds = max(time.monotonic() - started, 0.0)
        object_count = len(object_entries)
        byte_total = sum(entry.size_bytes for entry in object_entries)
        self.metrics.record_backup(
            operation=RecoveryOperation.CREATE,
            outcome=RecoveryMetricOutcome.SUCCEEDED,
            duration_seconds=duration_seconds,
            object_count=object_count,
            byte_total=byte_total,
        )
        _emit_registered_event(
            *canonical_backup_created_event_fields(
                bundle_id, object_count, byte_total, _duration_ms(duration_seconds)
            )
        )
        return BackupCreationResult(
            bundle_id=bundle_id,
            object_count=object_count,
            byte_total=byte_total,
            duration_seconds=duration_seconds,
        )

    async def _copy_referenced_objects(
        self,
        writer: RecoveryBundleWriter,
        referenced_objects: tuple[ExpectedObject, ...],
    ) -> tuple[ManifestObjectEntry, ...]:
        """Copy every referenced object at the bounded concurrency, sorted."""

        read_limiter = asyncio.Semaphore(BACKUP_OBJECT_READ_CONCURRENCY)
        copied_entries: list[ManifestObjectEntry] = []

        async def copy_one(expected: ExpectedObject) -> None:
            copied_entries.append(
                await self._copy_referenced_object(writer, read_limiter, expected)
            )

        # The task group cancels sibling copies on the first failure so no
        # reader stays open or staging write races the abandon path. Copies
        # that fail in the same scheduling round arrive as one group; the
        # first typed application error is surfaced so the failure path keeps
        # recording the registered event and metric. Groups without any typed
        # error (cancellation, programming bugs) propagate unchanged.
        try:
            async with asyncio.TaskGroup() as task_group:
                for expected in referenced_objects:
                    task_group.create_task(copy_one(expected))
        except BaseExceptionGroup as task_failures:
            typed_failures = [
                failure
                for failure in task_failures.exceptions
                if isinstance(failure, ApplicationError)
            ]
            if typed_failures:
                raise typed_failures[0] from None
            raise
        return tuple(sorted(copied_entries, key=lambda entry: entry.content_sha256))

    async def _copy_referenced_object(
        self,
        writer: RecoveryBundleWriter,
        read_limiter: asyncio.Semaphore,
        expected: ExpectedObject,
    ) -> ManifestObjectEntry:
        digest_text = expected.content_digest.hexadecimal
        object_key = derive_canonical_object_key(expected.content_digest).value
        async with read_limiter, self.object_store.open_verified_reader(expected) as reader:
            digest_hasher = hashlib.sha256()
            copied_bytes = 0
            if isinstance(writer, BufferedObjectWriter):
                buffer = bytearray()
                while True:
                    chunk = await reader.read(OBJECT_COPY_CHUNK_SIZE_BYTES)
                    if not chunk:
                        break
                    digest_hasher.update(chunk)
                    buffer.extend(chunk)
                    copied_bytes += len(chunk)
                _require_verified_copy(
                    digest_hasher.hexdigest(),
                    digest_text,
                    copied_bytes,
                    expected.size_bytes,
                )
                await writer.write_object(digest_text, bytes(buffer))
            else:
                with writer.object_path(digest_text).open(mode="xb") as object_file:
                    while True:
                        chunk = await reader.read(OBJECT_COPY_CHUNK_SIZE_BYTES)
                        if not chunk:
                            break
                        digest_hasher.update(chunk)
                        object_file.write(chunk)
                        copied_bytes += len(chunk)
                _require_verified_copy(
                    digest_hasher.hexdigest(),
                    digest_text,
                    copied_bytes,
                    expected.size_bytes,
                )
        return ManifestObjectEntry(
            content_sha256=digest_text,
            object_key=object_key,
            size_bytes=expected.size_bytes,
            media_type=expected.media_type.value,
            relative_path=object_key,
        )


def _require_verified_copy(
    digest_hexadecimal: str,
    digest_text: str,
    copied_bytes: int,
    expected_size_bytes: int,
) -> None:
    """Fail closed when the streamed bytes disagree with the verified claim."""

    if digest_hexadecimal != digest_text or copied_bytes != expected_size_bytes:
        raise RecoveryError(
            ErrorCode.CANONICAL_RECOVERY_INTEGRITY_FAILED,
            safe_details={"component": RecoveryComponent.OBJECT_SET},
        )


def canonical_backup_created_event_fields(
    bundle_id: UUID, object_count: int, byte_total: int, duration_ms: int
) -> tuple[EventName, dict[str, object]]:
    """The registered backup-created event and its safe field payload.

    Carries only registry-safe scalars — the closed enums, the server-assigned
    bundle id and bounded counts — so no path, key, hash or token can ever
    reach a diagnostic line.
    """

    return EventName.CANONICAL_BACKUP_CREATED, {
        "operation": RecoveryOperation.CREATE,
        "outcome": RecoveryMetricOutcome.SUCCEEDED,
        "duration_ms": duration_ms,
        "bundle_id": bundle_id,
        "object_count": object_count,
        "byte_total": byte_total,
    }


def canonical_backup_failed_event_fields(
    error: ApplicationError, duration_ms: int
) -> tuple[EventName, dict[str, object]]:
    """The registered backup-failed event and its safe field payload.

    Carries only the closed operation/outcome enums, a bounded duration and
    the closed error-code enum; the original exception, its message and any
    chained provider cause never enter the field set.
    """

    return EventName.CANONICAL_BACKUP_FAILED, {
        "operation": RecoveryOperation.CREATE,
        "outcome": RecoveryMetricOutcome.FAILED,
        "duration_ms": duration_ms,
        "error_code": error.error_code,
    }


def _duration_ms(duration_seconds: float) -> int:
    return max(0, int(duration_seconds * 1000))


def _emit_registered_event(event_name: EventName, fields: dict[str, object]) -> None:
    built = build_registered_event(event_name, fields)
    if isinstance(built, RejectedDiagnosticPayload):
        # A rejected payload here means registry drift, a programming error
        # rather than untrusted input; raise so it also surfaces in optimized
        # (python -O) runs instead of vanishing with assert.
        raise InternalApplicationError(ErrorCode.INTERNAL_ERROR)
