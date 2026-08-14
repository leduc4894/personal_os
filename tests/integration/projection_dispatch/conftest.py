"""Disposable PostgreSQL 18.4 stack fixtures for projection-dispatch integration tests.

The module-scoped stack fixture follows the canonical-postgresql-baseline
convention: it validates the bounded ``knowledge-ci-*`` project identity and
the ``CI`` guard, provisions the disposable local stack through
``tools.local_service_stack`` (reset -> bootstrap -> config -> up), applies the
real Alembic baseline with a sanitized child environment, and in a ``finally``
resets ONLY the named disposable project and asserts no labelled resource
remains. The function-scoped harness fixture builds the real async engine and
a :class:`PostgresqlProjectionIntentStore` per test with injected UUIDv7 lease
tokens, a recording diagnostic sink and the in-memory metrics recorder, and
seeds canonical intent graphs through schema-qualified, parameter-bound Core
statements.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess
import sys
from asyncio import AbstractEventLoop
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from personal_os.diagnostics.events import EventName
from personal_os.sources.metrics import InMemorySourcePublicationMetrics

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from tools.local_service_stack import main as stack_main
from tools.local_service_stack import validate_project_name

from postgresql_source_store.engine import create_source_store_engine, dispose_source_store_engine
from postgresql_source_store.projection_intents import (
    PostgresqlProjectionIntentStore,
    ProjectionDiagnosticSink,
)
from postgresql_source_store.settings import (
    DatabaseRuntimeSettings,
    load_database_runtime_settings,
)
from postgresql_source_store.tables import (
    content_objects,
    projection_intents,
    source_versions,
    sources,
    sync_events,
    users,
    workspaces,
)

_WORKTREE_ROOT: Path = Path(__file__).resolve().parents[3]
_SECRET_ROOT: Path = (_WORKTREE_ROOT / ".local" / "stack-secrets").resolve()


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
class ProjectionDispatchStack:
    """Provisioned disposable stack: project name, port and engine inputs."""

    project_name: str
    port: int
    settings: DatabaseRuntimeSettings
    password: SecretStr


@pytest.fixture(scope="module")
def projection_dispatch_stack() -> Iterator[ProjectionDispatchStack]:
    project_name = _require_project_name()
    port = _resolved_host_port()
    _run_stack_steps(project_name)
    environment = _build_sanitized_environment(port)
    _run_alembic_upgrade_head(environment)
    settings = load_database_runtime_settings(environ=environment)
    password = SecretStr(_read_application_password())
    try:
        yield ProjectionDispatchStack(
            project_name=project_name, port=port, settings=settings, password=password
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


@dataclass
class RecordingProjectionDiagnostics:
    """Recording structural diagnostic sink retaining only accepted payloads."""

    events: list[tuple[EventName, dict[str, Any]]] = field(default_factory=list)

    def emit(self, event_name: EventName, fields: dict[str, Any] | None = None) -> None:
        self.events.append((event_name, dict(fields or {})))

    def of(self, event_name: EventName) -> list[dict[str, Any]]:
        return [fields for recorded_name, fields in self.events if recorded_name is event_name]


@dataclass(frozen=True, slots=True)
class SeededWorkspace:
    """Canonical workspace graph: owner user and workspace."""

    owner_user_id: UUID
    workspace_id: UUID


@dataclass(frozen=True, slots=True)
class SeededIntent:
    """One committed pending intent row and its canonical graph identities."""

    projection_intent_id: UUID
    workspace_id: UUID
    source_id: UUID
    event_id: UUID
    source_version_id: UUID


class ProjectionDispatchHarness:
    """Seed and inspection helpers bound to one test's engine and store."""

    def __init__(
        self,
        engine: AsyncEngine,
        store: PostgresqlProjectionIntentStore,
        diagnostics: RecordingProjectionDiagnostics,
        metrics: InMemorySourcePublicationMetrics,
    ) -> None:
        self._engine = engine
        self._store = store
        self.diagnostics = diagnostics
        self.metrics = metrics

    @property
    def store(self) -> PostgresqlProjectionIntentStore:
        return self._store

    def competing_store(self) -> PostgresqlProjectionIntentStore:
        """A second claimer over the same engine with its own injected seams."""
        return PostgresqlProjectionIntentStore(
            self._engine,
            lease_token_generator=uuid4,
            diagnostics=self.diagnostics,
            metrics=self.metrics,
        )

    async def database_now(self) -> datetime:
        """The database clock reading, used as the injected ``now`` in tests."""
        async with self._engine.connect() as connection:
            result = await connection.execute(sa.text("SELECT CURRENT_TIMESTAMP"))
            value = result.scalar_one()
        assert isinstance(value, datetime)
        return value

    async def seed_workspace(self) -> SeededWorkspace:
        owner_user_id = uuid4()
        workspace_id = uuid4()
        nonce = uuid4().hex
        async with self._engine.begin() as connection:
            await connection.execute(
                sa.insert(users).values(
                    user_id=owner_user_id,
                    username=f"owner-{nonce}",
                    display_name="Projection Dispatch Owner",
                )
            )
            await connection.execute(
                sa.insert(workspaces).values(
                    workspace_id=workspace_id,
                    owner_user_id=owner_user_id,
                    workspace_key=f"ws-{nonce[:12]}",
                    display_name="Projection Dispatch Workspace",
                )
            )
        return SeededWorkspace(owner_user_id=owner_user_id, workspace_id=workspace_id)

    async def seed_due_intent(
        self,
        workspace: SeededWorkspace,
        *,
        projection_kind: str = "qdrant",
        available_at_sql: str = "CURRENT_TIMESTAMP - interval '1 second'",
        created_at_sql: str | None = None,
        attempt_count: int = 0,
    ) -> SeededIntent:
        """Seed one committed pending intent with its full canonical graph.

        Every value is parameter-bound or a fixed SQL interval expression; the
        row satisfies every baseline CHECK constraint directly.
        """
        source_id = uuid4()
        event_id = uuid4()
        source_version_id = uuid4()
        content_object_id = uuid4()
        projection_intent_id = uuid4()
        nonce = uuid4().hex
        content_hash = hashlib.sha256(nonce.encode("utf-8")).hexdigest()
        async with self._engine.begin() as connection:
            await connection.execute(
                sa.insert(content_objects).values(
                    content_object_id=content_object_id,
                    content_hash=content_hash,
                    object_key=f"objects/sha256/{content_hash[:2]}/{content_hash[2:4]}/{content_hash}",
                    byte_size=len(nonce),
                    media_type="text/markdown",
                    verified_at=sa.text("CURRENT_TIMESTAMP - interval '1 second'"),
                )
            )
            await connection.execute(
                sa.insert(sources).values(
                    source_id=source_id,
                    workspace_id=workspace.workspace_id,
                    source_type="markdown",
                    title=f"Projection Source {nonce[:8]}",
                )
            )
            await connection.execute(
                sa.insert(source_versions).values(
                    source_version_id=source_version_id,
                    workspace_id=workspace.workspace_id,
                    source_id=source_id,
                    content_object_id=content_object_id,
                    content_version=1,
                    author_kind="user",
                    author_id=workspace.owner_user_id,
                )
            )
            await connection.execute(
                sa.insert(sync_events).values(
                    event_id=event_id,
                    workspace_id=workspace.workspace_id,
                    source_id=source_id,
                    committed_version_id=source_version_id,
                    idempotency_key=f"projection-{nonce}",
                    request_fingerprint=content_hash,
                    event_type="create",
                )
            )
            intent_values: dict[str, Any] = {
                "projection_intent_id": projection_intent_id,
                "workspace_id": workspace.workspace_id,
                "event_id": event_id,
                "source_id": source_id,
                "source_version_id": source_version_id,
                "projection_kind": projection_kind,
                "operation": "upsert",
                "status": "pending",
                "attempt_count": attempt_count,
                "available_at": sa.text(available_at_sql),
            }
            if created_at_sql is not None:
                intent_values["created_at"] = sa.text(created_at_sql)
            else:
                # The timestamps CHECKs require available_at >= created_at and
                # updated_at >= created_at, so creation never follows the
                # availability expression, whichever direction it shifts.
                intent_values["created_at"] = sa.text(
                    f"LEAST(CURRENT_TIMESTAMP, {available_at_sql})"
                )
            await connection.execute(sa.insert(projection_intents).values(**intent_values))
        return SeededIntent(
            projection_intent_id=projection_intent_id,
            workspace_id=workspace.workspace_id,
            source_id=source_id,
            event_id=event_id,
            source_version_id=source_version_id,
        )

    async def expire_lease(self, projection_intent_id: UUID) -> None:
        """Move a leased row's expiry into the past, preserving the CHECKs.

        The creation and availability times are backdated together so the
        shifted ``updated_at`` still satisfies ``updated_at >= created_at``
        while ``leased_until`` lands beyond the reclaim horizon.
        """
        async with self._engine.begin() as connection:
            await connection.execute(
                sa.update(projection_intents)
                .values(
                    created_at=sa.text("CURRENT_TIMESTAMP - interval '80 seconds'"),
                    available_at=sa.text("CURRENT_TIMESTAMP - interval '80 seconds'"),
                    leased_until=sa.text("CURRENT_TIMESTAMP - interval '5 seconds'"),
                    updated_at=sa.text("CURRENT_TIMESTAMP - interval '70 seconds'"),
                )
                .where(projection_intents.c.projection_intent_id == projection_intent_id)
            )

    async def fetch_intent(self, projection_intent_id: UUID) -> dict[str, Any]:
        statement = sa.select(
            projection_intents.c.status,
            projection_intents.c.attempt_count,
            projection_intents.c.available_at,
            projection_intents.c.lease_token,
            projection_intents.c.leased_until,
            projection_intents.c.dispatched_at,
            projection_intents.c.last_error_code,
            projection_intents.c.updated_at,
        ).where(projection_intents.c.projection_intent_id == projection_intent_id)
        async with self._engine.connect() as connection:
            row = (await connection.execute(statement)).one()
        return dict(row._mapping)


