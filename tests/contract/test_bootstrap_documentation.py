"""Contract tests for the operator-facing bootstrap documentation.

These tests pin the README surface that operators rely on to clone, install
and verify the workspace. They are documentation-as-code: every required
prerequisite, command and intentional exclusion is asserted here so a silent
regression of the docs fails CI.

The still-reserved test-layer READMEs (``end_to_end``, ``golden``,
``performance``) are also asserted: they must state their owner and the future
spec that will populate them, and must not ship an executable placeholder test.
The local service-stack design now owns the integration layer's first executable
``local_stack`` test, so that layer is intentionally no longer reserved.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ROOT_README = REPO_ROOT / "README.md"
LOCAL_STACK_README = REPO_ROOT / "infra" / "compose" / "README.md"

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
# owns no executable test in this bootstrap spec. The end-to-end layer left the
# reserved set when the web-authentication child landed its first real
# Playwright spec (tests/end_to_end/authentication/web-security.spec.ts).
# The performance layer left the bootstrap reservation when the
# exclusion-policy publication spec (section 24) landed its reference
# performance gates; only the golden layer stays reserved.
RESERVED_TEST_LAYERS = ("tests/golden/README.md",)

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

# --- Local service stack operator contract ---------------------------------

LOCAL_STACK_POE_COMMANDS = tuple(
    f"uv run poe stack-{command}"
    for command in (
        "bootstrap",
        "config",
        "up",
        "status",
        "verify",
        "down",
        "reset",
        "smoke",
    )
)

LOCAL_STACK_SERVICE_VERSIONS = (
    ("PostgreSQL", "18.4"),
    ("Qdrant", "1.18.2"),
    ("Neo4j", "5.26.28"),
    ("Redis", "8.6.4"),
    ("Temporal Server", "1.31.2"),
    ("Temporal schema tools", "1.31.2"),
    ("Temporal UI", "2.53.0"),
    ("Temporal CLI", "1.8.0"),
)

LOCAL_STACK_PORT_OVERRIDES = (
    ("POSTGRES_PORT", "5432"),
    ("QDRANT_HTTP_PORT", "6333"),
    ("QDRANT_GRPC_PORT", "6334"),
    ("NEO4J_HTTP_PORT", "7474"),
    ("NEO4J_BOLT_PORT", "7687"),
    ("REDIS_PORT", "6379"),
    ("TEMPORAL_GRPC_PORT", "7233"),
    ("TEMPORAL_UI_PORT", "8080"),
)

LOCAL_STACK_EXIT_CODE_MEANINGS = (
    ("0", "success"),
    ("2", "syntax"),
    ("64", "prerequisite"),
    ("65", "contract"),
    ("69", "startup"),
    ("70", "internal"),
    ("75", "readiness"),
)

# --- Object-storage runtime check operator contract -------------------------
# The content-addressable object-storage spec adds a one-shot read-only R2
# HeadBucket diagnostic command. The root README and the operations guide are
# the operator contract for that command; these constants pin the surface.

# The seven approved object-storage environment names. Credential values are
# deliberately file-only: the two names ending in ``_FILE`` carry bounded secret
# file names, never plaintext secret values.
OBJECT_STORAGE_ENVIRONMENT_NAMES = (
    "KNOWLEDGE_ENVIRONMENT",
    "KNOWLEDGE_SECRET_ROOT",
    "KNOWLEDGE_R2_ENDPOINT",
    "KNOWLEDGE_R2_BUCKET_NAME",
    "KNOWLEDGE_R2_ACCESS_KEY_ID_FILE",
    "KNOWLEDGE_R2_SECRET_ACCESS_KEY_FILE",
    "KNOWLEDGE_OBJECT_STORAGE_SPOOL_ROOT",
)

# Anchors for the object-storage contract statements the root README must
# carry: the command name and its required flag, secret-file-only credentials,
# the private bucket requirement, and the no-fallback/no-delete/no-list rules.
OBJECT_STORAGE_COMMAND_TOKENS = (
    "object-storage-check-runtime",
    "--service",
)
OBJECT_STORAGE_CONTRACT_ANCHORS = (
    "secret files",
    "private bucket",
    "no fallback",
    "no delete",
    "no list",
)

# The five object-storage runtime-check exit codes paired with the meaning
# keyword each must be documented alongside (same line-pairing discipline as
# the check-runtime codes above).
OBJECT_STORAGE_EXIT_CODE_MEANINGS = (
    ("0", "success"),
    ("2", "syntax"),
    ("69", "unavailable"),
    ("70", "internal"),
    ("78", "configuration"),
)

OBJECT_STORAGE_OPERATIONS_GUIDE = REPO_ROOT / "docs" / "operations" / "object-storage.md"

# --- Exclusion-policy publication operator contract --------------------------
# The exclusion-policy publication child (Phase 2, child 3) is operated through
# a living runbook plus status records in the canonical docs. These constants
# pin that surface the same way the object-storage guide is pinned above: the
# exact gate commands, key-lifecycle commands, approved environment names,
# workflow identities, recovery limits, lock order and safety warnings must
# all exist verbatim so a silent docs regression fails CI.

EXCLUSION_POLICY_OPERATIONS_GUIDE = (
    REPO_ROOT / "docs" / "operations" / "exclusion-policy-publication.md"
)
EXCLUSION_POLICY_SPEC = (
    REPO_ROOT
    / "docs"
    / "superpowers"
    / "specs"
    / "2026-08-17-exclusion-policy-publication-design.md"
)
EXCLUSION_POLICY_HANDOFF = (
    REPO_ROOT / "docs" / "handoff" / "2026-08-17-exclusion-policy-publication.md"
)
SECURITY_POLICY_DOC = REPO_ROOT / "docs" / "14-SECURITY_PRIVACY_AND_POLICY.md"
TESTING_EVALUATION_DOC = REPO_ROOT / "docs" / "16-TESTING_AND_EVALUATION.md"
IMPLEMENTATION_PLAN_DOC = REPO_ROOT / "docs" / "20-IMPLEMENTATION_PLAN.md"

# Every acceptance gate name the child defines; the runbook must document all
# of them, including the device-verification gate that stays blocking while
# the reference-device records are absent.
EXCLUSION_POLICY_GATE_COMMANDS = (
    "uv run poe exclusion-policy-test",
    "pnpm run test:e2e:exclusion-policy",
    "uv run pytest tests/performance/test_exclusion_policy_performance.py -m local_stack -q",
    "uv run poe verify",
    "uv run poe exclusion-policy-device-verification",
)

# The four offline signing-key lifecycle commands (spec 13.2/13.3), verbatim
# with their placeholder arguments — copy-paste-safe, no real key material.
EXCLUSION_POLICY_KEY_LIFECYCLE_COMMANDS = (
    "personal-api policy-key initialize --workspace-id <uuid> --key-file-name policy_signing_a.pem",
    "personal-api policy-key stage --workspace-id <uuid> --key-file-name policy_signing_b.pem",
    "personal-api policy-key activate"
    " --workspace-id <uuid> --staged-key-file-name policy_signing_b.pem",
    "personal-api policy-key retire --workspace-id <uuid> --key-id <ed25519-sha256-…>",
)

# The policy-signer fragment of the approved environment names: identity and
# exact secret-file name only — no plaintext private-key variable exists.
EXCLUSION_POLICY_ENVIRONMENT_NAMES = (
    "KNOWLEDGE_POLICY_SIGNING_KEY_ID",
    "KNOWLEDGE_POLICY_SIGNING_KEY_FILE",
)

# Deterministic Temporal workflow identities (spec 10/15).
EXCLUSION_POLICY_WORKFLOW_IDS = (
    "exclusion-policy-preview/{workspace_id}/{policy_preview_id}",
    "exclusion-policy-reconciliation/{workspace_id}/{policy_revision_id}",
)

# Recovery limits paired value-with-context so a bare digit cannot satisfy the
# contract: preview expiry, scan/batch size, result page size, keyset page
# size, rule cap and signed-envelope byte cap.
EXCLUSION_POLICY_RECOVERY_LIMITS = (
    ("15 minutes", "preview"),
    ("500", "batch"),
    ("200", "page"),
    ("16", "keyset"),
    ("256", "rules"),
    ("256 KiB", "envelope"),
)

# Degraded states the runbook must cover with detection and recovery.
EXCLUSION_POLICY_DEGRADED_STATES = (
    "invalid signer",
    "PostgreSQL unavailable",
    "Temporal unavailable",
    "stale preview",
    "integrity failure",
    "reconciliation lag",
    "without the private key",
)

# Operator guarantees the operations guide must state verbatim (case-insensitive
# for prose anchors): offline startup validation, liveness/readiness never call
# R2, only the explicit command performs HeadBucket, credential rotation needs a
# process restart, spool storage must be encrypted/ephemeral, and production
# and test buckets plus credentials never cross.
OPERATIONS_GUIDE_ANCHORS = (
    "without calling r2",
    "liveness never calls r2",
    "readiness does not call r2",
    "only the explicit",
    "headbucket",
    "process restart",
    "encrypted",
    "ephemeral",
    "never cross",
)
OPERATIONS_GUIDE_COMMAND_TOKENS = (
    "object-storage-check-runtime",
    "--service worker",
)


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


def test_root_readme_documents_local_stack_prerequisites_and_all_poe_commands() -> None:
    content = _read(ROOT_README)
    missing_commands = [command for command in LOCAL_STACK_POE_COMMANDS if command not in content]
    assert not missing_commands, (
        f"root README must document all eight local-stack Poe commands: {missing_commands}"
    )
    for prerequisite in ("Docker Compose 2.30.0", "Linux containers", "linux/amd64"):
        assert prerequisite in content, (
            f"root README is missing local-stack prerequisite {prerequisite}"
        )


def test_local_stack_readme_documents_exact_service_versions_and_ports() -> None:
    content = _read(LOCAL_STACK_README)
    for service, version in LOCAL_STACK_SERVICE_VERSIONS:
        assert any(service in line and version in line for line in content.splitlines()), (
            f"local-stack README must pair {service} with {version}"
        )
    assert "linux/amd64" in content
    for variable, default_port in LOCAL_STACK_PORT_OVERRIDES:
        assert any(variable in line and default_port in line for line in content.splitlines()), (
            f"local-stack README must pair {variable} with {default_port}"
        )
    assert "127.0.0.1" in content


def test_local_stack_readme_documents_lifecycle_and_exact_reset_confirmation() -> None:
    content = _read(LOCAL_STACK_README)
    missing_commands = [command for command in LOCAL_STACK_POE_COMMANDS if command not in content]
    assert not missing_commands, (
        f"local-stack README must document all lifecycle commands: {missing_commands}"
    )
    exact_reset = (
        "uv run python tools/local_service_stack.py reset "
        "--project-name knowledge-local --confirm-project knowledge-local"
    )
    assert exact_reset in content
    assert "--rotate-secrets" in content
    assert "exact project name" in content.lower()


def test_local_stack_readme_documents_persistence_and_five_volume_reset() -> None:
    content = _read(LOCAL_STACK_README)
    lower = content.lower()
    assert any(
        "down" in line.lower() and "preserves" in line.lower() and "secrets" in line.lower()
        for line in content.splitlines()
    )
    for volume in (
        "postgres-data",
        "qdrant-data",
        "neo4j-data",
        "redis-data",
        "temporal-health-tools",
    ):
        assert volume in content
    assert "exact five" in lower
    assert "rebuildable" in lower
    assert "unknown labeled volumes" in lower


def test_local_stack_readme_documents_terminal_secret_and_drift_recovery() -> None:
    content = _read(LOCAL_STACK_README)
    lower = content.lower()
    for required in (
        "partial secret set",
        "missing secret set",
        "existing project volumes",
        "terminal",
        "operator investigation",
        "schema",
        "namespace",
        "drift",
    ):
        assert required in lower, f"local-stack recovery documentation is missing {required!r}"


def test_local_stack_readme_documents_exit_codes_with_meanings() -> None:
    content = _read(LOCAL_STACK_README)
    for code, meaning in LOCAL_STACK_EXIT_CODE_MEANINGS:
        assert any(code in line and meaning in line.lower() for line in content.splitlines()), (
            f"local-stack README must pair exit code {code} with {meaning}"
        )


def test_local_stack_readme_documents_windows_ci_and_r2_boundary() -> None:
    content = _read(LOCAL_STACK_README)
    lower = content.lower()
    assert "docker desktop" in lower and "linux containers" in lower
    assert "only ubuntu ci" in lower and "real containers" in lower
    assert "r2" in lower and "sole future canonical object store" in lower
    for excluded in ("not configured", "not contacted", "not tested"):
        assert excluded in lower


def test_root_readme_documents_all_seven_object_storage_environment_names() -> None:
    content = _read(ROOT_README)
    missing = [name for name in OBJECT_STORAGE_ENVIRONMENT_NAMES if name not in content]
    assert not missing, (
        "root README must document all seven approved object-storage environment "
        f"names; missing: {missing}"
    )


def test_root_readme_documents_object_storage_runtime_check_contract() -> None:
    content = _read(ROOT_README)
    lower = content.lower()
    offenders: list[str] = []
    for token in OBJECT_STORAGE_COMMAND_TOKENS:
        if token not in content:
            offenders.append(f"command token '{token}'")
    for anchor in OBJECT_STORAGE_CONTRACT_ANCHORS:
        if anchor not in lower:
            offenders.append(f"contract anchor '{anchor}'")
    # Credentials must be framed as secret-file-only, never plaintext variables.
    if "plaintext" not in lower or "secret" not in lower:
        offenders.append("secret-file-only credentials statement")
    assert not offenders, (
        "root README must document the object-storage runtime-check contract "
        f"(command, secret-file-only credentials, private bucket, no "
        f"fallback/delete/list); missing: {offenders}"
    )


def test_root_readme_documents_object_storage_exit_codes_with_meanings() -> None:
    content = _read(ROOT_README)
    offenders = []
    for code, meaning in OBJECT_STORAGE_EXIT_CODE_MEANINGS:
        if not any(code in line and meaning in line.lower() for line in content.splitlines()):
            offenders.append(f"exit code '{code}' paired with meaning '{meaning}'")
    assert not offenders, (
        "root README must document all five object-storage runtime-check exit "
        f"codes with their meanings; missing: {offenders}"
    )


def test_operations_guide_states_the_r2_runtime_contract() -> None:
    content = _read(OBJECT_STORAGE_OPERATIONS_GUIDE)
    lower = content.lower()
    offenders: list[str] = []
    for token in OPERATIONS_GUIDE_COMMAND_TOKENS:
        if token not in content:
            offenders.append(f"command token '{token}'")
    for anchor in OPERATIONS_GUIDE_ANCHORS:
        if anchor not in lower:
            offenders.append(f"guarantee anchor '{anchor}'")
    assert not offenders, (
        "docs/operations/object-storage.md must state the operator contract: "
        "offline startup validation, liveness/readiness never call R2, only the "
        "explicit command performs HeadBucket, rotation needs a process restart, "
        "encrypted/ephemeral spool storage, and production/test buckets and "
        f"credentials never cross; missing: {offenders}"
    )


def test_exclusion_policy_runbook_documents_every_acceptance_gate_command() -> None:
    content = _read(EXCLUSION_POLICY_OPERATIONS_GUIDE)
    missing = [command for command in EXCLUSION_POLICY_GATE_COMMANDS if command not in content]
    assert not missing, (
        "docs/operations/exclusion-policy-publication.md must document all "
        f"exclusion-policy acceptance gate commands; missing: {missing}"
    )


def test_exclusion_policy_runbook_documents_key_lifecycle_commands() -> None:
    content = _read(EXCLUSION_POLICY_OPERATIONS_GUIDE)
    missing = [
        command for command in EXCLUSION_POLICY_KEY_LIFECYCLE_COMMANDS if command not in content
    ]
    assert not missing, (
        "docs/operations/exclusion-policy-publication.md must document the four "
        f"policy-key lifecycle commands with placeholder arguments; missing: {missing}"
    )


def test_exclusion_policy_runbook_documents_signer_environment_names() -> None:
    content = _read(EXCLUSION_POLICY_OPERATIONS_GUIDE)
    missing = [name for name in EXCLUSION_POLICY_ENVIRONMENT_NAMES if name not in content]
    assert not missing, (
        "docs/operations/exclusion-policy-publication.md must document the two "
        f"policy-signer environment names; missing: {missing}"
    )


def test_exclusion_policy_runbook_documents_both_workflow_identities() -> None:
    content = _read(EXCLUSION_POLICY_OPERATIONS_GUIDE)
    missing = [
        workflow_id for workflow_id in EXCLUSION_POLICY_WORKFLOW_IDS if workflow_id not in content
    ]
    assert not missing, (
        "docs/operations/exclusion-policy-publication.md must document both "
        f"deterministic exclusion-policy workflow identities; missing: {missing}"
    )


def test_exclusion_policy_runbook_documents_recovery_limits() -> None:
    content = _read(EXCLUSION_POLICY_OPERATIONS_GUIDE)
    lower = content.lower()
    offenders: list[str] = []
    for value, context in EXCLUSION_POLICY_RECOVERY_LIMITS:
        if value not in content or context not in lower:
            offenders.append(f"recovery limit '{value}' (context '{context}')")
    assert not offenders, (
        "docs/operations/exclusion-policy-publication.md must document the "
        "closed recovery limits: 15-minute preview expiry, 500-row batches, "
        "200-row pages, 16-envelope keyset pages, 256 rules, 256 KiB envelope; "
        f"missing: {offenders}"
    )


def test_exclusion_policy_runbook_states_the_frozen_lock_order() -> None:
    content = _read(EXCLUSION_POLICY_OPERATIONS_GUIDE)
    source_chain = "publication idempotency advisory lock → workspace_policy_state row → source row"
    policy_chain = "policy idempotency advisory lock → workspace_policy_state row"
    assert source_chain in content, (
        "the runbook must state the frozen source/enforcement lock order as the "
        f"contiguous chain: {source_chain}"
    )
    assert policy_chain in content, (
        "the runbook must state the policy-publication lock order as the "
        f"contiguous chain: {policy_chain}"
    )


def test_exclusion_policy_runbook_warns_private_key_never_enters_backup() -> None:
    content = _read(EXCLUSION_POLICY_OPERATIONS_GUIDE)
    assert any(
        "private key" in line.lower()
        and ("backup" in line.lower() or "back up" in line.lower())
        and "separately" in line.lower()
        for line in content.splitlines()
    ), (
        "the runbook must warn that the policy private key is not part of the "
        "database backup and must be backed up separately"
    )
    assert any(
        "secret file" in line.lower() and "not" in line.lower() and "backup" in line.lower()
        for line in content.splitlines()
    ), "the runbook must state the database backup does not include secret files"
    assert "BEGIN PRIVATE KEY" not in content


def test_exclusion_policy_runbook_states_rollback_by_new_revision_semantics() -> None:
    content = _read(EXCLUSION_POLICY_OPERATIONS_GUIDE)
    lower = content.lower()
    for anchor in ("never edited", "never deleted", "new revision"):
        assert anchor in lower, (
            f"the runbook must state rollback-by-new-revision semantics (immutable "
            f"history is never edited or deleted); missing anchor: '{anchor}'"
        )


def test_exclusion_policy_runbook_covers_every_degraded_state() -> None:
    content = _read(EXCLUSION_POLICY_OPERATIONS_GUIDE)
    lower = content.lower()
    missing = [state for state in EXCLUSION_POLICY_DEGRADED_STATES if state.lower() not in lower]
    assert not missing, (
        "the runbook must cover detection and recovery for every degraded state: "
        f"invalid signer, PostgreSQL/Temporal unavailable, stale preview, plugin "
        f"integrity failure, reconciliation lag and restore without the private "
        f"key; missing: {missing}"
    )


def test_exclusion_policy_runbook_documents_publication_confirmation_phrase() -> None:
    content = _read(EXCLUSION_POLICY_OPERATIONS_GUIDE)
    assert "PUBLISH EXCLUSION POLICY" in content, (
        "the runbook must document the exact typed publication confirmation phrase"
    )
    assert "X-Idempotency-Key" in content, (
        "the runbook must document the publication idempotency header name"
    )


def test_exclusion_policy_runbook_documents_device_verification_contract() -> None:
    content = _read(EXCLUSION_POLICY_OPERATIONS_GUIDE)
    lower = content.lower()
    assert "docs/operations/exclusion-policy-device-verification.md" in content, (
        "the runbook must point at the reference-device verification records file"
    )
    assert "desktop and mobile" in lower, (
        "the runbook must require both Desktop and Mobile reference-device records"
    )
    assert "uv run poe exclusion-policy-device-verification" in content, (
        "the runbook must name the device-verification gate command"
    )


def test_exclusion_policy_runbook_documents_canonical_core_empty_policy_seeding() -> None:
    content = _read(EXCLUSION_POLICY_OPERATIONS_GUIDE)
    assert "phase-one-acceptance" in content and "signed empty policy" in content, (
        "the runbook must document that the Phase 1 canonical-core acceptance "
        "command seeds a signed empty policy before content operations"
    )


def test_exclusion_policy_spec_status_records_implementation() -> None:
    content = _read(EXCLUSION_POLICY_SPEC)
    assert "Implemented (2026-08-17)" in content, (
        "the exclusion-policy design spec must record its implemented status; "
        "it may no longer describe the child as awaiting review or merely planned"
    )


def test_canonical_docs_no_longer_describe_child_three_as_planned() -> None:
    plan = _read(IMPLEMENTATION_PLAN_DOC)
    assert "implementation chưa bắt đầu" not in plan, (
        "docs/20-IMPLEMENTATION_PLAN.md must no longer describe child 3 as "
        "not-yet-started after implementation completed"
    )
    assert "hoàn thành (2026-08-17)" in plan, (
        "docs/20-IMPLEMENTATION_PLAN.md must record child 3 completion with its "
        "completion date, matching the child 1/2 status convention"
    )
    for link in (
        "docs/operations/exclusion-policy-publication.md",
        "docs/handoff/2026-08-17-exclusion-policy-publication.md",
        "20260817_01",
    ):
        assert link in plan, f"docs/20-IMPLEMENTATION_PLAN.md must cite {link}"
    security = _read(SECURITY_POLICY_DOC)
    assert "docs/operations/exclusion-policy-publication.md" in security, (
        "docs/14-SECURITY_PRIVACY_AND_POLICY.md must link the exclusion-policy operations runbook"
    )
    testing = _read(TESTING_EVALUATION_DOC)
    for gate in ("exclusion-policy-test", "test:e2e:exclusion-policy"):
        assert gate in testing, (
            f"docs/16-TESTING_AND_EVALUATION.md must name the {gate} acceptance gate"
        )


def test_exclusion_policy_handoff_is_present_and_bounded() -> None:
    content = _read(EXCLUSION_POLICY_HANDOFF)
    line_count = len(content.splitlines())
    assert line_count <= 400, (
        f"the exclusion-policy handoff must stay under roughly 400 lines (found {line_count})"
    )
    assert "94a8a06" in content, (
        "the handoff must record the Task 13 implementation head SHA 94a8a06"
    )
    lower = content.lower()
    assert "reference-device" in lower and "blocking" in lower, (
        "the handoff must present the absent Desktop/Mobile reference-device "
        "verification records as the blocking deferred item"
    )


def test_exclusion_policy_runbook_metrics_match_the_implemented_vocabulary() -> None:
    """The runbook's metric set must equal the implemented metric vocabulary.

    Metric-shaped names (``..._total`` / ``..._seconds``) in the runbook are
    checked against the implemented contracts in
    ``personal_os.exclusion_policy.metrics`` and the reconciliation metric
    constants, so a spec-planned-but-unimplemented metric can never again be
    documented as live. A name outside the implemented vocabulary may appear
    only in a paragraph that explicitly marks it as planned/not implemented
    (wrapping can split a name from its framing sentence, so the paragraph —
    blank-line separated, whitespace-collapsed — is the checked unit).
    """

    from personal_os.exclusion_policy.metrics import EXCLUSION_POLICY_METRIC_CONTRACTS
    from personal_os.exclusion_policy.reconciliation import (
        RECONCILIATION_LAG_METRIC,
        RECONCILIATION_SOURCES_METRIC,
    )

    content = _read(EXCLUSION_POLICY_OPERATIONS_GUIDE)
    implemented = {
        *EXCLUSION_POLICY_METRIC_CONTRACTS,
        RECONCILIATION_SOURCES_METRIC,
        RECONCILIATION_LAG_METRIC,
    }
    missing = sorted(name for name in implemented if name not in content)
    assert not missing, (
        "docs/operations/exclusion-policy-publication.md must document every "
        f"implemented exclusion-policy metric; missing: {missing}"
    )
    unplanned: list[str] = []
    paragraphs = [
        " ".join(paragraph.split())
        for paragraph in re.split(r"\n\s*\n", content)
        if paragraph.strip()
    ]
    for paragraph in paragraphs:
        for name in re.findall(r"exclusion_policy_[a-z_]*(?:_total|_seconds)\b", paragraph):
            if name not in implemented and "planned" not in paragraph.lower():
                unplanned.append(f"{name}: {paragraph}")
    assert not unplanned, (
        "the runbook must not present an unimplemented metric as live; the "
        "only allowed framing is an explicit planned/not-implemented "
        f"paragraph; offending: {unplanned}"
    )
