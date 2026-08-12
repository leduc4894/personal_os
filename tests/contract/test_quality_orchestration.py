from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_PYPROJECT = REPO_ROOT / "pyproject.toml"
FAILING_PIPELINE_DIR = REPO_ROOT / "tests" / "fixtures" / "quality" / "failing_pipeline"

# The canonical public-gate order that `uv run poe verify` must run end to end.
VERIFY_SEQUENCE = ["format-check", "lint", "type-check", "boundary-check", "test", "build"]

# Tokens that would let a failing task masquerade as passing, or a lint task
# demote warnings to non-failing output. None may appear in any Poe task.
FAILURE_SWALLOWING_TOKENS = (
    "passWithNoTests",
    "|| true",
    "|| exit 0",
    "|| :",
    "; true",
    "2>/dev/null",
)


def _load_real_poe_tasks() -> dict[str, dict[str, Any]]:
    data = tomllib.loads(REAL_PYPROJECT.read_text(encoding="utf-8"))
    return data["tool"]["poe"]["tasks"]


def test_real_verify_sequence_matches_public_gate_order() -> None:
    tasks = _load_real_poe_tasks()
    verify = tasks["verify"]
    assert verify["sequence"] == VERIFY_SEQUENCE, (
        "the verify sequence must run the six public gates in canonical order"
    )


def test_public_poe_tasks_never_swallow_failures_or_demote_warnings() -> None:
    tasks = _load_real_poe_tasks()
    offenders: list[str] = []
    for name, task in tasks.items():
        if not isinstance(task, dict):
            continue
        if "continue_on_error" in task:
            offenders.append(f"{name}: uses continue_on_error")
        for field in ("cmd", "shell", "script"):
            value = task.get(field)
            if isinstance(value, str):
                for token in FAILURE_SWALLOWING_TOKENS:
                    if token in value:
                        offenders.append(f"{name}: {token!r} in {field}")
    assert not offenders, (
        "Poe tasks must propagate failures and keep warnings fatal:\n" + "\n".join(offenders)
    )


def test_failing_pipeline_propagates_nonzero_and_surfaces_build() -> None:
    completed = subprocess.run(
        ["poe", "-C", str(FAILING_PIPELINE_DIR), "verify"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode != 0, (
        "the failing fixture's build task must propagate a nonzero exit through poe verify"
    )
    assert "build" in completed.stdout, (
        "the failing stage must be identifiable on poe's stdout stream:\n"
        + completed.stdout
        + completed.stderr
    )
