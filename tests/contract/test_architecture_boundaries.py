from __future__ import annotations

import ast
import configparser
import os
import re
import subprocess
from collections.abc import Iterator
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES_ROOT = REPO_ROOT / "tests" / "fixtures"
INVALID_FIXTURE_DIR = FIXTURES_ROOT / "architecture" / "invalid_python"
INVALID_FIXTURE_CONFIG = INVALID_FIXTURE_DIR / ".importlinter"

# Real Python source roots (excludes tests/, which legitimately imports sys).
PYTHON_SOURCE_ROOTS = [
    REPO_ROOT / "src",
    REPO_ROOT / "apps" / "api" / "src",
    REPO_ROOT / "apps" / "mcp" / "src",
    REPO_ROOT / "apps" / "worker" / "src",
    REPO_ROOT / "packages" / "r2-object-storage" / "src",
    REPO_ROOT / "packages" / "postgresql-source-store" / "src",
    REPO_ROOT / "tools",
]

# Real TypeScript source roots.
TS_SOURCE_ROOTS = [
    REPO_ROOT / "apps" / "web" / "src",
    REPO_ROOT / "apps" / "obsidian-plugin" / "src",
    REPO_ROOT / "packages" / "api-client" / "src",
]

# Module specifiers that would bridge the Web and Obsidian members.
WEB_FORBIDDEN_SUBSTRINGS = ("obsidian-plugin",)
OBSIDIAN_FORBIDDEN_SUBSTRINGS = ("web-runtime", "apps/web")
# The shared api client serves both consumers; it must never import either.
API_CLIENT_FORBIDDEN_SUBSTRINGS = ("web-runtime", "obsidian-plugin", "apps/web")
# Alias prefixes that neither member tsconfig defines; their use would let a
# import escape the member without a relative path.
ALIAS_PREFIXES = ("@/", "~/")

_STATIC_FROM_RE = re.compile(r"\bfrom\s+[\"']([^\"']+)[\"']")
_SIDE_EFFECT_IMPORT_RE = re.compile(r"\bimport\s+[\"']([^\"']+)[\"']")
_DYNAMIC_IMPORT_RE = re.compile(r"\bimport\s*\(\s*[\"']([^\"']+)[\"']\s*\)")


def _is_sys_path_attr(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "path"
        and isinstance(node.value, ast.Name)
        and node.value.id == "sys"
    )


def _target_touches_sys_path(target: ast.AST) -> bool:
    return _is_sys_path_attr(target) or (
        isinstance(target, ast.Subscript) and _is_sys_path_attr(target.value)
    )


def _scan_sys_path_mutations(tree: ast.AST, path: Path) -> list[str]:
    offenders: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and _is_sys_path_attr(node.func.value)
        ):
            offenders.append(f"{path}: sys.path.{node.func.attr}() call")
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if _target_touches_sys_path(target):
                    offenders.append(f"{path}: sys.path assignment")
        if isinstance(node, ast.AugAssign) and _target_touches_sys_path(node.target):
            offenders.append(f"{path}: sys.path augmented assignment")
        if isinstance(node, ast.Delete):
            for target in node.targets:
                if _target_touches_sys_path(target):
                    offenders.append(f"{path}: sys.path deletion")
    return offenders


def _extract_import_specifiers(source: str) -> list[str]:
    specifiers: list[str] = []
    for pattern in (_STATIC_FROM_RE, _SIDE_EFFECT_IMPORT_RE, _DYNAMIC_IMPORT_RE):
        specifiers.extend(pattern.findall(source))
    return specifiers


def _classify_member(path: Path) -> str:
    if path.is_relative_to(REPO_ROOT / "apps" / "web"):
        return "web"
    if path.is_relative_to(REPO_ROOT / "apps" / "obsidian-plugin"):
        return "obsidian-plugin"
    if path.is_relative_to(REPO_ROOT / "packages" / "api-client"):
        return "api-client"
    return "other"


def _iter_python_files() -> list[Path]:
    files: list[Path] = []
    for root in PYTHON_SOURCE_ROOTS:
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            if not path.is_relative_to(FIXTURES_ROOT):
                files.append(path)
    return files


def _iter_typescript_files() -> list[Path]:
    files: list[Path] = []
    for root in TS_SOURCE_ROOTS:
        for path in root.rglob("*"):
            if path.suffix in (".ts", ".tsx") and not path.is_relative_to(FIXTURES_ROOT):
                files.append(path)
    return files


