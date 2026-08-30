"""Disposable PostgreSQL 18.4 stack fixtures for canonical-core integration tests.

The module-scoped stack fixture follows the canonical-postgresql-baseline
convention shared with the ``source_publication`` and ``projection_dispatch``
conftests: it validates the bounded ``knowledge-ci-*`` project identity and the
``CI`` guard, provisions the disposable local stack through
``tools.local_service_stack`` (reset -> bootstrap -> config -> up), applies the
real Alembic baseline with a sanitized child environment, and in a ``finally``
resets ONLY the named disposable project and asserts no labelled resource
remains.

Beyond the shared baseline, this conftest provisions the disposable restore
database (``knowledge_ci_restore_<nonce>``) on the stack's PostgreSQL instance
through ``docker compose exec`` with the stack-bootstrap superuser credential,
provides a local-filesystem fake implementing the
:class:`~personal_os.object_storage.CanonicalObjectStore` port (standing in for
the live R2 object store, which Task 14's live suite proves), and binds the
real recovery composition (snapshot store, filesystem bundle store, bounded
``pg_dump``/``pg_restore`` process adapter) for the integration flows.

On Windows the suite runs on a selector event loop (psycopg async cannot use
the proactor loop) while asyncio subprocesses need the proactor loop; the
bounded child runner therefore executes on a loop owned by a worker thread,
preserving the production argv, environment, passfile and timeout semantics.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from asyncio import AbstractEventLoop
from collections.abc import AsyncIterable, AsyncIterator, Callable, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

# Registers the live R2 harness fixture (fail-closed credential gating plus
# per-run exact-key cleanup) for the Task 14 live acceptance drills in this
# directory; every offline test here leaves the fixture uninstantiated.
from api_runtime.exclusion_policy_crypto import TrustAnchorEd25519Verifier
from tests.integration.r2_object_storage.conftest import live_r2_harness  # noqa: F401
from tools.local_service_stack import main as stack_main
from tools.local_service_stack import validate_project_name
from tools.postgresql_dump_process import (
    PostgresqlDumpProcessAdapter,
    ProcessRunResult,
    check_client_tools,
    run_bounded_child,
)
from tools.signed_policy_seed import seed_signed_policy

from personal_os.diagnostics.context import DiagnosticContext, create_diagnostic_context
from personal_os.diagnostics.events import EventName
from personal_os.error_contracts.codes import ErrorCode
from personal_os.exclusion_policy.metrics import InMemoryExclusionPolicyMetrics
from personal_os.object_storage import (
    CanonicalMediaType,
    CanonicalObjectStore,
    ContentDigest,
    ExpectedObject,
    VerificationMethod,
    VerifiedObjectReceipt,
    derive_canonical_object_key,
)
from personal_os.object_storage.errors import (
    DIGEST_MISMATCH,
    SIZE_MISMATCH,
    ObjectStorageError,
)
from personal_os.recovery.bundle import FilesystemRecoveryBundleStore
from personal_os.recovery.contracts import (
    InMemoryCanonicalBackupMetrics,
    RecoveryEnvironment,
    RecoveryError,
)
from personal_os.recovery.ports import PostgresqlConnectionTarget
from personal_os.recovery.service import RecoveryService
from personal_os.sources.actors import ActorKind, SourceActor
from personal_os.sources.commands import (
    CreateSourceVersion,
    IdempotencyKey,
    SourceTitle,
    SourceType,
)
from personal_os.sources.metrics import (
    InMemoryCanonicalReadMetrics,
    InMemorySourcePublicationMetrics,
)
from personal_os.sources.publication import SourceVersionPublicationService
from personal_os.sources.reading import CanonicalSourceReadService
from personal_os.sources.results import SourceVersionPublicationResult
from postgresql_source_store.backup_snapshot import (
    PostgresqlBackupSnapshotStore,
    PostgresqlRestoreTarget,
)
from postgresql_source_store.canonical_read import PostgresqlCanonicalSourceReadStore
from postgresql_source_store.engine import create_source_store_engine, dispose_source_store_engine
from postgresql_source_store.identity_bootstrap import PostgresqlIdentityBootstrapStore
from postgresql_source_store.policy_enforcement import compose_policy_enforcement
from postgresql_source_store.publication_store import PostgresqlSourcePublicationStore
from postgresql_source_store.settings import (
    DatabaseRuntimeSettings,
    load_database_runtime_settings,
)
from postgresql_source_store.tables import (
    SOURCE_STORE_TABLES,
    devices,
    users,
    workspace_policy_state,
    workspaces,
)

_WORKTREE_ROOT: Path = Path(__file__).resolve().parents[3]
_SECRET_ROOT: Path = (_WORKTREE_ROOT / ".local" / "stack-secrets").resolve()
_COMPOSE_FILE: Path = _WORKTREE_ROOT / "infra" / "compose" / "compose.yaml"

_POSTGRESQL_SERVICE_NAME: str = "postgresql"
_SUPERUSER_NAME: str = "stack_admin"
_SUPERUSER_PASSWORD_FILENAME: str = "postgres_admin_password"
_DOCKER_EXEC_TIMEOUT_SECONDS: float = 120.0


def pytest_asyncio_loop_factories(
    config: pytest.Config, item: pytest.Item
) -> dict[str, Callable[[], AbstractEventLoop]]:
    """Run every asyncio test and fixture on a selector event loop.

    psycopg async cannot run on the Windows proactor loop, and SelectorEventLoop
    is already the default loop on the Linux CI integration runs.
    """
    del config, item
    return {"selector": asyncio.SelectorEventLoop}


_APPLICATION_DATABASE: str = "knowledge"
_APPLICATION_USER: str = "knowledge_app"
_DATABASE_HOST: str = "127.0.0.1"
_SSL_MODE: str = "disable"
_APPLICATION_PASSWORD_FILENAME: str = "postgres_application_password"


def _require_project_name() -> str:
    raw_name = os.environ.get("LOCAL_STACK_TEST_PROJECT")
    if raw_name is None:
        pytest.fail("LOCAL_STACK_TEST_PROJECT must name a unique knowledge-ci-* project")
    validate_project_name(raw_name)
    if not raw_name.startswith("knowledge-ci-"):
        pytest.fail("LOCAL_STACK_TEST_PROJECT must start with 'knowledge-ci-'")
    if raw_name == "knowledge-local":
        pytest.fail("LOCAL_STACK_TEST_PROJECT must not be the operator 'knowledge-local' project")
    if os.environ.get("CI") != "true":
        pytest.fail("CI must be 'true' to operate a disposable knowledge-ci-* stack")
    return raw_name


def _resolved_host_port() -> int:
    return int(os.environ.get("POSTGRES_PORT", "5432"))


def _read_application_password() -> str:
    resolved_root = _SECRET_ROOT.resolve(strict=True)
    secret_path = (resolved_root / _APPLICATION_PASSWORD_FILENAME).resolve(strict=True)
    if not secret_path.is_relative_to(resolved_root):
        pytest.fail("application password must resolve beneath the bounded secret root")
    return secret_path.read_text(encoding="ascii").strip()


def _build_sanitized_environment(port: int) -> dict[str, str]:
    environment = dict(os.environ)
    for inherited_key in [key for key in environment if key.startswith("KNOWLEDGE_")]:
        del environment[inherited_key]
    environment.update(
        {
            "KNOWLEDGE_ENVIRONMENT": "test",
            "KNOWLEDGE_SECRET_ROOT": str(_SECRET_ROOT),
            "KNOWLEDGE_DATABASE_HOST": _DATABASE_HOST,
            "KNOWLEDGE_DATABASE_PORT": str(port),
            "KNOWLEDGE_DATABASE_NAME": _APPLICATION_DATABASE,
            "KNOWLEDGE_DATABASE_USER": _APPLICATION_USER,
            "KNOWLEDGE_DATABASE_PASSWORD_FILE": _APPLICATION_PASSWORD_FILENAME,
            "KNOWLEDGE_DATABASE_SSL_MODE": _SSL_MODE,
        }
    )
    return environment


def _run_stack_steps(project_name: str) -> None:
    steps: tuple[tuple[str, ...], ...] = (
        (
            "reset",
            "--project-name",
            project_name,
            "--confirm-project",
            project_name,
            "--non-interactive",
        ),
        ("bootstrap", "--project-name", project_name),
        ("config", "--project-name", project_name),
        ("up", "--project-name", project_name),
    )
    for argv in steps:
        return_code = stack_main(list(argv))
        assert return_code == 0, f"local-stack step '{argv[0]}' failed with code {return_code}"


def _run_alembic_upgrade_head(environment: dict[str, str]) -> None:
    result = subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        cwd=str(_WORKTREE_ROOT),
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, "alembic upgrade head failed for the disposable stack"


def _count_project_resources(project_name: str) -> dict[str, int]:
    label = f"label=com.docker.compose.project={project_name}"
    commands: dict[str, list[str]] = {
        "container": ["docker", "container", "ls", "--all", "--quiet", "--filter", label],
        "network": ["docker", "network", "ls", "--quiet", "--filter", label],
        "volume": ["docker", "volume", "ls", "--quiet", "--filter", label],
    }
    counts: dict[str, int] = {}
    for resource, command in commands.items():
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        counts[resource] = len(lines)
    return counts


def _assert_project_absent(project_name: str) -> None:
    counts = _count_project_resources(project_name)
    leftover = {resource: count for resource, count in counts.items() if count}
    assert not leftover, f"disposable project left resources behind: {leftover}"


@dataclass(frozen=True, slots=True)
class CanonicalCoreStack:
    """Provisioned disposable stack: project name, port and engine inputs."""

    project_name: str
    port: int
    settings: DatabaseRuntimeSettings
    password: SecretStr
    environment: dict[str, str]
    main_target: PostgresqlConnectionTarget


@pytest.fixture(scope="module")
def canonical_core_stack() -> Iterator[CanonicalCoreStack]:
    project_name = _require_project_name()
    port = _resolved_host_port()
    _run_stack_steps(project_name)
    environment = _build_sanitized_environment(port)
    _run_alembic_upgrade_head(environment)
    settings = load_database_runtime_settings(environ=environment)
    password = SecretStr(_read_application_password())
    try:
        yield CanonicalCoreStack(
            project_name=project_name,
            port=port,
            settings=settings,
            password=password,
            environment=environment,
            main_target=PostgresqlConnectionTarget(
                host=_DATABASE_HOST,
                port=port,
                database=_APPLICATION_DATABASE,
                user=_APPLICATION_USER,
            ),
        )
    finally:
        try:
            stack_main(
                [
                    "reset",
                    "--project-name",
                    project_name,
                    "--confirm-project",
                    project_name,
                    "--non-interactive",
                ]
            )
        finally:
            _assert_project_absent(project_name)


# --- disposable restore database ----------------------------------------------------


def _compose_exec_psql(project_name: str, sql_statement: str, success_marker: str) -> None:
    """Run one superuser SQL statement inside the stack's PostgreSQL container.

    The statement travels through ``docker compose exec`` with the stack's own
    bootstrap superuser credential read from the mounted secret; the credential
    never leaves the container shell and only the fixed success marker is
    asserted on stdout.
    """
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


@dataclass(frozen=True, slots=True)
class DisposableRestoreDatabase:
    """One per-run restore database on the disposable PostgreSQL instance."""

    database_name: str
    settings: DatabaseRuntimeSettings
    connection_target: PostgresqlConnectionTarget


@pytest.fixture
def disposable_restore_database(
    canonical_core_stack: CanonicalCoreStack,
) -> Iterator[DisposableRestoreDatabase]:
    database_name = f"knowledge_ci_restore_{uuid4().hex[:12]}"
    _compose_exec_psql(
        canonical_core_stack.project_name,
        f'CREATE DATABASE "{database_name}" OWNER {_APPLICATION_USER}',
        "restore_database_created",
    )
    settings = canonical_core_stack.settings.model_copy(update={"database_name": database_name})
    target = PostgresqlConnectionTarget(
        host=_DATABASE_HOST,
        port=canonical_core_stack.port,
        database=database_name,
        user=_APPLICATION_USER,
    )
    try:
        yield DisposableRestoreDatabase(
            database_name=database_name, settings=settings, connection_target=target
        )
    finally:
        _compose_exec_psql(
            canonical_core_stack.project_name,
            f'DROP DATABASE "{database_name}" WITH (FORCE)',
            "restore_database_dropped",
        )


# --- fake canonical object store ----------------------------------------------------


class _ByteChunkReader:
    """Bounded reader over already-verified in-memory canonical object bytes."""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self._offset = 0

    async def read(self, size_bytes: int = 1_048_576) -> bytes:
        if size_bytes < 0 or size_bytes > 1_048_576:
            raise ValueError("read size must be between 0 and 1 MiB")
        chunk = self._payload[self._offset : self._offset + size_bytes]
        self._offset += len(chunk)
        return chunk

    def __aiter__(self) -> _ByteChunkReader:
        return self

    async def __anext__(self) -> bytes:
        chunk = await self.read()
        if not chunk:
            raise StopAsyncIteration
        return chunk


class LocalFilesystemObjectStore:
    """Fake canonical object store backed by local files, standing in for R2.

    Implements the full :class:`CanonicalObjectStore` port with the same
    fail-closed verification contract: a receipt is issued only after the full
    size and digest of the stored bytes match the claim, existing objects are
    re-verified on every read and every re-store (so a same-digest
    different-media-type restore is a metadata conflict, never a silent pass),
    and existing keys are never overwritten.

    All state lives on disk beneath one root, so successive instances over the
    same root behave like one durable content-addressed store — mirroring how
    the canonical graph accumulates referenced objects across the module.
    """

    def __init__(self, root: Path) -> None:
        self._root = root

    def _object_path(self, digest_hexadecimal: str) -> Path:
        return self._root / digest_hexadecimal

    def _media_path(self, digest_hexadecimal: str) -> Path:
        return self._root / f"{digest_hexadecimal}.media"

    async def resolve_verified_object(
        self, expected: ExpectedObject
    ) -> VerifiedObjectReceipt | None:
        if not self._object_path(expected.content_digest.hexadecimal).exists():
            return None
        return await self.verify_existing_object(expected)

    async def store_stream(
        self,
        stream: AsyncIterable[bytes],
        expected_size_bytes: int,
        media_type: str,
        claimed_sha256: str | None = None,
    ) -> VerifiedObjectReceipt:
        chunks: list[bytes] = []
        total_bytes = 0
        async for chunk in stream:
            chunks.append(chunk)
            total_bytes += len(chunk)
        payload = b"".join(chunks)
        if total_bytes != expected_size_bytes:
            raise ObjectStorageError(
                ErrorCode.OBJECT_STORAGE_INPUT_INVALID, safe_details={"reason": SIZE_MISMATCH}
            )
        digest_hexadecimal = hashlib.sha256(payload).hexdigest()
        if claimed_sha256 is not None and digest_hexadecimal != claimed_sha256:
            raise ObjectStorageError(
                ErrorCode.OBJECT_STORAGE_INPUT_INVALID, safe_details={"reason": DIGEST_MISMATCH}
            )
        stored_media_type = CanonicalMediaType.parse(media_type)
        digest = ContentDigest.parse(digest_hexadecimal)
        object_path = self._object_path(digest_hexadecimal)
        already_present = object_path.exists()
        if not already_present:
            object_path.write_bytes(payload)
            self._media_path(digest_hexadecimal).write_text(
                stored_media_type.value, encoding="ascii"
            )
        else:
            # An existing digest re-verifies exactly like the production
            # conditional-create path: same digest under a different media
            # type is a metadata conflict and corrupted bytes fail integrity —
            # the existing key is never overwritten.
            return await self.verify_existing_object(
                ExpectedObject(
                    content_digest=digest,
                    size_bytes=total_bytes,
                    media_type=stored_media_type,
                )
            )
        return self._receipt(
            digest=digest,
            size_bytes=total_bytes,
            media_type=stored_media_type,
            method=VerificationMethod.UPLOADED_FULL_READ,
        )

    async def verify_existing_object(self, expected: ExpectedObject) -> VerifiedObjectReceipt:
        digest_hexadecimal = expected.content_digest.hexadecimal
        object_path = self._object_path(digest_hexadecimal)
        if not object_path.exists():
            raise ObjectStorageError(ErrorCode.OBJECT_STORAGE_OBJECT_MISSING)
        payload = object_path.read_bytes()
        if (
            hashlib.sha256(payload).hexdigest() != digest_hexadecimal
            or len(payload) != expected.size_bytes
        ):
            raise ObjectStorageError(ErrorCode.OBJECT_STORAGE_INTEGRITY_FAILED)
        stored_media_type = self._media_path(digest_hexadecimal).read_text(encoding="ascii")
        if stored_media_type != expected.media_type.value:
            raise ObjectStorageError(ErrorCode.OBJECT_STORAGE_METADATA_CONFLICT)
        return self._receipt(
            digest=expected.content_digest,
            size_bytes=expected.size_bytes,
            media_type=expected.media_type,
            method=VerificationMethod.EXISTING_FULL_READ,
        )

    @asynccontextmanager
    async def open_verified_reader(
        self, expected: ExpectedObject
    ) -> AsyncIterator[_ByteChunkReader]:
        await self.verify_existing_object(expected)
        yield _ByteChunkReader(self._object_path(expected.content_digest.hexadecimal).read_bytes())

    @staticmethod
    def _receipt(
        *,
        digest: ContentDigest,
        size_bytes: int,
        media_type: CanonicalMediaType,
        method: VerificationMethod,
    ) -> VerifiedObjectReceipt:
        return VerifiedObjectReceipt(
            content_digest=digest,
            object_key=derive_canonical_object_key(digest),
            size_bytes=size_bytes,
            media_type=media_type,
            verified_at=datetime.now(UTC),
            verification_method=method,
        )


# --- short-lived temp roots (Windows MAX_PATH guard) ---------------------------------


def _make_short_lived_temp_root(prefix: str) -> Path:
    """A short temporary root; pytest's default tree is too deep on Windows."""
    return Path(tempfile.mkdtemp(prefix=prefix))


