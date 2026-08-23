"""Bundle-boundary contract for the Obsidian plugin (spec 19, journal design).

The gate builds the real esbuild bundle through pnpm — failing, never
silently skipping, when the toolchain or the bundle is unavailable — then
scans the OUTPUT bundle for the forbidden load-time surface (Electron, Node
built-ins, ``FileSystemAdapter``, the QR generator) and for credential or
source-map leakage. A source-level companion scan pins the closed Obsidian
import surface the bundle is compiled from, so no API beyond ``requestUrl``,
``Platform``, the settings/SecretStorage surface and type-only imports can
reach module load time.

The journal design extends the same gate for the portable SQLite journal:
the pinned ``sql.js`` WebAssembly package is the one permitted database
engine, its esbuild module segments are excised before the bundle scan
(inert emscripten Node-detection text included), a probe bundle proves that
permission actually holds for the real esbuild configuration, and the
source scan additionally rejects native SQLite drivers, ORMs and hard-coded
Vault config-directory paths.
"""

from __future__ import annotations

import json
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

# Vault-path sentinel: journal files resolve their directory through
# Vault.configDir (journal design 6.1), so a literal config-directory name is
# a forbidden hard-coded Vault path.
VAULT_CONFIG_DIRECTORY_SENTINEL = ".obsidian"

# Full-digest sentinel: a quoted 64-character lowercase hex literal is a full
# SHA-256 digest, which diagnostics must never emit (journal design 9). Curve
# constants in the existing dependencies are 0x-prefixed bigints, not quoted
# digest strings, so they stay outside this pattern.
FULL_SHA256_LITERAL_PATTERN = re.compile(r"""["']([0-9a-f]{64})["']""")

# Native SQLite drivers and ORMs the portable journal must never import
# instead of sql.js (plan global constraints).
FORBIDDEN_NATIVE_SQL_TEXT_SUBSTRINGS = (
    "better-sqlite3",
    "sqlite3",
    "drizzle-orm",
    "typeorm",
    "sequelize",
    "prisma",
    "knex",
)

# The pinned portable journal engine: sql.js WebAssembly SQLite, exact
# production pin, no native runtime requirement (journal design 6.1).
SQLJS_PACKAGE_NAME = "sql.js"
PINNED_SQLJS_VERSION = "1.14.2"

# esbuild emits one `// <module-path>` marker comment per bundled module
# (the production build does not minify). sql.js is resolved through its
# package `exports` browser condition, so the bundled library is the
# `dist/sql-wasm-browser.js` module.
SQLJS_BUNDLED_MODULE_PATH_PREFIX = f"{SQLJS_PACKAGE_NAME}/dist/"
_ESBUILD_MODULE_MARKER_PATTERN = re.compile(r"^// (\S*/\S*)$", re.MULTILINE)

# The closed Obsidian import surface (values or erasable types) of spec 19.
# ``Notice`` is a deliberate spec-19 closed-surface addition by the sync error
# tracing design (2026-08-23): the UI notice surface of the two diagnostics
# commands (``Copy sync diagnostics``, ``Run sync self-check``). The addition
# is mirrored in the plugin-side import scan of ``src/plugin.test.ts``.
ALLOWED_OBSIDIAN_IMPORT_NAMES = frozenset(
    {
        "App",
        "Modal",
        "Notice",
        "Platform",
        "Plugin",
        "PluginSettingTab",
        "RequestUrlParam",
        "RequestUrlResponse",
        "Setting",
        "TAbstractFile",
        "TFile",
        "requestUrl",
    }
)

_OBSIDIAN_IMPORT_PATTERN = re.compile(r'import\s+(?:type\s+)?\{([^}]*)\}\s+from\s+"obsidian"')

# Mirrors apps/obsidian-plugin/scripts/build-plugin.mjs so the permission
# probe below exercises the exact esbuild configuration of the real bundle.
SQLJS_PERMISSION_PROBE_SCRIPT = """
import { build } from "esbuild";
const result = await build({
  stdin: {
    contents: 'import initSqlJs from "sql.js";\\nexport default initSqlJs;\\n',
    resolveDir: process.cwd(),
    sourcefile: "sqljs-permission-probe.ts",
    loader: "ts",
  },
  bundle: true,
  format: "cjs",
  platform: "browser",
  target: "es2022",
  sourcemap: false,
  write: false,
});
process.stdout.write(result.outputFiles[0].text);
"""


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