def _lease_token_generator() -> Callable[[], UUID]:
    """Injected UUID generator producing one fresh token per lease write."""
    return uuid4


@pytest_asyncio.fixture
async def projection_dispatch_harness(
    projection_dispatch_stack: ProjectionDispatchStack,
) -> Iterator[ProjectionDispatchHarness]:
    engine = create_source_store_engine(
        projection_dispatch_stack.settings, projection_dispatch_stack.password
    )
    diagnostics = RecordingProjectionDiagnostics()
    metrics = InMemorySourcePublicationMetrics()
    store = PostgresqlProjectionIntentStore(
        engine,
        lease_token_generator=_lease_token_generator(),
        diagnostics=diagnostics,
        metrics=metrics,
    )
    harness = ProjectionDispatchHarness(engine, store, diagnostics, metrics)
    try:
        # Claim and reclaim select globally: earlier tests' intent rows must
        # not leak into this test's claim batches.
        async with engine.begin() as connection:
            await connection.execute(projection_intents.delete())
        yield harness
    finally:
        await dispose_source_store_engine(engine)


__all__ = [
    "ProjectionDiagnosticSink",
    "ProjectionDispatchHarness",
    "RecordingProjectionDiagnostics",
    "SeededIntent",
    "SeededWorkspace",
    "projection_dispatch_harness",
]
