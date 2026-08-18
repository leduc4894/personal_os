"""Policy, keyset and evaluation state across canonical backup/restore (spec 22).

The disposable stack's real recovery machinery — quiesced exported snapshot,
bounded ``pg_dump``/``pg_restore`` process adapter, filesystem bundle store —
backs every case. One workspace publishes real signed revisions through the
real policy publication service and one canonical source through the guarded
publication service; a backup bundle captures the graph, and an empty-target
restore proves the policy state survives byte-exactly: the active pointer,
the signed snapshot payload and signature, the signing-key trust anchor, the
keyset chain, the evaluation rows and the reconciliation intents.

The lost-key contract is the second half: a restore that arrives without the
private signing file is inspectable only through offline tooling — the
restored snapshot digest still matches its persisted bytes and the offline
guarded read still verifies and serves the exact canonical bytes — while the
API composition cannot start (the signer load fails on the missing file) and
a replacement key cannot be minted for the restored workspace (keyset
initialization is refused for an already-initialized workspace), so no
publish or serve operation can run through the API surface.
"""

from __future__ import annotations

import hashlib
import subprocess
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from api_runtime.exclusion_policy_commands import (
    execute_policy_key_initialize,
    load_existing_policy_signing_key,
)
from api_runtime.exclusion_policy_settings import (
    load_exclusion_policy_signer,
    load_exclusion_policy_signing_settings,
)
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.canonical_core.conftest import run_bounded_child_on_worker_loop
from tests.integration.exclusion_policy.conftest import (
    PolicyMigrationHarness,
    PolicyMigrationStack,
)
from tests.integration.exclusion_policy.test_source_publication_enforcement import (
    KEY_FILE_NAME as ENFORCEMENT_KEY_FILE_NAME,
)
from tests.integration.exclusion_policy.test_source_publication_enforcement import (
    EnforcementHarness,
    RecordingObjectStore,
    _context,
)
from tools.postgresql_dump_process import (
    PostgresqlDumpProcessAdapter,
    check_client_tools,
)

from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import (
    ApplicationError,
    SecretFileError,
)
from personal_os.exclusion_policy.contracts import RuleKind
from personal_os.exclusion_policy.normalization import normalize_rule
from personal_os.object_storage import (
    CanonicalMediaType,
    ContentDigest,
    derive_canonical_object_key,
)
from personal_os.recovery.bundle import FilesystemRecoveryBundleStore
from personal_os.recovery.contracts import (
    InMemoryCanonicalBackupMetrics,
    RecoveryEnvironment,
    RecoveryError,
)
from personal_os.recovery.ports import PostgresqlConnectionTarget
from personal_os.recovery.service import (
    AcceptanceSmokeProbe,
    BackupCreateCommand,
    RecoveryService,
    RestoreEmptyCommand,
)
from personal_os.sources.metrics import InMemoryCanonicalReadMetrics
from personal_os.sources.reading import (
    CanonicalSourceReadService,
    ReadCurrentSourceCommand,
)
from postgresql_source_store.backup_snapshot import (
    PostgresqlBackupSnapshotStore,
    PostgresqlRestoreTarget,
)
from postgresql_source_store.canonical_read import PostgresqlCanonicalSourceReadStore
from postgresql_source_store.engine import (
    create_source_store_engine,
    dispose_source_store_engine,
)
from postgresql_source_store.policy_enforcement import compose_policy_enforcement
from postgresql_source_store.tables import (
    content_objects,
    policy_evaluations,
    policy_keyset_signatures,
    policy_keysets,
    policy_reconciliation_intents,
    policy_signing_keys,
    source_policies,
    source_versions,
    sources,
    workspace_policy_state,
)

pytestmark = pytest.mark.local_stack

_DATABASE_HOST = "127.0.0.1"
_APPLICATION_USER = "knowledge_app"
_COMPOSE_FILE = Path(__file__).resolve().parents[3] / "infra" / "compose" / "compose.yaml"
_POSTGRESQL_SERVICE_NAME = "postgresql"
_SUPERUSER_NAME = "stack_admin"
_DOCKER_EXEC_TIMEOUT_SECONDS = 120.0

