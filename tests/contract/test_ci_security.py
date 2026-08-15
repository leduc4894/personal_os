from __future__ import annotations

import re
from pathlib import Path

import yaml  # type: ignore[import-untyped]  # Pinned PyYAML does not ship type stubs.

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_DIRECTORY = REPO_ROOT / ".github" / "workflows"
WORKFLOW_PATH = WORKFLOW_DIRECTORY / "quality.yml"
LOCAL_STACK_WORKFLOW_PATH = WORKFLOW_DIRECTORY / "local-service-stack.yml"
CANONICAL_POSTGRESQL_WORKFLOW_PATH = WORKFLOW_DIRECTORY / "canonical-postgresql-baseline.yml"
R2_LIVE_WORKFLOW = WORKFLOW_DIRECTORY / "object-storage-live.yml"
CANONICAL_CORE_ACCEPTANCE_WORKFLOW = WORKFLOW_DIRECTORY / "canonical-core-acceptance.yml"
WORKFLOW_TEXT = WORKFLOW_PATH.read_text(encoding="utf-8")

CANONICAL_POSTGRESQL_STATIC_JOB_NAME = "windows-static"
CANONICAL_POSTGRESQL_LIFECYCLE_JOB_NAME = "ubuntu-lifecycle"
CANONICAL_POSTGRESQL_JUNIT_ARTIFACT_PATH = ".local/test-results/canonical-postgresql-baseline.xml"
CANONICAL_POSTGRESQL_PROJECT_TEMPLATE = (
    "knowledge-ci-${{ github.run_id }}-${{ github.run_attempt }}"
)
CANONICAL_POSTGRESQL_STATIC_TEST_COMMAND = (
    "uv run pytest tests/unit/migrations/test_database_migration_runtime.py"
    " tests/contract/test_canonical_postgresql_migration_contract.py"
    " tests/contract/test_ci_security.py -q"
)
CANONICAL_POSTGRESQL_STATIC_LINT_COMMAND = (
    "uv run ruff check migrations tests/unit/migrations"
    " tests/contract/test_canonical_postgresql_migration_contract.py"
)
# No provider credential, deployment surface, dump or log capture may appear.
CANONICAL_POSTGRESQL_FORBIDDEN_MATERIAL: tuple[str, ...] = (
    "secrets.",
    "R2",
    "cloudflare",
    "pg_dump",
    "docker logs",
    "docker inspect",
    "deploy",
    "publish",
    "services:",
    "environment:",
)
# Only the sanitized JUnit report may leave the runner.
CANONICAL_POSTGRESQL_FORBIDDEN_ARTIFACTS: tuple[str, ...] = (
    ".local/stack-secrets",
    ".env",
)

# A non-local ``uses:`` reference must be repo@<exactly 40 lowercase hex chars>.
SHA_PINNED_RE = re.compile(r"^[A-Za-z0-9._/-]+@[0-9a-f]{40}$")
LOCAL_STACK_CONFIG_JOB_NAMES = ("ubuntu-config", "windows-config")
LOCAL_STACK_LEAKAGE_TOKENS = (
    "postgres_admin_password",
    "qdrant_api_key",
    "qdrant_config.yaml",
    "secret-value-that-must-not-leak",
    "must-not-be-forwarded",
    "R2",
)


def _uses_references(text: str) -> list[str]:
    refs: list[str] = []
    for match in re.finditer(r"(?m)^\s*uses:\s+(.+)$", text):
        raw = match.group(1)
        ref = raw.split("#", 1)[0].strip().strip("'\"")
        refs.append(ref)
    return refs


def _is_sha_pinned(reference: str) -> bool:
    return reference.startswith("./") or SHA_PINNED_RE.fullmatch(reference) is not None


def _top_level_permissions(text: str) -> list[str]:
    block = re.search(r"(?ms)^permissions:\n((?:  [^\n]+\n)+)", text)
    return [] if block is None else block.group(1).splitlines()


def _job_blocks(text: str) -> dict[str, str]:
    jobs = re.search(r"(?ms)^jobs:\n(?P<body>(?:(?:  |\s*$).*\n?)*)", text)
    if jobs is None:
        return {}
    body = jobs.group("body")
    headers = list(re.finditer(r"(?m)^  ([a-z0-9_-]+):\s*$", body))
    return {
        header.group(1): body[header.start() : next_header.start()]
        for header, next_header in zip(
            headers,
            [*headers[1:], re.compile(r"$").search(body, len(body))],
            strict=True,
        )
        if next_header is not None
    }


