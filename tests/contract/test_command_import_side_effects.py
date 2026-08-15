from __future__ import annotations

import ast
import contextlib
import importlib
import os
import socket
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path
from types import ModuleType

import pytest

WRAPPERS = (
    "api_runtime.command",
    "mcp_runtime.command",
    "workflow_worker.command",
)

# Composition-root modules that wrappers must import lazily inside ``_check_runtime``
# rather than at module top level. Kept in sync with each wrapper's lazy callback.
LAZY_COMPOSITION_ROOTS = {
    "api_runtime.command": "api_runtime.runtime_check",
    "mcp_runtime.command": "mcp_runtime.runtime_check",
    "workflow_worker.command": "workflow_worker.runtime_check",
}

# Modules purged from sys.modules so every import in the chain re-executes under
# the monkeypatched environment, exposing import-time side effects in both the
# thin wrappers and the shared command shell alike.
PURGED_MODULES = (
    "api_runtime",
    "api_runtime.command",
    "api_runtime.runtime_check",
    "api_runtime.server",
    "api_runtime.application",
    "mcp_runtime",
    "mcp_runtime.command",
    "mcp_runtime.runtime_check",
    "workflow_worker",
    "workflow_worker.command",
    "workflow_worker.runtime_check",
    "personal_os",
    "personal_os.command_shell",
    "personal_os.package_metadata",
    "fastapi",
    "uvicorn",
)

# Server machinery that must stay unimported until a lazy serve callback runs.
# Shell-only parsing (including check-runtime, whose only imports live inside
# the checked callback) can never reach any of these modules.
HEAVY_SERVER_MODULES = (
    "api_runtime.server",
    "api_runtime.application",
    "fastapi",
    "uvicorn",
)

# stdlib argparse joins the allowlist because the typed BootstrapSubcommand
# callbacks (ArgumentParser/Namespace) are part of the lazy shell contract;
# no new external module is allowed.
ALLOWED_IMPORTS = frozenset(
    {"__future__", "argparse", "collections.abc", "typing", "personal_os.command_shell"}
)

# Shell-only invocations that never select ``check-runtime``. ``--version`` reads
# package metadata via ``Path.read_text`` (benign, not a secret read), so it is
# excluded from the broad secret/env/network forbiddance and covered instead by
# the dedicated lazy-import assertion.
SECRET_SAFE_INVOCATIONS: Sequence[Sequence[str]] = (
    ["--help"],
    [],
    ["--not-a-real-flag"],
)
ALL_SHELL_INVOCATIONS: Sequence[Sequence[str]] = (
    ["--help"],
    ["--version"],
    [],
    ["--not-a-real-flag"],
)


def _forbid_side_effect(*_args: object, **_kwargs: object) -> None:
    raise AssertionError(
        "command shell import or a shell-only invocation touched environment, "
        "secret files or the network"
    )


@contextlib.contextmanager
def _purge_module_registry() -> Iterator[None]:
    """Drop the harness module set from ``sys.modules`` for the wrapped block.

    Every entry is restored to its exact pre-block object afterwards. Restoring
    is not cosmetic: the import system binds a submodule on its parent package
    only when that submodule is freshly executed, so a bare purge followed by a
    re-import leaves a partial package in the registry (attributes such as
    ``uvicorn.logging`` stay missing because the old submodules are still
    cached). Later in-process consumers — ``logging.config`` resolving
    ``uvicorn.logging.DefaultFormatter`` inside ``uvicorn.Config`` — then fail
    with ``ValueError`` in otherwise-unrelated tests. Snapshotting keeps the
    laziness proofs hermetic instead of poisoning the shared session.
    """
    snapshot: dict[str, ModuleType] = {
        name: sys.modules[name] for name in PURGED_MODULES if name in sys.modules
    }
    for name in PURGED_MODULES:
        sys.modules.pop(name, None)
    try:
        yield
    finally:
        for name in PURGED_MODULES:
            if name in snapshot:
                sys.modules[name] = snapshot[name]
            else:
                sys.modules.pop(name, None)


def _module_body_imports(node: ast.AST) -> set[str]:
    """Collect imports reachable at module top level (and inside class bodies).

    Imports inside function bodies are the lazy escape hatch and are deliberately
    exempt: they are exercised only after ``check-runtime`` is selected. Class
    bodies are still scanned because their imports run at module import time.
    """
    modules: set[str] = set()
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if isinstance(child, ast.Import):
            modules.update(alias.name for alias in child.names)
        elif isinstance(child, ast.ImportFrom) and child.level == 0 and child.module:
            modules.add(child.module)
        elif isinstance(child, ast.ClassDef):
            modules |= _module_body_imports(child)
    return modules


def _collect_imported_modules(source: str) -> set[str]:
    tree = ast.parse(source)
    return _module_body_imports(tree)