def _sqljs_browser_entry_relative_path() -> str:
    """The dist entry the package `exports` browser condition resolves to."""
    package_root = PLUGIN_ROOT / "node_modules" / SQLJS_PACKAGE_NAME
    if not package_root.is_dir():
        pytest.fail("the pinned sql.js dependency must be installed before this contract can run")
    manifest = json.loads((package_root / "package.json").read_text(encoding="utf-8"))
    entry = manifest.get("exports", {}).get(".", {}).get("browser") or manifest["main"]
    return entry.removeprefix("./")


def _bundled_sqljs_module_segments(bundle: str) -> list[str]:
    """Return the esbuild module segments of the bundled sql.js library.

    Each segment runs from the library's `// <path>` marker comment to the
    next module marker. When the library is absent the list is empty and the
    caller scans the bundle unchanged.
    """
    markers = list(_ESBUILD_MODULE_MARKER_PATTERN.finditer(bundle))
    segments: list[str] = []
    for index, marker in enumerate(markers):
        if SQLJS_BUNDLED_MODULE_PATH_PREFIX not in marker.group(1):
            continue
        segment_end = markers[index + 1].start() if index + 1 < len(markers) else len(bundle)
        segments.append(bundle[marker.start() : segment_end])
    return segments


def _strip_bundled_sqljs_module_segments(bundle: str) -> str:
    """Excise the permitted sql.js module segments before scanning a bundle.

    sql.js is the only permitted third-party database engine; its emscripten
    glue embeds inert Node-detection text (including the literal ``node:``),
    so exactly its esbuild module segments are removed. Plugin-authored
    modules keep their markers and stay fully scanned, and a bundled sql.js
    copy that no longer carries a recognizable module marker simply fails
    the scan (fail-closed).
    """
    markers = list(_ESBUILD_MODULE_MARKER_PATTERN.finditer(bundle))
    residual_parts: list[str] = []
    cursor = 0
    for index, marker in enumerate(markers):
        segment_start = marker.start()
        segment_end = markers[index + 1].start() if index + 1 < len(markers) else len(bundle)
        residual_parts.append(bundle[cursor:segment_start])
        if SQLJS_BUNDLED_MODULE_PATH_PREFIX not in marker.group(1):
            residual_parts.append(bundle[segment_start:segment_end])
        cursor = segment_end
    residual_parts.append(bundle[cursor:])
    return "".join(residual_parts)