def _all_jobs_have_positive_timeouts(text: str) -> bool:
    jobs = _job_blocks(text)
    if not jobs:
        return False
    for block in jobs.values():
        timeout = re.search(r"(?m)^    timeout-minutes:\s*(\d+)\s*$", block)
        if timeout is None or int(timeout.group(1)) <= 0:
            return False
    return True


def _has_job_level_permissions_override(text: str) -> bool:
    # The exact top-level contents:read block is the sole workflow authority.
    # Reject every job override so a later edit cannot widen one job silently.
    try:
        workflow = yaml.safe_load(text)
    except yaml.YAMLError:
        return True
    if not isinstance(workflow, dict):
        return True
    jobs = workflow.get("jobs")
    if not isinstance(jobs, dict):
        return True
    if any(not isinstance(job_definition, dict) for job_definition in jobs.values()):
        return True
    return any("permissions" in job_definition for job_definition in jobs.values())


def _workflow_is_least_privilege_and_sha_pinned(text: str) -> bool:
    return (
        _top_level_permissions(text) == ["  contents: read"]
        and not _has_job_level_permissions_override(text)
        and "pull_request_target" not in text
        and _all_jobs_have_positive_timeouts(text)
        and all(_is_sha_pinned(ref) for ref in _uses_references(text))
    )


def _config_jobs_scan_for(text: str, leakage_token: str) -> bool:
    jobs = _job_blocks(text)
    return all(leakage_token in jobs.get(job_name, "") for job_name in LOCAL_STACK_CONFIG_JOB_NAMES)


def test_triggers_cover_pull_request_and_push_to_master_without_target() -> None:
    assert re.search(r"(?m)^\s{2}pull_request:\s*$", WORKFLOW_TEXT), (
        "workflow must trigger on pull_request"
    )
    assert re.search(r"branches:\s*\[\s*master\s*\]", WORKFLOW_TEXT), (
        "workflow must trigger on pushes to master"
    )
    assert "pull_request_target" not in WORKFLOW_TEXT, (
        "pull_request_target grants secrets to fork code and must never appear"
    )


def test_top_level_permissions_are_exactly_contents_read() -> None:
    permission_lines = _top_level_permissions(WORKFLOW_TEXT)
    assert permission_lines == ["  contents: read"], (
        f"top-level permissions must be exactly contents: read, got {permission_lines!r}"
    )


def test_every_non_local_uses_reference_is_sha_pinned() -> None:
    refs = _uses_references(WORKFLOW_TEXT)
    assert refs, "workflow must declare at least one action via uses:"
    for ref in refs:
        assert _is_sha_pinned(ref), f"uses reference must be pinned to a 40-hex SHA, got {ref!r}"


def test_matrix_runs_on_both_ubuntu_and_windows() -> None:
    assert "ubuntu-latest" in WORKFLOW_TEXT, "quality matrix must include ubuntu-latest"
    assert "windows-latest" in WORKFLOW_TEXT, "quality matrix must include windows-latest"


def test_finite_timeout_and_concurrency_cancellation() -> None:
    timeout = re.search(r"(?m)^\s*timeout-minutes:\s*(\d+)\s*$", WORKFLOW_TEXT)
    assert timeout is not None, "job must declare a finite timeout-minutes"
    assert int(timeout.group(1)) > 0, "timeout-minutes must be a positive integer"
    assert re.search(r"cancel-in-progress:\s*true", WORKFLOW_TEXT), (
        "workflow must cancel superseded runs via concurrency cancel-in-progress"
    )


def test_frozen_installs_and_frozen_verify() -> None:
    assert "uv sync --all-packages --frozen" in WORKFLOW_TEXT, (
        "Python install must be frozen via uv sync --all-packages --frozen"
    )
    assert "pnpm install --frozen-lockfile" in WORKFLOW_TEXT, (
        "npm install must be frozen via pnpm install --frozen-lockfile"
    )
    assert "uv run --all-packages --frozen poe verify" in WORKFLOW_TEXT, (
        "quality gates must run via uv run --all-packages --frozen poe verify"
    )


def test_stack_workflows_prefetch_images_before_live_gates() -> None:
    """`compose up` must not spend its startup deadline pulling images.

    The stack startup deadline bounds health waiting; a fresh runner pulling
    gigabytes of pinned images inside `compose up` can exceed it and fail
    closed with stack_startup_failed. Every workflow that brings the stack
    up must prefetch the pinned images first.
    """
    workflow_paths = (
        LOCAL_STACK_WORKFLOW_PATH,
        CANONICAL_POSTGRESQL_WORKFLOW_PATH,
        WORKFLOW_DIRECTORY / "canonical-core-acceptance.yml",
    )
    for workflow_path in workflow_paths:
        text = workflow_path.read_text(encoding="utf-8")
        assert "Prefetch pinned stack images" in text, (
            f"{workflow_path.name} must prefetch pinned images before live gates"
        )
        assert "pull --quiet" in text, (
            f"{workflow_path.name} prefetch must pull pinned images quietly"
        )