def _collect_function_body_imports(source: str) -> set[str]:
    """Collect imports nested inside any function body (the lazy import surface)."""
    tree = ast.parse(source)
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            for child in ast.walk(node):
                if isinstance(child, ast.Import):
                    modules.update(alias.name for alias in child.names)
                elif isinstance(child, ast.ImportFrom) and child.level == 0 and child.module:
                    modules.add(child.module)
    return modules


@pytest.mark.parametrize("module_name", WRAPPERS, ids=WRAPPERS)
def test_import_and_shell_invocations_touch_no_env_secret_file_or_network(
    monkeypatch: pytest.MonkeyPatch,
    module_name: str,
) -> None:
    with _purge_module_registry():
        monkeypatch.setattr(os, "getenv", _forbid_side_effect)
        monkeypatch.setattr(Path, "read_text", _forbid_side_effect)
        monkeypatch.setattr(socket, "create_connection", _forbid_side_effect)

        module = importlib.import_module(module_name)

        for argv in SECRET_SAFE_INVOCATIONS:
            with contextlib.suppress(SystemExit):
                module.run(list(argv))


@pytest.mark.parametrize("module_name", WRAPPERS, ids=WRAPPERS)
def test_shell_paths_never_execute_lazy_runtime_check_import(
    module_name: str,
) -> None:
    lazy_module = LAZY_COMPOSITION_ROOTS[module_name]
    with _purge_module_registry():
        module = importlib.import_module(module_name)
        assert lazy_module not in sys.modules, (
            f"importing {module_name} eagerly imported the lazy {lazy_module}"
        )

        for argv in ALL_SHELL_INVOCATIONS:
            with contextlib.suppress(SystemExit):
                module.run(list(argv))

        assert lazy_module not in sys.modules, (
            f"a shell-only invocation executed the lazy {lazy_module} import"
        )


@pytest.mark.parametrize("module_name", WRAPPERS, ids=WRAPPERS)
def test_wrapper_imports_only_allowlisted_modules(module_name: str) -> None:
    module = importlib.import_module(module_name)
    assert module.__file__ is not None
    source = Path(module.__file__).read_text(encoding="utf-8")
    imported = _collect_imported_modules(source)
    forbidden = imported - ALLOWED_IMPORTS
    assert not forbidden, f"{module_name} imports forbidden modules: {sorted(forbidden)}"


@pytest.mark.parametrize("module_name", WRAPPERS, ids=WRAPPERS)
def test_runtime_check_import_is_lazy_inside_a_function(module_name: str) -> None:
    expected_lazy = LAZY_COMPOSITION_ROOTS[module_name]
    module = importlib.import_module(module_name)
    assert module.__file__ is not None
    source = Path(module.__file__).read_text(encoding="utf-8")

    module_level = _collect_imported_modules(source)
    assert expected_lazy not in module_level, (
        f"{module_name} imports {expected_lazy} at module top level; it must be "
        "lazy inside the _check_runtime callback"
    )

    function_level = _collect_function_body_imports(source)
    assert expected_lazy in function_level, (
        f"{module_name} must import {expected_lazy} inside a function body so "
        "shell-only parsing paths never evaluate it"
    )


@pytest.mark.parametrize("module_name", WRAPPERS, ids=WRAPPERS)
def test_shell_paths_never_import_server_or_web_framework(module_name: str) -> None:
    with _purge_module_registry():
        module = importlib.import_module(module_name)
        for heavy_name in HEAVY_SERVER_MODULES:
            assert heavy_name not in sys.modules, (
                f"importing {module_name} eagerly imported the server module {heavy_name}"
            )

        for argv in ALL_SHELL_INVOCATIONS:
            with contextlib.suppress(SystemExit):
                module.run(list(argv))
            for heavy_name in HEAVY_SERVER_MODULES:
                assert heavy_name not in sys.modules, (
                    f"a shell-only invocation imported the server module {heavy_name}"
                )


def test_purge_cycle_restores_original_module_registry_entries() -> None:
    """A completed purge must leave the exact original module objects in place.

    Regression guard for the session-poisoning bug where a bare purge left a
    re-imported partial package behind: identity of the pre-purge object is the
    property later in-process consumers depend on.
    """
    original = importlib.import_module("personal_os.command_shell")
    with _purge_module_registry():
        assert "personal_os.command_shell" not in sys.modules
        fresh = importlib.import_module("personal_os.command_shell")
        assert fresh is not original
    assert sys.modules["personal_os.command_shell"] is original


@pytest.mark.parametrize("module_name", WRAPPERS, ids=WRAPPERS)
def test_wrapper_never_imports_server_or_web_framework_at_module_level(
    module_name: str,
) -> None:
    module = importlib.import_module(module_name)
    assert module.__file__ is not None
    source = Path(module.__file__).read_text(encoding="utf-8")
    imported = _collect_imported_modules(source)
    forbidden = imported.intersection(HEAVY_SERVER_MODULES)
    assert not forbidden, (
        f"{module_name} imports server machinery at module top level: {sorted(forbidden)}"
    )
