"""Closed-set exclusion-policy mutation runner smoke contracts."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parents[3]
RUNNER = REPO_ROOT / "tools" / "exclusion_policy_mutation_report.py"


def _write_import_setup(tests: Path, source: Path) -> None:
    (tests / "conftest.py").write_text(
        f"import sys\nfrom pathlib import Path\n\nsys.path.insert(0, {str(source)!r})\n",
        encoding="utf-8",
    )


def _run_runner(
    *, source: Path, tests: Path, output: Path, timeout_seconds: int
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(RUNNER),
            "--source",
            str(source),
            "--tests",
            str(tests),
            "--output",
            str(output),
            "--per-mutant-timeout-seconds",
            str(timeout_seconds),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_runner_enumerates_closed_set_and_kills_fixture_mutants(tmp_path: Path) -> None:
    subject = tmp_path / "subject.py"
    original = (
        "def is_enabled_and_large(enabled: bool, value: int) -> bool:\n"
        "    return enabled and value > 10\n"
    )
    subject.write_text(original, encoding="utf-8")
    tests = tmp_path / "tests"
    tests.mkdir()
    _write_import_setup(tests, tmp_path)
    (tests / "test_subject.py").write_text(
        "from subject import is_enabled_and_large\n\n"
        "def test_enabled_and_large_boundaries() -> None:\n"
        "    assert is_enabled_and_large(True, 11)\n"
        "    assert not is_enabled_and_large(True, 10)\n"
        "    assert not is_enabled_and_large(False, 100)\n",
        encoding="utf-8",
    )
    report = tmp_path / "report.md"

    result = _run_runner(source=tmp_path, tests=tests, output=report, timeout_seconds=60)

    assert result.returncode == 0, result.stderr
    assert report.read_text(encoding="utf-8").splitlines()[:7] == [
        "# Exclusion-policy mutation report",
        "",
        "- mutants: 4",
        "- killed: 4",
        "- killed by timeout: 0",
        "- survived: 0",
        "- score: 1.000",
    ]
    assert subject.read_text(encoding="utf-8") == original


def test_runner_counts_timeout_as_killed_and_restores_source(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    subject = source / "subject.py"
    original = b"def either(left: bool, right: bool) -> bool:\n    return left or right\n"
    subject.write_bytes(original)
    original_mtime_ns = subject.stat().st_mtime_ns
    tests = tmp_path / "tests"
    tests.mkdir()
    _write_import_setup(tests, source)
    (tests / "test_subject.py").write_text(
        "import time\n\n"
        "from subject import either\n\n"
        "def test_either() -> None:\n"
        "    time.sleep(2)\n"
        "    assert either(False, True)\n",
        encoding="utf-8",
    )
    report = tmp_path / "report.md"

    result = _run_runner(source=source, tests=tests, output=report, timeout_seconds=1)

    assert result.returncode == 0, result.stderr
    text = report.read_text(encoding="utf-8")
    assert "- killed: 1\n" in text
    assert "- killed by timeout: 1\n" in text
    assert "- survived: 0\n" in text
    assert subject.read_bytes() == original
    assert subject.stat().st_mtime_ns == original_mtime_ns


def test_survivor_report_contains_only_source_location_and_mutation(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "subject.py").write_text(
        "def both(left: bool, right: bool) -> bool:\n    return left and right\n",
        encoding="utf-8",
    )
    tests = tmp_path / "tests"
    tests.mkdir()
    _write_import_setup(tests, source)
    protected_output = "protected-test-output"
    (tests / "test_subject.py").write_text(
        "from subject import both\n\n"
        "def test_both_false_values() -> None:\n"
        f"    print({protected_output!r})\n"
        "    assert not both(False, False)\n",
        encoding="utf-8",
    )
    report = tmp_path / "report.md"

    result = _run_runner(source=source, tests=tests, output=report, timeout_seconds=60)

    assert result.returncode == 0, result.stderr
    text = report.read_text(encoding="utf-8")
    assert "- survived: 1\n" in text
    assert "- `subject.py:2` — And->Or\n" in text
    assert protected_output not in text
    assert str(tmp_path) not in text
    assert "test_subject.py" not in text