def test_no_secrets_docker_services_deploy_or_publish() -> None:
    for forbidden in ("secrets.", "docker", "services:", "deploy", "publish"):
        assert forbidden not in WORKFLOW_TEXT, (
            f"workflow must not reference forbidden token {forbidden!r}"
        )


def test_all_workflows_are_least_privilege_and_sha_pinned() -> None:
    workflow_paths = sorted(WORKFLOW_DIRECTORY.glob("*.yml"))
    assert workflow_paths, "at least one workflow must exist"
    for path in workflow_paths:
        text = path.read_text(encoding="utf-8")
        assert _workflow_is_least_privilege_and_sha_pinned(text), path


def test_permission_contract_rejects_job_level_override_mutation() -> None:
    text = LOCAL_STACK_WORKFLOW_PATH.read_text(encoding="utf-8")
    for override in (
        "permissions:\n      contents: write",
        "permissions: read-all",
        '"permissions": write-all',
        "'permissions':\n      contents: write",
    ):
        mutated = text.replace(
            "    runs-on: ubuntu-latest",
            f"    {override}\n    runs-on: ubuntu-latest",
            1,
        )
        assert mutated != text
        assert not _workflow_is_least_privilege_and_sha_pinned(mutated)


def test_permission_contract_rejects_scalar_job_definition_mutation() -> None:
    text = LOCAL_STACK_WORKFLOW_PATH.read_text(encoding="utf-8")
    mutated = text.replace("  ubuntu-config:\n", "  ubuntu-config: |\n", 1)
    assert mutated != text
    assert not _workflow_is_least_privilege_and_sha_pinned(mutated)


def test_local_stack_workflow_never_receives_provider_secrets() -> None:
    text = LOCAL_STACK_WORKFLOW_PATH.read_text(encoding="utf-8")
    for forbidden in ("secrets.", "R2_", "cloudflarestorage.com", "minio"):
        assert forbidden not in text
    assert "knowledge-ci-" in text
    assert "windows-latest" in text and "ubuntu-latest" in text
    assert "pytest tests/integration/test_local_service_stack.py -m local_stack" in text


def test_local_stack_workflow_has_bounded_path_filtered_triggers() -> None:
    text = LOCAL_STACK_WORKFLOW_PATH.read_text(encoding="utf-8")
    required_trigger_tokens = (
        "pull_request:",
        "push:",
        "branches: [master]",
        "schedule:",
        "workflow_dispatch:",
        "infra/**",
        "tools/local_service_stack.py",
        "tests/unit/tools/test_local_service_stack.py",
        "tests/contract/test_local_service_stack_contract.py",
        "tests/integration/test_local_service_stack.py",
        "pyproject.toml",
        "uv.lock",
        "pnpm-lock.yaml",
        ".github/workflows/local-service-stack.yml",
        "cancel-in-progress: true",
    )
    missing = [token for token in required_trigger_tokens if token not in text]
    assert not missing, f"local-stack workflow trigger contract is incomplete: {missing}"


def test_local_stack_workflow_installs_verified_compose_on_both_platforms() -> None:
    text = LOCAL_STACK_WORKFLOW_PATH.read_text(encoding="utf-8")
    assert text.count("v2.30.0") >= 2
    assert "1cddcb3399cc68c385796a6ab441ab5734d4c6a0cb4713bd2bf3f0d384550a38" in text
    assert "07ed10572bed0c42e5477bd33f9eb8f1b1c640d83120cc59feb7ce28f0c1bf86" in text
    jobs = _job_blocks(text)
    assert "grep -Ex 'v?2\\.30\\.0'" in jobs["ubuntu-config"]
    assert '$composeVersion -notin @("2.30.0", "v2.30.0")' in jobs["windows-config"]
    assert '$dockerConfig = Join-Path $env:RUNNER_TEMP "docker-config"' in jobs["windows-config"]
    assert "DOCKER_CONFIG=$dockerConfig" in jobs["windows-config"]


