from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES_ROOT = REPO_ROOT / "tests" / "fixtures"

# A canonical exact Python pin: ``name==x.y[.z...]``. Two- and three-component
# versions are both legitimate exact pins (e.g. ``import-linter==2.13``); the
# rule rejects wildcards, ranges, markers, extras, prereleases and VCS URLs by
# anchoring the whole specifier to this shape.
_PYTHON_NAME = r"[A-Za-z0-9][A-Za-z0-9._-]*"
_VERSION = r"\d+(?:\.\d+)*"
PYTHON_PIN_RE = re.compile(rf"^{_PYTHON_NAME}=={_VERSION}$")
# A canonical exact npm pin: a bare ``x.y[.z...]`` version literal with no
# caret, tilde, wildcard, range, prerelease, tag or package alias.
NPM_PIN_RE = re.compile(rf"^{_VERSION}$")

ALLOWED_SOURCE: dict[str, bool] = {"workspace": True}


def _iter_python_manifests() -> list[Path]:
    manifests = [REPO_ROOT / "pyproject.toml"]
    manifests.extend(sorted((REPO_ROOT / "apps").glob("*/pyproject.toml")))
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
        "(no ranges, wildcards, markers, extras, prereleases or VCS URLs):\n"
        + "\n".join(violations)
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


def test_npm_registry_dependencies_are_bare_versions() -> None:
    violations: list[str] = []
    for manifest in _iter_npm_manifests():
        data = json.loads(manifest.read_text(encoding="utf-8"))
        for section in ("dependencies", "devDependencies", "optionalDependencies"):
            entries = data.get(section, {})
            if not isinstance(entries, dict):
                continue
            for name, version in entries.items():
                if not NPM_PIN_RE.match(str(version)):
                    violations.append(
                        f"{manifest.relative_to(REPO_ROOT)}:{section} {name}={version!r}"
                    )
    assert not violations, (
        "npm dependencies must be bare x.y.z versions "
        "(no ranges, wildcards, prereleases, tags, aliases or git URLs):\n" + "\n".join(violations)
    )


def test_pnpm_workspace_only_builds_esbuild() -> None:
    workspace_path = REPO_ROOT / "pnpm-workspace.yaml"
    data = yaml.safe_load(workspace_path.read_text(encoding="utf-8"))
    built = data.get("onlyBuiltDependencies", [])
    assert built == ["esbuild"], f"onlyBuiltDependencies must be exactly ['esbuild'], got {built!r}"
