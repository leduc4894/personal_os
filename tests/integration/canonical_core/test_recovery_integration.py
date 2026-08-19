"""Canonical recovery backup/verify/restore against a disposable PostgreSQL 18.4.

Every case runs against the real migrated baseline with the real adapters: the
quiesced exported-snapshot store, the filesystem bundle store, the bounded
``pg_dump``/``pg_restore`` process adapter and the fake local-filesystem
object store standing in for R2. The cases prove: concurrent DML blocks behind
the quiescing share locks while a second snapshot fails bounded with
SNAPSHOT_BUSY; the dump and the manifest describe one exported snapshot even
when the live database mutates after the backup; offline verification detects
dump, object and manifest mutation; current v2 restores are exact across the
twenty-table graph; a real historical nine-table v1 bundle remains restorable;
a failed single-transaction restore leaves the target database empty; the
restored graph serves the exact canonical bytes through the real read service;
and a failed backup leaves no staging files, locks or client processes behind.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.canonical_core.conftest import (
    CanonicalCoreHarness,
    CanonicalCoreStack,
    LocalFilesystemObjectStore,
    PostgresqlDumpProcessAdapter,
    RestoreTargetContext,
    _compose_exec_psql,
    recovery_environment,
)

from personal_os.error_contracts.codes import ErrorCode
from personal_os.object_storage import CanonicalMediaType, ExpectedObject
from personal_os.object_storage.errors import ObjectStorageError
from personal_os.recovery.bundle import FilesystemRecoveryBundleStore
from personal_os.recovery.contracts import (
    CANONICAL_COUNT_TABLES,
    MANIFEST_CONTRACT_V1,
    POSTGRESQL_SCHEMA_REVISION,
    V1_CANONICAL_COUNT_TABLES,
    InMemoryCanonicalBackupMetrics,
    ManifestDumpEntry,
    RecoveryComponent,
    RecoveryError,
    RecoveryManifest,
)
from personal_os.recovery.ports import PostgresqlConnectionTarget
from personal_os.recovery.service import (
    AcceptanceSmokeProbe,
    BackupCreateCommand,
    RecoveryService,
    RestoreEmptyCommand,
    RestoreEmptyResult,
    VerifyBundleCommand,
)
from personal_os.sources.reading import ReadCurrentSourceCommand
from postgresql_source_store.backup_snapshot import PostgresqlBackupSnapshotStore
from postgresql_source_store.engine import create_source_store_engine, dispose_source_store_engine
from postgresql_source_store.tables import SOURCE_STORE_TABLES, users

pytestmark = pytest.mark.local_stack

_BLOCKED_WRITER_DEADLINE_SECONDS: float = 15.0
_BLOCKED_WRITER_LOCK_TIMEOUT_SECONDS: int = 20
_HISTORICAL_SCHEMA_REVISION: str = "20260813_01"


def _utc_clock() -> Callable[[], datetime]:
    return lambda: datetime.now(UTC)


# --- shared helpers ---------------------------------------------------------------------


async def _blocked_display_rename(engine: AsyncEngine, user_id: UUID) -> None:
    """A concurrent writer that queues behind the quiescing share locks."""
    async with engine.connect() as connection, connection.begin():
        await connection.execute(
            sa.text(f"SET LOCAL lock_timeout = '{_BLOCKED_WRITER_LOCK_TIMEOUT_SECONDS}s'")
        )
        await connection.execute(
            sa.update(users)
            .values(display_name="Blocked Canonical Writer")
            .where(users.c.user_id == user_id)
        )


async def _await_blocked_writer(snapshot_store: PostgresqlBackupSnapshotStore) -> None:
    """Fail the test unless a writer is really queued behind the share locks."""
    deadline = time.monotonic() + _BLOCKED_WRITER_DEADLINE_SECONDS
    while time.monotonic() < deadline:
        if await snapshot_store.observe_pending_writers() > 0:
            return
        await asyncio.sleep(0.1)
    pytest.fail("concurrent DML never queued behind the quiescing share locks")


def _assert_no_client_tool_processes() -> None:
    """No pg_dump/pg_restore child may outlive its bounded invocation."""
    binary_names = (
        ("pg_dump.exe", "pg_restore.exe") if sys.platform == "win32" else ("pg_dump", "pg_restore")
    )
    for binary_name in binary_names:
        if sys.platform == "win32":
            result = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {binary_name}"],
                capture_output=True,
                text=True,
                check=False,
            )
            assert binary_name not in result.stdout, f"leftover client process: {binary_name}"
        else:
            result = subprocess.run(
                ["pgrep", "-x", binary_name],
                capture_output=True,
                text=True,
                check=False,
            )
            assert result.returncode != 0, f"leftover client process: {binary_name}"


async def _table_rows_as_text(engine: AsyncEngine, table_name: str) -> list[tuple[str, ...]]:
    """Every row of one canonical table, stringified and sorted for equality."""
    table = SOURCE_STORE_TABLES[table_name]
    async with engine.connect() as connection:
        rows = (await connection.execute(sa.select(table))).all()
    return sorted(tuple(str(value) for value in row) for row in rows)


async def _restore_bundle(
    recovery_service: RecoveryService,
    restore_target_context: RestoreTargetContext,
    bundle_id: UUID,
    *,
    acceptance_probe: AcceptanceSmokeProbe | None,
) -> RestoreEmptyResult:
    return await recovery_service.restore_empty(
        RestoreEmptyCommand(
            environment=recovery_environment(),
            bundle_id=bundle_id,
            target=restore_target_context.database.connection_target,
            target_confirmation=restore_target_context.database.database_name,
            acceptance_probe=acceptance_probe,
        ),
        read_service=restore_target_context.read_service,
        restore_target=restore_target_context.restore_target,
    )


# --- snapshot busy ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_dml_and_snapshot_busy(
    canonical_core_harness: CanonicalCoreHarness,
) -> None:
    harness = canonical_core_harness
    workspace = await harness.seed_workspace()
    snapshot_store = PostgresqlBackupSnapshotStore(harness.engine)
    clock = _utc_clock()

    async with snapshot_store.open_quiesced_snapshot(now=clock()) as snapshot:
        assert snapshot.schema_head == POSTGRESQL_SCHEMA_REVISION
        assert snapshot.server_version.startswith("18")
        assert dict(snapshot.table_counts)["users"] >= 1

        blocked_writer = asyncio.create_task(
            _blocked_display_rename(harness.engine, workspace.owner_user_id)
        )
        try:
            await _await_blocked_writer(snapshot_store)
            with pytest.raises(RecoveryError) as captured:
                async with snapshot_store.open_quiesced_snapshot(now=clock()):
                    pytest.fail("the second snapshot must never be entered")
        finally:
            blocked_writer.cancel()
            with suppress(asyncio.CancelledError):
                await blocked_writer

    assert captured.value.error_code is ErrorCode.CANONICAL_RECOVERY_SNAPSHOT_BUSY
    assert await snapshot_store.observe_pending_writers() == 0
    async with harness.engine.connect() as connection:
        display_name = (
            await connection.execute(
                sa.select(users.c.display_name).where(users.c.user_id == workspace.owner_user_id)
            )
        ).scalar_one()
    assert display_name == "Canonical Core Owner"


# --- backup / verify / restore ----------------------------------------------------------


@pytest.mark.asyncio
async def test_dump_and_manifest_come_from_same_exported_snapshot(
    canonical_core_harness: CanonicalCoreHarness,
    canonical_core_stack: CanonicalCoreStack,
    recovery_service: RecoveryService,
    restore_target_context: RestoreTargetContext,
) -> None:
    harness = canonical_core_harness
    workspace = await harness.seed_workspace()
    await harness.publish_markdown_source(workspace, b"# Snapshot evidence\n", title="Snapshot")
    # The manifest counts the canonical table set; the harness counts every
    # store table, so the expectation is scoped to the manifest contract.
    counts_at_backup = {
        table: count
        for table, count in (await harness.table_counts()).items()
        if table in CANONICAL_COUNT_TABLES
    }

    backup = await recovery_service.create_backup(
        BackupCreateCommand(
            environment=recovery_environment(), target=canonical_core_stack.main_target
        )
    )
    verified = await recovery_service.verify_bundle(
        VerifyBundleCommand(environment=recovery_environment(), bundle_id=backup.bundle_id)
    )
    assert verified.table_counts == counts_at_backup
    assert verified.object_count == backup.object_count
    assert verified.object_count >= 1

    # Post-backup mutation of the live database must not leak into the bundle.
    await harness.seed_workspace()
    mutated_counts = await harness.table_counts()
    assert mutated_counts["users"] == counts_at_backup["users"] + 1
    assert mutated_counts["workspaces"] == counts_at_backup["workspaces"] + 1

    result = await _restore_bundle(
        recovery_service, restore_target_context, backup.bundle_id, acceptance_probe=None
    )
    assert dict(result.table_counts) == counts_at_backup
    assert dict(await restore_target_context.restore_target.read_canonical_counts()) == (
        counts_at_backup
    )


@pytest.mark.asyncio
async def test_historical_v1_baseline_bundle_restores_with_its_original_shape(
    canonical_core_stack: CanonicalCoreStack,
    dump_process: PostgresqlDumpProcessAdapter,
    recovery_service: RecoveryService,
    restore_target_context: RestoreTargetContext,
    bundle_root: Path,
) -> None:
    """A real baseline dump remains restorable under the immutable v1 contract."""

    database_name = f"knowledge_ci_v1_{uuid.uuid4().hex[:12]}"
    _compose_exec_psql(
        canonical_core_stack.project_name,
        f'CREATE DATABASE "{database_name}" OWNER knowledge_app',
        "historical_database_created",
    )
    environment = dict(canonical_core_stack.environment)
    environment["KNOWLEDGE_DATABASE_NAME"] = database_name
    migration = subprocess.run(
        ["uv", "run", "alembic", "upgrade", _HISTORICAL_SCHEMA_REVISION],
        cwd=str(Path(__file__).resolve().parents[3]),
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=120.0,
    )
    assert migration.returncode == 0, "historical baseline migration failed"

    settings = canonical_core_stack.settings.model_copy(update={"database_name": database_name})
    engine = create_source_store_engine(settings, canonical_core_stack.password)
    target = PostgresqlConnectionTarget(
        host=canonical_core_stack.main_target.host,
        port=canonical_core_stack.main_target.port,
        database=database_name,
        user=canonical_core_stack.main_target.user,
    )
    bundle_id = uuid.uuid7()
    bundle_store = FilesystemRecoveryBundleStore(bundle_root)
    try:
        async with engine.connect() as connection:
            await connection.execution_options(isolation_level="REPEATABLE READ")
            transaction = await connection.begin()
            try:
                await connection.execute(sa.text("SET LOCAL lock_timeout = '15000ms'"))
                await connection.execute(sa.text("SET LOCAL statement_timeout = '15000ms'"))
                for table_name in V1_CANONICAL_COUNT_TABLES:
                    await connection.execute(
                        sa.text(f'LOCK TABLE knowledge."{table_name}" IN SHARE MODE NOWAIT')
                    )
                snapshot_token = str(
                    (await connection.execute(sa.text("SELECT pg_export_snapshot()"))).scalar_one()
                )
                server_version = str(
                    (
                        await connection.execute(
                            sa.text("SELECT split_part(current_setting('server_version'), ' ', 1)")
                        )
                    ).scalar_one()
                )
                counts = {
                    table_name: int(
                        (
                            await connection.execute(
                                sa.select(sa.func.count()).select_from(
                                    SOURCE_STORE_TABLES[table_name]
                                )
                            )
                        ).scalar_one()
                    )
                    for table_name in V1_CANONICAL_COUNT_TABLES
                }
                async with bundle_store.create_staging(bundle_id) as writer:
                    dump_receipt = await dump_process.create_dump(
                        snapshot_token,
                        writer.dump_path,
                        target,
                        timeout_seconds=120.0,
                    )
                    await writer.finalize(
                        RecoveryManifest(
                            bundle_id=bundle_id,
                            created_at=datetime.now(UTC),
                            source_environment=recovery_environment().value,
                            postgresql_server_version=server_version,
                            postgresql_schema_revision=_HISTORICAL_SCHEMA_REVISION,
                            postgres_dump=ManifestDumpEntry(
                                relative_path="postgres.dump",
                                size_bytes=dump_receipt.size_bytes,
                                sha256=dump_receipt.sha256,
                            ),
                            canonical_counts=counts,
                            objects=(),
                            contract=MANIFEST_CONTRACT_V1,
                        )
                    )
            finally:
                await transaction.rollback()

        verified = await recovery_service.verify_bundle(
            VerifyBundleCommand(
                environment=recovery_environment(),
                bundle_id=bundle_id,
            )
        )
        assert set(verified.table_counts) == set(V1_CANONICAL_COUNT_TABLES)

        result = await _restore_bundle(
            recovery_service,
            restore_target_context,
            bundle_id,
            acceptance_probe=None,
        )
        assert dict(result.table_counts) == counts
        assert await restore_target_context.restore_target.read_schema_head() == (
            _HISTORICAL_SCHEMA_REVISION
        )
    finally:
        await dispose_source_store_engine(engine)
        _compose_exec_psql(
            canonical_core_stack.project_name,
            f'DROP DATABASE "{database_name}" WITH (FORCE)',
            "historical_database_dropped",
        )


@pytest.mark.asyncio
async def test_bundle_verify_detects_dump_object_and_manifest_mutation(
    canonical_core_harness: CanonicalCoreHarness,
    canonical_core_stack: CanonicalCoreStack,
    recovery_service: RecoveryService,
    bundle_root: Path,
) -> None:
    harness = canonical_core_harness
    workspace = await harness.seed_workspace()
    await harness.publish_markdown_source(workspace, b"# Mutation target\n", title="Mutation")
    backup = await recovery_service.create_backup(
        BackupCreateCommand(
            environment=recovery_environment(), target=canonical_core_stack.main_target
        )
    )
    verify_command = VerifyBundleCommand(
        environment=recovery_environment(), bundle_id=backup.bundle_id
    )
    assert (await recovery_service.verify_bundle(verify_command)).bundle_id == backup.bundle_id

    bundle_directory = bundle_root / str(backup.bundle_id)
    dump_path = bundle_directory / "postgres.dump"
    object_paths = sorted((bundle_directory / "objects").rglob("*"))
    object_path = next(path for path in object_paths if path.is_file())
    manifest_path = bundle_directory / "manifest.json"
    original_bytes = {path: path.read_bytes() for path in (dump_path, object_path, manifest_path)}

    for victim in (dump_path, object_path, manifest_path):
        victim.write_bytes(original_bytes[victim] + b"\x00")
        with pytest.raises(RecoveryError) as captured:
            await recovery_service.verify_bundle(verify_command)
        assert captured.value.error_code is ErrorCode.CANONICAL_RECOVERY_BUNDLE_INVALID
        victim.write_bytes(original_bytes[victim])
        assert (await recovery_service.verify_bundle(verify_command)).bundle_id == backup.bundle_id


@pytest.mark.asyncio
async def test_empty_target_restore_is_single_transaction_and_exact(
    canonical_core_harness: CanonicalCoreHarness,
    canonical_core_stack: CanonicalCoreStack,
    recovery_service: RecoveryService,
    restore_target_context: RestoreTargetContext,
) -> None:
    harness = canonical_core_harness
    workspace = await harness.seed_workspace()
    await harness.publish_markdown_source(workspace, b"# Exact restore one\n", title="One")
    await harness.publish_markdown_source(workspace, b"# Exact restore two\n", title="Two")
    # The manifest counts the canonical table set; the harness counts every
    # store table, so the expectation is scoped to the manifest contract.
    counts_at_backup = {
        table: count
        for table, count in (await harness.table_counts()).items()
        if table in CANONICAL_COUNT_TABLES
    }
    assert counts_at_backup["sources"] >= 2

    backup = await recovery_service.create_backup(
        BackupCreateCommand(
            environment=recovery_environment(), target=canonical_core_stack.main_target
        )
    )
    assert await restore_target_context.restore_target.is_application_empty() is True

    result = await _restore_bundle(
        recovery_service, restore_target_context, backup.bundle_id, acceptance_probe=None
    )

    assert dict(result.table_counts) == counts_at_backup
    assert await restore_target_context.restore_target.read_schema_head() == (
        POSTGRESQL_SCHEMA_REVISION
    )
    assert dict(await restore_target_context.restore_target.read_canonical_counts()) == (
        counts_at_backup
    )
    assert await restore_target_context.restore_target.read_current_pointer_resolution() == 0
    for table_name in CANONICAL_COUNT_TABLES:
        assert await _table_rows_as_text(harness.engine, table_name) == (
            await _table_rows_as_text(restore_target_context.engine, table_name)
        ), f"restored table {table_name} differs from the backed-up graph"


@pytest.mark.asyncio
async def test_restore_failure_leaves_target_database_empty(
    canonical_core_harness: CanonicalCoreHarness,
    canonical_core_stack: CanonicalCoreStack,
    recovery_service: RecoveryService,
    restore_target_context: RestoreTargetContext,
) -> None:
    harness = canonical_core_harness
    workspace = await harness.seed_workspace()
    await harness.publish_markdown_source(workspace, b"# Failing restore\n", title="Failing")
    backup = await recovery_service.create_backup(
        BackupCreateCommand(
            environment=recovery_environment(), target=canonical_core_stack.main_target
        )
    )
    # The empty-target admission probes see no relation and no alembic table,
    # but the dump's CREATE SCHEMA statement will collide with this schema and
    # abort the single restore transaction.
    async with restore_target_context.engine.begin() as connection:
        await connection.execute(sa.text('CREATE SCHEMA "knowledge"'))

    with pytest.raises(RecoveryError) as captured:
        await _restore_bundle(
            recovery_service, restore_target_context, backup.bundle_id, acceptance_probe=None
        )

    assert captured.value.error_code is ErrorCode.CANONICAL_RECOVERY_RESTORE_FAILED
    assert dict(captured.value.safe_details) == {"component": RecoveryComponent.POSTGRES_RESTORE}
    assert await restore_target_context.restore_target.is_application_empty() is True
    async with restore_target_context.engine.connect() as connection:
        relation_count = (
            await connection.execute(
                sa.text(
                    "SELECT count(*) FROM information_schema.tables"
                    " WHERE table_schema = 'knowledge'"
                )
            )
        ).scalar_one()
    assert relation_count == 0


@pytest.mark.asyncio
async def test_post_restore_canonical_read_returns_exact_bytes(
    canonical_core_harness: CanonicalCoreHarness,
    canonical_core_stack: CanonicalCoreStack,
    recovery_service: RecoveryService,
    restore_target_context: RestoreTargetContext,
) -> None:
    harness = canonical_core_harness
    workspace = await harness.seed_workspace()
    payload = b"# Post-restore canonical read\n\nexact bytes\n"
    published = await harness.publish_markdown_source(workspace, payload, title="PostRestore")
    backup = await recovery_service.create_backup(
        BackupCreateCommand(
            environment=recovery_environment(), target=canonical_core_stack.main_target
        )
    )
    acceptance_probe = AcceptanceSmokeProbe(
        workspace_id=workspace.workspace_id,
        source_id=published.command.source_id,
        expected_sha256=published.result.content_digest.hexadecimal,
        expected_size_bytes=len(payload),
        expected_media_type=CanonicalMediaType.parse("text/markdown"),
    )

    result = await _restore_bundle(
        recovery_service,
        restore_target_context,
        backup.bundle_id,
        acceptance_probe=acceptance_probe,
    )
    assert dict(result.table_counts)["sources"] >= 1

    restored_bytes = await restore_target_context.read_service.read_current_source_bytes(
        ReadCurrentSourceCommand(
            workspace_id=workspace.workspace_id, source_id=published.command.source_id
        ),
        harness.diagnostic_context(),
    )
    assert restored_bytes == payload


@pytest.mark.asyncio
async def test_failed_backup_leaves_no_staging_files_locks_or_processes(
    canonical_core_harness: CanonicalCoreHarness,
    canonical_core_stack: CanonicalCoreStack,
    dump_process: PostgresqlDumpProcessAdapter,
    bundle_root: Path,
) -> None:
    harness = canonical_core_harness
    workspace = await harness.seed_workspace()
    await harness.publish_markdown_source(workspace, b"# Failed backup\n", title="Failed")
    snapshot_store = PostgresqlBackupSnapshotStore(harness.engine)
    failing_service = RecoveryService(
        snapshot_store=snapshot_store,
        bundle_store=FilesystemRecoveryBundleStore(bundle_root),
        dump_process=dump_process,
        object_store=_ReaderRefusingObjectStore(harness.object_store),
        metrics=InMemoryCanonicalBackupMetrics(),
        clock=_utc_clock(),
    )

    with pytest.raises(ObjectStorageError) as captured:
        await failing_service.create_backup(
            BackupCreateCommand(
                environment=recovery_environment(), target=canonical_core_stack.main_target
            )
        )
    assert captured.value.error_code is not None

    assert list(bundle_root.iterdir()) == []
    assert await snapshot_store.observe_pending_writers() == 0
    _assert_no_client_tool_processes()


class _ReaderRefusingObjectStore:
    """Object-store stand-in whose verified readers always fail closed."""

    def __init__(self, inner: LocalFilesystemObjectStore) -> None:
        self._inner = inner

    async def resolve_verified_object(self, expected: ExpectedObject) -> object:
        return await self._inner.resolve_verified_object(expected)

    async def store_stream(
        self,
        stream: AsyncIterator[bytes],
        expected_size_bytes: int,
        media_type: str,
        claimed_sha256: str | None = None,
    ) -> object:
        return await self._inner.store_stream(
            stream, expected_size_bytes, media_type, claimed_sha256
        )

    async def verify_existing_object(self, expected: ExpectedObject) -> object:
        return await self._inner.verify_existing_object(expected)

    @asynccontextmanager
    async def open_verified_reader(self, expected: ExpectedObject) -> AsyncIterator[object]:
        raise ObjectStorageError(ErrorCode.OBJECT_STORAGE_UNAVAILABLE)
        yield  # pragma: no cover