def test_local_stack_config_jobs_cover_defaults_overrides_and_safe_failures() -> None:
    text = LOCAL_STACK_WORKFLOW_PATH.read_text(encoding="utf-8")
    jobs = _job_blocks(text)
    for job_name in LOCAL_STACK_CONFIG_JOB_NAMES:
        assert job_name in jobs
        block = jobs[job_name]
        for required in (
            "timeout-minutes: 10",
            "uv sync --all-packages --frozen",
            "tests/unit/tools/test_local_service_stack.py",
            "tests/contract/test_local_service_stack_contract.py",
            "uv run poe stack-bootstrap",
            "uv run poe stack-config",
            "POSTGRES_PORT",
            "QDRANT_HTTP_PORT",
            "QDRANT_GRPC_PORT",
            "NEO4J_HTTP_PORT",
            "NEO4J_BOLT_PORT",
            "REDIS_PORT",
            "TEMPORAL_GRPC_PORT",
            "TEMPORAL_UI_PORT",
            "64",
            "git status --porcelain",
        ):
            assert required in block, f"{job_name} is missing {required!r}"
        for leakage_token in LOCAL_STACK_LEAKAGE_TOKENS:
            assert leakage_token in block, (
                f"{job_name} must scan sanitized output for {leakage_token!r}"
            )
    assert "git check-ignore --quiet --no-index .local/.ci-probe" in jobs["ubuntu-config"]
    assert "rm -rf -- .local" in jobs["ubuntu-config"]
    assert "install -d -m 700 .local" in jobs["ubuntu-config"]
    assert "git check-ignore --quiet --no-index .local/.ci-probe" in jobs["windows-config"]
    assert 'Remove-Item -LiteralPath ".local" -Recurse -Force' in jobs["windows-config"]
    assert "$PSNativeCommandUseErrorActionPreference = $false" in jobs["windows-config"]


def test_config_leakage_contract_rejects_missing_qdrant_config_scan_mutation() -> None:
    text = LOCAL_STACK_WORKFLOW_PATH.read_text(encoding="utf-8")
    mutated = text.replace("qdrant_config.yaml", "", 1)
    assert mutated != text
    assert not _config_jobs_scan_for(mutated, "qdrant_config.yaml")


def test_local_stack_smoke_has_exact_cleanup_and_junit_only_artifact() -> None:
    text = LOCAL_STACK_WORKFLOW_PATH.read_text(encoding="utf-8")
    smoke = _job_blocks(text)["ubuntu-smoke"]
    assert "timeout-minutes: 20" in smoke
    assert 'CI: "true"' in smoke
    assert "knowledge-ci-${{ github.run_id }}-${{ github.run_attempt }}" in smoke
    for command in (
        "uv sync --all-packages --frozen",
        'bootstrap --project-name "$LOCAL_STACK_TEST_PROJECT"',
        'config --project-name "$LOCAL_STACK_TEST_PROJECT"',
        "pytest tests/integration/test_local_service_stack.py -m local_stack -q",
        'docker compose --project-name "$LOCAL_STACK_TEST_PROJECT"',
        "ps --all --format json",
        '--confirm-project "$LOCAL_STACK_TEST_PROJECT"',
        "--non-interactive",
        "docker container ls -a --filter",
        "docker network ls --filter",
        "docker volume ls --filter",
    ):
        assert command in smoke
    assert smoke.count("if: always()") >= 2
    assert "inventory_status=0" in smoke
    assert "if (( reset_status != 0 || inventory_status != 0 )); then" in smoke
    assert "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02" in smoke
    assert "path: .local/test-results/local-service-stack.xml" in smoke
    assert "include-hidden-files: true" in smoke
    for forbidden_artifact in (
        ".local/stack-secrets",
        "docker inspect",
        "docker logs",
        "compose-render",
    ):
        assert forbidden_artifact not in smoke


# ---------------------------------------------------------------------------
# Canonical PostgreSQL baseline workflow contract (Task 7)
# ---------------------------------------------------------------------------


def _canonical_postgresql_workflow_text() -> str:
    return CANONICAL_POSTGRESQL_WORKFLOW_PATH.read_text(encoding="utf-8")


def _canonical_postgresql_step_block(text: str, job_name: str, step_name: str) -> str:
    jobs = _job_blocks(text)
    pattern = re.compile(
        rf"(?ms)^      - name: {re.escape(step_name)}\n(?P<body>.*?)(?=^      - |\Z)"
    )
    match = pattern.search(jobs.get(job_name, ""))
    return "" if match is None else match.group("body")


def _canonical_postgresql_is_free_of_forbidden_material(text: str) -> bool:
    return all(token not in text for token in CANONICAL_POSTGRESQL_FORBIDDEN_MATERIAL)


def _canonical_postgresql_artifact_scope_is_junit_only(text: str) -> bool:
    if text.count("actions/upload-artifact@") != 1:
        return False
    if any(token in text for token in CANONICAL_POSTGRESQL_FORBIDDEN_ARTIFACTS):
        return False
    uploaded_paths = re.findall(r"(?m)^ +path: (.+)$", text)
    return uploaded_paths == [CANONICAL_POSTGRESQL_JUNIT_ARTIFACT_PATH]


