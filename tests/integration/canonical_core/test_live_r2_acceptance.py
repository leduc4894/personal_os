"""Protected live-R2 acceptance and corruption drills (design 12.2, 12.3, 18.4).

Every case combines the real publication / canonical-read / recovery
composition with a REAL ``R2S3ObjectStore`` on one dedicated private test
bucket: publications upload through the production conditional-create path,
canonical reads verify the full object before any byte is exposed, backups
copy the verified bytes out of the live bucket, and restores conditionally
re-create missing objects from the bundle. The PostgreSQL side runs on the
Task 13 disposable stack, with one freshly migrated database per test so each
case owns exactly the canonical graph and referenced-object set it created.

Harness discipline (design 12.1) is binding: the suite never lists the bucket,
never deletes a prefix or wildcard, and touches only exact canonical keys the
current run created and registered in the live cleanup manifest — mid-test
deletions validate against the manifest before the single low-level delete
call runs, and the harness fixture repeats the same validated exact-key
cleanup on every exit path. Corruption always preserves size AND media type so
a HEAD-only implementation cannot pass. Missing live credentials or a missing
disposable stack FAIL, never skip; live execution is proven by the protected
CI workflow, not by the default offline suite.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, cast
from uuid import uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.canonical_core.conftest import (
    CanonicalCoreHarness,
    CanonicalCoreStack,
    DisposableIdentityDatabase,
    DisposableRestoreDatabase,
    LocalFilesystemObjectStore,
    PostgresqlDumpProcessAdapter,
    PublishedSource,
    SeededWorkspace,
    recovery_environment,
    single_chunk_stream,
)
from tests.integration.r2_object_storage.cleanup_manifest import (
    CreatedObjectRecord,
    validate_cleanup_deletions,
)
from tests.integration.r2_object_storage.conftest import LiveR2Harness

from personal_os.error_contracts.codes import ErrorCode
from personal_os.object_storage import (
    CanonicalMediaType,
    ContentDigest,
    ExpectedObject,
    VerificationMethod,
    derive_canonical_object_key,
)
from personal_os.object_storage.errors import DIGEST_MISMATCH, ObjectStorageError
from personal_os.recovery.bundle import FilesystemRecoveryBundleStore
from personal_os.recovery.contracts import CANONICAL_COUNT_TABLES, InMemoryCanonicalBackupMetrics
from personal_os.recovery.ports import PostgresqlConnectionTarget
from personal_os.recovery.service import (
    AcceptanceSmokeProbe,
    BackupCreateCommand,
    RecoveryService,
    RestoreEmptyCommand,
    VerifyBundleCommand,
)
from personal_os.sources.commands import CreateSourceVersion
from personal_os.sources.metrics import InMemoryCanonicalReadMetrics
from personal_os.sources.reading import CanonicalSourceReadService, ReadCurrentSourceCommand
from postgresql_source_store.backup_snapshot import (
    PostgresqlBackupSnapshotStore,
    PostgresqlRestoreTarget,
)
from postgresql_source_store.canonical_read import PostgresqlCanonicalSourceReadStore
from postgresql_source_store.engine import create_source_store_engine, dispose_source_store_engine
from postgresql_source_store.tables import (
    audit_events,
    content_objects,
    projection_intents,
    source_versions,
    sources,
    sync_events,
)

pytestmark = [pytest.mark.r2_live, pytest.mark.local_stack, pytest.mark.asyncio]

#: The canonical media type every drill publishes under.
_LIVE_MEDIA_TYPE: Final[str] = "text/markdown"
#: Bundle files stream into production ``store_stream`` calls in 1 MiB chunks.
_STREAM_CHUNK_BYTES: Final[int] = 1_048_576
#: Objects per multi-object backup drill.
_BACKUP_OBJECT_COUNT: Final[int] = 3


def _unique_payload(headline: str) -> bytes:
    """Unique synthetic non-personal markdown bytes for one drill object.

    Two payloads with same-length headlines always have equal length and
    different content, so claim-mismatch drills can keep the declared size
    exact while every published digest is unique per run (content addressing
    means identical bytes would share one canonical key).
    """

    return f"# {headline}\n\nrun {uuid4().hex}\n".encode()


def _same_size_corruption(payload: bytes) -> bytes:
    """Corrupted bytes of exactly the original size (and the same media type).

    The last byte is complemented, so the size never changes and the digest
    always does: a HEAD-only implementation sees a perfect match and only the
    full GET's independent digest verification can fail (design 12.2).
    """

    corrupted = bytearray(payload)
    corrupted[-1] ^= 0xFF
    return bytes(corrupted)


def _expected_object(published: PublishedSource) -> ExpectedObject:
    return ExpectedObject(
        content_digest=published.result.content_digest,
        size_bytes=len(published.payload),
        media_type=CanonicalMediaType.parse(_LIVE_MEDIA_TYPE),
    )


def _read_command(
    workspace: SeededWorkspace, published: PublishedSource
) -> ReadCurrentSourceCommand:
    return ReadCurrentSourceCommand(
        workspace_id=workspace.workspace_id, source_id=published.command.source_id
    )


def _record_published_object(r2_harness: LiveR2Harness, published: PublishedSource) -> str:
    """Register the exact canonical key a publication created for cleanup.

    Publications run through the real service, not the harness payload path,
    so the run's cleanup allowlist must record the created key here.
    """

    digest = published.result.content_digest
    object_key = str(derive_canonical_object_key(digest))
    r2_harness.manifest.record_created(
        CreatedObjectRecord(
            key=object_key,
            digest_hexadecimal=digest.hexadecimal,
            size_bytes=len(published.payload),
            media_type=_LIVE_MEDIA_TYPE,
        )
    )
    return object_key


async def _delete_recorded_object(r2_harness: LiveR2Harness, object_key: str) -> None:
    """Delete one exact key this run recorded, validated before the call.

    The mid-test deletion obeys the same exact-key contract as teardown
    (design 12.1): validation rejects an unrecorded key, a noncanonical key or
    a wildcard BEFORE any network request exists.
    """

    validate_cleanup_deletions(
        r2_harness.manifest, bucket_name=r2_harness.manifest.bucket_name, keys=[object_key]
    )
    await r2_harness.delete_exact_object(object_key)


async def _assert_canonical_read_fails_exposing_no_bytes(
    harness: CanonicalCoreHarness,
    command: ReadCurrentSourceCommand,
    error_code: ErrorCode,
) -> None:
    """The canonical reader fails with ``error_code`` and yields zero chunks."""

    exposed_chunks: list[bytes] = []
    with pytest.raises(ObjectStorageError) as rejection:
        async with harness.read_service.open_current_source(
            command, harness.diagnostic_context()
        ) as (_, reader):
            async for chunk in reader:
                exposed_chunks.append(chunk)
    assert rejection.value.error_code is error_code
    assert exposed_chunks == [], "not a single byte may reach the consumer"


async def _file_chunk_stream(path: Path) -> AsyncIterator[bytes]:
    """Stream one bundle sidecar file in bounded chunks (production shape)."""

    with path.open(mode="rb") as object_file:
        while chunk := object_file.read(_STREAM_CHUNK_BYTES):
            yield chunk


async def _referencing_row_counts(
    engine: AsyncEngine, command: CreateSourceVersion
) -> dict[str, int]:
    """Row counts mentioning the attempted publication identity, per table."""

    async with engine.connect() as connection:
        counts = {
            "sources": await connection.scalar(
                sa.select(sa.func.count()).where(sources.c.source_id == command.source_id)
            ),
            "source_versions": await connection.scalar(
                sa.select(sa.func.count()).where(source_versions.c.source_id == command.source_id)
            ),
            "sync_events": await connection.scalar(
                sa.select(sa.func.count()).where(
                    (sync_events.c.event_id == command.event_id)
                    | (sync_events.c.source_id == command.source_id)
                )
            ),
            "projection_intents": await connection.scalar(
                sa.select(sa.func.count()).where(
                    (projection_intents.c.event_id == command.event_id)
                    | (projection_intents.c.source_id == command.source_id)
                )
            ),
            "audit_events": await connection.scalar(
                sa.select(sa.func.count()).where(audit_events.c.target_id == command.source_id)
            ),
            "content_objects": await connection.scalar(
                sa.select(sa.func.count()).where(
                    content_objects.c.content_hash
                    == command.expected_object.content_digest.hexadecimal
                )
            ),
        }
    normalized_counts: dict[str, int] = {}
    for table_name, count in counts.items():
        assert count is not None
        normalized_counts[table_name] = count
    return normalized_counts


# --- live composition fixtures -----------------------------------------------------


@dataclass(frozen=True, slots=True)
class LiveAcceptanceContext:
    """One per-test migrated database, its backup target and live harness."""

    database_name: str
    backup_target: PostgresqlConnectionTarget
    harness: CanonicalCoreHarness


@pytest_asyncio.fixture
async def live_acceptance_context(
    canonical_core_stack: CanonicalCoreStack,
    disposable_identity_database: DisposableIdentityDatabase,
    live_r2_harness: LiveR2Harness,
) -> AsyncIterator[LiveAcceptanceContext]:
    """The Task 13 canonical-core harness with the REAL R2 store attached.

    The per-test identity database keeps every drill's referenced-object set
    exactly what that drill published, so bundle equality is provable and
    earlier drills' teardown deletions cannot break a later backup. The Task 13
    harness type names the fake local store but consumes only the
    ``CanonicalObjectStore`` port, which the live adapter implements in full.
    """

    settings = canonical_core_stack.settings.model_copy(
        update={"database_name": disposable_identity_database.database_name}
    )
    engine = create_source_store_engine(settings, canonical_core_stack.password)
    try:
        yield LiveAcceptanceContext(
            database_name=disposable_identity_database.database_name,
            backup_target=replace(
                canonical_core_stack.main_target,
                database=disposable_identity_database.database_name,
            ),
            harness=CanonicalCoreHarness(
                engine, cast(LocalFilesystemObjectStore, live_r2_harness.store)
            ),
        )
    finally:
        await dispose_source_store_engine(engine)


@pytest.fixture
def live_recovery_service(
    live_acceptance_context: LiveAcceptanceContext,
    live_r2_harness: LiveR2Harness,
    bundle_root: Path,
    dump_process: PostgresqlDumpProcessAdapter,
) -> RecoveryService:
    """The real recovery composition with the live R2 store as object storage."""

    return RecoveryService(
        snapshot_store=PostgresqlBackupSnapshotStore(live_acceptance_context.harness.engine),
        bundle_store=FilesystemRecoveryBundleStore(bundle_root),
        dump_process=dump_process,
        object_store=live_r2_harness.store,
        metrics=InMemoryCanonicalBackupMetrics(),
        clock=lambda: datetime.now(UTC),
    )


@dataclass(frozen=True, slots=True)
class LiveRestoreTargetContext:
    """A disposable restore database with its read service on the live store."""

    database: DisposableRestoreDatabase
    engine: AsyncEngine
    restore_target: PostgresqlRestoreTarget
    read_service: CanonicalSourceReadService


@pytest_asyncio.fixture
async def live_restore_target_context(
    canonical_core_stack: CanonicalCoreStack,
    disposable_restore_database: DisposableRestoreDatabase,
    live_r2_harness: LiveR2Harness,
) -> AsyncIterator[LiveRestoreTargetContext]:
    engine = create_source_store_engine(
        disposable_restore_database.settings, canonical_core_stack.password
    )
    try:
        from api_runtime.exclusion_policy_crypto import TrustAnchorEd25519Verifier

        from personal_os.exclusion_policy.metrics import InMemoryExclusionPolicyMetrics
        from postgresql_source_store.policy_enforcement import compose_policy_enforcement

        policy_verifier = TrustAnchorEd25519Verifier()
        policy_metrics = InMemoryExclusionPolicyMetrics()
        yield LiveRestoreTargetContext(
            database=disposable_restore_database,
            engine=engine,
            restore_target=PostgresqlRestoreTarget(engine),
            read_service=CanonicalSourceReadService(
                store=PostgresqlCanonicalSourceReadStore(
                    engine,
                    policy_verifier=policy_verifier,
                    policy_metrics=policy_metrics,
                ),
                object_store=live_r2_harness.store,
                metrics=InMemoryCanonicalReadMetrics(),
                policy_guard=compose_policy_enforcement(
                    engine, verifier=policy_verifier, metrics=policy_metrics
                ),
            ),
        )
    finally:
        await dispose_source_store_engine(engine)


# --- 12.2: same-size corruption -----------------------------------------------------


async def test_same_size_corruption_detected_before_byte_exposure(
    live_acceptance_context: LiveAcceptanceContext,
    live_r2_harness: LiveR2Harness,
    live_recovery_service: RecoveryService,
) -> None:
    harness = live_acceptance_context.harness
    workspace = await harness.seed_workspace()
    payload = _unique_payload("Same-size corruption drill")
    published = await harness.publish_markdown_source(workspace, payload, title="Corruption drill")
    object_key = _record_published_object(live_r2_harness, published)
    command = _read_command(workspace, published)
    assert (
        await harness.read_service.read_current_source_bytes(command, harness.diagnostic_context())
        == payload
    )

    backup = await live_recovery_service.create_backup(
        BackupCreateCommand(
            environment=recovery_environment(), target=live_acceptance_context.backup_target
        )
    )
    verified = await live_recovery_service.verify_bundle(
        VerifyBundleCommand(environment=recovery_environment(), bundle_id=backup.bundle_id)
    )
    assert verified.object_count >= 1

    counts_before = await harness.table_counts()

    corrupt_key = await live_r2_harness.write_object_under_digest(
        digest_hexadecimal=published.result.content_digest.hexadecimal,
        payload=_same_size_corruption(payload),
        media_type=_LIVE_MEDIA_TYPE,
    )
    assert corrupt_key == object_key

    with pytest.raises(ObjectStorageError) as rejection:
        await harness.read_service.read_current_source_bytes(command, harness.diagnostic_context())
    assert rejection.value.error_code is ErrorCode.OBJECT_STORAGE_INTEGRITY_FAILED
    await _assert_canonical_read_fails_exposing_no_bytes(
        harness, command, ErrorCode.OBJECT_STORAGE_INTEGRITY_FAILED
    )
    assert await harness.table_counts() == counts_before

    await _delete_recorded_object(live_r2_harness, corrupt_key)

    # Restore the original through the production conditional store path,
    # streamed from the verified bundle file with the claimed digest.
    async with live_recovery_service.bundle_store.open_verified(backup.bundle_id) as bundle:
        bundle_file = bundle.object_path(published.result.content_digest.hexadecimal)
        receipt = await live_r2_harness.store.store_stream(
            _file_chunk_stream(bundle_file),
            len(payload),
            _LIVE_MEDIA_TYPE,
            claimed_sha256=published.result.content_digest.hexadecimal,
        )
    assert receipt.verification_method is VerificationMethod.UPLOADED_FULL_READ
    assert str(receipt.object_key) == object_key

    assert (
        await harness.read_service.read_current_source_bytes(command, harness.diagnostic_context())
        == payload
    )


# --- 12.2: missing referenced object ------------------------------------------------


async def test_missing_referenced_object_fails_closed_without_mutation(
    live_acceptance_context: LiveAcceptanceContext,
    live_r2_harness: LiveR2Harness,
) -> None:
    harness = live_acceptance_context.harness
    workspace = await harness.seed_workspace()
    payload = _unique_payload("Missing referenced object drill")
    published = await harness.publish_markdown_source(workspace, payload, title="Missing object")
    object_key = _record_published_object(live_r2_harness, published)
    command = _read_command(workspace, published)
    counts_before = await harness.table_counts()

    await _delete_recorded_object(live_r2_harness, object_key)

    with pytest.raises(ObjectStorageError) as rejection:
        await harness.read_service.read_current_source_bytes(command, harness.diagnostic_context())
    assert rejection.value.error_code is ErrorCode.OBJECT_STORAGE_OBJECT_MISSING
    await _assert_canonical_read_fails_exposing_no_bytes(
        harness, command, ErrorCode.OBJECT_STORAGE_OBJECT_MISSING
    )
    assert await harness.table_counts() == counts_before


# --- 12.3: pre-publication claim mismatch -------------------------------------------


async def test_pre_publication_claim_mismatch_creates_no_canonical_pointer(
    live_acceptance_context: LiveAcceptanceContext,
    live_r2_harness: LiveR2Harness,
) -> None:
    harness = live_acceptance_context.harness
    workspace = await harness.seed_workspace()
    claimed_payload = _unique_payload("Claim mismatch declared")
    supplied_payload = _unique_payload("Claim mismatch supplied")
    assert len(supplied_payload) == len(claimed_payload)
    command = harness.build_markdown_create_command(workspace, claimed_payload)
    counts_before = await harness.table_counts()

    with pytest.raises(ObjectStorageError) as rejection:
        await harness.publication_service.publish_create(
            command=command,
            stream=single_chunk_stream(supplied_payload),
            diagnostic_context=harness.diagnostic_context(),
        )
    assert rejection.value.error_code is ErrorCode.OBJECT_STORAGE_INPUT_INVALID
    assert dict(rejection.value.safe_details) == {"reason": DIGEST_MISMATCH}

    # The object store rejected the claim before the store port was invoked:
    # no row in any of the nine tables and no canonical pointer anywhere.
    assert await harness.table_counts() == counts_before
    referencing_counts = await _referencing_row_counts(harness.engine, command)
    assert set(referencing_counts) == {
        "sources",
        "source_versions",
        "sync_events",
        "projection_intents",
        "audit_events",
        "content_objects",
    }
    assert all(count == 0 for count in referencing_counts.values())
    # Nothing was uploaded: the claimed digest stays absent and the run's
    # cleanup allowlist stays empty.
    assert await live_r2_harness.store.resolve_verified_object(command.expected_object) is None
    assert len(live_r2_harness.manifest) == 0


# --- 18.4: backup completeness ------------------------------------------------------


async def test_backup_contains_every_referenced_object_and_exact_bytes(
    live_acceptance_context: LiveAcceptanceContext,
    live_r2_harness: LiveR2Harness,
    live_recovery_service: RecoveryService,
) -> None:
    harness = live_acceptance_context.harness
    workspace = await harness.seed_workspace()
    published_objects = [
        await harness.publish_markdown_source(
            workspace, _unique_payload(f"Backup object drill {index}"), title=f"Backup {index}"
        )
        for index in range(_BACKUP_OBJECT_COUNT)
    ]
    for published in published_objects:
        _record_published_object(live_r2_harness, published)

    backup = await live_recovery_service.create_backup(
        BackupCreateCommand(
            environment=recovery_environment(), target=live_acceptance_context.backup_target
        )
    )

    # The referenced set is re-derived independently through a fresh quiesced
    # snapshot of the same database the backup dumped.
    snapshot_store = PostgresqlBackupSnapshotStore(harness.engine)
    async with snapshot_store.open_quiesced_snapshot(now=datetime.now(UTC)) as snapshot:
        referenced = snapshot.referenced_objects
    assert {expected.content_digest.hexadecimal for expected in referenced} == {
        published.result.content_digest.hexadecimal for published in published_objects
    }

    async with live_recovery_service.bundle_store.open_verified(backup.bundle_id) as bundle:
        manifest_objects = bundle.manifest.objects
        assert backup.object_count == len(referenced) == len(manifest_objects)
        assert [entry.content_sha256 for entry in manifest_objects] == sorted(
            expected.content_digest.hexadecimal for expected in referenced
        )
        for entry in manifest_objects:
            assert entry.object_key == str(
                derive_canonical_object_key(ContentDigest.parse(entry.content_sha256))
            )
            bundle_bytes = bundle.object_path(entry.content_sha256).read_bytes()
            assert len(bundle_bytes) == entry.size_bytes
            assert hashlib.sha256(bundle_bytes).hexdigest() == entry.content_sha256
            live_expected = ExpectedObject(
                content_digest=ContentDigest.parse(entry.content_sha256),
                size_bytes=entry.size_bytes,
                media_type=CanonicalMediaType.parse(entry.media_type),
            )
            live_chunks: list[bytes] = []
            async with live_r2_harness.store.open_verified_reader(live_expected) as reader:
                async for chunk in reader:
                    live_chunks.append(chunk)
            assert b"".join(live_chunks) == bundle_bytes


# --- 12.2: dedup and never-overwrite ------------------------------------------------


async def test_existing_exact_object_reused_and_mismatch_never_overwritten(
    live_acceptance_context: LiveAcceptanceContext,
    live_r2_harness: LiveR2Harness,
) -> None:
    harness = live_acceptance_context.harness
    workspace = await harness.seed_workspace()
    payload = _unique_payload("Dedup and no-overwrite drill")
    published = await harness.publish_markdown_source(workspace, payload, title="Dedup original")
    object_key = _record_published_object(live_r2_harness, published)
    expected = _expected_object(published)

    duplicate = await harness.publish_markdown_source(workspace, payload, title="Dedup duplicate")
    assert duplicate.result.content_digest == published.result.content_digest
    # Identical bytes never create a second key: the whole run still owns
    # exactly one exact canonical key.
    assert live_r2_harness.manifest.recorded_keys() == (object_key,)

    # The production conditional store path proves the reuse directly: an
    # existing exact object resolves as a dedup receipt, never a new upload.
    dedup_receipt = await live_r2_harness.store.store_stream(
        single_chunk_stream(payload),
        len(payload),
        _LIVE_MEDIA_TYPE,
        claimed_sha256=expected.content_digest.hexadecimal,
    )
    assert dedup_receipt.verification_method is VerificationMethod.EXISTING_FULL_READ
    assert str(dedup_receipt.object_key) == object_key

    corrupt_key = await live_r2_harness.write_object_under_digest(
        digest_hexadecimal=expected.content_digest.hexadecimal,
        payload=_same_size_corruption(payload),
        media_type=_LIVE_MEDIA_TYPE,
    )
    assert corrupt_key == object_key

    # A fresh store of the ORIGINAL bytes over a corrupted existing object
    # fails closed: HEAD matches, the full verification does not.
    with pytest.raises(ObjectStorageError) as rejection:
        await live_r2_harness.store.store_stream(
            single_chunk_stream(payload),
            len(payload),
            _LIVE_MEDIA_TYPE,
            claimed_sha256=expected.content_digest.hexadecimal,
        )
    assert rejection.value.error_code is ErrorCode.OBJECT_STORAGE_INTEGRITY_FAILED

    # The corrupted object was never overwritten: it still fails verification
    # exactly as before, and the canonical read stays fail-closed.
    with pytest.raises(ObjectStorageError) as unchanged:
        await live_r2_harness.store.verify_existing_object(expected)
    assert unchanged.value.error_code is ErrorCode.OBJECT_STORAGE_INTEGRITY_FAILED
    with pytest.raises(ObjectStorageError) as read_rejection:
        await harness.read_service.read_current_source_bytes(
            _read_command(workspace, published), harness.diagnostic_context()
        )
    assert read_rejection.value.error_code is ErrorCode.OBJECT_STORAGE_INTEGRITY_FAILED
    assert live_r2_harness.manifest.recorded_keys() == (object_key,)


# --- 18.4: live restore and post-restore read ---------------------------------------


async def test_restore_matches_source_bundle_and_post_restore_read(
    live_acceptance_context: LiveAcceptanceContext,
    live_r2_harness: LiveR2Harness,
    live_recovery_service: RecoveryService,
    live_restore_target_context: LiveRestoreTargetContext,
) -> None:
    harness = live_acceptance_context.harness
    workspace = await harness.seed_workspace()
    payload = _unique_payload("Post-restore live read drill")
    published = await harness.publish_markdown_source(workspace, payload, title="Post-restore")
    object_key = _record_published_object(live_r2_harness, published)
    expected = _expected_object(published)

    backup = await live_recovery_service.create_backup(
        BackupCreateCommand(
            environment=recovery_environment(), target=live_acceptance_context.backup_target
        )
    )
    counts_at_backup = await harness.table_counts()
    assert counts_at_backup["policy_previews"] == 1
    assert "policy_previews" not in CANONICAL_COUNT_TABLES
    assert "policy_preview_results" not in CANONICAL_COUNT_TABLES
    counts_at_backup = {
        table_name: counts_at_backup[table_name] for table_name in CANONICAL_COUNT_TABLES
    }
    assert set(counts_at_backup) == set(CANONICAL_COUNT_TABLES)

    # Remove the exact published object so the restore must conditionally
    # re-create it in the LIVE bucket from the verified bundle bytes.
    await _delete_recorded_object(live_r2_harness, object_key)
    assert await live_r2_harness.store.resolve_verified_object(expected) is None

    probe = AcceptanceSmokeProbe(
        workspace_id=workspace.workspace_id,
        source_id=published.command.source_id,
        expected_sha256=expected.content_digest.hexadecimal,
        expected_size_bytes=len(payload),
        expected_media_type=expected.media_type,
    )
    result = await live_recovery_service.restore_empty(
        RestoreEmptyCommand(
            environment=recovery_environment(),
            bundle_id=backup.bundle_id,
            target=live_restore_target_context.database.connection_target,
            target_confirmation=live_restore_target_context.database.database_name,
            acceptance_probe=probe,
        ),
        read_service=live_restore_target_context.read_service,
        restore_target=live_restore_target_context.restore_target,
    )

    assert result.object_count == 1
    assert set(result.table_counts) == set(CANONICAL_COUNT_TABLES)
    assert dict(result.table_counts) == counts_at_backup
    restored_counts = dict(await live_restore_target_context.restore_target.read_canonical_counts())
    assert set(restored_counts) == set(CANONICAL_COUNT_TABLES)
    assert restored_counts == counts_at_backup
    restored_bytes = await live_restore_target_context.read_service.read_current_source_bytes(
        _read_command(workspace, published), harness.diagnostic_context()
    )
    assert restored_bytes == payload
