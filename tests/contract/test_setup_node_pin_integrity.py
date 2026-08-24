from __future__ import annotations

import re
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Final

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
APPROVED_SETUP_NODE_REFERENCE: Final[str] = (
    "actions/setup-node@820762786026740c76f36085b0efc47a31fe5020"
)
SETUP_NODE_RE = re.compile(r"^\s*uses:\s*(['\"]?)(actions/setup-node@[^\s#'\"]+)\1")


def _tracked_workflow_paths() -> list[Path]:
    result = subprocess.run(
        ("git", "ls-files", "-z", "--", ".github/workflows"),
        check=True,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    return sorted(
        (
            REPO_ROOT / entry
            for entry in result.stdout.split("\0")
            if entry.endswith((".yml", ".yaml"))
        ),
        key=lambda path: path.relative_to(REPO_ROOT).as_posix(),
    )


def _setup_node_references(
    workflow_paths: Sequence[Path],
) -> list[tuple[str, int, str]]:
    references: list[tuple[str, int, str]] = []
    for workflow_path in workflow_paths:
        relative_path = (
            workflow_path.relative_to(REPO_ROOT).as_posix()
            if workflow_path.is_relative_to(REPO_ROOT)
            else workflow_path.name
        )
        for line_number, line in enumerate(
            workflow_path.read_text(encoding="utf-8").splitlines(), 1
        ):
            match = SETUP_NODE_RE.match(line.split("#", 1)[0].rstrip())
            if match:
                references.append((relative_path, line_number, match.group(2)))
    return references


def _assert_approved_setup_node_references(
    references: Sequence[tuple[str, int, str]],
) -> None:
    assert references, "tracked workflows must contain actions/setup-node"
    invalid = [
        f"{path}:{line_number}: {reference}"
        for path, line_number, reference in references
        if reference != APPROVED_SETUP_NODE_REFERENCE
    ]
    assert not invalid, "invalid actions/setup-node references:\n" + "\n".join(invalid)


def test_every_tracked_setup_node_reference_uses_the_approved_full_sha() -> None:
    _assert_approved_setup_node_references(_setup_node_references(_tracked_workflow_paths()))


def test_historical_typo_reports_relative_location(tmp_path: Path) -> None:
    workflow_path = tmp_path / "workflow.yml"
    workflow_path.write_text(
        "uses: actions/setup-node@820762786026740c76336085b0efc47a31fe5020\n",
        encoding="utf-8",
    )

    references = _setup_node_references((workflow_path,))
    with pytest.raises(
        AssertionError,
        match=r"workflow\.yml:1: actions/setup-node@820762786026740c76336085b0efc47a31fe5020",
    ):
        _assert_approved_setup_node_references(references)
