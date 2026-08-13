"""Contract tests for the operator-facing bootstrap documentation.

These tests pin the README surface that operators rely on to clone, install
and verify the workspace. They are documentation-as-code: every required
prerequisite, command and intentional exclusion is asserted here so a silent
regression of the docs fails CI.

The reserved test-layer READMEs (``integration``, ``end_to_end``, ``golden``,
``performance``) are also asserted: they must state their owner and the future
spec that will populate them, and must not ship an executable placeholder test.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ROOT_README = REPO_ROOT / "README.md"

# Exact operator-facing prerequisites. These four strings must appear verbatim
# in the root README; internal dev-dependency versions (ruff, mypy, eslint, ...)
# are intentionally not operator-facing and must not be listed as prerequisites.
REQUIRED_PREREQUISITES = (
    "Python 3.14.6",
    "uv 0.11.32",
    "Node.js 24.18.0",
    "pnpm 10.34.0",
)

# Both frozen install commands an operator must be able to run from a clean clone.
FROZEN_INSTALL_COMMANDS = (
    "uv sync --all-packages --frozen",
    "pnpm install --frozen-lockfile",
)

# All eight public Poe gates must be documented in the root README.
PUBLIC_POE_COMMANDS = (
    "uv run poe format",
    "uv run poe format-check",
    "uv run poe lint",
    "uv run poe type-check",
    "uv run poe test",
    "uv run poe boundary-check",
    "uv run poe build",
    "uv run poe verify",
)

# CLI examples that must work after a frozen install. Their packages are
# installed by ``uv sync --all-packages``.
CLI_EXAMPLES = (
    "uv run --package api-runtime personal-api --help",
    "uv run --package mcp-runtime personal-mcp --version",
    "uv run --package workflow-worker personal-worker",
)

# Documented verify order. The README must list the six gates in this order so
# an operator can reason about the failure surface.
VERIFY_ORDER = (
    "format-check",
    "lint",
    "type-check",
    "boundary-check",
    "test",
    "build",
)

# Each composition-root README must name its shell/role and explicitly mark the
# product behavior it deliberately omits. ``role`` anchors the shell identity;
# ``omission_anchor`` anchors at least one named absent behavior.
APP_README_EXPECTATIONS = {
    "apps/api/README.md": {
        "role": "API process shell",
        "omission_anchor": "FastAPI",
    },
    "apps/mcp/README.md": {
        "role": "MCP process shell",
        "omission_anchor": "MCP tool",
    },
    "apps/worker/README.md": {
        "role": "Temporal worker process shell",
        "omission_anchor": "Temporal workflow",
    },
    "apps/web/README.md": {
        "role": "Web App shell",
        "omission_anchor": "API route",
    },
    "apps/obsidian-plugin/README.md": {
        "role": "Obsidian plugin shell",
        "omission_anchor": "Vault",
    },
}

# Reserved test layers. Each one is part of the canonical test hierarchy but
# owns no executable test in this bootstrap spec.
RESERVED_TEST_LAYERS = (
    "tests/integration/README.md",
    "tests/end_to_end/README.md",
    "tests/golden/README.md",
    "tests/performance/README.md",
)

# Each reserved layer README must declare its bootstrap owner and the future
# spec/later-spec source for its acceptance tests.
RESERVED_LAYER_REQUIRED_TOKENS = ("bootstrap", "later spec")

# Tokens that would turn a reserved layer README into an executable test stub.
# None may appear in any reserved layer README.
RESERVED_LAYER_FORBIDDEN_TOKENS = (
    "def test_",
    "it('",
    'it("',
    "describe('",
    'describe("',
    "test('",
    'test("',
)

# Extensions that, if present inside a reserved test layer, would constitute an
# executable placeholder test that the spec forbids.
PLACEHOLDER_TEST_SUFFIXES = (".py", ".test.ts", ".test.tsx", ".spec.ts", ".spec.tsx")

# --- Runtime configuration & diagnostics contract ---------------------------
# The runtime configuration and diagnostics spec (the composition-root
# ``check-runtime`` command) is operator-facing and security-sensitive. The
# following constants pin the README surface an operator relies on to run the
# command safely. They are documentation-as-code for the same reason as the
# bootstrap constants above: a silent regression of the operator contract fails
# CI.

# Unambiguous tokens the root README must document verbatim for the runtime
# configuration contract: the three approved environment variables, the
# production POSIX secret root, and the subcommand name.
RUNTIME_CONFIGURATION_TOKENS = (
    "KNOWLEDGE_ENVIRONMENT",
    "KNOWLEDGE_LOG_LEVEL",
    "KNOWLEDGE_SECRET_ROOT",
    "/run/secrets",
    "check-runtime",
)

# The four ``check-runtime`` exit codes paired with the meaning keyword each
# must be documented alongside. Asserting the code plus its meaning keyword on a
# single line keeps the test meaningful: the bare digit ``0`` would match almost
# any README fragment, but ``0`` + ``success`` only matches the exit-code
# contract. Meanings are linked, not duplicated, in the app READMEs.
RUNTIME_EXIT_CODE_MEANINGS = (
    ("0", "success"),
    ("2", "syntax"),
    ("70", "internal"),
    ("78", "configuration"),
)

# Settings sources and secret transports the runtime contract explicitly
# rejects. The root README must document each as an unsupported/prohibited path
# so an operator never attempts to configure secrets through them.
UNSUPPORTED_CONFIG_TRANSPORTS = (
    ".env",
    "TOML",
    "YAML",
    "JSON",
)
# Prohibition markers; at least one must appear so the unsupported transports
# are framed as a rejection rather than an option.
PROHIBITION_MARKERS = (
    "unsupported",
    "not supported",
    "prohibited",
    "forbidden",
    "must not",
    "never",
)

# Each composition-root README must document its exact ``check-runtime``
# command, the safe-JSON and no-settings-dump guarantees, and list the four exit
# codes. Full security rules live in the root contract; the app README links
# back rather than duplicating them incompletely.
RUNTIME_CHECK_APPS = {
    "apps/api/README.md": "personal-api check-runtime",
    "apps/mcp/README.md": "personal-mcp check-runtime",
    "apps/worker/README.md": "personal-worker check-runtime",
}
APP_SAFE_JSON_ANCHOR = "safe JSON"
APP_NO_SETTINGS_DUMP_ANCHOR = "settings dump"


def _read(path: Path) -> str:
    assert path.exists(), f"required documentation file is missing: {path}"
    return path.read_text(encoding="utf-8")


def test_root_readme_documents_exact_prerequisites() -> None:
    content = _read(ROOT_README)
    missing = [prereq for prereq in REQUIRED_PREREQUISITES if prereq not in content]
    assert not missing, (
        f"root README must document the exact operator prerequisites verbatim; missing: {missing}"
    )


def test_root_readme_documents_both_frozen_install_commands() -> None:
    content = _read(ROOT_README)
    missing = [cmd for cmd in FROZEN_INSTALL_COMMANDS if cmd not in content]
    assert not missing, (
        f"root README must document both frozen install commands; missing: {missing}"
    )


def test_root_readme_documents_all_eight_public_poe_commands() -> None:
    content = _read(ROOT_README)
    missing = [cmd for cmd in PUBLIC_POE_COMMANDS if cmd not in content]
    assert not missing, (
        f"root README must document all eight public Poe commands; missing: {missing}"
    )


def test_root_readme_documents_verify_gate_order() -> None:
    content = _read(ROOT_README)
    # The README must document the canonical verify order as a contiguous,
    # ordered arrow chain so the documented sequence cannot be silently
    # reordered. Matching individual gate names is insufficient because the
    # same names appear out of order in the per-command reference table.
    canonical_chain = " → ".join(VERIFY_ORDER)
    assert canonical_chain in content, (
        "root README must document the verify gate order as the contiguous "
        f"chain: {canonical_chain}"
    )


def test_root_readme_documents_cli_examples() -> None:
    content = _read(ROOT_README)
    missing = [example for example in CLI_EXAMPLES if example not in content]
    assert not missing, f"root README must document the three CLI examples; missing: {missing}"


def test_root_readme_defers_later_spec_concerns() -> None:
    content = _read(ROOT_README)
    # The README must explicitly defer the concerns owned by later specs so an
    # operator does not expect them from the bootstrap.
    deferred_anchors = (
        "configuration",
        "secret",
        "database",
        "object storage",
        "later spec",
    )
    missing = [anchor for anchor in deferred_anchors if anchor not in content.lower()]
    assert not missing, (
        "root README must defer configuration/secrets, databases, object storage "
        f"and product behavior to later specs; missing anchors: {missing}"
    )


def test_every_app_readme_names_shell_and_lists_absent_behavior() -> None:
    offenders: list[str] = []
    for relative_path, expectation in APP_README_EXPECTATIONS.items():
        path = REPO_ROOT / relative_path
        if not path.exists():
            offenders.append(f"{relative_path}: README missing")
            continue
        content = path.read_text(encoding="utf-8")
        if expectation["role"] not in content:
            offenders.append(f"{relative_path}: missing role/shell '{expectation['role']}'")
        if expectation["omission_anchor"] not in content:
            offenders.append(
                f"{relative_path}: missing absent-behavior anchor "
                f"'{expectation['omission_anchor']}'"
            )
        if "absent" not in content.lower():
            offenders.append(
                f"{relative_path}: must explicitly state which product behavior is absent"
            )
        if "build" not in content.lower() or "test" not in content.lower():
            offenders.append(f"{relative_path}: must name its build/test command")
    assert not offenders, "app README contract violations:\n" + "\n".join(offenders)


def test_reserved_test_layer_readmes_state_owner_and_future_source() -> None:
    offenders: list[str] = []
    for relative_path in RESERVED_TEST_LAYERS:
        path = REPO_ROOT / relative_path
        if not path.exists():
            offenders.append(f"{relative_path}: README missing")
            continue
        content = path.read_text(encoding="utf-8")
        lower = content.lower()
        for token in RESERVED_LAYER_REQUIRED_TOKENS:
            if token not in lower:
                offenders.append(
                    f"{relative_path}: missing required token '{token}' "
                    "(owner / future acceptance source)"
                )
        for token in RESERVED_LAYER_FORBIDDEN_TOKENS:
            if token in content:
                offenders.append(f"{relative_path}: forbidden executable-test token '{token}'")
    assert not offenders, "reserved test-layer README contract violations:\n" + "\n".join(offenders)


def test_reserved_test_layers_contain_no_executable_placeholder_tests() -> None:
    offenders: list[str] = []
    for relative_path in RESERVED_TEST_LAYERS:
        layer_dir = (REPO_ROOT / relative_path).parent
        if not layer_dir.exists():
            offenders.append(f"{layer_dir}: reserved layer directory missing")
            continue
        for entry in layer_dir.rglob("*"):
            if entry.is_dir():
                continue
            # The README itself is the only allowed artifact in the bootstrap.
            if entry.name == "README.md":
                continue
            # Match against the full filename, not ``Path.suffix``: the latter
            # returns only the final dot-segment (``foo.test.ts`` -> ``.ts``),
            # which would let TypeScript placeholder files slip through while
            # the reserved-layer READMEs promise they are forbidden.
            if any(entry.name.endswith(suffix) for suffix in PLACEHOLDER_TEST_SUFFIXES):
                offenders.append(
                    f"{entry}: reserved test layer must not contain an executable placeholder test"
                )
    assert not offenders, "reserved test-layer placeholder violations:\n" + "\n".join(offenders)


def test_root_readme_documents_runtime_configuration_tokens() -> None:
    content = _read(ROOT_README)
    missing = [token for token in RUNTIME_CONFIGURATION_TOKENS if token not in content]
    assert not missing, (
        "root README must document the runtime configuration contract tokens "
        f"(approved env vars, POSIX secret root, subcommand); missing: {missing}"
    )


def test_root_readme_documents_runtime_exit_codes_with_meanings() -> None:
    content = _read(ROOT_README)
    offenders: list[str] = []
    for code, meaning in RUNTIME_EXIT_CODE_MEANINGS:
        # Each exit code must be documented on a line that also carries its
        # meaning keyword, so the contract cannot degrade to a bare digit that
        # happens to appear elsewhere in the README.
        if not any(code in line and meaning in line.lower() for line in content.splitlines()):
            offenders.append(f"exit code '{code}' paired with meaning '{meaning}'")
    assert not offenders, (
        "root README must document all four check-runtime exit codes with their "
        f"meanings; missing: {offenders}"
    )


def test_root_readme_documents_unsupported_config_and_secret_transports() -> None:
    content = _read(ROOT_README)
    lower = content.lower()
    offenders: list[str] = []
    for transport in UNSUPPORTED_CONFIG_TRANSPORTS:
        if transport.lower() not in lower:
            offenders.append(transport)
    # Plaintext secret environment variables and command-line secret values must
    # also be marked unsupported.
    if "plaintext" not in lower or "secret" not in lower:
        offenders.append("plaintext secret environment variables")
    if "command line" not in lower and "command-line" not in lower:
        offenders.append("command-line secret values")
    # At least one prohibition marker must frame these as rejected, not optional.
    if not any(marker in lower for marker in PROHIBITION_MARKERS):
        offenders.append("a prohibition marker (unsupported/prohibited/...)")
    assert not offenders, (
        "root README must document unsupported config/secret transports as "
        f"prohibited; missing: {offenders}"
    )


def test_each_runtime_check_app_readme_documents_command_guarantees_and_exit_codes() -> None:
    offenders: list[str] = []
    for relative_path, command in RUNTIME_CHECK_APPS.items():
        path = REPO_ROOT / relative_path
        if not path.exists():
            offenders.append(f"{relative_path}: README missing")
            continue
        content = path.read_text(encoding="utf-8")
        if command not in content:
            offenders.append(f"{relative_path}: missing command '{command}'")
        if APP_SAFE_JSON_ANCHOR.lower() not in content.lower():
            offenders.append(f"{relative_path}: missing safe-JSON-output statement")
        if APP_NO_SETTINGS_DUMP_ANCHOR.lower() not in content.lower():
            offenders.append(f"{relative_path}: missing no-settings-dump statement")
        # The four exit codes must be listed; the word ``exit`` anchors them as
        # exit-code context rather than incidental digits.
        if "exit" not in content.lower():
            offenders.append(f"{relative_path}: missing 'exit' context anchor")
        for code, _meaning in RUNTIME_EXIT_CODE_MEANINGS:
            if code not in content:
                offenders.append(f"{relative_path}: missing exit code '{code}'")
    assert not offenders, (
        "composition-root check-runtime README contract violations:\n" + "\n".join(offenders)
    )
