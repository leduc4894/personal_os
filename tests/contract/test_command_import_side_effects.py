from __future__ import annotations

import ast
import importlib
import os
import socket
import sys
from pathlib import Path

import pytest

WRAPPERS = (
    "api_runtime.command",
    "mcp_runtime.command",
    "workflow_worker.command",
)

# Modules purged from sys.modules so every import in the chain re-executes under
# the monkeypatched environment, exposing import-time side effects in both the
# thin wrappers and the shared command shell alike.
PURGED_MODULES = (
    "api_runtime",
    "api_runtime.command",
    "mcp_runtime",
    "mcp_runtime.command",
    "workflow_worker",
    "workflow_worker.command",
    "personal_os",
    "personal_os.command_shell",
    "personal_os.package_metadata",
)

ALLOWED_IMPORTS = frozenset(
    {"__future__", "collections.abc", "typing", "personal_os.command_shell"}
)


def _forbid_side_effect(*_args: object, **_kwargs: object) -> None:
    raise AssertionError(
        "command shell import or --help touched environment, secret files or the network"
    )


def _collect_imported_modules(source: str) -> set[str]:
    tree = ast.parse(source)
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.add(node.module)
    return modules


@pytest.mark.parametrize("module_name", WRAPPERS, ids=WRAPPERS)
def test_import_and_help_touch_no_env_secret_file_or_network(
    monkeypatch: pytest.MonkeyPatch,
    module_name: str,
) -> None:
    for name in PURGED_MODULES:
        sys.modules.pop(name, None)

    monkeypatch.setattr(os, "getenv", _forbid_side_effect)
    monkeypatch.setattr(Path, "read_text", _forbid_side_effect)
    monkeypatch.setattr(socket, "create_connection", _forbid_side_effect)

    module = importlib.import_module(module_name)

    with pytest.raises(SystemExit) as raised:
        module.run(["--help"])
    assert raised.value.code == 0


@pytest.mark.parametrize("module_name", WRAPPERS, ids=WRAPPERS)
def test_wrapper_imports_only_allowlisted_modules(module_name: str) -> None:
    module = importlib.import_module(module_name)
    assert module.__file__ is not None
    source = Path(module.__file__).read_text(encoding="utf-8")
    imported = _collect_imported_modules(source)
    forbidden = imported - ALLOWED_IMPORTS
    assert not forbidden, f"{module_name} imports forbidden modules: {sorted(forbidden)}"