#: The policy graph tables the backup/restore contract must preserve exactly.
POLICY_STATE_TABLES: tuple[tuple[str, sa.Table], ...] = (
    ("workspace_policy_state", workspace_policy_state),
    ("policy_signing_keys", policy_signing_keys),
    ("policy_keysets", policy_keysets),
    ("policy_keyset_signatures", policy_keyset_signatures),
    ("source_policies", source_policies),
    ("policy_evaluations", policy_evaluations),
    ("policy_reconciliation_intents", policy_reconciliation_intents),
)


def _compose_exec_psql(project_name: str, sql_statement: str, success_marker: str) -> None:
    """Run one superuser SQL statement inside the stack's PostgreSQL container."""
    script = "\n".join(
        (
            "admin_password=$(cat /run/secrets/postgres_admin_password) || exit 75",
            '[ -n "$admin_password" ] || exit 65',
            'PGPASSWORD="$admin_password" psql -XAtq --set ON_ERROR_STOP=1 '
            "--host 127.0.0.1 --port 5432 "
            f"--username {_SUPERUSER_NAME} --dbname postgres "
            f'--command "{sql_statement}" >/dev/null 2>&1 || exit 75',
            "unset admin_password",
            f"printf '%s\\n' {success_marker}",
        )
    )
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--file",
            str(_COMPOSE_FILE),
            "--project-name",
            project_name,
            "exec",
            "--no-TTY",
            _POSTGRESQL_SERVICE_NAME,
            "/bin/sh",
            "-ec",
            script,
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=_DOCKER_EXEC_TIMEOUT_SECONDS,
    )
    assert result.returncode == 0 and result.stdout.strip() == success_marker, (
        "docker compose exec psql statement failed for the disposable stack"
    )


async def _table_rows_sorted(engine: AsyncEngine, table: sa.Table) -> list[tuple[str, ...]]:
    ordering = list(table.primary_key.columns)
    async with engine.connect() as connection:
        rows = (await connection.execute(sa.select(table).order_by(*ordering))).all()
    return sorted(tuple(repr(value) for value in row) for row in rows)


def _build_read_service(engine: AsyncEngine, object_store: Any) -> CanonicalSourceReadService:
    from api_runtime.exclusion_policy_crypto import TrustAnchorEd25519Verifier

    verifier = TrustAnchorEd25519Verifier()
    return CanonicalSourceReadService(
        store=PostgresqlCanonicalSourceReadStore(engine, policy_verifier=verifier),
        object_store=object_store,
        metrics=InMemoryCanonicalReadMetrics(),
        policy_guard=compose_policy_enforcement(engine, verifier=verifier),
    )


@dataclass(frozen=True, slots=True)
class PolicyBackupGraph:
    """The seeded graph one backup captures: harness, bytes and identities."""

    harness: EnforcementHarness
    payload: bytes
    allowed_source_id: UUID
    denied_source_id: UUID
    revision_two_id: UUID


