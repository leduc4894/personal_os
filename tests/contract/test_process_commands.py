from __future__ import annotations

import subprocess
import sys
from collections.abc import Sequence
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
    module_name: str, arguments: Sequence[str], cwd: Path
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", module_name, *arguments],
        cwd=cwd,
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