@pytest.fixture
def bundle_root() -> Iterator[Path]:
    root = _make_short_lived_temp_root("recovery-bundle-")
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture(scope="module")
def object_store_root() -> Iterator[Path]:
    """One module-scoped object-store root: referenced objects accumulate like R2."""
    root = _make_short_lived_temp_root("object-store-")
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


# --- bounded child execution off the selector loop -----------------------------------


async def run_bounded_child_on_worker_loop(
    argv: Sequence[str], *, env: Mapping[str, str], timeout_seconds: float
) -> ProcessRunResult:
    """Run the production bounded child runner on a worker thread's loop.

    Windows cannot host psycopg async (selector-only) and asyncio subprocesses
    (proactor-only) on one event loop. The recovery integration suite runs on
    the selector loop, so the real :func:`run_bounded_child` — with its exact
    argv, sanitized environment and timeout semantics — executes on a fresh
    loop owned by a worker thread (the default proactor policy on Windows).
    """

    def execute_in_worker() -> ProcessRunResult:
        return asyncio.run(run_bounded_child(list(argv), env=env, timeout_seconds=timeout_seconds))

    return await asyncio.to_thread(execute_in_worker)


@pytest_asyncio.fixture
async def dump_process(canonical_core_stack: CanonicalCoreStack) -> PostgresqlDumpProcessAdapter:
    try:
        await check_client_tools("pg_dump", "pg_restore", runner=run_bounded_child_on_worker_loop)
    except RecoveryError:
        pytest.fail(
            "pg_dump and pg_restore client tools at exactly version 18.4 must be on PATH "
            "for the recovery integration suite (never skipped)"
        )
    return PostgresqlDumpProcessAdapter(
        "pg_dump",
        "pg_restore",
        password=canonical_core_stack.password,
        runner=run_bounded_child_on_worker_loop,
    )