@pytest.fixture(scope="module")
def backup_secret_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The module's one signing-key secret root.

    The module-scoped stack's database persists across the function-scoped
    graph fixtures, so every instantiation must derive the same workspace
    signer from the same key file — a per-test directory would mint a new
    key that the already-initialized keyset refuses.
    """
    return tmp_path_factory.mktemp("policy-backup-secrets")


@pytest.fixture(scope="module")
def backup_object_store() -> RecordingObjectStore:
    """The module's one canonical object store.

    The module stack's database accumulates every fixture run's published
    sources, and a backup copies every graph object, so the store must retain
    earlier runs' objects just like the real object store would.
    """
    return RecordingObjectStore()


async def _complete_bare_source_pointer(
    harness: EnforcementHarness, source_id: UUID, payload: bytes
) -> bool:
    """Give one bare seeded source a current version; False when already set.

    The restore's pointer-resolution contract counts a source whose
    ``current_version_id`` is null as unresolved, and production graphs only
    contain sources created through the guarded publication path (source and
    version commit atomically). The stack's migration-seeded source and the
    harness's bare seeded rows are completed the same way before the backup,
    so the whole restored database resolves to zero.
    """
    digest_hexadecimal = hashlib.sha256(payload).hexdigest()
    version_id = uuid4()
    content_object_id = uuid4()
    committed_at = datetime.now(UTC)
    async with harness.base.engine.begin() as connection:
        existing = (
            await connection.execute(
                sa.select(sources.c.current_version_id).where(sources.c.source_id == source_id)
            )
        ).scalar_one_or_none()
        if existing is not None:
            return False
        await connection.execute(
            sa.insert(content_objects).values(
                content_object_id=content_object_id,
                content_hash=digest_hexadecimal,
                object_key=derive_canonical_object_key(
                    ContentDigest.parse(digest_hexadecimal)
                ).value,
                byte_size=len(payload),
                media_type="text/markdown",
                verified_at=committed_at,
                created_at=committed_at,
            )
        )
        await connection.execute(
            sa.insert(source_versions).values(
                source_version_id=version_id,
                workspace_id=harness.workspace_id,
                source_id=source_id,
                content_object_id=content_object_id,
                content_version=1,
                author_kind="user",
                author_id=harness.owner_user_id,
                committed_at=committed_at,
            )
        )
        # The pointer check ties ``current_version_id`` to ``sync_state``:
        # a resolved pointer moves the source to ``active`` in the same write.
        await connection.execute(
            sa.update(sources)
            .values(current_version_id=version_id, sync_state="active")
            .where(sources.c.source_id == source_id)
        )
    # The canonical bytes exist too: the backup copies every graph object,
    # so the recording store carries the completed sources' payloads.
    harness.object_store.objects[digest_hexadecimal] = payload
    return True


@pytest_asyncio.fixture
async def policy_backup_graph(
    policy_migration_harness: PolicyMigrationHarness,
    backup_secret_root: Path,
    backup_object_store: RecordingObjectStore,
) -> AsyncIterator[PolicyBackupGraph]:
    """One workspace with real revisions, sources and an evaluation row."""
    harness = EnforcementHarness(
        policy_migration_harness, backup_secret_root, object_store=backup_object_store
    )
    await harness.ensure_keys_initialized()
    await _complete_bare_source_pointer(
        harness, harness.base.stack.seeded_source_id, b"# Migration seeded note\nrestore-pointer"
    )

    # Revision 1 (empty) admits the canonical source publication; before it
    # every guarded content operation fails closed (spec 14), so the empty
    # revision is published explicitly first.
    base_revision_number = await harness.publish_revision()
    assert base_revision_number >= 1
    payload = b"# Backup restore sentinel note\n" + uuid4().bytes * 64
    published = await harness.publish_source(payload)

    # The next revision denies exactly one seeded source — completed with its
    # own current version so the whole graph resolves — while the published
    # source stays allowed, proving both enforcement directions survive.
    # The module stack's workspace accumulates revisions across the fixture's
    # per-test runs, so the graph chains onto whatever revision is active.
    denied_source_id = await policy_migration_harness.seed_policy_source()
    await _complete_bare_source_pointer(
        harness, denied_source_id, b"# Denied sentinel note\n" + uuid4().bytes * 8
    )
    denied_rule = normalize_rule(
        uuid4(), RuleKind.EXACT_SOURCE_ID, source_id_operand=denied_source_id
    )
    revision_number = await harness.publish_revision(denied_rule)
    assert revision_number == base_revision_number + 1
    revision_rows = await policy_migration_harness.fetch_all(
        "SELECT policy_revision_id FROM knowledge.source_policies"
        " WHERE workspace_id = :workspace_id AND revision_number = :revision_number",
        {"workspace_id": harness.workspace_id, "revision_number": revision_number},
    )
    assert revision_rows, "the deny revision must exist before the backup"
    await policy_migration_harness.seed_evaluation(UUID(str(revision_rows[0][0])), denied_source_id)

    yield PolicyBackupGraph(
        harness=harness,
        payload=payload,
        allowed_source_id=published.source_id,
        denied_source_id=denied_source_id,
        revision_two_id=UUID(str(revision_rows[0][0])),
    )


@pytest_asyncio.fixture
async def backup_bundle(
    policy_backup_graph: PolicyBackupGraph,
    policy_migration_stack: PolicyMigrationStack,
    tmp_path_factory: pytest.TempPathFactory,
) -> AsyncIterator[tuple[UUID, RecoveryService]]:
    """One verified backup bundle of the seeded policy graph."""
    try:
        await check_client_tools("pg_dump", "pg_restore", runner=run_bounded_child_on_worker_loop)
    except RecoveryError:
        pytest.fail(
            "pg_dump and pg_restore client tools at exactly version 18.4 must be on PATH "
            "for the policy backup/restore suite (never skipped)"
        )
    stack = policy_migration_stack
    dump_process = PostgresqlDumpProcessAdapter(
        "pg_dump",
        "pg_restore",
        password=stack.password,
        runner=run_bounded_child_on_worker_loop,
    )
    recovery_service = RecoveryService(
        snapshot_store=PostgresqlBackupSnapshotStore(policy_backup_graph.harness.base.engine),
        bundle_store=FilesystemRecoveryBundleStore(tmp_path_factory.mktemp("policy-bundles")),
        dump_process=dump_process,
        object_store=policy_backup_graph.harness.object_store,
        metrics=InMemoryCanonicalBackupMetrics(),
        clock=lambda: datetime.now(UTC),
    )
    result = await recovery_service.create_backup(
        BackupCreateCommand(
            environment=RecoveryEnvironment.TEST,
            target=PostgresqlConnectionTarget(
                host=_DATABASE_HOST,
                port=stack.port,
                database="knowledge",
                user=_APPLICATION_USER,
            ),
        )
    )
    yield result.bundle_id, recovery_service


@dataclass(frozen=True, slots=True)
class RestoredDatabase:
    """One restored empty-target database and its engine."""

    database_name: str
    engine: AsyncEngine


@pytest_asyncio.fixture
async def restored_database(
    backup_bundle: tuple[UUID, RecoveryService],
    policy_migration_stack: PolicyMigrationStack,
    policy_backup_graph: PolicyBackupGraph,
) -> AsyncIterator[RestoredDatabase]:
    """Restore the bundle into one disposable empty database on the same stack."""
    bundle_id, recovery_service = backup_bundle
    stack = policy_migration_stack
    database_name = f"knowledge_ci_policy_restore_{uuid4().hex[:12]}"
    _compose_exec_psql(
        stack.project_name,
        f'CREATE DATABASE "{database_name}" OWNER {_APPLICATION_USER}',
        "policy_restore_database_created",
    )
    settings = stack.settings.model_copy(update={"database_name": database_name})
    engine = create_source_store_engine(settings, stack.password)
    try:
        await recovery_service.restore_empty(
            RestoreEmptyCommand(
                environment=RecoveryEnvironment.TEST,
                bundle_id=bundle_id,
                target=PostgresqlConnectionTarget(
                    host=_DATABASE_HOST,
                    port=stack.port,
                    database=database_name,
                    user=_APPLICATION_USER,
                ),
                target_confirmation=database_name,
                acceptance_probe=AcceptanceSmokeProbe(
                    workspace_id=policy_backup_graph.harness.workspace_id,
                    source_id=policy_backup_graph.allowed_source_id,
                    expected_sha256=hashlib.sha256(policy_backup_graph.payload).hexdigest(),
                    expected_size_bytes=len(policy_backup_graph.payload),
                    expected_media_type=CanonicalMediaType.parse("text/markdown"),
                ),
            ),
            read_service=_build_read_service(engine, policy_backup_graph.harness.object_store),
            restore_target=PostgresqlRestoreTarget(engine),
        )
        yield RestoredDatabase(database_name=database_name, engine=engine)
    finally:
        await dispose_source_store_engine(engine)
        _compose_exec_psql(
            stack.project_name,
            f'DROP DATABASE "{database_name}" WITH (FORCE)',
            "policy_restore_database_dropped",
        )


@pytest.mark.asyncio
async def test_policy_keyset_and_evaluation_state_survives_restore(
    policy_backup_graph: PolicyBackupGraph,
    restored_database: RestoredDatabase,
) -> None:
    for table_name, table in POLICY_STATE_TABLES:
        original = await _table_rows_sorted(policy_backup_graph.harness.base.engine, table)
        restored = await _table_rows_sorted(restored_database.engine, table)
        assert restored == original, f"table {table_name} did not survive the restore exactly"


@pytest.mark.asyncio
async def test_restored_workspace_enforces_and_serves_exact_bytes(
    policy_backup_graph: PolicyBackupGraph,
    restored_database: RestoredDatabase,
) -> None:
    graph = policy_backup_graph
    read_service = _build_read_service(restored_database.engine, graph.harness.object_store)

    served = await read_service.read_current_source_bytes(
        ReadCurrentSourceCommand(
            workspace_id=graph.harness.workspace_id, source_id=graph.allowed_source_id
        ),
        _context(),
    )
    assert served == graph.payload

    from personal_os.exclusion_policy.errors import ExclusionPolicyError

    with pytest.raises(ExclusionPolicyError) as denial:
        await read_service.read_current_source_bytes(
            ReadCurrentSourceCommand(
                workspace_id=graph.harness.workspace_id, source_id=graph.denied_source_id
            ),
            _context(),
        )
    from personal_os.error_contracts.codes import ErrorCode

    assert denial.value.error_code is ErrorCode.EXCLUSION_POLICY_DENIED


@pytest.mark.asyncio
async def test_restore_without_the_private_signing_file_cannot_start_api_or_publish(
    policy_backup_graph: PolicyBackupGraph,
    restored_database: RestoredDatabase,
    tmp_path: Path,
) -> None:
    graph = policy_backup_graph
    missing_secret_root = tmp_path / "restored-without-signing-key"
    missing_secret_root.mkdir()

    # The API composition fails before binding: the configured private file
    # is absent from the secret root, so the typed secret-file failure stops
    # the signer load and no publish or serve surface starts.
    environ = {
        "KNOWLEDGE_ENVIRONMENT": "test",
        "KNOWLEDGE_SECRET_ROOT": str(missing_secret_root),
        "KNOWLEDGE_POLICY_SIGNING_KEY_ID": graph.harness.signing_key.key_id,
        "KNOWLEDGE_POLICY_SIGNING_KEY_FILE": ENFORCEMENT_KEY_FILE_NAME,
    }
    settings = load_exclusion_policy_signing_settings(environ=environ)
    with pytest.raises(SecretFileError) as signer_failure:
        load_exclusion_policy_signer(settings, secret_root=missing_secret_root)
    assert signer_failure.value.error_code is ErrorCode.SECRET_FILE_MISSING

    # A replacement key cannot be minted for the restored workspace: its
    # keyset is already initialized, so initialization is refused and the
    # restored current key stays the only legitimate signer.
    replacement_root = tmp_path / "restored-replacement-key"
    replacement_root.mkdir()
    with pytest.raises(ApplicationError) as rejection:
        await execute_policy_key_initialize(
            engine=restored_database.engine,
            workspace_id=graph.harness.workspace_id,
            key_file_name="replacement.pem",
            secret_root=replacement_root,
            context=_context(),
        )
    assert rejection.value.safe_details.get("reason") is not None

    # Offline tooling still inspects the restored state: the persisted
    # snapshot bytes match their persisted digest, and the operator who kept
    # the original key file can still load the exact signer identity.
    async with restored_database.engine.connect() as connection:
        revision_row = (
            await connection.execute(
                sa.select(
                    source_policies.c.snapshot_payload_bytes,
                    source_policies.c.snapshot_payload_sha256,
                ).where(
                    source_policies.c.workspace_id == graph.harness.workspace_id,
                    source_policies.c.policy_revision_id == graph.revision_two_id,
                )
            )
        ).one()
    assert hashlib.sha256(revision_row[0]).hexdigest() == revision_row[1]
    original_key = load_existing_policy_signing_key(
        graph.harness.secret_root, ENFORCEMENT_KEY_FILE_NAME
    )
    assert original_key.key_id == graph.harness.signing_key.key_id
