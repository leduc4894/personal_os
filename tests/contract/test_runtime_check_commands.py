"""Process-level and unit contracts for the ``check-runtime`` composition-root command.

The subprocess parameterization drives the real installed entry module for each
composition root (``personal-api``/``personal-mcp``/``personal-worker``) through a
hermetic environment, then asserts the single success line shape. Invalid and
unknown-key configurations must exit ``78`` with one safe ``runtime_configuration_failed``
record on stderr and nothing on stdout. The exit ``70`` path is covered by an
in-process unit test that monkeypatches the loader to raise a hostile exception.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from uuid import UUID

import pytest

from personal_os.diagnostics import runtime_check as runtime_check_module
from personal_os.diagnostics.runtime_check import run_runtime_check
from personal_os.runtime_configuration.models import ServiceName

# (console-script, python -m module, canonical service value emitted on the wire).
RUNTIME_COMMANDS = (
    ("personal-api", "api_runtime.command", "api"),
    ("personal-mcp", "mcp_runtime.command", "mcp"),
    ("personal-worker", "workflow_worker.command", "worker"),
)

COMMAND_IDS = [command for command, _, _ in RUNTIME_COMMANDS]

# Operating-system essentials required to launch the interpreter under a curated
# environment. Everything else is dropped so the subprocess sees only what the
# runtime-configuration contract owns plus the explicit secret root.
_ESSENTIAL_ENV_KEYS = frozenset(
    {
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "WINDIR",
        "TEMP",
        "TMP",
        "NUMBER_OF_PROCESSORS",
        "OS",
        "USERPROFILE",
        "APPDATA",
        "PROGRAMDATA",
        "HOMEDRIVE",
        "HOMEPATH",
        "PYTHONIOENCODING",
        "PYTHONUTF8",
        "PYTHONPATH",
    }
)


def _run_module(
    module_name: str,
    arguments: tuple[str, ...] | list[str],
    *,
    env: Mapping[str, str],
    cwd: Path,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", module_name, *arguments],
        cwd=cwd,
        env=dict(env),
        check=False,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def clean_runtime_environment(tmp_path: Path) -> dict[str, str]:
    """Curated environment: OS essentials only, no inherited KNOWLEDGE_* config."""
    environment = {key: value for key, value in os.environ.items() if key in _ESSENTIAL_ENV_KEYS}
    for inherited in [key for key in environment if key.startswith("KNOWLEDGE_")]:
        del environment[inherited]
    environment["KNOWLEDGE_SECRET_ROOT"] = str(tmp_path)
    return environment


def _single_json_line(stream: str) -> dict[str, object]:
    lines = stream.splitlines()
    assert len(lines) == 1, f"expected exactly one JSON line, got {lines!r}"
    record = json.loads(lines[0])
    assert isinstance(record, dict)
    return record


@pytest.mark.parametrize(
    ("command", "module_name", "service"),
    RUNTIME_COMMANDS,
    ids=COMMAND_IDS,
)
def test_check_runtime_emits_equivalent_success_shape(
    command: str,
    module_name: str,
    service: str,
    clean_runtime_environment: dict[str, str],
    tmp_path: Path,
) -> None:
    del command
    completed = _run_module(
        module_name,
        ["check-runtime"],
        env=clean_runtime_environment,
        cwd=tmp_path,
    )

    assert completed.returncode == 0, completed.stderr
    record = _single_json_line(completed.stdout)
    assert record["service"] == service
    assert record["event"] == "runtime_configuration_validated"
    assert record["result_code"] == "succeeded"
    assert UUID(str(record["request_id"])).version == 7
    assert len(str(record["trace_id"])) == 32
    assert completed.stderr == ""


@pytest.mark.parametrize(
    ("command", "module_name", "service"),
    RUNTIME_COMMANDS,
    ids=COMMAND_IDS,
)
def test_check_runtime_invalid_log_level_exits_seventy_eight(
    command: str,
    module_name: str,
    service: str,
    clean_runtime_environment: dict[str, str],
    tmp_path: Path,
) -> None:
    del command, service
    hostile_env = {
        **clean_runtime_environment,
        "KNOWLEDGE_LOG_LEVEL": "do-not-emit-invalid-level",
    }
    completed = _run_module(
        module_name,
        ["check-runtime"],
        env=hostile_env,
        cwd=tmp_path,
    )

    assert completed.returncode == 78, completed.stdout
    assert completed.stdout == ""
    record = _single_json_line(completed.stderr)
    assert record["event"] == "runtime_configuration_failed"
    assert record["result_code"] == "failed"
    assert record["environment"] is None
    assert "do-not-emit-invalid-level" not in completed.stdout + completed.stderr


@pytest.mark.parametrize(
    ("command", "module_name", "service"),
    RUNTIME_COMMANDS,
    ids=COMMAND_IDS,
)
def test_check_runtime_unknown_key_exits_seventy_eight_with_safe_count(
    command: str,
    module_name: str,
    service: str,
    clean_runtime_environment: dict[str, str],
    tmp_path: Path,
) -> None:
    del command, service
    hostile_env = {
        **clean_runtime_environment,
        "KNOWLEDGE_UNKNOWN": "do-not-emit-unknown",
    }
    completed = _run_module(
        module_name,
        ["check-runtime"],
        env=hostile_env,
        cwd=tmp_path,
    )

    assert completed.returncode == 78, completed.stdout
    assert completed.stdout == ""
    record = _single_json_line(completed.stderr)
    assert record["event"] == "runtime_configuration_failed"
    assert record["result_code"] == "failed"
    assert record["environment"] is None
    assert isinstance(record.get("count"), int)
    assert record["count"] >= 1
    assert "do-not-emit-unknown" not in completed.stdout + completed.stderr


def test_hostile_loader_failure_emits_internal_error_without_exception_text(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    sentinel = "do-not-emit-exception-message"

    def hostile_loader(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError(sentinel)

    monkeypatch.setattr(runtime_check_module, "load_runtime_settings", hostile_loader)
    monkeypatch.setenv("KNOWLEDGE_SECRET_ROOT", str(tmp_path))

    exit_code = run_runtime_check(ServiceName.API)

    assert exit_code == 70
    captured = capsys.readouterr()
    assert captured.out == ""
    record = _single_json_line(captured.err)
    assert record["event"] == "internal_error"
    assert record["result_code"] == "failed"
    assert record["error_code"] == "internal_error"
    assert sentinel not in captured.err
