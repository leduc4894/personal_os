"""Recovery service: backup creation, verification and empty-target restore.

:class:`RecoveryService.create_backup` composes the quiesced snapshot store,
the bundle store, the ``pg_dump`` process boundary and the verified object
reader exactly as design spec 9.1-9.3 binds it: the environment gate fires
before any client, connection or path is opened; the schema head and bundle
identity are re-checked inside the snapshot; object copies run at most four
concurrently; finalization happens while the snapshot transaction is still
open; and every failure or cancellation path abandons staging, closes readers
and re-raises. The snapshot token flows only into ``create_dump`` — never into
a manifest, event, metric or error detail.

:class:`RecoveryService.verify_bundle` performs one offline bundle
verification (spec 10) touching no PostgreSQL, R2 or Temporal port, and
:class:`RecoveryService.restore_empty` restores an empty target (spec 11):
gates fire before any I/O, R2 objects are restored before the single-
transaction ``pg_restore``, and the restored graph plus a canonical read are
re-verified before the safe receipt. Events carry only safe scalars, are
always built and registry-validated, and are delivered to the optional
composition-provided diagnostics sink; metrics carry only the closed
operation/outcome enums; no path, key, hash, digest or raw content is ever
disclosed.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
import uuid
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Protocol, runtime_checkable
from uuid import UUID

from personal_os.diagnostics.context import DiagnosticContext, create_diagnostic_context
from personal_os.diagnostics.events import (
    DiagnosticEventSink,
    EventName,
    RejectedDiagnosticPayload,
    build_registered_event,
)
from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import ApplicationError, InternalApplicationError
from personal_os.object_storage import (
    CanonicalMediaType,
    CanonicalObjectStore,
    ContentDigest,
    ExpectedObject,
    VerifiedObjectReceipt,
    derive_canonical_object_key,
)
from personal_os.recovery.contracts import (
    POSTGRESQL_SCHEMA_REVISION,
    POSTGRESQL_SERVER_VERSION,
    CanonicalBackupMetrics,
    ManifestDumpEntry,
    ManifestObjectEntry,
    RecoveryComponent,
    RecoveryConfigurationReason,
    RecoveryDependency,
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
    VerifiedRecoveryBundle,
)
from personal_os.sources.reading import ReadCurrentSourceCommand

__all__ = [
    "BACKUP_OBJECT_READ_CONCURRENCY",
    "POSTGRESQL_DUMP_TIMEOUT_SECONDS",
    "POSTGRESQL_RESTORE_TIMEOUT_SECONDS",
    "RECOVERY_COMMAND_TIMEOUT_SECONDS",
    "RESTORE_OBJECT_WRITE_CONCURRENCY",
    "AcceptanceSmokeProbe",
    "BackupCreateCommand",
    "BackupCreationResult",
    "BufferedObjectWriter",
    "BundleVerificationResult",
    "CanonicalSourceBytesReader",
    "PostgresqlRestoreTarget",
    "RecoveryService",
    "RestoreEmptyCommand",
    "RestoreEmptyResult",
    "VerifyBundleCommand",
    "canonical_backup_created_event_fields",
    "canonical_backup_failed_event_fields",
    "canonical_backup_verified_event_fields",
    "canonical_restore_failed_event_fields",
    "canonical_restore_succeeded_event_fields",
]

#: Peak number of concurrent verified object reads during one backup (spec 9.2).
BACKUP_OBJECT_READ_CONCURRENCY: Final[int] = 4

#: Peak number of concurrent conditional object writes during one restore (spec 11.2).
RESTORE_OBJECT_WRITE_CONCURRENCY: Final[int] = 4

#: Whole-command bound applied by the composition layer (spec 9.2, Task 12).
RECOVERY_COMMAND_TIMEOUT_SECONDS: Final[float] = 30 * 60.0

#: The ``pg_dump`` subprocess bound (spec 9.2: ten minutes).
POSTGRESQL_DUMP_TIMEOUT_SECONDS: Final[float] = 600.0

#: The ``pg_restore`` subprocess bound (spec 11.2: ten minutes).
POSTGRESQL_RESTORE_TIMEOUT_SECONDS: Final[float] = 600.0

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


@dataclass(frozen=True, slots=True)
class VerifyBundleCommand:
    """One offline bundle verification request."""

    environment: RecoveryEnvironment
    bundle_id: UUID


@dataclass(frozen=True, slots=True)
class BundleVerificationResult:
    """Observed outcome of one complete offline bundle verification.

    Carries only safe scalars: the verified bundle id, the supported manifest
    contract token and bounded counts. No path, object key, hash or digest is
    ever disclosed (spec 10.8).
    """

    bundle_id: UUID
    contract: str
    object_count: int
    byte_total: int
    table_counts: Mapping[str, int]


@dataclass(frozen=True, slots=True)
class AcceptanceSmokeProbe:
    """The acceptance source whose bytes the restored graph must serve exactly."""

    workspace_id: UUID
    source_id: UUID
    expected_sha256: str
    expected_size_bytes: int
    expected_media_type: CanonicalMediaType


@dataclass(frozen=True, slots=True)
class RestoreEmptyCommand:
    """One empty-target restore request for a verified bundle (spec 11.1).

    ``target_confirmation`` must equal ``target.database`` exactly before any
    I/O beyond offline verification happens.
    """

    environment: RecoveryEnvironment
    bundle_id: UUID
    target: PostgresqlConnectionTarget
    target_confirmation: str
    acceptance_probe: AcceptanceSmokeProbe | None


@dataclass(frozen=True, slots=True)
class RestoreEmptyResult:
    """Observed outcome of one completed empty-target restore.

    Carries only safe scalars: the restored bundle id, the completion moment
    and bounded counts. No path, object key, hash or digest is ever disclosed
    (spec 11.3).
    """

    bundle_id: UUID
    completed_at: datetime
    table_counts: Mapping[str, int]
    object_count: int


@runtime_checkable
class PostgresqlRestoreTarget(Protocol):
    """Narrow restore-target probe surface consumed by the restore flow.

    Mirrors the async probe protocol of the PostgreSQL restore-target adapter
    without importing the provider package, so the core service stays
    provider-neutral (spec 11.1, 11.3).
    """

    async def is_application_empty(self) -> bool: ...

    async def server_version(self) -> str: ...

    async def read_schema_head(self) -> str | None: ...

    async def read_canonical_counts(self, table_names: tuple[str, ...]) -> Mapping[str, int]: ...

    async def read_current_pointer_resolution(self) -> int: ...


@runtime_checkable
class CanonicalSourceBytesReader(Protocol):
    """Narrow canonical-read surface consumed by the acceptance smoke probe."""

    async def read_current_source_bytes(
        self, command: ReadCurrentSourceCommand, diagnostic_context: DiagnosticContext
    ) -> bytes: ...


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
    object verification or transaction behavior. Registered outcome events are
    always built and registry-validated and are delivered to the optional
    composition-provided diagnostics sink when one is bound.
    """

    snapshot_store: CanonicalBackupSnapshotStore
    bundle_store: RecoveryBundleStore
    dump_process: PostgresqlDumpProcess
    object_store: CanonicalObjectStore
    metrics: CanonicalBackupMetrics
    clock: Callable[[], datetime]
    diagnostics: DiagnosticEventSink | None = None

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
            self._emit_registered_event(
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
        self._emit_registered_event(
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

    async def verify_bundle(self, command: VerifyBundleCommand) -> BundleVerificationResult:
        """Verify one bundle offline, touching no other port (spec 10).

        The environment gate fires before the bundle store is read; a failed
        verification records the closed verify metric, builds and validates
        the registered backup-failed event (delivering it when a diagnostics
        sink is bound), then re-raises the typed bundle-invalid error.
        """

        if command.environment not in _ALLOWED_ENVIRONMENTS:
            # Before any path is opened: no metric or event is recorded for a
            # refused gate (spec 9.1).
            raise RecoveryError(
                ErrorCode.CANONICAL_RECOVERY_ENVIRONMENT_REFUSED,
                safe_details={"operation": RecoveryOperation.VERIFY},
            )
        started = time.monotonic()
        try:
            manifest = self.bundle_store.verify_offline(command.bundle_id)
        except ApplicationError as error:
            duration_seconds = max(time.monotonic() - started, 0.0)
            self.metrics.record_backup(
                operation=RecoveryOperation.VERIFY,
                outcome=RecoveryMetricOutcome.FAILED,
                duration_seconds=duration_seconds,
                object_count=0,
                byte_total=0,
            )
            self._emit_registered_event(
                *_recovery_failed_event_fields(
                    EventName.CANONICAL_BACKUP_FAILED,
                    RecoveryOperation.VERIFY,
                    command.bundle_id,
                    error,
                    _duration_ms(duration_seconds),
                )
            )
            raise
        object_count = len(manifest.objects)
        byte_total = sum(entry.size_bytes for entry in manifest.objects)
        duration_seconds = max(time.monotonic() - started, 0.0)
        self.metrics.record_backup(
            operation=RecoveryOperation.VERIFY,
            outcome=RecoveryMetricOutcome.SUCCEEDED,
            duration_seconds=duration_seconds,
            object_count=object_count,
            byte_total=byte_total,
        )
        self._emit_registered_event(
            *canonical_backup_verified_event_fields(
                command.bundle_id, object_count, byte_total, _duration_ms(duration_seconds)
            )
        )
        return BundleVerificationResult(
            bundle_id=command.bundle_id,
            contract=manifest.contract,
            object_count=object_count,
            byte_total=byte_total,
            table_counts=dict(manifest.canonical_counts),
        )

    async def restore_empty(
        self,
        command: RestoreEmptyCommand,
        *,
        read_service: CanonicalSourceBytesReader,
        restore_target: PostgresqlRestoreTarget,
        diagnostic_context: DiagnosticContext | None = None,
    ) -> RestoreEmptyResult:
        """Restore one verified bundle into an admitted empty target (spec 11).

        Gates fire before any I/O beyond offline verification; R2 objects are
        restored and verified before the single-transaction ``pg_restore``;
        the restored graph and the acceptance smoke read are re-verified
        before the safe receipt is returned. A later failure never deletes
        already restored objects: they stay as safe unreferenced CAS bytes.
        """

        if command.environment not in _ALLOWED_ENVIRONMENTS:
            raise RecoveryError(
                ErrorCode.CANONICAL_RECOVERY_ENVIRONMENT_REFUSED,
                safe_details={"operation": RecoveryOperation.RESTORE},
            )
        if command.target_confirmation != command.target.database:
            raise RecoveryError(
                ErrorCode.CANONICAL_RECOVERY_ADMISSION_REFUSED,
                safe_details={"operation": RecoveryOperation.RESTORE},
            )
        started = time.monotonic()
        try:
            return await self._restore_within_gates(
                command, read_service, restore_target, diagnostic_context, started
            )
        except ApplicationError as error:
            duration_seconds = max(time.monotonic() - started, 0.0)
            self.metrics.record_backup(
                operation=RecoveryOperation.RESTORE,
                outcome=RecoveryMetricOutcome.FAILED,
                duration_seconds=duration_seconds,
                object_count=0,
                byte_total=0,
            )
            self._emit_registered_event(
                *canonical_restore_failed_event_fields(
                    command.bundle_id, error, _duration_ms(duration_seconds)
                )
            )
            raise

    async def _restore_within_gates(
        self,
        command: RestoreEmptyCommand,
        read_service: CanonicalSourceBytesReader,
        restore_target: PostgresqlRestoreTarget,
        diagnostic_context: DiagnosticContext | None,
        started: float,
    ) -> RestoreEmptyResult:
        async with self.bundle_store.open_verified(command.bundle_id) as bundle:
            manifest = bundle.manifest
            await self._admit_restore_target(restore_target)
            await self._restore_object_set(bundle, manifest)
            receipt = await self.dump_process.restore_dump(
                bundle.dump_path,
                command.target,
                timeout_seconds=POSTGRESQL_RESTORE_TIMEOUT_SECONDS,
            )
            await self._verify_restored_graph(manifest, restore_target)
            if command.acceptance_probe is not None:
                await self._run_acceptance_smoke(
                    command.acceptance_probe, read_service, diagnostic_context
                )
        duration_seconds = max(time.monotonic() - started, 0.0)
        object_count = len(manifest.objects)
        byte_total = sum(entry.size_bytes for entry in manifest.objects)
        self.metrics.record_backup(
            operation=RecoveryOperation.RESTORE,
            outcome=RecoveryMetricOutcome.SUCCEEDED,
            duration_seconds=duration_seconds,
            object_count=object_count,
            byte_total=byte_total,
        )
        self._emit_registered_event(
            *canonical_restore_succeeded_event_fields(
                command.bundle_id, object_count, byte_total, _duration_ms(duration_seconds)
            )
        )
        return RestoreEmptyResult(
            bundle_id=command.bundle_id,
            completed_at=receipt.completed_at,
            table_counts=dict(manifest.canonical_counts),
            object_count=object_count,
        )

    async def _admit_restore_target(self, restore_target: PostgresqlRestoreTarget) -> None:
        """Admit only an empty, correctly versioned target (spec 11.1)."""

        if not await restore_target.is_application_empty():
            raise RecoveryError(ErrorCode.CANONICAL_RECOVERY_TARGET_NOT_EMPTY)
        if await restore_target.server_version() != POSTGRESQL_SERVER_VERSION:
            raise RecoveryError(
                ErrorCode.CANONICAL_RECOVERY_DEPENDENCY_UNAVAILABLE,
                safe_details={"dependency": RecoveryDependency.POSTGRESQL},
            )
        if await restore_target.read_schema_head() is not None:
            # The dump brings the baseline itself; any pre-existing head means
            # the target is not empty.
            raise RecoveryError(ErrorCode.CANONICAL_RECOVERY_TARGET_NOT_EMPTY)

    async def _restore_object_set(
        self, bundle: VerifiedRecoveryBundle, manifest: RecoveryManifest
    ) -> None:
        """Restore and verify every manifest object into canonical storage."""

        write_limiter = asyncio.Semaphore(RESTORE_OBJECT_WRITE_CONCURRENCY)

        async def restore_one(entry: ManifestObjectEntry) -> None:
            async with write_limiter:
                await self._restore_single_object(bundle, entry)

        # The task group cancels sibling restores on the first failure; the
        # first typed application error is surfaced so the failure path keeps
        # recording the registered event and metric. Restored objects are
        # never overwritten, deleted or compensated (spec 11.2).
        try:
            async with asyncio.TaskGroup() as task_group:
                for entry in manifest.objects:
                    task_group.create_task(restore_one(entry))
        except BaseExceptionGroup as task_failures:
            typed_failures = [
                failure
                for failure in task_failures.exceptions
                if isinstance(failure, ApplicationError)
            ]
            if typed_failures:
                raise typed_failures[0] from None
            raise

    async def _restore_single_object(
        self, bundle: VerifiedRecoveryBundle, entry: ManifestObjectEntry
    ) -> None:
        expected = _expected_object_from_entry(entry)
        existing_receipt = await self.object_store.resolve_verified_object(expected)
        if existing_receipt is None:
            receipt = await self.object_store.store_stream(
                self._stream_bundle_object(bundle, entry),
                entry.size_bytes,
                entry.media_type,
                claimed_sha256=entry.content_sha256,
            )
        else:
            receipt = await self.object_store.verify_existing_object(expected)
        _require_matching_receipt(receipt, expected)

    async def _stream_bundle_object(
        self, bundle: VerifiedRecoveryBundle, entry: ManifestObjectEntry
    ) -> AsyncIterator[bytes]:
        """Yield the verified bundle sidecar in bounded 1 MiB chunks."""

        with bundle.object_path(entry.content_sha256).open(mode="rb") as object_file:
            while chunk := object_file.read(OBJECT_COPY_CHUNK_SIZE_BYTES):
                yield chunk

    async def _verify_restored_graph(
        self, manifest: RecoveryManifest, restore_target: PostgresqlRestoreTarget
    ) -> None:
        """Re-verify the restored schema, counts, pointers and objects (spec 11.3)."""

        if await restore_target.read_schema_head() != manifest.postgresql_schema_revision:
            raise RecoveryError(
                ErrorCode.CANONICAL_RECOVERY_RESTORE_FAILED,
                safe_details={"component": RecoveryComponent.CANONICAL_GRAPH},
            )
        count_tables = tuple(manifest.canonical_counts)
        if dict(await restore_target.read_canonical_counts(count_tables)) != dict(
            manifest.canonical_counts
        ):
            raise RecoveryError(
                ErrorCode.CANONICAL_RECOVERY_RESTORE_FAILED,
                safe_details={"component": RecoveryComponent.CANONICAL_GRAPH},
            )
        if await restore_target.read_current_pointer_resolution() != 0:
            raise RecoveryError(
                ErrorCode.CANONICAL_RECOVERY_RESTORE_FAILED,
                safe_details={"component": RecoveryComponent.CANONICAL_GRAPH},
            )
        for entry in manifest.objects:
            # Full verification is requested from R2 again after pg_restore;
            # receipts from the restore phase are never reused.
            expected = _expected_object_from_entry(entry)
            receipt = await self.object_store.verify_existing_object(expected)
            _require_matching_receipt(receipt, expected)

    async def _run_acceptance_smoke(
        self,
        probe: AcceptanceSmokeProbe,
        read_service: CanonicalSourceBytesReader,
        diagnostic_context: DiagnosticContext | None,
    ) -> None:
        """Require the acceptance source to serve the exact restored bytes."""

        context = (
            diagnostic_context
            if diagnostic_context is not None
            else create_diagnostic_context().context
        )
        payload = await read_service.read_current_source_bytes(
            ReadCurrentSourceCommand(workspace_id=probe.workspace_id, source_id=probe.source_id),
            context,
        )
        if (
            hashlib.sha256(payload).hexdigest() != probe.expected_sha256
            or len(payload) != probe.expected_size_bytes
        ):
            raise RecoveryError(
                ErrorCode.CANONICAL_RECOVERY_RESTORE_FAILED,
                safe_details={"component": RecoveryComponent.CANONICAL_READ},
            )

    def _emit_registered_event(self, event_name: EventName, fields: dict[str, object]) -> None:
        """Validate the registered event; deliver it when a sink is bound.

        Without a composition-provided sink the validated payload is discarded
        (build-and-validate only); a rejected payload is registry drift and
        raises regardless of sink presence.
        """
        built = build_registered_event(event_name, fields)
        if isinstance(built, RejectedDiagnosticPayload):
            # A rejected payload here means registry drift, a programming error
            # rather than untrusted input; raise so it also surfaces in optimized
            # (python -O) runs instead of vanishing with assert.
            raise InternalApplicationError(ErrorCode.INTERNAL_ERROR)
        if self.diagnostics is not None:
            self.diagnostics.emit(event_name, dict(fields))


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


def _expected_object_from_entry(entry: ManifestObjectEntry) -> ExpectedObject:
    """The verification claim a manifest object entry describes."""

    return ExpectedObject(
        content_digest=ContentDigest.parse(entry.content_sha256),
        size_bytes=entry.size_bytes,
        media_type=CanonicalMediaType.parse(entry.media_type),
    )


def _require_matching_receipt(receipt: VerifiedObjectReceipt, expected: ExpectedObject) -> None:
    """Fail closed when an object-storage receipt disagrees with the claim."""

    if (
        receipt.content_digest != expected.content_digest
        or receipt.size_bytes != expected.size_bytes
        or receipt.media_type != expected.media_type
    ):
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


def canonical_backup_verified_event_fields(
    bundle_id: UUID, object_count: int, byte_total: int, duration_ms: int
) -> tuple[EventName, dict[str, object]]:
    """The registered backup-verified event and its safe field payload.

    Carries only registry-safe scalars — the closed enums, the bundle id and
    bounded counts — so no path, key, hash or token can ever reach a
    diagnostic line.
    """

    return EventName.CANONICAL_BACKUP_VERIFIED, {
        "operation": RecoveryOperation.VERIFY,
        "outcome": RecoveryMetricOutcome.SUCCEEDED,
        "duration_ms": duration_ms,
        "bundle_id": bundle_id,
        "object_count": object_count,
        "byte_total": byte_total,
    }


def canonical_restore_succeeded_event_fields(
    bundle_id: UUID, object_count: int, byte_total: int, duration_ms: int
) -> tuple[EventName, dict[str, object]]:
    """The registered restore-succeeded event and its safe field payload."""

    return EventName.CANONICAL_RESTORE_SUCCEEDED, {
        "operation": RecoveryOperation.RESTORE,
        "outcome": RecoveryMetricOutcome.SUCCEEDED,
        "duration_ms": duration_ms,
        "bundle_id": bundle_id,
        "object_count": object_count,
        "byte_total": byte_total,
    }


def canonical_restore_failed_event_fields(
    bundle_id: UUID, error: ApplicationError, duration_ms: int
) -> tuple[EventName, dict[str, object]]:
    """The registered restore-failed event and its safe field payload."""

    return _recovery_failed_event_fields(
        EventName.CANONICAL_RESTORE_FAILED,
        RecoveryOperation.RESTORE,
        bundle_id,
        error,
        duration_ms,
    )


def canonical_backup_failed_event_fields(
    error: ApplicationError, duration_ms: int
) -> tuple[EventName, dict[str, object]]:
    """The registered backup-failed event and its safe field payload.

    Carries only the closed operation/outcome enums, a bounded duration and
    the closed error-code enum; the original exception, its message and any
    chained provider cause never enter the field set.
    """

    return _recovery_failed_event_fields(
        EventName.CANONICAL_BACKUP_FAILED,
        RecoveryOperation.CREATE,
        None,
        error,
        duration_ms,
    )


def _recovery_failed_event_fields(
    event_name: EventName,
    operation: RecoveryOperation,
    bundle_id: UUID | None,
    error: ApplicationError,
    duration_ms: int,
) -> tuple[EventName, dict[str, object]]:
    """A registered recovery-failure event and its safe field payload."""

    fields: dict[str, object] = {
        "operation": operation,
        "outcome": RecoveryMetricOutcome.FAILED,
        "duration_ms": duration_ms,
    }
    if bundle_id is not None:
        fields["bundle_id"] = bundle_id
    fields["error_code"] = error.error_code
    return event_name, fields


def _duration_ms(duration_seconds: float) -> int:
    return max(0, int(duration_seconds * 1000))
