"""Disposable PostgreSQL 18.4 stack fixtures for the policy schema migration.

The module-scoped stack fixture follows the canonical-postgresql-baseline
convention: it validates the bounded ``knowledge-ci-*`` project identity and
the ``CI`` guard, provisions the disposable local stack through
``tools.local_service_stack`` (reset -> bootstrap -> config -> up), applies the
real Alembic chain in two stages — first to the Child 2 head ``20260816_01``,
then seeds one canonical workspace graph plus one pending source-event intent
at that head, then upgrades to the policy head ``20260817_01`` — and in a
``finally`` resets ONLY the named disposable project and asserts no labelled
resource remains. The function-scoped harness fixture builds the real async
engine per test and seeds policy graphs (draft preview, signing key, published
revision, policy-transition intent) through schema-qualified parameter-bound
Core statements.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess
import sys
from asyncio import AbstractEventLoop
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
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

from postgresql_source_store.engine import create_source_store_engine, dispose_source_store_engine
from postgresql_source_store.projection_intents import PostgresqlProjectionIntentStore
from postgresql_source_store.settings import (
    DatabaseRuntimeSettings,
    load_database_runtime_settings,
)
from postgresql_source_store.tables import (
    policy_evaluations,
    policy_preview_results,
    policy_previews,
    policy_signing_keys,
    projection_intents,
    source_policies,
    sources,
    workspace_policy_state,
)

_WORKTREE_ROOT: Path = Path(__file__).resolve().parents[3]
_SECRET_ROOT: Path = (_WORKTREE_ROOT / ".local" / "stack-secrets").resolve()

_CHILD_2_HEAD_REVISION: str = "20260816_01"


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


def _run_alembic(environment: dict[str, str], *arguments: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["uv", "run", "alembic", *arguments],
        cwd=str(_WORKTREE_ROOT),
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    return result


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
class PolicyMigrationStack:
    """Provisioned disposable stack staged through the policy migration."""

    project_name: str
    port: int
    settings: DatabaseRuntimeSettings
    password: SecretStr
    owner_user_id: UUID
    workspace_id: UUID
    seeded_event_id: UUID
    seeded_source_id: UUID


@dataclass(frozen=True, slots=True)
class PublishedPolicyGraph:
    """One committed published-policy graph at its allocated revision."""

    signing_key_id: UUID
    policy_preview_id: UUID
    policy_revision_id: UUID
    revision_number: int


def _sha256_hex(nonce: str) -> str:
    return hashlib.sha256(nonce.encode("utf-8")).hexdigest()


@pytest.fixture(scope="module")
def policy_migration_stack() -> Iterator[PolicyMigrationStack]:
    project_name = _require_project_name()
    port = _resolved_host_port()
    _run_stack_steps(project_name)
    try:
        yield from _stage_policy_upgrade_and_yield(project_name, port)
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


def _stage_policy_upgrade_and_yield(project_name: str, port: int) -> Iterator[PolicyMigrationStack]:
    environment = _build_sanitized_environment(port)

    staged = _run_alembic(environment, "upgrade", _CHILD_2_HEAD_REVISION)
    assert staged.returncode == 0, "alembic upgrade to the Child 2 head failed"

    # Seed one canonical workspace graph plus one pending source-event intent
    # at the Child 2 head, so the policy upgrade must preserve and backfill it.
    owner_user_id = uuid4()
    workspace_id = uuid4()
    source_id = uuid4()
    event_id = uuid4()
    nonce = uuid4().hex
    sync_engine = sa.create_engine(
        sa.URL.create(
            "postgresql+psycopg",
            username=_APPLICATION_USER,
            password=_read_application_password(),
            host=_DATABASE_HOST,
            port=port,
            database=_APPLICATION_DATABASE,
            query={"sslmode": _SSL_MODE},
        )
    )
    try:
        with sync_engine.begin() as connection:
            connection.execute(
                sa.text(
                    "INSERT INTO knowledge.users (user_id, username, display_name)"
                    " VALUES (:user_id, :username, :display_name)"
                ),
                {
                    "user_id": owner_user_id,
                    "username": f"policy-owner-{nonce[:12]}",
                    "display_name": "Policy Migration Owner",
                },
            )
            connection.execute(
                sa.text(
                    "INSERT INTO knowledge.workspaces"
                    " (workspace_id, owner_user_id, workspace_key, display_name)"
                    " VALUES (:workspace_id, :owner_user_id, :workspace_key, :display_name)"
                ),
                {
                    "workspace_id": workspace_id,
                    "owner_user_id": owner_user_id,
                    "workspace_key": f"ws-{nonce[:12]}",
                    "display_name": "Policy Migration Workspace",
                },
            )
            connection.execute(
                sa.text(
                    "INSERT INTO knowledge.sources"
                    " (workspace_id, source_id, source_type, title)"
                    " VALUES (:workspace_id, :source_id, 'markdown', :title)"
                ),
                {
                    "workspace_id": workspace_id,
                    "source_id": source_id,
                    "title": f"Note {nonce[:8]}",
                },
            )
            connection.execute(
                sa.text(
                    "INSERT INTO knowledge.sync_events"
                    " (event_id, workspace_id, source_id, idempotency_key,"
                    " request_fingerprint, event_type)"
                    " VALUES (:event_id, :workspace_id, :source_id, :idempotency_key,"
                    " :request_fingerprint, 'create')"
                ),
                {
                    "event_id": event_id,
                    "workspace_id": workspace_id,
                    "source_id": source_id,
                    "idempotency_key": f"policy-seed-{nonce}",
                    "request_fingerprint": _sha256_hex(nonce),
                },
            )
            connection.execute(
                sa.text(
                    "INSERT INTO knowledge.projection_intents"
                    " (projection_intent_id, workspace_id, event_id, source_id,"
                    " projection_kind, operation, status, available_at, created_at)"
                    " VALUES (:projection_intent_id, :workspace_id, :event_id, :source_id,"
                    " 'qdrant', 'delete', 'pending',"
                    " CURRENT_TIMESTAMP - interval '1 second',"
                    " CURRENT_TIMESTAMP - interval '1 second')"
                ),
                {
                    "projection_intent_id": uuid4(),
                    "workspace_id": workspace_id,
                    "event_id": event_id,
                    "source_id": source_id,
                },
            )
    finally:
        sync_engine.dispose()

    upgraded = _run_alembic(environment, "upgrade", "head")
    assert upgraded.returncode == 0, "alembic upgrade head failed for the disposable stack"

    settings = load_database_runtime_settings(environ=environment)
    password = SecretStr(_read_application_password())
    yield PolicyMigrationStack(
        project_name=project_name,
        port=port,
        settings=settings,
        password=password,
        owner_user_id=owner_user_id,
        workspace_id=workspace_id,
        seeded_event_id=event_id,
        seeded_source_id=source_id,
    )


class PolicyMigrationHarness:
    """Seed and inspection helpers bound to one test's engine."""

    def __init__(self, engine: AsyncEngine, stack: PolicyMigrationStack) -> None:
        self.engine = engine
        self.stack = stack
        self.store = PostgresqlProjectionIntentStore(engine, lease_token_generator=uuid4)

    async def database_now(self) -> datetime:
        async with self.engine.connect() as connection:
            result = await connection.execute(sa.text("SELECT CURRENT_TIMESTAMP"))
            value = result.scalar_one()
        assert isinstance(value, datetime)
        return value

    async def seed_policy_source(self) -> UUID:
        """Seed one canonical source row for evaluation/preview-result rows."""
        source_id = uuid4()
        async with self.engine.begin() as connection:
            await connection.execute(
                sa.insert(sources).values(
                    source_id=source_id,
                    workspace_id=self.stack.workspace_id,
                    source_type="markdown",
                    title=f"Policy Source {uuid4().hex[:8]}",
                )
            )
        return source_id

    async def seed_published_policy(self) -> PublishedPolicyGraph:
        """Seed the full revision-1 graph: key, ready preview, revision, pointer."""
        nonce = uuid4().hex
        signing_key_id = uuid4()
        policy_preview_id = uuid4()
        policy_revision_id = uuid4()
        digest = _sha256_hex(nonce)
        async with self.engine.begin() as connection:
            seeded_draft = await connection.execute(
                sa.text(
                    "SELECT policy_draft_id FROM knowledge.policy_drafts"
                    " WHERE workspace_id = :workspace_id"
                ),
                {"workspace_id": self.stack.workspace_id},
            )
            draft_id = seeded_draft.scalar_one()
            # Published revisions are append-only across the module's tests, so
            # every seeded graph allocates the workspace's next revision number
            # and chains to the previous revision when one exists.
            revision_result = await connection.execute(
                sa.text(
                    "SELECT revision_number, policy_revision_id"
                    " FROM knowledge.source_policies WHERE workspace_id = :workspace_id"
                    " ORDER BY revision_number DESC LIMIT 1"
                ),
                {"workspace_id": self.stack.workspace_id},
            )
            revision_row = revision_result.first()
            revision_number = 1 if revision_row is None else int(revision_row[0]) + 1
            parent_revision_id = None if revision_row is None else revision_row[1]
            await connection.execute(
                sa.insert(policy_signing_keys).values(
                    signing_key_id=signing_key_id,
                    workspace_id=self.stack.workspace_id,
                    public_key_bytes=digest[:32].encode("ascii"),
                    introduced_keyset_revision=1,
                )
            )
            await connection.execute(
                sa.insert(policy_previews).values(
                    policy_preview_id=policy_preview_id,
                    workspace_id=self.stack.workspace_id,
                    policy_draft_id=draft_id,
                    draft_version=1,
                    draft_sha256=_sha256_hex(f"draft-{nonce}"),
                    source_checkpoint_event_sequence=0,
                    state="ready",
                    impact_digest=_sha256_hex(f"impact-{nonce}"),
                    created_by_user_id=self.stack.owner_user_id,
                    ready_at=sa.text("CURRENT_TIMESTAMP"),
                )
            )
            await connection.execute(
                sa.insert(source_policies).values(
                    policy_revision_id=policy_revision_id,
                    workspace_id=self.stack.workspace_id,
                    revision_number=revision_number,
                    parent_policy_revision_id=parent_revision_id,
                    source_checkpoint_event_sequence=0,
                    policy_preview_id=policy_preview_id,
                    publication_idempotency_key=f"publish-{nonce}",
                    request_fingerprint=_sha256_hex(f"request-{nonce}"),
                    snapshot_contract="exclusion_policy_snapshot/v1",
                    snapshot_payload_bytes=b"{}",
                    snapshot_payload_sha256=_sha256_hex(f"payload-{nonce}"),
                    signing_key_id=signing_key_id,
                    signature_bytes=digest.encode("ascii"),
                    published_by_user_id=self.stack.owner_user_id,
                )
            )
            await connection.execute(
                sa.update(workspace_policy_state)
                .values(
                    active_policy_revision_id=policy_revision_id,
                    active_revision_number=revision_number,
                    updated_at=sa.text("CURRENT_TIMESTAMP"),
                )
                .where(workspace_policy_state.c.workspace_id == self.stack.workspace_id)
            )
        return PublishedPolicyGraph(
            signing_key_id=signing_key_id,
            policy_preview_id=policy_preview_id,
            policy_revision_id=policy_revision_id,
            revision_number=revision_number,
        )

    async def seed_policy_transition_intent(
        self, policy_revision_id: UUID, source_id: UUID, *, projection_kind: str = "qdrant"
    ) -> UUID:
        """Seed one pending policy-transition intent referencing the revision."""
        projection_intent_id = uuid4()
        async with self.engine.begin() as connection:
            await connection.execute(
                sa.insert(projection_intents).values(
                    projection_intent_id=projection_intent_id,
                    workspace_id=self.stack.workspace_id,
                    origin_kind="policy_transition",
                    event_id=None,
                    policy_revision_id=policy_revision_id,
                    source_id=source_id,
                    projection_kind=projection_kind,
                    operation="delete",
                    status="pending",
                    attempt_count=0,
                    available_at=sa.text("CURRENT_TIMESTAMP - interval '1 second'"),
                    created_at=sa.text("CURRENT_TIMESTAMP - interval '1 second'"),
                )
            )
        return projection_intent_id

    async def seed_evaluation(
        self,
        policy_revision_id: UUID,
        source_id: UUID,
        *,
        subject_event_sequence: int = 1,
    ) -> UUID:
        policy_evaluation_id = uuid4()
        async with self.engine.begin() as connection:
            await connection.execute(
                sa.insert(policy_evaluations).values(
                    policy_evaluation_id=policy_evaluation_id,
                    policy_revision_id=policy_revision_id,
                    source_id=source_id,
                    subject_event_sequence=subject_event_sequence,
                    raw_decision="allowed",
                    enforced_decision="allowed",
                    matched_rule_ids="",
                    missing_fields="",
                    subject_fingerprint=_sha256_hex(f"subject-{uuid4().hex}"),
                )
            )
        return policy_evaluation_id

    async def seed_preview_result(self, policy_preview_id: UUID, source_id: UUID) -> None:
        async with self.engine.begin() as connection:
            await connection.execute(
                sa.insert(policy_preview_results).values(
                    policy_preview_id=policy_preview_id,
                    source_id=source_id,
                    previous_raw_decision="indeterminate",
                    previous_enforced_decision="excluded",
                    proposed_raw_decision="allowed",
                    proposed_enforced_decision="allowed",
                    proposed_match_state="not_matched",
                    impact_class="newly_allowed",
                    matched_rule_ids="",
                    missing_fields="",
                    subject_fingerprint=_sha256_hex(f"result-{uuid4().hex}"),
                )
            )

    async def fetch_scalar(self, sql: str, parameters: dict[str, Any]) -> Any:
        async with self.engine.connect() as connection:
            result = await connection.execute(sa.text(sql), parameters)
            return result.scalar_one()

    async def fetch_all(self, sql: str, parameters: dict[str, Any] | None = None) -> list[Any]:
        async with self.engine.connect() as connection:
            result = await connection.execute(sa.text(sql), parameters or {})
            return list(result.fetchall())


