"""Disposable PostgreSQL 18.4 stack fixtures for authentication schema tests.

The module-scoped stack fixture follows the canonical-postgresql-baseline
convention: it validates the bounded ``knowledge-ci-*`` project identity and
the ``CI`` guard, provisions the disposable local stack through
``tools.local_service_stack`` (reset -> bootstrap -> config -> up) and hands
the sanitized Alembic environment plus one autocommit ``knowledge_app``
connection to the tests WITHOUT applying any revision — the migration
lifecycle tests own every upgrade and downgrade step. In a ``finally`` it
closes the connection, performs a tolerant gated downgrade-to-base teardown
and resets ONLY the named disposable project, asserting no labelled resource
remains.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from asyncio import AbstractEventLoop
from collections.abc import Callable, Iterator
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
import pytest
from pydantic import SecretStr

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from tools.local_service_stack import main as stack_main
from tools.local_service_stack import validate_project_name

_WORKTREE_ROOT: Path = Path(__file__).resolve().parents[3]
_SECRET_ROOT: Path = (_WORKTREE_ROOT / ".local" / "stack-secrets").resolve()

_APPLICATION_DATABASE: str = "knowledge"
_APPLICATION_USER: str = "knowledge_app"
_DATABASE_HOST: str = "127.0.0.1"
_SSL_MODE: str = "disable"
_APPLICATION_PASSWORD_FILENAME: str = "postgres_application_password"
_ALEMBIC_APPLICATION_NAME: str = "knowledge-auth-schema-test"


def pytest_asyncio_loop_factories(
    config: pytest.Config, item: pytest.Item
) -> dict[str, Callable[[], AbstractEventLoop]]:
    """Run every asyncio test and fixture on a selector event loop.

    psycopg async cannot run on the Windows proactor loop, and SelectorEventLoop
    is already the default loop on the Linux CI integration runs.
    """
    del config, item
    return {"selector": asyncio.SelectorEventLoop}


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


def _read_application_password() -> SecretStr:
    resolved_root = _SECRET_ROOT.resolve(strict=True)
    secret_path = (resolved_root / _APPLICATION_PASSWORD_FILENAME).resolve(strict=True)
    if not secret_path.is_relative_to(resolved_root):
        pytest.fail("application password must resolve beneath the bounded secret root")
    return SecretStr(secret_path.read_text(encoding="ascii").strip())


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


def _run_gated_downgrade_if_head(alembic_env: dict[str, str]) -> None:
    """Tolerant teardown: downgrade to base only when the stack is reachable."""
    with suppress(AssertionError, subprocess.CalledProcessError, OSError):
        subprocess.run(
            ["uv", "run", "alembic", "-x", "allow_destructive=true", "downgrade", "base"],
            cwd=str(_WORKTREE_ROOT),
            env=alembic_env,
            capture_output=True,
            text=True,
            check=False,
        )


@dataclass(frozen=True, slots=True)
class AuthenticationSchemaStack:
    """Provisioned disposable stack: identity, connection and Alembic env."""

    project_name: str
    port: int
    password: SecretStr
    alembic_env: dict[str, str]
    connection: psycopg.Connection[Any]

    @property
    def database_name(self) -> str:
        return _APPLICATION_DATABASE

    @property
    def database_user(self) -> str:
        return _APPLICATION_USER


@pytest.fixture(scope="module")
def authentication_schema_stack() -> Iterator[AuthenticationSchemaStack]:
    project_name = _require_project_name()
    port = _resolved_host_port()
    _run_stack_steps(project_name)
    password = _read_application_password()
    alembic_env = _build_sanitized_environment(port)
    connection = psycopg.connect(
        host=_DATABASE_HOST,
        port=port,
        user=_APPLICATION_USER,
        password=password.get_secret_value(),
        dbname=_APPLICATION_DATABASE,
        sslmode=_SSL_MODE,
        application_name=_ALEMBIC_APPLICATION_NAME,
    )
    connection.autocommit = True
    try:
        yield AuthenticationSchemaStack(
            project_name=project_name,
            port=port,
            password=password,
            alembic_env=alembic_env,
            connection=connection,
        )
    finally:
        with suppress(psycopg.Error):
            connection.close()
        _run_gated_downgrade_if_head(alembic_env)
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


__all__ = ["AuthenticationSchemaStack", "authentication_schema_stack"]