# --- disposable per-test identity database -------------------------------------------


@dataclass(frozen=True, slots=True)
class DisposableIdentityDatabase:
    """One pristine migrated database and its bound harness for one test."""

    database_name: str
    harness: CanonicalCoreHarness


@pytest_asyncio.fixture
async def disposable_identity_database(
    canonical_core_stack: CanonicalCoreStack, object_store_root: Path
) -> AsyncIterator[DisposableIdentityDatabase]:
    """A freshly migrated database per identity test.

    Identity bootstrap classifies the whole users/workspaces/devices graph, so
    every case needs globally empty identity tables; and ``audit_events`` rows
    (append-only by migration trigger) FK-RESTRICT the workspaces they name,
    so the graph cannot be reset by deletion. Each case therefore runs against
    its own disposable database created through the stack's PostgreSQL
    container and migrated with the real Alembic baseline.
    """
    database_name = f"knowledge_ci_identity_{uuid4().hex[:12]}"
    _compose_exec_psql(
        canonical_core_stack.project_name,
        f'CREATE DATABASE "{database_name}" OWNER {_APPLICATION_USER}',
        "identity_database_created",
    )
    environment = dict(canonical_core_stack.environment)
    environment["KNOWLEDGE_DATABASE_NAME"] = database_name
    try:
        _run_alembic_upgrade_head(environment)
        settings = canonical_core_stack.settings.model_copy(update={"database_name": database_name})
        engine = create_source_store_engine(settings, canonical_core_stack.password)
        try:
            yield DisposableIdentityDatabase(
                database_name=database_name,
                harness=CanonicalCoreHarness(engine, LocalFilesystemObjectStore(object_store_root)),
            )
        finally:
            await dispose_source_store_engine(engine)
    finally:
        _compose_exec_psql(
            canonical_core_stack.project_name,
            f'DROP DATABASE "{database_name}" WITH (FORCE)',
            "identity_database_dropped",
        )