def _canonical_postgresql_project_identity_is_ci_scoped(text: str) -> bool:
    return (
        CANONICAL_POSTGRESQL_PROJECT_TEMPLATE in text
        and "knowledge-local" not in text
        and "^knowledge-ci-[0-9]+-[0-9]+$" in text
        and "${#LOCAL_STACK_TEST_PROJECT} > 63" in text
    )


def _canonical_postgresql_cleanup_is_always_gated(text: str) -> bool:
    cleanup = _canonical_postgresql_step_block(
        text,
        CANONICAL_POSTGRESQL_LIFECYCLE_JOB_NAME,
        "Reset exact project and assert cleanup",
    )
    required_tokens = (
        "if: always()",
        '--confirm-project "$LOCAL_STACK_TEST_PROJECT"',
        "--non-interactive",
        "docker container ls -a --filter",
        "docker network ls --filter",
        "docker volume ls --filter",
        '[[ -n "$remaining_containers" || -n "$remaining_networks" || -n "$remaining_volumes" ]]',
    )
    return all(token in cleanup for token in required_tokens)


def test_canonical_postgresql_workflow_is_least_privilege_and_sha_pinned() -> None:
    text = _canonical_postgresql_workflow_text()
    assert _workflow_is_least_privilege_and_sha_pinned(text)
    assert _top_level_permissions(text) == ["  contents: read"]
    assert not _has_job_level_permissions_override(text)


def test_canonical_postgresql_workflow_triggers_are_bounded_and_path_filtered() -> None:
    text = _canonical_postgresql_workflow_text()
    required_trigger_tokens = (
        "pull_request:",
        "push:",
        "branches: [master]",
        "schedule:",
        "workflow_dispatch:",
        "migrations/**",
        "docs/superpowers/specs/canonical-postgresql-baseline-design.md",
        "tests/unit/migrations/test_database_migration_runtime.py",
        "tests/contract/test_canonical_postgresql_migration_contract.py",
        "tests/contract/test_ci_security.py",
        "tests/integration/test_canonical_postgresql_baseline.py",
        "pyproject.toml",
        "uv.lock",
        "infra/compose/compose.yaml",
        "infra/compose/scripts/postgres-provision.sh",
        "infra/compose/images.lock.yaml",
        ".github/workflows/canonical-postgresql-baseline.yml",
        "cancel-in-progress: true",
    )
    missing = [token for token in required_trigger_tokens if token not in text]
    assert not missing, f"canonical PostgreSQL workflow contract is incomplete: {missing}"


def test_canonical_postgresql_workflow_declares_exactly_two_jobs_with_timeouts() -> None:
    text = _canonical_postgresql_workflow_text()
    jobs = _job_blocks(text)
    assert tuple(jobs) == (
        CANONICAL_POSTGRESQL_STATIC_JOB_NAME,
        CANONICAL_POSTGRESQL_LIFECYCLE_JOB_NAME,
    )
    assert _all_jobs_have_positive_timeouts(text)
    assert "runs-on: windows-latest" in jobs[CANONICAL_POSTGRESQL_STATIC_JOB_NAME]
    assert "runs-on: ubuntu-latest" in jobs[CANONICAL_POSTGRESQL_LIFECYCLE_JOB_NAME]


def test_canonical_postgresql_windows_static_job_is_purely_static() -> None:
    text = _canonical_postgresql_workflow_text()
    static_job = _job_blocks(text)[CANONICAL_POSTGRESQL_STATIC_JOB_NAME]
    for required_command in (
        "uv sync --all-packages --frozen",
        "uv run alembic heads",
        CANONICAL_POSTGRESQL_STATIC_TEST_COMMAND,
        CANONICAL_POSTGRESQL_STATIC_LINT_COMMAND,
        "uv run mypy migrations",
        'python-version: "3.14.6"',
    ):
        assert required_command in static_job, f"windows-static job is missing {required_command!r}"
    assert "docker" not in static_job.lower(), (
        "the Windows job must stay static and never install or start Docker"
    )
    assert "secrets." not in static_job, "the Windows job must not read any secret"


