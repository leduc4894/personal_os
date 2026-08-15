from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from importlib.metadata import entry_points
from pathlib import Path

import pytest

COMMANDS: Sequence[tuple[str, str, str]] = (
    ("personal-api", "api_runtime.command", "API process shell"),
    ("personal-mcp", "mcp_runtime.command", "MCP process shell"),
    ("personal-worker", "workflow_worker.command", "Temporal worker process shell"),
)

COMMAND_IDS = [command for command, _, _ in COMMANDS]


def _run_module(
    module_name: str,
    arguments: Sequence[str],
    cwd: Path,
    *,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", module_name, *arguments],
        cwd=cwd,
        env=None if env is None else dict(env),
        check=False,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize(
    ("command", "module_name", "description"),
    COMMANDS,
    ids=COMMAND_IDS,
)
def test_no_arguments_exits_zero(
    command: str,
    module_name: str,
    description: str,
    tmp_path: Path,
) -> None:
    completed = _run_module(module_name, [], tmp_path)
    assert completed.returncode == 0, completed.stderr
    assert f"usage: {command}" in completed.stdout
    assert description in completed.stdout


@pytest.mark.parametrize(
    ("command", "module_name", "description"),
    COMMANDS,
    ids=COMMAND_IDS,
)
def test_help_flag_exits_zero(
    command: str,
    module_name: str,
    description: str,
    tmp_path: Path,
) -> None:
    completed = _run_module(module_name, ["--help"], tmp_path)
    assert completed.returncode == 0, completed.stderr
    assert f"usage: {command}" in completed.stdout
    assert description in completed.stdout


@pytest.mark.parametrize(
    ("command", "module_name", "description"),
    COMMANDS,
    ids=COMMAND_IDS,
)
def test_version_flag_prints_distribution_version(
    command: str,
    module_name: str,
    description: str,
    tmp_path: Path,
) -> None:
    completed = _run_module(module_name, ["--version"], tmp_path)
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == f"{command} 0.1.0"


@pytest.mark.parametrize(
    ("command", "module_name", "description"),
    COMMANDS,
    ids=COMMAND_IDS,
)
def test_invalid_argument_exits_two_without_traceback(
    command: str,
    module_name: str,
    description: str,
    tmp_path: Path,
) -> None:
    completed = _run_module(module_name, ["--invalid"], tmp_path)
    assert completed.returncode == 2, completed.stdout
    assert "unrecognized arguments" in completed.stderr
    assert "Traceback" not in completed.stderr


@pytest.mark.parametrize(
    ("command", "module_name", "description"),
    COMMANDS,
    ids=COMMAND_IDS,
)
def test_console_script_entry_point_maps_to_declared_module(
    command: str,
    module_name: str,
    description: str,
) -> None:
    matches = [entry for entry in entry_points(group="console_scripts") if entry.name == command]
    assert len(matches) == 1
    assert matches[0].value == f"{module_name}:main"


def _hostile_secret_environment(secret_root: Path) -> dict[str, str]:
    """An inherited-looking environment that would fail or leak if settings loaded.

    Every inherited ``KNOWLEDGE_*`` key is removed, then the secret root is pointed
    at a directory whose single secret file carries a unique sentinel value, and an
    invalid log level is injected. Any parsing path that loaded settings would trip
    the invalid log level (exit 78 / crash); any path that read the secret would
    echo the sentinel. Shell-only parsing paths do neither.
    """
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("KNOWLEDGE_")
    }
    environment["KNOWLEDGE_SECRET_ROOT"] = str(secret_root)
    environment["KNOWLEDGE_LOG_LEVEL"] = "do-not-emit-invalid-level"
    return environment


@pytest.mark.parametrize(
    ("command", "module_name", "description"),
    COMMANDS,
    ids=COMMAND_IDS,
)
def test_parsing_paths_never_read_secret_file_or_load_settings(
    command: str,
    module_name: str,
    description: str,
    tmp_path: Path,
) -> None:
    del command, description
    sentinel_value = "do-not-emit-secret-value"
    sentinel_name = "database-password"
    (tmp_path / sentinel_name).write_text(sentinel_value, encoding="utf-8")
    env = _hostile_secret_environment(tmp_path)

    # No args / --help / --version must still succeed (exit 0) despite the hostile
    # environment: they parse without selecting check-runtime, so no settings load.
    for argv in ([], ["--help"], ["--version"]):
        completed = _run_module(module_name, argv, tmp_path, env=env)
        assert completed.returncode == 0, completed.stderr
        assert sentinel_value not in completed.stdout
        assert sentinel_value not in completed.stderr
        assert sentinel_name not in completed.stdout
        assert sentinel_name not in completed.stderr

    # Invalid syntax must still exit 2 with usage on stderr and no traceback,
    # proving the parser error path also never loads settings or reads the secret.
    invalid = _run_module(module_name, ["--invalid"], tmp_path, env=env)
    assert invalid.returncode == 2, invalid.stdout
    assert "unrecognized arguments" in invalid.stderr
    assert "Traceback" not in invalid.stderr
    assert sentinel_value not in invalid.stdout + invalid.stderr
    assert sentinel_name not in invalid.stdout + invalid.stderr


def test_api_help_lists_lazy_server_subcommands(tmp_path: Path) -> None:
    completed = _run_module("api_runtime.command", ["--help"], tmp_path)
    assert completed.returncode == 0, completed.stderr
    assert "serve" in completed.stdout
    assert "export-openapi" in completed.stdout


def test_api_export_openapi_missing_output_is_syntax_failure(tmp_path: Path) -> None:
    completed = _run_module("api_runtime.command", ["export-openapi"], tmp_path)
    assert completed.returncode == 2, completed.stdout
    assert "--output" in completed.stderr
    assert "Traceback" not in completed.stderr