# --- canonical-core harness ---------------------------------------------------------


@dataclass
class RecordingIdentityDiagnostics:
    """Recording structural diagnostic sink retaining only accepted payloads."""

    events: list[tuple[EventName, dict[str, object]]]

    def emit(self, event_name: EventName, fields: Mapping[str, object] | None = None) -> None:
        self.events.append((event_name, dict(fields or {})))

    def of(self, event_name: EventName) -> list[dict[str, object]]:
        return [fields for recorded_name, fields in self.events if recorded_name is event_name]


@dataclass(frozen=True, slots=True)
class SeededWorkspace:
    """Canonical workspace graph: owner user, workspace and active device."""

    owner_user_id: UUID
    workspace_id: UUID
    device_id: UUID


@dataclass(frozen=True, slots=True)
class PublishedSource:
    """One published source version with its command, result and exact bytes."""

    command: CreateSourceVersion
    result: SourceVersionPublicationResult
    payload: bytes


def single_chunk_stream(payload: bytes) -> AsyncIterator[bytes]:
    """One-shot async byte stream for a publication command."""

    async def stream() -> AsyncIterator[bytes]:
        yield payload

    return stream()


class CanonicalCoreHarness:
    """Engine-bound stores, services and seeding helpers for one test.

    The object store binds through the :class:`CanonicalObjectStore` port —
    the local-filesystem fake by default, the live R2 adapter for the live
    acceptance drills via :meth:`with_object_store` (which shares this
    harness's single engine instead of stacking a second one).
    """

    def __init__(self, engine: AsyncEngine, object_store: CanonicalObjectStore) -> None:
        self._engine = engine
        self.object_store = object_store
        policy_verifier = TrustAnchorEd25519Verifier()
        policy_metrics = InMemoryExclusionPolicyMetrics()
        self.publication_service = SourceVersionPublicationService(
            store=PostgresqlSourcePublicationStore(
                engine,
                policy_verifier=policy_verifier,
                policy_metrics=policy_metrics,
            ),
            object_store=object_store,
            metrics=InMemorySourcePublicationMetrics(),
            clock=lambda: datetime.now(UTC),
            policy_guard=compose_policy_enforcement(
                engine, verifier=policy_verifier, metrics=policy_metrics
            ),
        )
        self.read_service = CanonicalSourceReadService(
            store=PostgresqlCanonicalSourceReadStore(
                engine,
                policy_verifier=policy_verifier,
                policy_metrics=policy_metrics,
            ),
            object_store=object_store,
            metrics=InMemoryCanonicalReadMetrics(),
            policy_guard=compose_policy_enforcement(
                engine, verifier=policy_verifier, metrics=policy_metrics
            ),
        )
        self.identity_diagnostics = RecordingIdentityDiagnostics(events=[])
        self.identity_store = PostgresqlIdentityBootstrapStore(
            engine, diagnostics=self.identity_diagnostics
        )
        self.snapshot_store = PostgresqlBackupSnapshotStore(engine)

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    def with_object_store(self, object_store: CanonicalObjectStore) -> CanonicalCoreHarness:
        """A harness over the same engine with every service bound to ``object_store``."""
        return CanonicalCoreHarness(self._engine, object_store)

    async def seed_workspace(self) -> SeededWorkspace:
        owner_user_id = uuid4()
        workspace_id = uuid4()
        device_id = uuid4()
        nonce = uuid4().hex
        async with self._engine.begin() as connection:
            await connection.execute(
                sa.insert(users).values(
                    user_id=owner_user_id,
                    username=f"owner-{nonce}",
                    display_name="Canonical Core Owner",
                )
            )
            await connection.execute(
                sa.insert(workspaces).values(
                    workspace_id=workspace_id,
                    owner_user_id=owner_user_id,
                    workspace_key=f"ws-{nonce[:12]}",
                    display_name="Canonical Core Workspace",
                )
            )
            await connection.execute(
                sa.insert(devices).values(
                    device_id=device_id,
                    workspace_id=workspace_id,
                    user_id=owner_user_id,
                    device_name="Canonical Core Device",
                    device_kind="obsidian",
                )
            )
            await connection.execute(
                sa.insert(workspace_policy_state).values(
                    workspace_id=workspace_id,
                    active_policy_revision_id=None,
                    active_revision_number=0,
                )
            )
        # Task 11: the harness explicitly seeds a signed empty policy per
        # workspace (spec 14) so the guarded services can publish and read.
        await seed_signed_policy(
            self._engine,
            workspace_id=workspace_id,
            published_by_user_id=owner_user_id,
        )
        return SeededWorkspace(
            owner_user_id=owner_user_id, workspace_id=workspace_id, device_id=device_id
        )

    async def insert_bare_user(self, username: str, display_name: str) -> UUID:
        """Insert one user row with no workspace or device (partial state)."""
        user_id = uuid4()
        async with self._engine.begin() as connection:
            await connection.execute(
                sa.insert(users).values(
                    user_id=user_id,
                    username=username,
                    display_name=display_name,
                    status="active",
                )
            )
        return user_id

    def build_markdown_create_command(
        self,
        workspace: SeededWorkspace,
        claimed_payload: bytes,
        *,
        title: str = "Canonical Core Note",
    ) -> CreateSourceVersion:
        nonce = uuid4().hex
        return CreateSourceVersion(
            workspace_id=workspace.workspace_id,
            source_id=uuid4(),
            event_id=uuid4(),
            idempotency_key=IdempotencyKey(f"canonical-core-{nonce}"),
            source_type=SourceType.MARKDOWN,
            title=SourceTitle(title),
            actor=SourceActor(ActorKind.USER, workspace.owner_user_id),
            expected_object=ExpectedObject(
                content_digest=ContentDigest.parse(hashlib.sha256(claimed_payload).hexdigest()),
                size_bytes=len(claimed_payload),
                media_type=CanonicalMediaType.parse("text/markdown"),
            ),
            client_timestamp=None,
        )

    async def publish_markdown_source(
        self,
        workspace: SeededWorkspace,
        payload: bytes,
        *,
        title: str = "Canonical Core Note",
    ) -> PublishedSource:
        command = self.build_markdown_create_command(workspace, payload, title=title)
        result = await self.publication_service.publish_create(
            command=command,
            stream=single_chunk_stream(payload),
            diagnostic_context=self.diagnostic_context(),
        )
        return PublishedSource(command=command, result=result, payload=payload)

    @staticmethod
    def diagnostic_context() -> DiagnosticContext:
        return create_diagnostic_context().context

    async def table_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        async with self._engine.connect() as connection:
            for table_name, table in SOURCE_STORE_TABLES.items():
                result = await connection.execute(sa.select(sa.func.count()).select_from(table))
                counts[table_name] = int(result.scalar_one())
        return counts

    async def table_rows_as_text(self, table_name: str) -> list[tuple[str, ...]]:
        """Every row of one canonical table, stringified and sorted."""
        table = SOURCE_STORE_TABLES[table_name]
        async with self._engine.connect() as connection:
            rows = (await connection.execute(sa.select(table))).all()
        rendered = sorted(tuple(str(value) for value in row) for row in rows)
        return rendered


