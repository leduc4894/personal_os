from __future__ import annotations

import re
from pathlib import Path

# The quality workflow is the workspace's only continuous-integration entry
# point. This contract reads it as plain text (never executes it) and pins the
# least-privilege security posture that every change to the file must preserve.
WORKFLOW_PATH = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "quality.yml"
WORKFLOW_TEXT = WORKFLOW_PATH.read_text(encoding="utf-8")

# A non-local ``uses:`` reference must be repo@<exactly 40 lowercase hex chars>.
SHA_PINNED_RE = re.compile(r"^[A-Za-z0-9._/-]+@[0-9a-f]{40}$")


def _uses_references(text: str) -> list[str]:
    refs: list[str] = []
    for match in re.finditer(r"(?m)^\s*uses:\s+(.+)$", text):
        raw = match.group(1)
        ref = raw.split("#", 1)[0].strip().strip("'\"")
        refs.append(ref)
    return refs


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
    block = re.search(r"(?ms)^permissions:\n((?:  [^\n]+\n)+)", WORKFLOW_TEXT)
    assert block is not None, "top-level permissions block is missing"
    permission_lines = block.group(1).splitlines()
    assert permission_lines == ["  contents: read"], (
        f"top-level permissions must be exactly contents: read, got {permission_lines!r}"
    )


def test_every_non_local_uses_reference_is_sha_pinned() -> None:
    refs = _uses_references(WORKFLOW_TEXT)
    assert refs, "workflow must declare at least one action via uses:"
    for ref in refs:
        if ref.startswith("./"):
            continue
        assert SHA_PINNED_RE.match(ref), (
            f"uses reference must be pinned to a 40-hex SHA, got {ref!r}"
        )


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
