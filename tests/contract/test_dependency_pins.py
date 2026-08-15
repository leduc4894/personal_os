from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES_ROOT = REPO_ROOT / "tests" / "fixtures"

# A canonical exact Python pin: ``name==x.y[.z...]`` or ``name[extra]==x.y[.z...]``.
# Two- and three-component versions are both legitimate exact pins (e.g.
# ``import-linter==2.13``); the optional ``[extra,...]`` clause permits a pinned
# extras selection only. The rule rejects wildcards, ranges, environment
# markers, prereleases and VCS URLs by anchoring the whole specifier to this
# shape.
_PYTHON_NAME = r"[A-Za-z0-9][A-Za-z0-9._-]*"
_PYTHON_EXTRAS = r"(?:\[[A-Za-z0-9._-]+(?:,[A-Za-z0-9._-]+)*\])?"
_VERSION = r"\d+(?:\.\d+)*"
PYTHON_PIN_RE = re.compile(rf"^{_PYTHON_NAME}{_PYTHON_EXTRAS}=={_VERSION}$")
# A canonical exact npm pin: a bare ``x.y[.z...]`` version literal with no
# caret, tilde, wildcard, range, prerelease, tag or package alias.
NPM_PIN_RE = re.compile(rf"^{_VERSION}$")
# pnpm workspace protocol: links a package inside the workspace and never
# resolves against the npm registry, so it is not a registry specifier.
NPM_WORKSPACE_PROTOCOL = "workspace:"

ALLOWED_SOURCE: dict[str, bool] = {"workspace": True}


def _iter_python_manifests() -> list[Path]:
    manifests = [REPO_ROOT / "pyproject.toml"]
    manifests.extend(sorted((REPO_ROOT / "apps").glob("*/pyproject.toml")))
    manifests.extend(sorted((REPO_ROOT / "packages").glob("*/pyproject.toml")))
    return [path for path in manifests if not path.is_relative_to(FIXTURES_ROOT)]


def _iter_npm_manifests() -> list[Path]:
    manifests = [REPO_ROOT / "package.json"]
    manifests.extend(sorted((REPO_ROOT / "apps").glob("*/package.json")))
    return [path for path in manifests if not path.is_relative_to(FIXTURES_ROOT)]


def _python_dependency_specs(data: dict[str, Any]) -> list[str]:
    specs: list[str] = []
    project = data.get("project", {})
    if isinstance(project, dict):
        dependencies = project.get("dependencies", [])
        if isinstance(dependencies, list):
            specs.extend(str(spec) for spec in dependencies)
        optional = project.get("optional-dependencies", {})
        if isinstance(optional, dict):
            for group in optional.values():
                if isinstance(group, list):
                    specs.extend(str(spec) for spec in group)
    dependency_groups = data.get("dependency-groups", {})
    if isinstance(dependency_groups, dict):
        for group in dependency_groups.values():
            if isinstance(group, list):
                specs.extend(str(spec) for spec in group)
    build_system = data.get("build-system", {})
    if isinstance(build_system, dict):
        requires = build_system.get("requires", [])
        if isinstance(requires, list):
            specs.extend(str(spec) for spec in requires)
    return specs


def test_python_registry_dependencies_are_exact_pins() -> None:
    violations: list[str] = []
    for manifest in _iter_python_manifests():
        data = tomllib.loads(manifest.read_text(encoding="utf-8"))
        for spec in _python_dependency_specs(data):
            if not PYTHON_PIN_RE.match(spec):
                violations.append(f"{manifest.relative_to(REPO_ROOT)}: {spec!r}")
    assert not violations, (
        "Python dependencies must be exact name==version pins "
        "(optionally with a pinned [extra] list; no ranges, wildcards, markers, "
        "prereleases or VCS URLs):\n" + "\n".join(violations)
    )


def test_uv_sources_only_allow_workspace_true() -> None:
    violations: list[str] = []
    for manifest in _iter_python_manifests():
        data = tomllib.loads(manifest.read_text(encoding="utf-8"))
        sources = (
            data.get("tool", {}).get("uv", {}).get("sources", {})
            if isinstance(data.get("tool"), dict)
            else {}
        )
        for name, value in sources.items():
            if value != ALLOWED_SOURCE:
                violations.append(f"{manifest.relative_to(REPO_ROOT)}: {name}={value!r}")
    assert not violations, (
        "tool.uv.sources may only declare { workspace = true } "
        "(no git/path/url sources):\n" + "\n".join(violations)
    )


