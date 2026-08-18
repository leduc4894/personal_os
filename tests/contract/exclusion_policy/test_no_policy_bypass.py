"""Static bypass contract: no canonical-source or R2 access skips the guard.

The only approved path to canonical source bytes is the guarded adapter pair
— :class:`SourceVersionPublicationService` and
:class:`CanonicalSourceReadService`, each composed with
:func:`compose_policy_enforcement` so the guard runs before any object-store
access (proven call-by-call by ``test_enforcement_boundaries``). This gate
proves the negative space (spec 14.2/23.4): no API, MCP or worker module
imports the R2 adapter or a cloud SDK, no module outside the approved
guarded set calls the object-store port members, nothing outside the
guarded services touches the canonical-read store, and every composition
that constructs a canonical content service binds the policy guard in the
same module. A synthetic violating module fed through the same scanner
proves the scanner detects real violations.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Final

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[3]

#: Composition trees that must never reach R2 or a cloud SDK directly.
RUNTIME_COMPOSITION_ROOTS: Final[tuple[Path, ...]] = (
    REPO_ROOT / "apps" / "api" / "src" / "api_runtime",
    REPO_ROOT / "apps" / "mcp" / "src" / "mcp_runtime",
    REPO_ROOT / "apps" / "worker" / "src" / "workflow_worker",
)

#: Import roots the runtime compositions must never reach.
FORBIDDEN_RUNTIME_IMPORT_ROOTS: Final[frozenset[str]] = frozenset(
    {"r2_object_storage", "boto3", "botocore", "aiobotocore"}
)

#: The object-store port members: every call site must live in the approved
#: guarded adapter set below.
OBJECT_STORE_MEMBERS: Final[frozenset[str]] = frozenset(
    {"open_verified_reader", "resolve_verified_object", "store_stream", "verify_existing_object"}
)

#: The approved guarded adapter set: the two guarded domain services (which
#: the enforcement-boundary contract proves call the policy guard first),
#: the offline recovery restore path (operator-driven offline tooling), the
#: acceptance CLI composition (which binds the same guarded services), the
#: adapter package itself and the port contract definition.
APPROVED_OBJECT_STORE_MODULES: Final[frozenset[Path]] = frozenset(
    {
        REPO_ROOT / "src" / "personal_os" / "sources" / "publication.py",
        REPO_ROOT / "src" / "personal_os" / "sources" / "reading.py",
        REPO_ROOT / "src" / "personal_os" / "recovery" / "service.py",
        REPO_ROOT / "tools" / "canonical_core_operations.py",
        REPO_ROOT / "packages" / "r2-object-storage" / "src" / "r2_object_storage" / "adapter.py",
        REPO_ROOT / "src" / "personal_os" / "object_storage" / "contracts.py",
    }
)

#: The canonical-read store adapter: reachable only from the guarded read
#: service composition (tools acceptance CLI), never from a runtime surface.
CANONICAL_READ_STORE_MODULE: Final[str] = "postgresql_source_store.canonical_read"
CANONICAL_READ_STORE_PATH: Final[Path] = (
    REPO_ROOT
    / "packages"
    / "postgresql-source-store"
    / "src"
    / "postgresql_source_store"
    / "canonical_read.py"
)
APPROVED_CANONICAL_READ_IMPORTERS: Final[frozenset[Path]] = frozenset(
    {
        REPO_ROOT / "tools" / "canonical_core_operations.py",
    }
)

#: Every constructor of a canonical content service must bind a policy guard
#: in the same module.
CANONICAL_SERVICE_NAMES: Final[frozenset[str]] = frozenset(
    {"CanonicalSourceReadService", "SourceVersionPublicationService"}
)
POLICY_GUARD_BINDINGS: Final[frozenset[str]] = frozenset(
    {"compose_policy_enforcement", "policy_guard"}
)

#: The scanned production trees (domain, packages, apps, tools).
SCANNED_SOURCE_ROOTS: Final[tuple[Path, ...]] = (
    REPO_ROOT / "src",
    REPO_ROOT / "apps",
    REPO_ROOT / "packages",
    REPO_ROOT / "tools",
)

_VIOLATING_MODULE_SOURCE: Final[str] = '''
"""Synthetic violating module for the scanner self-check."""
import r2_object_storage.adapter
from postgresql_source_store.canonical_read import PostgresqlCanonicalSourceReadStore

def fetch_secret_note(object_store):
    with object_store.open_verified_reader("sentinel") as reader:
        return reader.read()
'''


def _python_files(*roots: Path) -> list[Path]:
    files: list[Path] = []
    for root in roots:
        files.extend(sorted(root.rglob("*.py")))
    return files


def _module_tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _import_roots(tree: ast.Module) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".", maxsplit=1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None and node.level == 0:
            roots.add(node.module.split(".", maxsplit=1)[0])
            roots.add(node.module)
            # ``from package import module`` surfaces the submodule by name.
            roots.update(alias.name for alias in node.names)
    return roots


def _called_member_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            names.add(node.func.attr)
    return names


def _instantiated_class_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            names.add(node.func.id)
    return names


def test_scanner_detects_a_violating_module() -> None:
    tree = ast.parse(_VIOLATING_MODULE_SOURCE)
    assert FORBIDDEN_RUNTIME_IMPORT_ROOTS & _import_roots(tree) == {"r2_object_storage"}
    assert CANONICAL_READ_STORE_MODULE in _import_roots(tree)
    assert OBJECT_STORE_MEMBERS & _called_member_names(tree) == {"open_verified_reader"}


def test_runtime_compositions_import_no_object_store_adapter_or_cloud_sdk() -> None:
    offenders: dict[str, set[str]] = {}
    for path in _python_files(*RUNTIME_COMPOSITION_ROOTS):
        violating = FORBIDDEN_RUNTIME_IMPORT_ROOTS & _import_roots(_module_tree(path))
        if violating:
            offenders[str(path)] = violating
    assert not offenders, (
        "API, MCP and worker compositions must not reach R2 or a cloud SDK directly; "
        f"object-store access exists only behind the guarded adapter: {offenders}"
    )


def test_object_store_members_are_called_only_by_the_approved_guarded_modules() -> None:
    offenders: list[str] = []
    for path in _python_files(*SCANNED_SOURCE_ROOTS):
        if path in APPROVED_OBJECT_STORE_MODULES:
            continue
        called = OBJECT_STORE_MEMBERS & _called_member_names(_module_tree(path))
        if called:
            offenders.append(f"{path}: {sorted(called)}")
    assert not offenders, (
        "object-store access outside the approved guarded adapter set: " + "; ".join(offenders)
    )


def test_canonical_read_store_is_composed_only_through_the_guarded_read_service() -> None:
    offenders: list[str] = []
    for path in _python_files(*SCANNED_SOURCE_ROOTS):
        # Package ``__init__`` re-exports are not composition call sites.
        if path.name == "__init__.py":
            continue
        if path == CANONICAL_READ_STORE_PATH or path in APPROVED_CANONICAL_READ_IMPORTERS:
            continue
        module_imports = _import_roots(_module_tree(path))
        if CANONICAL_READ_STORE_MODULE in module_imports or "canonical_read" in {
            root.split(".")[-1] for root in module_imports
        }:
            offenders.append(str(path))
    assert not offenders, (
        "the canonical-read store adapter is reachable only from the guarded read "
        f"composition: {offenders}"
    )


def test_every_canonical_content_service_construction_binds_the_policy_guard() -> None:
    offenders: list[str] = []
    for path in _python_files(*SCANNED_SOURCE_ROOTS):
        if path.suffix != ".py" or "/tests/" in path.as_posix() or "\\tests\\" in path.as_posix():
            continue
        tree = _module_tree(path)
        constructed = CANONICAL_SERVICE_NAMES & _instantiated_class_names(tree)
        if not constructed:
            continue
        source = path.read_text(encoding="utf-8")
        bound = any(binding in source for binding in POLICY_GUARD_BINDINGS)
        if not bound:
            offenders.append(f"{path}: constructs {sorted(constructed)} without a policy guard")
    assert not offenders, "; ".join(offenders)