@pytest.fixture(scope="module")
def sqljs_permission_probe_bundle() -> str:
    """Bundle a stdin entry importing sql.js with the real esbuild settings.

    Task 1 adds the dependency without importing it, so this probe is the
    proof that the gate genuinely permits the selected WebAssembly package
    under the exact esbuild configuration the production bundle uses.
    """
    node = shutil.which("node")
    if node is None:
        pytest.fail("node is required to build the sql.js permission probe for this contract")
    completed = subprocess.run(
        [node, "--input-type=module", "-e", SQLJS_PERMISSION_PROBE_SCRIPT],
        cwd=PLUGIN_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert completed.returncode == 0, (
        "the sql.js permission probe bundle failed to build:\n"
        + completed.stdout
        + completed.stderr
    )
    return completed.stdout


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
    scanned = _strip_bundled_sqljs_module_segments(built_bundle)
    for forbidden in BUNDLE_FORBIDDEN_SUBSTRINGS:
        assert forbidden not in scanned, (
            f"the plugin bundle must not contain the forbidden capability {forbidden!r}"
        )


def test_bundle_has_no_credential_sentinels(built_bundle: str) -> None:
    for sentinel in CREDENTIAL_SENTINEL_SUBSTRINGS:
        assert sentinel not in built_bundle, (
            f"the plugin bundle must not embed credential material matching {sentinel!r}"
        )


def test_bundle_has_no_vault_path_or_full_digest_sentinels(built_bundle: str) -> None:
    assert VAULT_CONFIG_DIRECTORY_SENTINEL not in built_bundle, (
        "the plugin bundle must not hard-code the Vault config directory name; "
        "journal paths resolve through Vault.configDir"
    )
    digests = FULL_SHA256_LITERAL_PATTERN.findall(built_bundle)
    assert digests == [], (
        "the plugin bundle must not embed full SHA-256 digest literals: " + ", ".join(digests)
    )


def test_bundle_emits_no_source_maps() -> None:
    # The distribution contract: the two load-time artifacts plus exactly one
    # asset — the vendored sql.js WebAssembly engine the journal loads
    # lazily from the plugin directory (journal design 6.1).
    emitted = sorted(path.name for path in PLUGIN_DIST_DIRECTORY.iterdir())
    assert emitted == ["main.js", "manifest.json", "sql-wasm.wasm"], (
        "the plugin distribution must stay exactly main.js, manifest.json and "
        "the sql-wasm.wasm engine asset, got: " + ", ".join(emitted)
    )
    for path in PLUGIN_DIST_DIRECTORY.iterdir():
        if path.suffix in {".js", ".json"}:
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


def test_plugin_sources_never_reference_forbidden_platform_modules() -> None:
    offenders: list[str] = []
    for path in sorted(PLUGIN_SOURCE_ROOT.rglob("*.ts")):
        if path.name.endswith(".test.ts"):
            continue
        source = path.read_text(encoding="utf-8")
        for forbidden in (
            "node:",
            "electron",
            "FileSystemAdapter",
            *FORBIDDEN_NATIVE_SQL_TEXT_SUBSTRINGS,
            VAULT_CONFIG_DIRECTORY_SENTINEL,
        ):
            if forbidden in source:
                offenders.append(f"{path}: contains forbidden text {forbidden!r}")
                break
    assert offenders == [], (
        "plugin sources must never reference Node, Electron, FileSystemAdapter, "
        "a native SQLite driver, an ORM or a hard-coded Vault path:\n" + "\n".join(offenders)
    )


def test_plugin_declares_the_pinned_portable_sqlite_dependency() -> None:
    package = json.loads((PLUGIN_ROOT / "package.json").read_text(encoding="utf-8"))
    dependencies = package.get("dependencies", {})
    declared = dependencies.get(SQLJS_PACKAGE_NAME)
    assert declared == PINNED_SQLJS_VERSION, (
        f"the portable journal must pin sql.js at exactly {PINNED_SQLJS_VERSION} "
        f"in production dependencies, got {declared!r}"
    )
    assert package.get("devDependencies", {}).get(SQLJS_PACKAGE_NAME) is None, (
        "sql.js is a production dependency of the journal, not a dev dependency"
    )


def test_bundle_boundary_permits_the_vendored_wasm_sqlite_package(
    sqljs_permission_probe_bundle: str,
) -> None:
    browser_entry = _sqljs_browser_entry_relative_path()
    bundled_path = f"{SQLJS_PACKAGE_NAME}/{browser_entry}"
    assert bundled_path in sqljs_permission_probe_bundle, (
        "the sql.js permission probe must actually bundle the vendored sql.js "
        f"{browser_entry} module"
    )
    segments = _bundled_sqljs_module_segments(sqljs_permission_probe_bundle)
    assert segments != [], "the probe bundle must contain an excisable sql.js module segment"
    # The permission is load-bearing: the emscripten glue really carries inert
    # Node-detection text that the scan must excuse only inside its segments.
    assert any("node:" in segment for segment in segments), (
        "the vendored sql.js module is expected to embed inert Node-detection text"
    )
    scanned = _strip_bundled_sqljs_module_segments(sqljs_permission_probe_bundle)
    assert "node:" not in scanned, "the excision must remove the sql.js module text"
    for forbidden in BUNDLE_FORBIDDEN_SUBSTRINGS:
        assert forbidden not in scanned, (
            f"the permitted sql.js module must not carry forbidden capability {forbidden!r} "
            "outside its excised segments"
        )
    for sentinel in CREDENTIAL_SENTINEL_SUBSTRINGS:
        assert sentinel not in sqljs_permission_probe_bundle, (
            f"the permitted sql.js module must not embed credential material {sentinel!r}"
        )
    assert VAULT_CONFIG_DIRECTORY_SENTINEL not in sqljs_permission_probe_bundle
    assert FULL_SHA256_LITERAL_PATTERN.findall(sqljs_permission_probe_bundle) == [], (
        "the permitted sql.js module must not embed full SHA-256 digest literals"
    )
