"""API contract artifact gate: stale snapshots fail without being rewritten.

These tests pin the non-mutating byte comparison behind the
``api-contract-snapshot-check`` gate: the committed snapshot is compared with
the deterministic :func:`render_openapi_json` bytes through a constant-time
digest, stale bytes make the check exit ``1`` while leaving the file untouched,
an exact render exits ``0``, and the only stdout is one fixed
``api_contract_current``/``api_contract_stale`` token line — never schema
content, never filesystem paths.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[3]))

from api_runtime.openapi_export import render_openapi_json
from tools.api_contract_artifacts import (
    TOKEN_CONTRACT_CURRENT,
    TOKEN_CONTRACT_STALE,
    check_snapshot,
    main,
)


def test_snapshot_check_detects_stale_bytes_without_rewriting(tmp_path: Path) -> None:
    snapshot = tmp_path / "openapi.json"
    stale = b'{"openapi":"stale"}\n'
    snapshot.write_bytes(stale)
    assert check_snapshot(snapshot) == 1
    assert snapshot.read_bytes() == stale


def test_snapshot_check_accepts_exact_render(tmp_path: Path) -> None:
    snapshot = tmp_path / "openapi.json"
    snapshot.write_bytes(render_openapi_json())
    assert check_snapshot(snapshot) == 0


def test_stale_check_emits_exactly_one_fixed_token_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    snapshot = tmp_path / "openapi.json"
    snapshot.write_bytes(b'{"openapi":"stale"}\n')
    assert check_snapshot(snapshot) == 1
    captured = capsys.readouterr()
    assert captured.out == f"{TOKEN_CONTRACT_STALE}\n"
    assert captured.err == ""
    assert str(tmp_path) not in captured.out + captured.err


def test_current_check_emits_exactly_one_fixed_token_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    snapshot = tmp_path / "openapi.json"
    snapshot.write_bytes(render_openapi_json())
    assert check_snapshot(snapshot) == 0
    captured = capsys.readouterr()
    assert captured.out == f"{TOKEN_CONTRACT_CURRENT}\n"
    assert captured.err == ""


def test_missing_snapshot_reports_stale_without_path_or_schema_leak(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "absent-openapi.json"
    assert check_snapshot(missing) == 1
    captured = capsys.readouterr()
    assert captured.out == f"{TOKEN_CONTRACT_STALE}\n"
    assert str(tmp_path) not in captured.out + captured.err
    assert "Traceback" not in captured.err


def test_default_committed_snapshot_matches_current_render(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The repository's fixed snapshot path is current: the default-suite gate."""
    assert check_snapshot() == 0
    assert capsys.readouterr().out == f"{TOKEN_CONTRACT_CURRENT}\n"


def test_main_accepts_only_the_check_command(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["check"]) == 0
    captured = capsys.readouterr()
    assert captured.out == f"{TOKEN_CONTRACT_CURRENT}\n"
    assert captured.err == ""


@pytest.mark.parametrize(
    "argv", [[], ["export"], ["check", "--output", "x.json"], ["check", "check"]]
)
def test_main_rejects_every_other_argument_shape(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(argv) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip() != ""
    assert "Traceback" not in captured.err
