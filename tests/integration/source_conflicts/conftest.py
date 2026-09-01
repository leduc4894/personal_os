"""Disposable PostgreSQL 18.4 stack fixtures for source-conflict integration tests.

The module-scoped stack fixture follows the source-publication convention: it
validates the bounded ``knowledge-ci-*`` project identity and the ``CI``
guard, provisions the disposable local stack through
``tools.local_service_stack`` (reset -> bootstrap -> config -> up), applies
the real Alembic baseline with a sanitized child environment, and in a
``finally`` resets ONLY the named disposable project and asserts no labelled
resource remains. The function-scoped harness builds the real async engine
and :class:`PostgresqlSourceConflictStore` per test and seeds canonical rows
through schema-qualified, parameter-bound Core statements.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess
import sys
from asyncio import AbstractEventLoop
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from tools.local_service_stack import main as stack_main
from tools.local_service_stack import validate_project_name

from postgresql_source_store.conflict_store import PostgresqlSourceConflictStore
from postgresql_source_store.engine import create_source_store_engine, dispose_source_store_engine
from postgresql_source_store.settings import (
    DatabaseRuntimeSettings,
    load_database_runtime_settings,
)
from postgresql_source_store.tables import (
    SOURCE_STORE_TABLES,
    audit_events,
    content_objects,
    devices,
    source_conflicts,
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
    current = subprocess.run(
        ["uv", "run", "alembic", "current", "--check-heads"],
        cwd=str(_WORKTREE_ROOT),
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert current.returncode == 0, "disposable stack is not at the Alembic head"


def build_conflict_stack_environment(port: int) -> dict[str, str]:
    """Build the sanitized child environment for in-test Alembic invocations.

    The environment names only file-backed secret locations — never a secret
    value — and matches the environment the stack fixture upgraded under.
    """
    return _build_sanitized_environment(port)


def run_alembic_arguments(
    environment: dict[str, str], arguments: tuple[str, ...]
) -> subprocess.CompletedProcess[str]:
    """Run one Alembic CLI invocation against the disposable stack, unchecked."""
    return subprocess.run(
        ["uv", "run", "alembic", *arguments],
        cwd=str(_WORKTREE_ROOT),
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


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
class SourceConflictStack:
    """Provisioned disposable stack: project name, port and engine inputs."""

    project_name: str
    port: int
    settings: DatabaseRuntimeSettings
    password: SecretStr


@pytest.fixture(scope="module")
def source_conflict_stack() -> Iterator[SourceConflictStack]:
    project_name = _require_project_name()
    port = _resolved_host_port()
    _run_stack_steps(project_name)
    environment = _build_sanitized_environment(port)
    _run_alembic_upgrade_head(environment)
    settings = load_database_runtime_settings(environ=environment)
    password = SecretStr(_read_application_password())
    try:
        yield SourceConflictStack(
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


@dataclass(frozen=True, slots=True)
class SeededWorkspace:
    """Canonical workspace graph: owner user, workspace and active device."""

    owner_user_id: UUID
    workspace_id: UUID
    device_id: UUID


@dataclass(frozen=True, slots=True)
class SeededVersion:
    """A committed source version, its per-source ordinal and content object."""

    source_version_id: UUID
    content_version: int
    content_object_id: UUID


class ConflictStoreHarness:
    """Seed and inspection helpers bound to one test's engine and store."""

    def __init__(self, engine: AsyncEngine, store: PostgresqlSourceConflictStore) -> None:
        self._engine = engine
        self._store = store

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    @property
    def store(self) -> PostgresqlSourceConflictStore:
        return self._store

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
                    display_name="Conflict Owner",
                )
            )
            await connection.execute(
                sa.insert(workspaces).values(
                    workspace_id=workspace_id,
                    owner_user_id=owner_user_id,
                    workspace_key=f"ws-{nonce[:12]}",
                    display_name="Conflict Workspace",
                )
            )
            await connection.execute(
                sa.insert(devices).values(
                    device_id=device_id,
                    workspace_id=workspace_id,
                    user_id=owner_user_id,
                    device_name="Conflict Device",
                    device_kind="obsidian",
                )
            )
        return SeededWorkspace(
            owner_user_id=owner_user_id, workspace_id=workspace_id, device_id=device_id
        )

    async def seed_active_source_with_version_one(
        self, *, workspace: SeededWorkspace, source_id: UUID, title: str
    ) -> SeededVersion:
        content_object_id = uuid4()
        source_version_id = uuid4()
        async with self._engine.begin() as connection:
            await self._insert_content_object(
                connection, content_object_id, f"conflict-seed-{source_id}"
            )
            await connection.execute(
                sa.insert(sources).values(
                    source_id=source_id,
                    workspace_id=workspace.workspace_id,
                    source_type="markdown",
                    title=title,
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
                sa.update(sources)
                .values(sync_state="active", current_version_id=source_version_id)
                .where(sources.c.source_id == source_id)
            )
        return SeededVersion(
            source_version_id=source_version_id,
            content_version=1,
            content_object_id=content_object_id,
        )

    async def advance_source_version(
        self, *, workspace: SeededWorkspace, source_id: UUID, parent: SeededVersion
    ) -> SeededVersion:
        content_object_id = uuid4()
        next_version_id = uuid4()
        next_ordinal = parent.content_version + 1
        async with self._engine.begin() as connection:
            await self._insert_content_object(
                connection, content_object_id, f"conflict-advance-{next_version_id}"
            )
            await connection.execute(
                sa.insert(source_versions).values(
                    source_version_id=next_version_id,
                    workspace_id=workspace.workspace_id,
                    source_id=source_id,
                    content_object_id=content_object_id,
                    content_version=next_ordinal,
                    parent_version_id=parent.source_version_id,
                    author_kind="user",
                    author_id=workspace.owner_user_id,
                )
            )
            guarded = await connection.execute(
                sa.update(sources)
                .values(current_version_id=next_version_id)
                .where(
                    sources.c.source_id == source_id,
                    sources.c.current_version_id == parent.source_version_id,
                )
            )
            assert guarded.rowcount == 1, "guarded pointer update must affect exactly one row"
        return SeededVersion(
            source_version_id=next_version_id,
            content_version=next_ordinal,
            content_object_id=content_object_id,
        )

    async def seed_content_object(self, salt: str) -> UUID:
        content_object_id = uuid4()
        async with self._engine.begin() as connection:
            await self._insert_content_object(connection, content_object_id, salt)
        return content_object_id

    async def current_version_id(self, source_id: UUID) -> UUID | None:
        async with self._engine.connect() as connection:
            result = await connection.execute(
                sa.select(sources.c.current_version_id).where(sources.c.source_id == source_id)
            )
            return result.scalar_one()

    async def count_conflicts(self, workspace_id: UUID | None = None) -> int:
        statement = sa.select(sa.func.count()).select_from(source_conflicts)
        if workspace_id is not None:
            statement = statement.where(source_conflicts.c.workspace_id == workspace_id)
        async with self._engine.connect() as connection:
            result = await connection.execute(statement)
            return int(result.scalar_one())

    async def conflict_row(self, conflict_id: UUID) -> Any:
        async with self._engine.connect() as connection:
            result = await connection.execute(
                sa.select(
                    source_conflicts.c.conflict_id,
                    source_conflicts.c.workspace_id,
                    source_conflicts.c.source_id,
                    source_conflicts.c.conflict_kind,
                    source_conflicts.c.status,
                    source_conflicts.c.originating_event_id,
                    source_conflicts.c.base_version_id,
                    source_conflicts.c.observed_remote_version_id,
                    source_conflicts.c.candidate_kind,
                    source_conflicts.c.verified_candidate_object_id,
                    source_conflicts.c.normalized_locator,
                    source_conflicts.c.resolution_kind,
                    source_conflicts.c.resolution_event_id,
                    source_conflicts.c.resulting_version_id,
                    source_conflicts.c.successor_conflict_id,
                ).where(source_conflicts.c.conflict_id == conflict_id)
            )
            return result.one_or_none()

    async def sync_conflict_event_count(self, workspace_id: UUID) -> int:
        async with self._engine.connect() as connection:
            result = await connection.execute(
                sa.select(sa.func.count())
                .select_from(sync_events)
                .where(
                    sync_events.c.workspace_id == workspace_id,
                    sync_events.c.event_type.in_(("conflict_capture", "conflict_resolve")),
                )
            )
            return int(result.scalar_one())

    async def audit_conflict_action_count(self, workspace_id: UUID) -> int:
        async with self._engine.connect() as connection:
            result = await connection.execute(
                sa.select(sa.func.count())
                .select_from(audit_events)
                .where(
                    audit_events.c.workspace_id == workspace_id,
                    audit_events.c.action.in_(
                        ("source.conflict_captured", "source.conflict_resolved")
                    ),
                )
            )
            return int(result.scalar_one())

    async def published_version_count(self, source_id: UUID) -> int:
        async with self._engine.connect() as connection:
            result = await connection.execute(
                sa.select(sa.func.count())
                .select_from(source_versions)
                .where(source_versions.c.source_id == source_id)
            )
            return int(result.scalar_one())

    async def resulting_version_object(self, source_version_id: UUID) -> UUID:
        async with self._engine.connect() as connection:
            result = await connection.execute(
                sa.select(source_versions.c.content_object_id).where(
                    source_versions.c.source_version_id == source_version_id
                )
            )
            return result.scalar_one()

    async def _insert_content_object(
        self, connection: Any, content_object_id: UUID, salt: str
    ) -> None:
        content_hash = hashlib.sha256(salt.encode("utf-8")).hexdigest()
        object_key = f"objects/sha256/{content_hash[:2]}/{content_hash[2:4]}/{content_hash}"
        await connection.execute(
            sa.insert(content_objects).values(
                content_object_id=content_object_id,
                content_hash=content_hash,
                object_key=object_key,
                byte_size=len(salt),
                media_type="text/markdown",
                verified_at=sa.text("CURRENT_TIMESTAMP - interval '1 second'"),
            )
        )


@pytest_asyncio.fixture
async def conflict_harness(
    source_conflict_stack: SourceConflictStack,
) -> Iterator[ConflictStoreHarness]:
    engine = create_source_store_engine(
        source_conflict_stack.settings, source_conflict_stack.password
    )
    try:
        yield ConflictStoreHarness(engine, PostgresqlSourceConflictStore(engine))
    finally:
        await dispose_source_store_engine(engine)


def expected_row_deltas(**nonzero_deltas: int) -> dict[str, int]:
    """Zero delta for every registry table, overridden by the nonzero ones."""

    return {table_name: 0 for table_name in SOURCE_STORE_TABLES} | nonzero_deltas


__all__ = [
    "ConflictStoreHarness",
    "SeededVersion",
    "SeededWorkspace",
    "build_conflict_stack_environment",
    "conflict_harness",
    "expected_row_deltas",
    "run_alembic_arguments",
]