def test_canonical_postgresql_ubuntu_lifecycle_job_uses_pinned_stack() -> None:
    text = _canonical_postgresql_workflow_text()
    lifecycle_job = _job_blocks(text)[CANONICAL_POSTGRESQL_LIFECYCLE_JOB_NAME]
    assert 'CI: "true"' in lifecycle_job
    assert CANONICAL_POSTGRESQL_PROJECT_TEMPLATE in lifecycle_job
    assert "uv sync --all-packages --frozen" in lifecycle_job
    # The stack bootstrap requires the .local parent directory at mode 0700;
    # a default-umask mkdir -p .local/test-results would create it 0755 and
    # fail the run closed with unsafe_secret_set.
    assert "install -d -m 700 .local" in lifecycle_job
    assert "mkdir -p .local/test-results" in lifecycle_job
    assert (
        "pytest tests/integration/test_canonical_postgresql_baseline.py -m local_stack -q"
        in lifecycle_job
    )
    assert f"--junitxml={CANONICAL_POSTGRESQL_JUNIT_ARTIFACT_PATH}" in lifecycle_job
    # The pinned Compose binary and checksum must be copied verbatim.
    assert "v2.30.0" in lifecycle_job
    assert "1cddcb3399cc68c385796a6ab441ab5734d4c6a0cb4713bd2bf3f0d384550a38" in lifecycle_job
    assert "grep -Ex 'v?2\\.30\\.0'" in lifecycle_job
    assert lifecycle_job.count("if: always()") >= 2


def test_canonical_postgresql_workflow_carries_no_secrets_or_provider_material() -> None:
    text = _canonical_postgresql_workflow_text()
    assert _canonical_postgresql_is_free_of_forbidden_material(text)


def test_canonical_postgresql_artifact_is_junit_only_with_exact_flags() -> None:
    text = _canonical_postgresql_workflow_text()
    assert _canonical_postgresql_artifact_scope_is_junit_only(text)
    upload_step = _canonical_postgresql_step_block(
        text,
        CANONICAL_POSTGRESQL_LIFECYCLE_JOB_NAME,
        "Upload sanitized JUnit report",
    )
    assert "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02" in upload_step
    for required_flag in (
        "if: always()",
        "include-hidden-files: true",
        "if-no-files-found: ignore",
        "retention-days: 7",
    ):
        assert required_flag in upload_step


def test_canonical_postgresql_project_identity_is_disposable_ci_scoped() -> None:
    text = _canonical_postgresql_workflow_text()
    assert _canonical_postgresql_project_identity_is_ci_scoped(text)


def test_canonical_postgresql_cleanup_is_always_gated_and_label_exhaustive() -> None:
    text = _canonical_postgresql_workflow_text()
    assert _canonical_postgresql_cleanup_is_always_gated(text)


def test_canonical_postgresql_contract_rejects_job_write_permission_mutation() -> None:
    text = _canonical_postgresql_workflow_text()
    for override in (
        "permissions:\n      contents: write",
        "permissions: read-all",
        '"permissions": write-all',
    ):
        mutated = text.replace(
            "    runs-on: windows-latest",
            f"    {override}\n    runs-on: windows-latest",
            1,
        )
        assert mutated != text
        assert not _workflow_is_least_privilege_and_sha_pinned(mutated)


def test_canonical_postgresql_contract_rejects_unpinned_action_mutation() -> None:
    text = _canonical_postgresql_workflow_text()
    for unpinned in (
        "actions/checkout@v7",
        "astral-sh/setup-uv@main",
    ):
        mutated = text.replace(
            "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
            unpinned,
            1,
        )
        assert mutated != text
        assert not _workflow_is_least_privilege_and_sha_pinned(mutated)


def test_canonical_postgresql_contract_rejects_local_project_mutation() -> None:
    text = _canonical_postgresql_workflow_text()
    mutated = text.replace(
        "LOCAL_STACK_TEST_PROJECT: knowledge-ci-",
        "LOCAL_STACK_TEST_PROJECT: knowledge-local-",
        1,
    )
    assert mutated != text
    assert not _canonical_postgresql_project_identity_is_ci_scoped(mutated)


def test_canonical_postgresql_contract_rejects_missing_always_cleanup_mutation() -> None:
    text = _canonical_postgresql_workflow_text()
    mutated = text.replace("        if: always()\n", "", 1)
    assert mutated != text
    assert not _canonical_postgresql_cleanup_is_always_gated(mutated)


def test_canonical_postgresql_contract_rejects_provider_secret_mutation() -> None:
    text = _canonical_postgresql_workflow_text()
    mutated = text.replace(
        'CI: "true"',
        'CI: "true"\n      R2_ACCOUNT_TOKEN: ${{ secrets.R2_ACCOUNT_TOKEN }}',
        1,
    )
    assert mutated != text
    assert not _canonical_postgresql_is_free_of_forbidden_material(mutated)


