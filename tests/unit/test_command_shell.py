from __future__ import annotations

import pytest

from personal_os.command_shell import CommandIdentity, run_bootstrap_command

IDENTITY = CommandIdentity(program_name="personal-api", process_description="API process shell")


def test_no_arguments_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert run_bootstrap_command(IDENTITY, []) == 0
    assert "usage: personal-api" in capsys.readouterr().out


def test_version_uses_distribution_metadata(capsys: pytest.CaptureFixture[str]) -> None:
    assert run_bootstrap_command(IDENTITY, ["--version"]) == 0
    assert capsys.readouterr().out.strip() == "personal-api 0.1.0"


def test_invalid_argument_exits_two_without_traceback(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as raised:
        run_bootstrap_command(IDENTITY, ["--invalid"])
    captured = capsys.readouterr()
    assert raised.value.code == 2
    assert "unrecognized arguments" in captured.err
    assert "Traceback" not in captured.err
