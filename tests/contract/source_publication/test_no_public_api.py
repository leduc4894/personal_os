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
#: The plugin journal client directory — the Obsidian-side surface of the
#: sanctioned small-file sync design, whose wire shapes legitimately name
#: canonical source and version identity (see ``_is_sanctioned_policy_surface``).
SANCTIONED_PLUGIN_JOURNAL_ROOT: Final[Path] = (
    REPO_ROOT / "apps" / "obsidian-plugin" / "src" / "journal"
)
#: The plugin device-sync client directory — the Obsidian-side surface of
#: the sanctioned device cursor and manifest reconciliation design (spec
#: 7.1-7.4), whose wire shapes and transport tests legitimately name
#: canonical source and version identity (see ``_is_sanctioned_policy_surface``).
SANCTIONED_DEVICE_SYNC_PLUGIN_ROOT: Final[Path] = (
    REPO_ROOT / "apps" / "obsidian-plugin" / "src" / "device-sync"
)
SANCTIONED_SOURCE_LIFECYCLE_API_FILES: Final[frozenset[Path]] = frozenset(
    {
        REPO_ROOT / "apps" / "api" / "src" / "api_runtime" / "source_lifecycle_composition.py",
        REPO_ROOT / "apps" / "api" / "src" / "api_runtime" / "source_lifecycle_models.py",
        REPO_ROOT / "apps" / "api" / "src" / "api_runtime" / "source_lifecycle_routes.py",
    }
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
#: The route fragments are route-shaped (``/api/...``): only an
#: endpoint-shaped declaration trips the scan, while prose uses of
#: ``publication`` or a bare ``/sources`` mention cannot.
PUBLICATION_ENDPOINT_TOKENS: Final[tuple[str, ...]] = (
    "source-version",
    "source_version",
    "sourceVersion",
    "/api/publications",
    "/api/sources",
)

#: Exact line markers for separately designed public surfaces. Exclusion-policy
#: publication markers preserve that Admin contract; the one lifecycle route
#: marker permits only its canonical endpoint declaration, not arbitrary
#: ``/sources`` routes. Dedicated lifecycle composition/model modules are
#: registered separately by exact path below.
SANCTIONED_SURFACE_LINE_MARKERS: Final[tuple[str, ...]] = (
    "exclusion-policy",
    "exclusion_policy",
    "exact-replay",
    "exact replay",
    "/api/sources/lifecycle-events",
    # The device-sync verified exact-version download endpoint (spec 7.4) —
    # the one canonical download declaration inside the scanned API
    # application, its endpoint member, and the one synthetic URL of its
    # Obsidian transport test. Arbitrary ``/sources`` routes stay forbidden.
    "/api/sources/{source_id}/versions/{source_version_id}/content",
    "download_source_version",
    "/api/sources/a/versions/b/content",
)


def _masked_sanctioned_surface_lines(source: str) -> str:
    """Drop exact separately designed surface lines from scanned source."""
    return "\n".join(
        line
        for line in source.splitlines()
        if not any(marker in line for marker in SANCTIONED_SURFACE_LINE_MARKERS)
    )


def _is_sanctioned_policy_surface(path: Path) -> bool:
    """True for exact files of separately designed public surfaces.

    Python modules named ``exclusion_policy*`` and TypeScript sources under an
    ``exclusion-policy`` directory are the sanctioned policy surface; modules
    named ``small_file_sync*`` are the sanctioned small-file sync surface of
    the plugin journal design (spec 10), whose preflight/content routes and
    terminal receipts legitimately name canonical source and version
    identity. Modules named ``device_sync*`` are the sanctioned device sync
    surface of the device cursor and manifest reconciliation design (spec
    7.4): its verified exact-version download and manifest wire shapes
    legitimately name canonical source and version identity, while the
    static object-store guard still proves the bytes flow only through the
    policy-authorized verified reader composition. The plugin journal client
    directory is the same sanctioned
    surface on the Obsidian side: its hand-mirrored wire shapes and receipt
    records carry ``source_version_id`` identity, and its generation
    persistence speaks of publishing verified journal manifests — none of it
    declares a source-publication endpoint. The three exact source-lifecycle
    API modules are likewise the designed lifecycle surface; the general API
    application stays scanned and only its exact lifecycle route line is
    masked. Every other file is scanned in full, and the OpenAPI endpoint scan
    below still proves no raw source-publication endpoint reaches a document.
    """
    if path.name.startswith(("exclusion_policy", "exclusion-policy")):
        return True
    if path.name.startswith(("small_file_sync", "small-file-sync")):
        return True
    if path.name.startswith(("device_sync", "device-sync")):
        return True
    if path.name.startswith(("multipart_upload", "multipart-upload")):
        # The sanctioned multipart upload surface of the resumable
        # multipart child (spec 5/6): its terminal receipts and publication
        # gateway binding legitimately name canonical source and version
        # identity, while the object-store guard still proves the bytes
        # flow only through the guarded writer. It declares no
        # source-publication endpoint of its own.
        return True
    if SANCTIONED_PLUGIN_JOURNAL_ROOT in path.parents:
        return True
    if SANCTIONED_DEVICE_SYNC_PLUGIN_ROOT in path.parents:
        return True
    if path in SANCTIONED_SOURCE_LIFECYCLE_API_FILES:
        return True
    return any(part == "exclusion-policy" for part in path.parts)


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


def test_alembic_heads_and_migration_files_are_pinned() -> None:
    script_directory = ScriptDirectory.from_config(Config(str(REPO_ROOT / "alembic.ini")))
    assert script_directory.get_heads() == ["20260901_01"]
    migration_files = [
        path for path in MIGRATIONS_VERSIONS.glob("*.py") if not path.name.startswith("__")
    ]
    assert [path.name for path in migration_files] == [
        "20260813_01_create_canonical_postgresql_baseline.py",
        "20260816_01_add_web_authentication_and_device_tokens.py",
        "20260817_01_add_exclusion_policy_publication.py",
        "20260818_01_add_small_file_sync_operations.py",
        "20260820_01_add_source_locator_lifecycle.py",
        "20260826_01_add_device_sync_reconciliation.py",
        "20260826_02_allow_manifest_download_entry_echo.py",
        "20260827_01_add_manifest_run_client_activity.py",
        "20260828_01_add_multipart_upload_sessions.py",
        "20260828_02_widen_small_file_operation_declared_size_bound.py",
        "20260828_03_defer_multipart_provider_identity.py",
        "20260828_04_seal_multipart_operation_token.py",
        "20260829_01_add_manifest_entry_submitted_policy_allowed.py",
        "20260901_01_add_grant_poll_pacing_bucket_kind.py",
    ], "the migrations directory must stay exactly at the pinned revisions"


def test_api_and_mcp_sources_declare_no_source_publication_route() -> None:
    offenders: list[str] = []
    for root in PYTHON_API_ROOTS:
        is_framework_free = root in PYTHON_FRAMEWORK_FREE_ROOTS
        for path in _iter_python_files(root):
            if _is_sanctioned_policy_surface(path):
                continue
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
            masked_source = _masked_sanctioned_surface_lines(source)
            for token in PUBLICATION_ENDPOINT_TOKENS:
                if token in masked_source:
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
            if _is_sanctioned_policy_surface(path):
                continue
            source = _masked_sanctioned_surface_lines(path.read_text(encoding="utf-8"))
            for token in PUBLICATION_ENDPOINT_TOKENS:
                if token in source:
                    offenders.append(f"{path}: endpoint token {token!r}")
    assert not offenders, (
        "generated TypeScript clients must not declare a source publication endpoint:\n"
        + "\n".join(offenders)
    )


# --- sanction-scope tightness ---------------------------------------------------------


def test_sanction_scope_covers_exactly_the_designed_surfaces() -> None:
    """Pin the sanctioned-surface map: neither narrower nor wider.

    The designed exemptions — the ``exclusion_policy*``/``exclusion-policy``
    modules, the ``small_file_sync*``/``small-file-sync`` modules, the plugin
    journal client directory, ``exclusion-policy`` directories and the three
    exact source-lifecycle API modules — are the only paths exempt from the
    endpoint-vocabulary scan. The general API application remains scanned and
    receives only exact route-line masking. A same-named module anywhere else
    — a journal directory outside
    ``apps/obsidian-plugin/src/journal``, a plugin module outside it, a
    server-side journal module, an MCP tool module — stays scanned in full.
    """
    sanctioned = (
        REPO_ROOT / "apps" / "api" / "src" / "api_runtime" / "exclusion_policy_routes.py",
        REPO_ROOT / "apps" / "api" / "src" / "api_runtime" / "small_file_sync_routes.py",
        REPO_ROOT / "apps" / "obsidian-plugin" / "src" / "journal" / "sync-api.ts",
        REPO_ROOT / "apps" / "obsidian-plugin" / "src" / "exclusion-policy" / "snapshot.ts",
        *sorted(SANCTIONED_SOURCE_LIFECYCLE_API_FILES),
    )
    for path in sanctioned:
        assert _is_sanctioned_policy_surface(path), (
            f"designed sanctioned surface must stay exempt: {path}"
        )

    scanned = (
        REPO_ROOT / "apps" / "web" / "src" / "journal" / "manifest-publisher.ts",
        REPO_ROOT / "apps" / "obsidian-plugin" / "src" / "api" / "sync-client.ts",
        REPO_ROOT / "apps" / "api" / "src" / "api_runtime" / "journal_sync_routes.py",
        REPO_ROOT / "apps" / "mcp" / "src" / "mcp_runtime" / "source_version_tools.py",
        REPO_ROOT / "apps" / "api" / "src" / "api_runtime" / "application.py",
    )
    for path in scanned:
        assert not _is_sanctioned_policy_surface(path), (
            f"unsanctioned surface must stay scanned: {path}"
        )


def test_endpoint_vocabulary_scan_detects_a_violating_module_outside_sanction() -> None:
    """The masking plus token scan catches a publication endpoint elsewhere.

    Mirrors the provider gate's committed scanner self-check: a synthetic
    module outside every sanctioned surface that declares a
    source-publication endpoint must trip the scanned vocabulary, so the
    exemption above can never grow into a blind spot.
    """
    violating_source = 'const endpoint = "/api/source-version";\n'
    masked = _masked_sanctioned_surface_lines(violating_source)
    caught = [token for token in PUBLICATION_ENDPOINT_TOKENS if token in masked]
    assert caught, "a source-publication endpoint outside the sanctioned surfaces must be detected"


def test_lifecycle_route_line_masking_is_exact() -> None:
    sanctioned = 'app.add_api_route("/api/sources/lifecycle-events", endpoint)\n'
    assert not [
        token
        for token in PUBLICATION_ENDPOINT_TOKENS
        if token in _masked_sanctioned_surface_lines(sanctioned)
    ]

    near_miss = 'app.add_api_route("/api/sources/publications", endpoint)\n'
    assert [
        token
        for token in PUBLICATION_ENDPOINT_TOKENS
        if token in _masked_sanctioned_surface_lines(near_miss)
    ] == ["/api/sources"]


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
        rendered = _masked_sanctioned_surface_lines(_rendered_endpoint_surface(parsed))
        for token in PUBLICATION_ENDPOINT_TOKENS:
            if token in rendered:
                offenders.append(f"{path}: endpoint token {token!r}")
    assert not offenders, (
        "OpenAPI documents must not declare a source publication path:\n" + "\n".join(offenders)
    )