@pytest_asyncio.fixture
async def canonical_core_harness(
    canonical_core_stack: CanonicalCoreStack, object_store_root: Path
) -> AsyncIterator[CanonicalCoreHarness]:
    engine = create_source_store_engine(
        canonical_core_stack.settings, canonical_core_stack.password
    )
    harness = CanonicalCoreHarness(engine, LocalFilesystemObjectStore(object_store_root))
    try:
        yield harness
    finally:
        await dispose_source_store_engine(engine)


@pytest_asyncio.fixture
async def recovery_service(
    canonical_core_harness: CanonicalCoreHarness,
    bundle_root: Path,
    dump_process: PostgresqlDumpProcessAdapter,
) -> RecoveryService:
    return RecoveryService(
        snapshot_store=PostgresqlBackupSnapshotStore(canonical_core_harness.engine),
        bundle_store=FilesystemRecoveryBundleStore(bundle_root),
        dump_process=dump_process,
        object_store=canonical_core_harness.object_store,
        metrics=InMemoryCanonicalBackupMetrics(),
        clock=lambda: datetime.now(UTC),
    )


@dataclass(frozen=True, slots=True)
class RestoreTargetContext:
    """Engine, probes and read service bound to one disposable restore database."""

    database: DisposableRestoreDatabase
    engine: AsyncEngine
    restore_target: PostgresqlRestoreTarget
    read_service: CanonicalSourceReadService


