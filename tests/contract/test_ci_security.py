from __future__ import annotations

import re
from pathlib import Path

import yaml  # type: ignore[import-untyped]  # Pinned PyYAML does not ship type stubs.

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_DIRECTORY = REPO_ROOT / ".github" / "workflows"
WORKFLOW_PATH = WORKFLOW_DIRECTORY / "quality.yml"
LOCAL_STACK_WORKFLOW_PATH = WORKFLOW_DIRECTORY / "local-service-stack.yml"
WORKFLOW_TEXT = WORKFLOW_PATH.read_text(encoding="utf-8")

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
    assert "git check-ignore --quiet .local" in jobs["ubuntu-config"]
    assert "rm -rf -- .local" in jobs["ubuntu-config"]
    assert "git check-ignore --quiet .local" in jobs["windows-config"]
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
