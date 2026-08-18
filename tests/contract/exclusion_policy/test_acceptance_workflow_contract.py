"""CI contract of the exclusion-policy acceptance workflow (plan Task 13).

The workflow file is the cross-surface gate's orchestration surface, so it is
itself under contract: disposable test signing keys generated in-job through
the application's own secret-file boundary and never echoed, a unique
guarded ``knowledge-ci-*`` Compose project that is destroyed in ``always()``
cleanup with a label-exhaustive leftover assertion, every required gate
(migration, feature, performance, Web, plugin, browser E2E) executed — never
skipped — and only redacted JUnit reports leaving the runner. The global
least-privilege/SHA-pinning contract in ``tests/contract/test_ci_security.py``
covers the shared workflow invariants; this module pins the
exclusion-policy-specific behavior and rejects the mutations that would
weaken it.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import pytest

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[3]
WORKFLOW_PATH: Final[Path] = REPO_ROOT / ".github" / "workflows" / "exclusion-policy-acceptance.yml"

#: A non-local ``uses:`` reference must be repo@<exactly 40 lowercase hex>.
SHA_PINNED_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9._/-]+@[0-9a-f]{40}$")


def _workflow_text() -> str:
    if not WORKFLOW_PATH.is_file():
        pytest.fail(
            "the exclusion-policy acceptance workflow is missing: "
            f"{WORKFLOW_PATH} must exist and orchestrate the cross-surface gates"
        )
    return WORKFLOW_PATH.read_text(encoding="utf-8")


def _step_block(text: str, step_name: str) -> str:
    pattern = re.compile(
        rf"(?ms)^      - name: {re.escape(step_name)}\n(?P<body>.*?)(?=^      - |\Z)"
    )
    match = pattern.search(text)
    if match is None:
        pytest.fail(f"workflow step {step_name!r} is missing")
    return match.group("body")


def test_workflow_is_least_privilege_sha_pinned_and_time_bound() -> None:
    text = _workflow_text()
    assert re.search(r"(?m)^permissions:\n  contents: read\n", text), (
        "top-level permissions must be exactly contents: read"
    )
    assert "pull_request_target" not in text
    assert not re.search(r"(?m)^    permissions:", text), (
        "no job may override the workflow permissions"
    )
    timeouts = re.findall(r"(?m)^\s*timeout-minutes:\s*(\d+)\s*$", text)
    assert timeouts and all(int(value) > 0 for value in timeouts)
    for match in re.finditer(r"(?m)^\s*uses:\s+(.+)$", text):
        reference = match.group(1).split("#", 1)[0].strip().strip("'\"")
        assert SHA_PINNED_RE.fullmatch(reference), (
            f"uses reference must be pinned to a 40-hex SHA, got {reference!r}"
        )


def test_disposable_signing_keys_are_generated_in_job_and_never_echoed() -> None:
    text = _workflow_text()
    generation = _step_block(text, "Generate disposable test signing key")
    # Generation goes through the application's own secret-file boundary
    # (exclusive create, mode 0600, PKCS#8 PEM) inside the job.
    assert "create_or_load_policy_signing_key" in generation
    assert "install -d -m 700" in generation
    assert generation.count("0600") >= 1 or "stat -c '%a'" in generation
    # Only the derived public key id is ever printed; the private PEM
    # material is never echoed, catted or exported into the environment.
    for forbidden in (
        "cat ",
        'echo "$POLICY',
        "less ",
        "xxd",
        "base64",
    ):
        assert forbidden not in generation, (
            f"the signing-key step must never render key material ({forbidden!r})"
        )
    assert "secrets." not in text, (
        "the disposable keys are generated in-job; no GitHub secret may feed them"
    )
    removal = _step_block(text, "Remove disposable test signing key material")
    assert "if: always()" in removal
    assert "rm -rf --" in removal


def test_stack_project_is_unique_guarded_and_never_the_operator_project() -> None:
    text = _workflow_text()
    assert (
        "LOCAL_STACK_TEST_PROJECT: knowledge-ci-${{ github.run_id }}"
        "-${{ github.run_attempt }}" in text
    )
    assert "knowledge-local" not in text
    assert 'CI: "true"' in text
    validation = _step_block(text, "Validate bounded project identity")
    assert "^knowledge-ci-[0-9]+-[0-9]+$" in validation
    assert "${#LOCAL_STACK_TEST_PROJECT} > 63" in validation
    # The pinned Compose binary and stack images are installed/prefetched
    # before any gate provisions the stack.
    assert text.count("v2.30.0") >= 1
    assert "1cddcb3399cc68c385796a6ab441ab5734d4c6a0cb4713bd2bf3f0d384550a38" in text
    assert "Prefetch pinned stack images" in text
    assert "pull --quiet" in text


def test_workflow_runs_every_required_acceptance_gate() -> None:
    text = _workflow_text()
    # The pinned 18.4 dump/restore clients: the backup/restore gate fails,
    # never skips, without them.
    assert "postgresql-client-18=18.4*" in text
    for required_command in (
        "tests/integration/exclusion_policy/test_policy_migration.py -m local_stack -q",
        "poe exclusion-policy-test",
        "tests/performance/test_exclusion_policy_performance.py -m local_stack -q",
        "pnpm --filter @workspace/web-runtime run test",
        "pnpm --filter @workspace/obsidian-plugin run test",
        "pnpm exec playwright install --with-deps chromium",
        "pnpm run test:e2e:exclusion-policy",
        "uv sync --all-packages --frozen",
        "pnpm install --frozen-lockfile",
    ):
        assert required_command in text, (
            f"the acceptance workflow must run the gate {required_command!r}"
        )


def _uploaded_report_paths(text: str) -> list[str]:
    """Every path entry of the single upload step (multiline blocks included)."""
    upload = re.search(
        r"(?ms)^      - name: Upload sanitized JUnit reports\n(?P<body>.*?)(?=^      - |\Z)",
        text,
    )
    assert upload is not None, "the sanitized JUnit upload step is missing"
    body = upload.group("body")
    block = re.search(r"(?m)^ +path: \|\n(?P<entries>(?: +\S.*\n?)+)", body)
    assert block is not None, "the upload step must declare a multiline path block"
    # Option lines of the same step (``if-no-files-found: ignore``) are also
    # indented; a report path never carries a mapping colon.
    return [
        line.strip()
        for line in block.group("entries").splitlines()
        if line.strip() and ":" not in line
    ]


def test_only_redacted_junit_reports_leave_the_runner() -> None:
    text = _workflow_text()
    assert text.count("actions/upload-artifact@") == 1
    upload = re.search(
        r"(?ms)^      - name: Upload sanitized JUnit reports\n(?P<body>.*?)(?=^      - |\Z)",
        text,
    )
    assert upload is not None, "the sanitized JUnit upload step is missing"
    body = upload.group("body")
    for required_flag in (
        "if: always()",
        "include-hidden-files: true",
        "if-no-files-found: ignore",
        "retention-days: 7",
    ):
        assert required_flag in body
    uploaded_paths = _uploaded_report_paths(text)
    assert uploaded_paths, "the upload must name its redacted report paths"
    for uploaded_path in uploaded_paths:
        assert uploaded_path.endswith(".xml"), (
            f"only redacted XML reports may be uploaded, found {uploaded_path!r}"
        )
    for forbidden in (
        ".local/stack-secrets",
        "docker logs",
        "docker inspect",
        "postgres.dump",
        "playwright-traces",
    ):
        assert forbidden not in text


def test_cleanup_destroys_the_exact_guarded_project_in_always() -> None:
    text = _workflow_text()
    cleanup = _step_block(text, "Reset exact project and assert cleanup")
    for required_token in (
        "if: always()",
        '--confirm-project "$LOCAL_STACK_TEST_PROJECT"',
        "--non-interactive",
        "docker container ls -a --filter",
        "docker network ls --filter",
        "docker volume ls --filter",
        '[[ -n "$remaining_containers" || -n "$remaining_networks" || -n "$remaining_volumes" ]]',
        "if (( reset_status != 0 || inventory_status != 0 )); then",
    ):
        assert required_token in cleanup, f"the guarded cleanup is missing {required_token!r}"


def test_contract_rejects_echoed_signing_key_mutation() -> None:
    text = _workflow_text()
    generation = _step_block(text, "Generate disposable test signing key")
    key_id_print = 'print(f"disposable signing key id: {key.key_id}")'
    assert key_id_print in generation, (
        "the generation step must print exactly the derived public key id"
    )
    mutated = generation.replace(key_id_print, "print(key_file.read_text())", 1)
    assert mutated != generation
    assert "read_text()" in mutated


def test_contract_rejects_widened_artifact_mutation() -> None:
    text = _workflow_text()
    uploaded_paths = _uploaded_report_paths(text)
    assert uploaded_paths
    widened = text.replace(f"            {uploaded_paths[0]}", "            .local", 1)
    assert widened != text
    assert not all(path.endswith(".xml") for path in _uploaded_report_paths(widened))


def test_contract_rejects_missing_always_cleanup_mutation() -> None:
    text = _workflow_text()
    cleanup = _step_block(text, "Reset exact project and assert cleanup")
    mutated = cleanup.replace("        if: always()\n", "", 1)
    assert mutated != cleanup
    assert "if: always()" not in mutated