def test_canonical_postgresql_contract_rejects_widened_artifact_mutation() -> None:
    text = _canonical_postgresql_workflow_text()
    for widened in (
        f"          path: {CANONICAL_POSTGRESQL_JUNIT_ARTIFACT_PATH}\n"
        "            - .local/stack-secrets",
        "          path: .local",
    ):
        mutated = text.replace(
            f"          path: {CANONICAL_POSTGRESQL_JUNIT_ARTIFACT_PATH}",
            widened,
            1,
        )
        assert mutated != text
        assert not _canonical_postgresql_artifact_scope_is_junit_only(mutated)


# ---------------------------------------------------------------------------
# Dedicated live R2 harness workflow contract (Task 11)
# ---------------------------------------------------------------------------


def test_r2_live_workflow_is_trusted_and_exact_cleanup_only() -> None:
    text = R2_LIVE_WORKFLOW.read_text("utf-8")
    assert "pull_request:" not in text
    assert "branches: [master]" in text
    assert "schedule:" in text and "workflow_dispatch:" in text
    assert "R2_TEST_ACCESS_KEY_ID" in text
    assert "R2_TEST_SECRET_ACCESS_KEY" in text
    assert "R2_PRODUCTION" not in text
    assert "--junitxml=.local/test-results/object-storage-live.xml" in text
    assert "ListObjects" not in text and "prefix-delete" not in text


# ---------------------------------------------------------------------------
# Protected phase-one acceptance workflow contract (Task 15, spec 18.4)
# ---------------------------------------------------------------------------

CANONICAL_CORE_ACCEPTANCE_JUNIT_ARTIFACT_PATH = ".local/test-results/canonical-core-acceptance.xml"


def _acceptance_workflow_text() -> str:
    return CANONICAL_CORE_ACCEPTANCE_WORKFLOW.read_text(encoding="utf-8")


def _acceptance_step_block(text: str, step_name: str) -> str:
    jobs = _job_blocks(text)
    pattern = re.compile(
        rf"(?ms)^      - name: {re.escape(step_name)}\n(?P<body>.*?)(?=^      - |\Z)"
    )
    matches = [
        match.group("body") for job_body in jobs.values() for match in pattern.finditer(job_body)
    ]
    return "".join(matches)


def _acceptance_artifact_scope_is_junit_only(text: str) -> bool:
    if text.count("actions/upload-artifact@") != 1:
        return False
    if any(token in text for token in (".local/stack-secrets", ".env\n")):
        return False
    uploaded_paths = re.findall(r"(?m)^ +path: (.+)$", text)
    return uploaded_paths == [CANONICAL_CORE_ACCEPTANCE_JUNIT_ARTIFACT_PATH]


def test_acceptance_workflow_never_runs_fork_prs_and_never_cancels() -> None:
    text = _acceptance_workflow_text()
    assert "pull_request:" not in text
    assert "pull_request_target" not in text
    assert "branches: [master]" in text
    assert "schedule:" in text and "workflow_dispatch:" in text
    # Per-bucket safety: a superseded run keeps its cleanup, so live-bucket
    # objects are never orphaned mid-flight (spec 18.4).
    assert "cancel-in-progress: false" in text
    assert "canonical-core-acceptance-${{ github.workflow }}-${{ github.ref }}" in text


def test_acceptance_workflow_is_least_privilege_with_bounded_job() -> None:
    text = _acceptance_workflow_text()
    assert _workflow_is_least_privilege_and_sha_pinned(text)
    assert _top_level_permissions(text) == ["  contents: read"]
    assert not _has_job_level_permissions_override(text)
    assert "timeout-minutes: 45" in text
    assert "runs-on: ubuntu-latest" in text


def test_acceptance_workflow_writes_secrets_as_mode_0600_files_only() -> None:
    text = _acceptance_workflow_text()
    secret_step = _acceptance_step_block(
        text, "Compose live R2 environment as mode-0600 secret files"
    )
    assert "R2_TEST_ACCESS_KEY_ID: ${{ secrets.R2_TEST_ACCESS_KEY_ID }}" in secret_step
    assert "R2_TEST_SECRET_ACCESS_KEY: ${{ secrets.R2_TEST_SECRET_ACCESS_KEY }}" in secret_step
    assert "${{ runner.temp }}" in secret_step
    assert "umask 0177" in secret_step
    assert "chmod 0600" in secret_step
    # The credential-shape guard fails the run explicitly on mismatch.
    assert "r2_test_access_key_id:32" in secret_step
    assert "r2_test_secret_access_key:64" in secret_step
    removal_step = _acceptance_step_block(text, "Remove dedicated test secret files")
    assert "if: always()" in removal_step
    assert 'rm -rf -- "$R2_TEST_SECRET_ROOT"' in removal_step
    # Secrets exist only inside the two step-local env blocks.
    assert text.count("secrets.R2_TEST_ACCESS_KEY_ID") == 1
    assert text.count("secrets.R2_TEST_SECRET_ACCESS_KEY") == 1


