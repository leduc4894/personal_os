"""Exact, cross-platform toolchain version checker.

Probes the four approved tools (python, uv, node, pnpm) without a shell and
compares each normalized result to a pinned exact version. Exits 0 only when
every tool matches; any mismatch is reported to stderr as
``<tool>: expected <expected>, got <actual>`` and the process exits 1.

The GitHub Actions quality matrix runs this script first on Ubuntu and Windows
so a runner with the wrong toolchain fails fast, before the slower quality
gates. The :func:`verify_toolchain` entry point accepts an injectable command
runner so unit tests can exercise the comparison logic without depending on the
real system versions.
"""

from __future__ import annotations

import shutil
import sys
from collections.abc import Callable, Mapping, Sequence
from subprocess import CompletedProcess, run

# Pinned exact toolchain versions. Each key is a no-shell command tuple and each
# value is the exact normalized stdout the tool must emit. Bump only via a
# deliberate, workspace-wide toolchain change and update the CI action pins in
# ``.github/workflows/quality.yml`` in lockstep.
EXPECTED_OUTPUTS: Mapping[tuple[str, ...], str] = {
    ("python", "--version"): "Python 3.14.6",
    ("uv", "--version"): "uv 0.11.32",
    ("node", "--version"): "v24.18.0",
    ("pnpm", "--version"): "10.34.0",
}

# A command runner maps a no-shell command (program + args) to a completed
# process whose ``stdout`` carries the tool's version probe output. The default
# runner shells out to the real toolchain; tests inject a fake.
CommandRunner = Callable[[Sequence[str]], CompletedProcess[str]]


def _normalize(raw_stdout: str) -> str:
    """Reduce a version probe's stdout to its comparable form.

    Strips surrounding whitespace and the trailing build-metadata parenthetical
    that some tools append after the version (e.g. ``uv`` prints
    ``uv 0.11.32 (<commit> <date>)``), leaving the pinned version line for an
    exact comparison.
    """
    stripped = raw_stdout.strip()
    if not stripped:
        return ""
    first_line = stripped.splitlines()[0]
    return first_line.split(" (", 1)[0].strip()


def verify_toolchain(
    run_command: CommandRunner,
    expectations: Mapping[tuple[str, ...], str] = EXPECTED_OUTPUTS,
) -> int:
    """Return 0 when every probe matches, else 1 after reporting mismatches.

    Mismatch lines mention only the tool name and its expected/actual values;
    they never echo paths, environment variables, or other commands' output.
    """
    mismatches: list[str] = []
    for command, expected in expectations.items():
        completed = run_command(command)
        actual = _normalize(completed.stdout or "")
        if actual != expected:
            mismatches.append(f"{command[0]}: expected {expected!r}, got {actual!r}")
    for line in mismatches:
        print(line, file=sys.stderr)
    return 1 if mismatches else 0


def _run_command(command: Sequence[str]) -> CompletedProcess[str]:
    # No shell: the program is resolved directly by the OS, matching the
    # contract and avoiding shell-injection surface.
    program, arguments = command[0], list(command[1:])
    # ``python``: probe the running interpreter's absolute path. CI invokes this
    # checker via ``uv run python``, so ``sys.executable`` is the pinned CPython
    # 3.14.6; bare ``python`` is not directly executable on Windows runners (App
    # Execution Alias stub) nor the right version when it is. Every other tool is
    # resolved via PATH (and PATHEXT on Windows) to target the binary the runner
    # actually installed, wherever setup-* placed it.
    resolved_program = sys.executable if program == "python" else shutil.which(program) or program
    launch = [resolved_program, *arguments]
    # Windows .cmd/.bat shims (e.g. an npm-installed pnpm) cannot be executed by
    # CreateProcess directly. Launch them through cmd.exe with the resolved
    # absolute path and the controlled argument list. This stays free of shell-
    # injection surface (no interpolation; arguments come from EXPECTED_OUTPUTS)
    # and is the standard non-shell way to spawn a .cmd shim on Windows.
    if sys.platform == "win32" and resolved_program.lower().endswith((".cmd", ".bat")):
        launch = ["cmd", "/c", resolved_program, *arguments]
    return run(launch, capture_output=True, text=True, check=False)


def main() -> int:
    return verify_toolchain(_run_command, EXPECTED_OUTPUTS)


if __name__ == "__main__":
    raise SystemExit(main())