def test_repository_import_contracts_pass() -> None:
    completed = subprocess.run(
        ["lint-imports"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, (
        "lint-imports reported contract violations:\n" + completed.stdout + completed.stderr
    )


def test_invalid_import_fixture_is_rejected_by_lint_imports() -> None:
    env = os.environ.copy()
    existing_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        f"{INVALID_FIXTURE_DIR}{os.pathsep}{existing_pythonpath}"
        if existing_pythonpath
        else str(INVALID_FIXTURE_DIR)
    )
    completed = subprocess.run(
        ["lint-imports", "--config", str(INVALID_FIXTURE_CONFIG)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert completed.returncode != 0, (
        "the invalid fixture must trip lint-imports, but it exited 0:\n"
        + completed.stdout
        + completed.stderr
    )
    combined = completed.stdout + completed.stderr
    assert "Broken contracts" in combined, (
        "expected a broken-contract report from lint-imports:\n" + combined
    )


CORE_FORBIDDEN_SECTION = (
    "importlinter:contract:domain-does-not-import-composition-or-infrastructure"
)
R2_FORBIDDEN_SECTION = (
    "importlinter:contract:r2-adapter-does-not-import-composition-or-infrastructure"
)
POSTGRESQL_FORBIDDEN_SECTION = (
    "importlinter:contract:postgresql-adapter-does-not-import-composition-or-infrastructure"
)
TEMPORAL_FORBIDDEN_SECTION = "importlinter:contract:temporal-sdk-imports-only-from-worker"


def test_importlinter_core_contract_forbids_provider_packages() -> None:
    parser = configparser.ConfigParser()
    parser.read(REPO_ROOT / ".importlinter")
    forbidden = parser[CORE_FORBIDDEN_SECTION]["forbidden_modules"].split()
    for provider in (
        "r2_object_storage",
        "postgresql_source_store",
        "aiobotocore",
        "botocore",
        "aiohttp",
    ):
        assert provider in forbidden, f"core forbidden contract must forbid importing {provider!r}"


def test_importlinter_r2_contract_isolates_adapter_from_composition_roots() -> None:
    parser = configparser.ConfigParser()
    parser.read(REPO_ROOT / ".importlinter")
    assert parser.has_section(R2_FORBIDDEN_SECTION), (
        "an importlinter forbidden contract must isolate r2_object_storage"
    )
    section = parser[R2_FORBIDDEN_SECTION]
    assert section["type"] == "forbidden"
    assert section["source_modules"].split() == ["r2_object_storage"]
    forbidden = set(section["forbidden_modules"].split())
    expected_forbidden = {
        "api_runtime",
        "mcp_runtime",
        "workflow_worker",
        "fastapi",
        "sqlalchemy",
        "psycopg",
        "temporalio",
        "qdrant_client",
        "neo4j",
        "redis",
    }
    assert expected_forbidden <= forbidden, (
        "r2 adapter forbidden contract must block every composition root and "
        f"infrastructure SDK, missing: {expected_forbidden - forbidden}"
    )


def test_importlinter_postgresql_contract_isolates_adapter_from_composition_roots() -> None:
    parser = configparser.ConfigParser()
    parser.read(REPO_ROOT / ".importlinter")
    assert parser.has_section(POSTGRESQL_FORBIDDEN_SECTION), (
        "an importlinter forbidden contract must isolate postgresql_source_store"
    )
    section = parser[POSTGRESQL_FORBIDDEN_SECTION]
    assert section["type"] == "forbidden"
    assert section["source_modules"].split() == ["postgresql_source_store"]
    forbidden = set(section["forbidden_modules"].split())
    expected_forbidden = {
        "api_runtime",
        "mcp_runtime",
        "workflow_worker",
        "r2_object_storage",
        "temporalio",
        "qdrant_client",
        "neo4j",
        "redis",
    }
    assert expected_forbidden <= forbidden, (
        "postgresql adapter forbidden contract must block every composition root, "
        "the R2 adapter and every non-SQL infrastructure SDK, missing: "
        f"{expected_forbidden - forbidden}"
    )


def test_importlinter_temporal_sdk_is_only_importable_by_worker() -> None:
    parser = configparser.ConfigParser()
    parser.read(REPO_ROOT / ".importlinter")
    assert parser.has_section(TEMPORAL_FORBIDDEN_SECTION), (
        "an importlinter forbidden contract must reserve temporalio for workflow_worker"
    )
    section = parser[TEMPORAL_FORBIDDEN_SECTION]
    assert section["type"] == "forbidden"
    assert section["forbidden_modules"].split() == ["temporalio"]
    assert section["source_modules"].split() == [
        "api_runtime",
        "mcp_runtime",
        "personal_os",
        "r2_object_storage",
        "postgresql_source_store",
    ], "every root except workflow_worker must forbid importing temporalio"


def test_python_source_never_mutates_sys_path() -> None:
    offenders: list[str] = []
    for path in _iter_python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        offenders.extend(_scan_sys_path_mutations(tree, path))
    assert not offenders, "Python source must not mutate sys.path:\n" + "\n".join(offenders)


# The framework-neutral API contract package: exactly these modules, and never a
# web framework, database driver, provider SDK, composition root or sibling
# adapter import (spec 4.1: Pydantic and core contracts only).
API_CONTRACTS_ROOT = REPO_ROOT / "src" / "personal_os" / "api_contracts"
API_CONTRACTS_MODULE_FILES = (
    "__init__.py",
    "envelopes.py",
    "errors.py",
    "health.py",
    "request_values.py",
)
API_CONTRACTS_FORBIDDEN_IMPORT_ROOTS = frozenset(
    {
        "aiohttp",
        "aiobotocore",
        "api_runtime",
        "boto3",
        "botocore",
        "fastapi",
        "mcp",
        "mcp_runtime",
        "neo4j",
        "psycopg",
        "qdrant_client",
        "r2_object_storage",
        "redis",
        "sqlalchemy",
        "temporalio",
        "uvicorn",
        "workflow_worker",
    }
)


def _iter_imported_module_names(tree: ast.AST) -> Iterator[str]:
    """Yield every absolute module name imported by one parsed module."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom) and node.module is not None and node.level == 0:
            yield node.module


def test_api_contracts_modules_reject_web_framework_and_provider_imports() -> None:
    module_paths = sorted(API_CONTRACTS_ROOT.rglob("*.py"))
    assert [path.name for path in module_paths] == sorted(API_CONTRACTS_MODULE_FILES), (
        "personal_os.api_contracts must stay the closed five-module contract package"
    )
    offenders: list[str] = []
    for path in module_paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for module_name in _iter_imported_module_names(tree):
            root = module_name.partition(".")[0]
            if root in API_CONTRACTS_FORBIDDEN_IMPORT_ROOTS:
                offenders.append(f"{path}: forbidden import {module_name!r}")
    assert not offenders, (
        "personal_os.api_contracts must not import web frameworks, database drivers, "
        "provider SDKs, composition roots or sibling adapters:\n" + "\n".join(offenders)
    )


# The framework-neutral authentication domain package: exactly these modules,
# and never a crypto implementation package, web framework, database driver,
# provider SDK, composition root or sibling adapter. The concrete Argon2id and
# AEAD/HKDF/HMAC adapters live in the API composition root, so the domain
# package pins only parameter constants and ports.
AUTHENTICATION_DOMAIN_ROOT = REPO_ROOT / "src" / "personal_os" / "authentication"
AUTHENTICATION_DOMAIN_MODULE_FILES = (
    "__init__.py",
    "contracts.py",
    "crypto.py",
    "errors.py",
    "passwords.py",
    "ports.py",
)
AUTHENTICATION_DOMAIN_FORBIDDEN_IMPORT_ROOTS = API_CONTRACTS_FORBIDDEN_IMPORT_ROOTS | {
    "argon2",
    "cryptography",
}


def test_authentication_domain_rejects_crypto_and_framework_imports() -> None:
    module_paths = sorted(path for path in AUTHENTICATION_DOMAIN_ROOT.rglob("*.py"))
    assert module_paths, "personal_os.authentication must exist as the domain package"
    assert [path.name for path in module_paths] == sorted(AUTHENTICATION_DOMAIN_MODULE_FILES), (
        "personal_os.authentication must stay the closed six-module domain package"
    )
    offenders: list[str] = []
    for path in module_paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for module_name in _iter_imported_module_names(tree):
            root = module_name.partition(".")[0]
            if root in AUTHENTICATION_DOMAIN_FORBIDDEN_IMPORT_ROOTS:
                offenders.append(f"{path}: forbidden import {module_name!r}")
    assert not offenders, (
        "personal_os.authentication must not import crypto implementations, web frameworks, "
        "database drivers, provider SDKs, composition roots or sibling adapters:\n"
        + "\n".join(offenders)
    )


def test_typescript_imports_stay_within_member_boundaries() -> None:
    offenders: list[str] = []
    for path in _iter_typescript_files():
        member = _classify_member(path)
        source = path.read_text(encoding="utf-8")
        for specifier in _extract_import_specifiers(source):
            if specifier.startswith(ALIAS_PREFIXES):
                offenders.append(f"{path}: undefined path alias {specifier!r}")
                continue
            if member == "web" and any(token in specifier for token in WEB_FORBIDDEN_SUBSTRINGS):
                offenders.append(f"{path}: web imports obsidian-plugin via {specifier!r}")
            elif member == "obsidian-plugin" and any(
                token in specifier for token in OBSIDIAN_FORBIDDEN_SUBSTRINGS
            ):
                offenders.append(f"{path}: obsidian-plugin imports web via {specifier!r}")
            elif member == "api-client" and any(
                token in specifier for token in API_CLIENT_FORBIDDEN_SUBSTRINGS
            ):
                offenders.append(f"{path}: api-client imports a consumer via {specifier!r}")
    assert not offenders, (
        "TypeScript imports must not cross member boundaries (web, obsidian-plugin, "
        "shared api-client) or use undefined path aliases:\n" + "\n".join(offenders)
    )