def test_acceptance_workflow_runs_disposable_stack_and_live_suite() -> None:
    text = _acceptance_workflow_text()
    run_step = _acceptance_step_block(
        text, "Run canonical-core acceptance live suite with exact-key cleanup"
    )
    assert 'CI: "true"' in text
    assert (
        "LOCAL_STACK_TEST_PROJECT: knowledge-ci-${{ github.run_id }}-${{ github.run_attempt }}"
        in text
    )
    assert "knowledge-local" not in text
    assert "R2_TEST_ENDPOINT: ${{ vars.R2_TEST_ENDPOINT }}" in text
    assert "R2_TEST_BUCKET_NAME: ${{ vars.R2_TEST_BUCKET_NAME }}" in text
    assert "uv sync --all-packages --frozen" in text
    # The stack bootstrap requires the .local parent directory at mode 0700;
    # a default-umask mkdir -p .local/test-results would create it 0755 and
    # fail the run closed with unsafe_secret_set.
    assert "install -d -m 700 .local" in run_step
    # The recovery drills require the pinned 18.4 pg_dump/pg_restore clients;
    # the runner image only ships an older major, so the workflow installs
    # them from the PGDG repository.
    assert "apt.postgresql.org/pub/repos/apt noble-pgdg main" in text
    assert "postgresql-client-18=18.4*" in text
    assert (
        'uv run pytest tests/integration/canonical_core -m "local_stack and r2_live" -q'
        " --junitxml=.local/test-results/canonical-core-acceptance.xml" in run_step
    )


def test_acceptance_workflow_cleanup_is_always_gated_and_label_exhaustive() -> None:
    text = _acceptance_workflow_text()
    cleanup = _acceptance_step_block(text, "Reset exact project and assert cleanup")
    required_tokens = (
        "if: always()",
        '--confirm-project "$LOCAL_STACK_TEST_PROJECT"',
        "--non-interactive",
        "docker container ls -a --filter",
        "docker network ls --filter",
        "docker volume ls --filter",
        '[[ -n "$remaining_containers" || -n "$remaining_networks" || -n "$remaining_volumes" ]]',
    )
    assert all(token in cleanup for token in required_tokens)


def test_acceptance_workflow_uploads_scrubbed_junit_only() -> None:
    text = _acceptance_workflow_text()
    assert _acceptance_artifact_scope_is_junit_only(text)
    upload_step = _acceptance_step_block(text, "Upload sanitized JUnit report")
    assert "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02" in upload_step
    for required_flag in (
        "if: always()",
        "include-hidden-files: true",
        "if-no-files-found: ignore",
        "retention-days: 7",
    ):
        assert required_flag in upload_step
    # No bundle, dump, service log, environment dump or Temporal history may
    # ever leave the runner (spec 18.4).
    for forbidden in (
        "docker logs",
        "docker inspect",
        ".local/stack-secrets",
        "postgres.dump",
        "temporal history",
        "env -0",
        "/proc/self/environ",
    ):
        assert forbidden not in text


def test_acceptance_contract_rejects_cancel_in_progress_mutation() -> None:
    text = _acceptance_workflow_text()
    mutated = text.replace("cancel-in-progress: false", "cancel-in-progress: true", 1)
    assert mutated != text
    assert "cancel-in-progress: false" not in mutated


def test_acceptance_contract_rejects_widened_artifact_mutation() -> None:
    text = _acceptance_workflow_text()
    for widened in (
        f"          path: {CANONICAL_CORE_ACCEPTANCE_JUNIT_ARTIFACT_PATH}\n"
        "            - .local/stack-secrets",
        "          path: .local",
        "          path: $RUNNER_TEMP",
    ):
        mutated = text.replace(
            f"          path: {CANONICAL_CORE_ACCEPTANCE_JUNIT_ARTIFACT_PATH}", widened, 1
        )
        assert mutated != text
        assert not _acceptance_artifact_scope_is_junit_only(mutated)


def test_acceptance_contract_rejects_fork_pr_trigger_mutation() -> None:
    text = _acceptance_workflow_text()
    mutated = text.replace(
        "on:\n  push:\n    branches: [master]",
        "on:\n  pull_request:\n  push:\n    branches: [master]",
        1,
    )
    assert mutated != text
    assert "pull_request:" in mutated
