"""Static boundary contract: source publication stays off every public API surface.

The publication paths are backend-internal for this plan: no FastAPI route, no
MCP tool, no OpenAPI document and no generated TypeScript client may declare a
source-publication endpoint, and the Alembic graph must still be exactly the
single ``20260813_01`` baseline head with exactly one migration file — the
acceptance work added tests only, never a migration.

The API runtime package under ``apps/api/src/api_runtime`` is the sanctioned
FastAPI composition root of the API plan, so its framework imports and route
registrations are permitted there; the structural web-framework prohibitions
below guard the MCP surface, while the publication endpoint vocabulary is
forbidden on every public surface root.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Final

import pytest
import yaml  # type: ignore[import-untyped]  # Pinned PyYAML does not ship type stubs.
from alembic.config import Config
from alembic.script import ScriptDirectory

REPO_ROOT = Path(__file__).resolve().parents[3]
PYTHON_API_ROOTS: Final[tuple[Path, ...]] = (
    REPO_ROOT / "apps" / "api" / "src",
    REPO_ROOT / "apps" / "mcp" / "src",
)
#: Root whose modules may never import a web framework or register a route:
#: the MCP composition has no sanctioned web surface, unlike the API runtime.
PYTHON_FRAMEWORK_FREE_ROOTS: Final[tuple[Path, ...]] = (REPO_ROOT / "apps" / "mcp" / "src",)
TYPESCRIPT_ROOTS: Final[tuple[Path, ...]] = (
    REPO_ROOT / "apps" / "web" / "src",
    REPO_ROOT / "apps" / "obsidian-plugin" / "src",
)
MIGRATIONS_VERSIONS = REPO_ROOT / "migrations" / "versions"
BASELINE_REVISION: Final[str] = "20260813_01"

#: Web-framework import roots that would carry a route into a composition root.
FORBIDDEN_WEB_IMPORT_ROOTS: Final[frozenset[str]] = frozenset(
    {"fastapi", "starlette", "flask", "mcp", "fastmcp", "quart"}
)

#: Route/tool registration surfaces (decorator or direct call).
FORBIDDEN_REGISTRATION_CALLS: Final[frozenset[str]] = frozenset(
    {
        "api_route",
        "add_api_route",
        "include_router",
        "add_middleware",
        "add_url_rule",
        "list_tools",
        "call_tool",
    }
)
FORBIDDEN_DECORATOR_ATTRS: Final[frozenset[str]] = frozenset(
    {"get", "post", "put", "patch", "delete", "head", "options", "route", "tool", "mount"}
)

#: Source-publication endpoint vocabulary that must never appear on a surface.
PUBLICATION_ENDPOINT_TOKENS: Final[tuple[str, ...]] = (
    "source-version",
    "source_version",
    "sourceVersion",
    "publication",
    "/sources",
)

#: Repository subtrees that never hold a sanctioned API surface document.
_EXCLUDED_DIRECTORY_NAMES: Final[frozenset[str]] = frozenset(
    {".git", ".venv", "node_modules", "__pycache__", ".local", ".superpowers", ".pytest_cache"}
)


def _import_roots(tree: ast.Module) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".", maxsplit=1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None and node.level == 0:
            roots.add(node.module.split(".", maxsplit=1)[0])
    return roots


def _call_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            function = node.func
            if isinstance(function, ast.Name):
                names.add(function.id)
            elif isinstance(function, ast.Attribute):
                names.add(function.attr)
    return names


def _decorator_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            for decorator in node.decorator_list:
                decorator_call = decorator.func if isinstance(decorator, ast.Call) else decorator
                if isinstance(decorator_call, ast.Name):
                    names.add(decorator_call.id)
                elif isinstance(decorator_call, ast.Attribute):
                    names.add(decorator_call.attr)
    return names


def _iter_python_files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*.py") if "__pycache__" not in path.parts)


def _iter_surface_documents() -> list[Path]:
    """Every OpenAPI document in the repository outside excluded subtrees."""

    found: list[Path] = []
    for path in REPO_ROOT.rglob("openapi*"):
        if path.suffix not in {".json", ".yaml", ".yml"}:
            continue
        if _EXCLUDED_DIRECTORY_NAMES & set(path.parts):
            continue
        if not path.is_file():
            continue
        found.append(path)
    return sorted(found)


def test_alembic_heads_and_migration_file_count_are_unchanged() -> None:
    script_directory = ScriptDirectory.from_config(Config(str(REPO_ROOT / "alembic.ini")))
    assert script_directory.get_heads() == [BASELINE_REVISION]
    migration_files = [
        path for path in MIGRATIONS_VERSIONS.glob("*.py") if not path.name.startswith("__")
    ]
    assert [path.name for path in migration_files] == [
        "20260813_01_create_canonical_postgresql_baseline.py"
    ], "the acceptance task must not add, rename or remove a migration file"


def test_api_and_mcp_sources_declare_no_source_publication_route() -> None:
    offenders: list[str] = []
    for root in PYTHON_API_ROOTS:
        is_framework_free = root in PYTHON_FRAMEWORK_FREE_ROOTS
        for path in _iter_python_files(root):
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
            if is_framework_free:
                violating_imports = FORBIDDEN_WEB_IMPORT_ROOTS & _import_roots(tree)
                if violating_imports:
                    offenders.append(f"{path}: imports {sorted(violating_imports)}")
                violating_registrations = FORBIDDEN_REGISTRATION_CALLS & _call_names(tree)
                if violating_registrations:
                    offenders.append(f"{path}: registers {sorted(violating_registrations)}")
                violating_decorators = (
                    FORBIDDEN_REGISTRATION_CALLS | FORBIDDEN_DECORATOR_ATTRS
                ) & _decorator_names(tree)
                if violating_decorators:
                    offenders.append(f"{path}: decorator {sorted(violating_decorators)}")
            for token in PUBLICATION_ENDPOINT_TOKENS:
                if token in source:
                    offenders.append(f"{path}: endpoint token {token!r}")
    assert not offenders, (
        "no source publication route may reach the API or MCP surfaces:\n" + "\n".join(offenders)
    )


def test_generated_typescript_clients_declare_no_source_publication_endpoint() -> None:
    offenders: list[str] = []
    for root in TYPESCRIPT_ROOTS:
        for path in sorted(root.rglob("*.ts*")):
            if "__pycache__" in path.parts or path.suffix not in {".ts", ".tsx"}:
                continue
            source = path.read_text(encoding="utf-8")
            for token in PUBLICATION_ENDPOINT_TOKENS:
                if token in source:
                    offenders.append(f"{path}: endpoint token {token!r}")
    assert not offenders, (
        "generated TypeScript clients must not declare a source publication endpoint:\n"
        + "\n".join(offenders)
    )


def _rendered_endpoint_surface(parsed: object) -> str:
    """Render every endpoint-declaring subtree of one OpenAPI document.

    Endpoint declarations live in ``paths``, ``webhooks`` and
    ``components.pathItems``; each present subtree is rendered and the
    renderings joined so a document declaring endpoints under only one of
    them is still scanned. The remaining ``components`` subtree is data
    schema, not endpoint declaration: it legitimately embeds registry enums
    such as the error-code table, whose values (for example
    ``source_version_conflict``) name error conditions, not endpoints, and
    the generated API-client snapshot carries that schema verbatim. A
    document with none of the three structures is a scanning gap and fails
    loudly instead of silently passing.
    """
    if not isinstance(parsed, dict):
        raise AssertionError("openapi surface document must be a mapping")
    rendered_sections: list[str] = []
    for section_key in ("paths", "webhooks"):
        section = parsed.get(section_key)
        if isinstance(section, dict):
            rendered_sections.append(json.dumps(section, default=repr))
    components = parsed.get("components")
    if isinstance(components, dict) and isinstance(components.get("pathItems"), dict):
        rendered_sections.append(json.dumps(components["pathItems"], default=repr))
    if not rendered_sections:
        raise AssertionError(
            "openapi surface document declares none of paths, webhooks or components.pathItems"
        )
    return "\n".join(rendered_sections)


def test_endpoint_surface_scan_catches_publication_tokens_declared_only_under_webhooks() -> None:
    document = {
        "openapi": "3.1.0",
        "webhooks": {"new-source-version": {"post": {"operationId": "publishSourceVersion"}}},
    }
    rendered = _rendered_endpoint_surface(document)
    caught = [token for token in PUBLICATION_ENDPOINT_TOKENS if token in rendered]
    assert caught, "a publication endpoint declared only under webhooks must be scanned"


def test_endpoint_surface_scan_fails_loudly_when_no_endpoint_structure_is_present() -> None:
    document = {"openapi": "3.1.0", "info": {"title": "no endpoint declarations"}}
    with pytest.raises(AssertionError, match="paths"):
        _rendered_endpoint_surface(document)


def test_openapi_documents_declare_no_source_publication_path() -> None:
    offenders: list[str] = []
    for path in _iter_surface_documents():
        parsed: object
        if path.suffix == ".json":
            parsed = json.loads(path.read_text(encoding="utf-8"))
        else:
            parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
        rendered = _rendered_endpoint_surface(parsed)
        for token in PUBLICATION_ENDPOINT_TOKENS:
            if token in rendered:
                offenders.append(f"{path}: endpoint token {token!r}")
    assert not offenders, (
        "OpenAPI documents must not declare a source publication path:\n" + "\n".join(offenders)
    )
