from __future__ import annotations

import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from subprocess import CompletedProcess

import pytest

# The checker lives in the standalone ``tools`` scripts directory: a PEP 420
# namespace package, not an installable distribution. Put the repository root on
# ``sys.path`` so the checker can be imported and exercised in-process with an
# injectable command runner (no subprocess, no dependence on system versions).
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools.check_toolchain_versions import EXPECTED_OUTPUTS, verify_toolchain  # noqa: E402


@dataclass(frozen=True)
class ToolVersionExpectation:
    """One (command, expected-version) probe the checker must satisfy.

    The checker's contract is: given an injectable command runner and a set of
    these expectations, exit zero only when every normalized probe output equals
    its expected pinned version.
    """

    command: tuple[str, ...]
    expected: str


# Canonical pinned toolchain, declared independently here so the test pins the
# exact versions the workspace contract requires (it must not drift silently).
PINNED_EXPECTATIONS: list[ToolVersionExpectation] = [
    ToolVersionExpectation(command=("python", "--version"), expected="Python 3.14.6"),
    ToolVersionExpectation(command=("uv", "--version"), expected="uv 0.11.32"),
    ToolVersionExpectation(command=("node", "--version"), expected="v24.18.0"),
    ToolVersionExpectation(command=("pnpm", "--version"), expected="10.34.0"),
]

# Realistic probe stdout mirrors production: uv appends build metadata in a
# trailing parenthetical, so the success case proves the checker normalizes it.
SUCCESS_RAW_STDOUT: Mapping[tuple[str, ...], str] = {
    ("python", "--version"): "Python 3.14.6\n",
    ("uv", "--version"): "uv 0.11.32 (3010295ae 2026-07-23 x86_64-pc-windows-msvc)\n",
    ("node", "--version"): "v24.18.0\n",
    ("pnpm", "--version"): "10.34.0\n",
}


def _expectation_mapping() -> dict[tuple[str, ...], str]:
    return {item.command: item.expected for item in PINNED_EXPECTATIONS}


def _fake_runner(
    responses: Mapping[tuple[str, ...], str],
) -> Callable[[Sequence[str]], CompletedProcess[str]]:
    def _run(command: Sequence[str]) -> CompletedProcess[str]:
        return CompletedProcess(
            args=list(command),
            returncode=0,
            stdout=responses.get(tuple(command), ""),
            stderr="",
        )

    return _run


def test_pinned_expectations_match_checker_declared_outputs() -> None:
    """The independently-pinned values must equal the checker's declared set."""
    assert _expectation_mapping() == dict(EXPECTED_OUTPUTS)


def test_exact_pinned_versions_match_returns_zero() -> None:
    expectations = _expectation_mapping()
    runner = _fake_runner(SUCCESS_RAW_STDOUT)
    assert verify_toolchain(runner, expectations) == 0


def test_single_mismatch_returns_nonzero_with_only_tool_expected_actual(
    capsys: pytest.CaptureFixture[str],
) -> None:
    expectations = _expectation_mapping()
    poisoned = dict(SUCCESS_RAW_STDOUT)
    poisoned[("python", "--version")] = "Python 3.12.10\n"

    return_code = verify_toolchain(_fake_runner(poisoned), expectations)

    assert return_code != 0
    error = capsys.readouterr().err
    # The mismatch line must surface the tool name plus expected and actual.
    assert "python" in error
    assert "Python 3.14.6" in error
    assert "Python 3.12.10" in error
    # It must never echo other commands, paths, or environment contents.
    assert "uv " not in error
    assert "node" not in error
    assert "pnpm" not in error