def _npm_registry_violations(manifest: Path, data: dict[str, Any]) -> list[str]:
    violations: list[str] = []
    for section in ("dependencies", "devDependencies", "optionalDependencies"):
        entries = data.get(section, {})
        if not isinstance(entries, dict):
            continue
        for name, version in entries.items():
            specifier = str(version)
            if specifier.startswith(NPM_WORKSPACE_PROTOCOL):
                continue
            if not NPM_PIN_RE.match(specifier):
                violations.append(
                    f"{manifest.relative_to(REPO_ROOT)}:{section} {name}={specifier!r}"
                )
    return violations


def test_npm_registry_dependencies_are_bare_versions() -> None:
    violations: list[str] = []
    for manifest in _iter_npm_manifests():
        data = json.loads(manifest.read_text(encoding="utf-8"))
        violations.extend(_npm_registry_violations(manifest, data))
    assert not violations, (
        "npm registry dependencies must be bare x.y.z versions "
        "(no ranges, wildcards, prereleases, tags, aliases or git URLs; "
        "workspace: protocol specifiers are exempt because pnpm links them "
        "inside the workspace):\n" + "\n".join(violations)
    )


def test_npm_registry_guard_exempts_workspace_protocol_only() -> None:
    manifest = REPO_ROOT / "apps" / "pin-guard-sample" / "package.json"
    workspace_linked = {
        "dependencies": {"@workspace/api-client": "workspace:*"},
        "devDependencies": {"@workspace/api-client": "workspace:^1.2.3"},
        "optionalDependencies": {"@workspace/api-client": "workspace:~2.0.0"},
    }
    assert _npm_registry_violations(manifest, workspace_linked) == []

    rejected_registry_specifiers = [
        "^1.2.3",
        "~1.2.3",
        ">=1.0.0 <2.0.0",
        "1.2.x",
        "*",
        "latest",
        "next",
        "npm:aliased-package@1.2.3",
        "git+https://example.com/repo.git",
        "https://example.com/pkg.tgz",
        "1.2.3-beta.1",
    ]
    for specifier in rejected_registry_specifiers:
        violations = _npm_registry_violations(
            manifest, {"dependencies": {"registry-package": specifier}}
        )
        assert len(violations) == 1, (
            f"expected exactly one violation for {specifier!r}, got {violations}"
        )
        assert f"registry-package={specifier!r}" in violations[0]


def test_pnpm_workspace_only_builds_esbuild() -> None:
    workspace_path = REPO_ROOT / "pnpm-workspace.yaml"
    data = yaml.safe_load(workspace_path.read_text(encoding="utf-8"))
    built = data.get("onlyBuiltDependencies", [])
    assert built == ["esbuild"], f"onlyBuiltDependencies must be exactly ['esbuild'], got {built!r}"


def test_r2_workspace_member_has_exact_sdk_pins() -> None:
    manifest = tomllib.loads(
        (REPO_ROOT / "packages" / "r2-object-storage" / "pyproject.toml").read_text("utf-8")
    )
    assert manifest["project"]["dependencies"] == [
        "aiobotocore==3.9.0",
        "knowledge-core==0.1.0",
    ]
    assert manifest["dependency-groups"]["dev"] == ["types-aiobotocore[s3]==3.9.0"]


def test_postgresql_source_store_has_exact_dependencies() -> None:
    manifest = tomllib.loads(
        (REPO_ROOT / "packages/postgresql-source-store/pyproject.toml").read_text("utf-8")
    )
    assert manifest["project"]["dependencies"] == [
        "knowledge-core==0.1.0",
        "psycopg[binary]==3.3.4",
        "SQLAlchemy==2.0.51",
    ]


def test_worker_has_exact_publication_dependencies() -> None:
    manifest = tomllib.loads((REPO_ROOT / "apps/worker/pyproject.toml").read_text("utf-8"))
    assert manifest["project"]["dependencies"] == [
        "knowledge-core==0.1.0",
        "postgresql-source-store==0.1.0",
        "temporalio==1.30.0",
    ]


def test_root_dev_group_pins_pytest_asyncio() -> None:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert "pytest-asyncio==1.4.0" in data["dependency-groups"]["dev"]