def run_guarded_alembic(
    stack: PolicyMigrationStack, *arguments: str
) -> subprocess.CompletedProcess[str]:
    """Run Alembic against the disposable stack with the sanitized environment."""
    return _run_alembic(_build_sanitized_environment(stack.port), *arguments)


@contextmanager
def sanitized_database_environment(stack: PolicyMigrationStack) -> Iterator[None]:
    """Expose the sanitized KNOWLEDGE_* environment for in-process Alembic.

    ``migrations/env.py`` loads its settings from the process environment, so
    in-process Alembic commands need the same sanitized, secret-file-driven
    environment as the subprocess runs. The previous environment is restored
    on exit.
    """
    environment = _build_sanitized_environment(stack.port)
    sentinel = object()
    saved: dict[str, str | None] = {}
    try:
        for key, value in environment.items():
            saved[key] = os.environ.get(key, sentinel)
            os.environ[key] = value
        yield
    finally:
        for key, original in saved.items():
            if original is sentinel:
                del os.environ[key]
            else:
                assert isinstance(original, str)
                os.environ[key] = original


def run_inprocess_alembic_downgrade(stack: PolicyMigrationStack, *, destructive: bool) -> None:
    """Run ``alembic downgrade`` in-process against the disposable stack.

    The in-process command path leaves ``Config.cmd_opts`` unset unless
    ``destructive`` is requested, so ``migrations/env.py`` skips its own CLI
    downgrade gate and the policy migration's own row-level gate decides. This
    isolates the migration-level refusal from the environment-level one.
    """
    from argparse import Namespace

    from alembic import command
    from alembic.config import Config

    configuration = Config(str(_WORKTREE_ROOT / "alembic.ini"))
    configuration.set_main_option("script_location", str(_WORKTREE_ROOT / "migrations"))
    if destructive:
        configuration.cmd_opts = Namespace(x=["allow_destructive=true"])  # type: ignore[assignment]
    with sanitized_database_environment(stack):
        command.downgrade(configuration, _CHILD_2_HEAD_REVISION)


@pytest_asyncio.fixture
async def policy_migration_harness(
    policy_migration_stack: PolicyMigrationStack,
) -> Iterator[PolicyMigrationHarness]:
    engine = create_source_store_engine(
        policy_migration_stack.settings, policy_migration_stack.password
    )
    harness = PolicyMigrationHarness(engine, policy_migration_stack)
    try:
        yield harness
    finally:
        await dispose_source_store_engine(engine)


__all__ = [
    "PolicyMigrationHarness",
    "PolicyMigrationStack",
    "PublishedPolicyGraph",
    "policy_migration_harness",
    "policy_migration_stack",
    "run_guarded_alembic",
    "run_inprocess_alembic_downgrade",
]