@pytest_asyncio.fixture
async def restore_target_context(
    canonical_core_stack: CanonicalCoreStack,
    canonical_core_harness: CanonicalCoreHarness,
    disposable_restore_database: DisposableRestoreDatabase,
) -> AsyncIterator[RestoreTargetContext]:
    engine = create_source_store_engine(
        disposable_restore_database.settings, canonical_core_stack.password
    )
    try:
        policy_verifier = TrustAnchorEd25519Verifier()
        policy_metrics = InMemoryExclusionPolicyMetrics()
        yield RestoreTargetContext(
            database=disposable_restore_database,
            engine=engine,
            restore_target=PostgresqlRestoreTarget(engine),
            read_service=CanonicalSourceReadService(
                store=PostgresqlCanonicalSourceReadStore(
                    engine,
                    policy_verifier=policy_verifier,
                    policy_metrics=policy_metrics,
                ),
                object_store=canonical_core_harness.object_store,
                metrics=InMemoryCanonicalReadMetrics(),
                policy_guard=compose_policy_enforcement(
                    engine, verifier=policy_verifier, metrics=policy_metrics
                ),
            ),
        )
    finally:
        await dispose_source_store_engine(engine)


def recovery_environment() -> RecoveryEnvironment:
    """The only recovery environment the integration suite commands target."""
    return RecoveryEnvironment.TEST


__all__ = [
    "CanonicalCoreHarness",
    "CanonicalCoreStack",
    "DisposableIdentityDatabase",
    "DisposableRestoreDatabase",
    "LocalFilesystemObjectStore",
    "PostgresqlDumpProcessAdapter",
    "PublishedSource",
    "RecordingIdentityDiagnostics",
    "RestoreTargetContext",
    "SeededWorkspace",
    "bundle_root",
    "canonical_core_harness",
    "canonical_core_stack",
    "disposable_identity_database",
    "disposable_restore_database",
    "dump_process",
    "recovery_environment",
    "recovery_service",
    "restore_target_context",
    "run_bounded_child_on_worker_loop",
    "single_chunk_stream",
]
