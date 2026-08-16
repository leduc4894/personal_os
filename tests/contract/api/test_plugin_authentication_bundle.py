"""Bundle-boundary contract for the Obsidian plugin authentication (spec 19).

The gate builds the real esbuild bundle through pnpm — failing, never
silently skipping, when the toolchain or the bundle is unavailable — then
scans the OUTPUT bundle for the forbidden load-time surface (Electron, Node
built-ins, ``FileSystemAdapter``, the QR generator) and for credential or
source-map leakage. A source-level companion scan pins the closed Obsidian
import surface the bundle is compiled from, so no API beyond ``requestUrl``,
``Platform``, the settings/SecretStorage surface and type-only imports can
reach module load time.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
PLUGIN_ROOT = REPO_ROOT / "apps" / "obsidian-plugin"
PLUGIN_DIST_DIRECTORY = PLUGIN_ROOT / "dist"
BUNDLE_PATH = PLUGIN_DIST_DIRECTORY / "main.js"
PLUGIN_SOURCE_ROOT = PLUGIN_ROOT / "src"

# Exact forbidden load-time capabilities (spec 19 static prohibition).
BUNDLE_FORBIDDEN_SUBSTRINGS = (
    "electron",
    "node:",
    'require("fs',
    "FileSystemAdapter",
    "process.env",
    "qrcode",
)

# Credential sentinels: the versioned credential prefixes of spec 12.1. The
# plugin treats credentials as opaque strings, so no literal prefix — and no
# test fixture value built on one — may survive into the production bundle.
CREDENTIAL_SENTINEL_SUBSTRINGS = ("pg1.", "at1.", "rt1.")

# The closed Obsidian import surface (values or erasable types) of spec 19.
ALLOWED_OBSIDIAN_IMPORT_NAMES = frozenset(
    {
        "App",
        "Platform",
        "Plugin",
        "PluginSettingTab",
        "RequestUrlParam",
        "RequestUrlResponse",
        "Setting",
        "requestUrl",
    }
)

_OBSIDIAN_IMPORT_PATTERN = re.compile(r'import\s+(?:type\s+)?\{([^}]*)\}\s+from\s+"obsidian"')


def _resolve_pnpm() -> str:
    pnpm = shutil.which("pnpm")
    if pnpm is None:
        pytest.fail("pnpm is required to build the Obsidian plugin bundle for this contract")
    return pnpm


def _pnpm_build_command(pnpm: str) -> list[str]:
    arguments = ["--filter", "@workspace/obsidian-plugin", "run", "build"]
    if pnpm.lower().endswith((".cmd", ".bat")):
        return ["cmd.exe", "/c", pnpm, *arguments]
    return [pnpm, *arguments]


@pytest.fixture(scope="module")
def built_bundle() -> str:
    """Build the plugin once per run and return the emitted bundle text."""
    pnpm = _resolve_pnpm()
    completed = subprocess.run(
        _pnpm_build_command(pnpm),
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert completed.returncode == 0, (
        "the Obsidian plugin bundle build failed:\n" + completed.stdout + completed.stderr
    )
    assert BUNDLE_PATH.is_file(), (
        "the Obsidian plugin build produced no dist/main.js bundle to scan"
    )
    return BUNDLE_PATH.read_text(encoding="utf-8")


def test_bundle_has_no_forbidden_load_time_capability(built_bundle: str) -> None:
    for forbidden in BUNDLE_FORBIDDEN_SUBSTRINGS:
        assert forbidden not in built_bundle, (
            f"the plugin bundle must not contain the forbidden capability {forbidden!r}"
        )


def test_bundle_has_no_credential_sentinels(built_bundle: str) -> None:
    for sentinel in CREDENTIAL_SENTINEL_SUBSTRINGS:
        assert sentinel not in built_bundle, (
            f"the plugin bundle must not embed credential material matching {sentinel!r}"
        )


def test_bundle_emits_no_source_maps() -> None:
    emitted = sorted(path.name for path in PLUGIN_DIST_DIRECTORY.iterdir())
    assert emitted == ["main.js", "manifest.json"], (
        "the plugin distribution must stay exactly main.js and manifest.json, got: "
        + ", ".join(emitted)
    )
    for path in PLUGIN_DIST_DIRECTORY.iterdir():
        if path.is_file():
            content = path.read_text(encoding="utf-8")
            assert "sourceMappingURL" not in content, f"{path.name} must not reference source maps"


def test_bundle_keeps_obsidian_external(built_bundle: str) -> None:
    assert 'require("obsidian")' in built_bundle, (
        "the bundle must consume the Obsidian runtime only through its external module"
    )


def test_plugin_sources_import_only_the_closed_obsidian_surface() -> None:
    offenders: list[str] = []
    for path in sorted(PLUGIN_SOURCE_ROOT.rglob("*.ts")):
        source = path.read_text(encoding="utf-8")
        for match in _OBSIDIAN_IMPORT_PATTERN.finditer(source):
            for specifier in match.group(1).split(","):
                name = specifier.strip().split(" as ")[0].strip()
                if name and name not in ALLOWED_OBSIDIAN_IMPORT_NAMES:
                    offenders.append(f"{path}: imports obsidian symbol {name!r}")
    assert offenders == [], (
        "the plugin may import only the closed spec-19 Obsidian surface:\n" + "\n".join(offenders)
    )


def test_plugin_sources_never_import_node_or_electron_modules() -> None:
    offenders: list[str] = []
    for path in sorted(PLUGIN_SOURCE_ROOT.rglob("*.ts")):
        if path.name.endswith(".test.ts"):
            continue
        source = path.read_text(encoding="utf-8")
        for forbidden in ("node:", "electron", "FileSystemAdapter"):
            if forbidden in source:
                offenders.append(f"{path}: contains forbidden text {forbidden!r}")
                break
    assert offenders == [], (
        "plugin sources must never reference Node, Electron or FileSystemAdapter:\n"
        + "\n".join(offenders)
    )
