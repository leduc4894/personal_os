from __future__ import annotations

from argparse import ArgumentParser, Namespace
from collections.abc import Sequence

import pytest

from personal_os.command_shell import (
    BootstrapSubcommand,
    CommandIdentity,
    run_bootstrap_command,
)

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


def test_check_runtime_dispatches_explicit_callback() -> None:
    calls = 0

    def check_runtime() -> int:
        nonlocal calls
        calls += 1
        return 78

    result = run_bootstrap_command(
        IDENTITY,
        ["check-runtime"],
        runtime_check=check_runtime,
    )

    assert result == 78
    assert calls == 1


def test_check_runtime_propagates_callback_return_code() -> None:
    def check_runtime() -> int:
        return 42

    assert run_bootstrap_command(IDENTITY, ["check-runtime"], runtime_check=check_runtime) == 42


def test_no_arguments_never_invokes_callback() -> None:
    calls = 0

    def check_runtime() -> int:
        nonlocal calls
        calls += 1
        return 0

    assert run_bootstrap_command(IDENTITY, [], runtime_check=check_runtime) == 0
    assert calls == 0


def test_help_flag_never_invokes_callback(
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls = 0

    def check_runtime() -> int:
        nonlocal calls
        calls += 1
        return 0

    with pytest.raises(SystemExit) as raised:
        run_bootstrap_command(IDENTITY, ["--help"], runtime_check=check_runtime)
    assert raised.value.code == 0
    assert "usage: personal-api" in capsys.readouterr().out
    assert calls == 0


def test_version_flag_never_invokes_callback(
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls = 0

    def check_runtime() -> int:
        nonlocal calls
        calls += 1
        return 0

    assert run_bootstrap_command(IDENTITY, ["--version"], runtime_check=check_runtime) == 0
    assert capsys.readouterr().out.strip() == "personal-api 0.1.0"
    assert calls == 0


def test_invalid_syntax_never_invokes_callback(
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls = 0

    def check_runtime() -> int:
        nonlocal calls
        calls += 1
        return 0

    with pytest.raises(SystemExit) as raised:
        run_bootstrap_command(IDENTITY, ["--invalid"], runtime_check=check_runtime)
    captured = capsys.readouterr()
    assert raised.value.code == 2
    assert "unrecognized arguments" in captured.err
    assert "Traceback" not in captured.err
    assert calls == 0


def test_check_runtime_without_callback_is_syntax_failure(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as raised:
        run_bootstrap_command(IDENTITY, ["check-runtime"])
    captured = capsys.readouterr()
    assert raised.value.code == 2
    assert "Traceback" not in captured.err


def test_help_lists_check_runtime_subcommand(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as raised:
        run_bootstrap_command(IDENTITY, ["--help"], runtime_check=lambda: 0)
    assert raised.value.code == 0
    assert "check-runtime" in capsys.readouterr().out


def test_shell_only_argv_sequence_types_accept_sequence() -> None:
    """A plain list argv must remain accepted alongside the keyword callback."""
    argv: Sequence[str] = ["check-runtime"]
    assert run_bootstrap_command(IDENTITY, argv, runtime_check=lambda: 7) == 7


def test_bootstrap_subcommand_parses_owned_arguments_and_calls_handler() -> None:
    calls: list[Namespace] = []
    command = BootstrapSubcommand(
        name="export-openapi",
        help="export contract",
        configure=lambda parser: parser.add_argument("--output", required=True),
        handler=lambda arguments: calls.append(arguments) or 0,
    )
    assert (
        run_bootstrap_command(
            IDENTITY,
            ["export-openapi", "--output", "schema.json"],
            subcommands=(command,),
        )
        == 0
    )
    assert calls[0].output == "schema.json"


def test_bootstrap_subcommand_handler_waits_for_successful_parsing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[Namespace] = []
    command = BootstrapSubcommand(
        name="export-openapi",
        help="export contract",
        configure=lambda parser: parser.add_argument("--output", required=True),
        handler=lambda arguments: calls.append(arguments) or 0,
    )
    with pytest.raises(SystemExit) as raised:
        run_bootstrap_command(IDENTITY, ["export-openapi"], subcommands=(command,))
    captured = capsys.readouterr()
    assert raised.value.code == 2
    assert "--output" in captured.err
    assert "Traceback" not in captured.err
    assert calls == []


def test_bootstrap_subcommand_handler_receives_declared_configure_arguments() -> None:
    """``configure`` owns the subparser surface, including flags with dest renaming."""
    seen: list[Namespace] = []

    def configure(parser: ArgumentParser) -> None:
        parser.add_argument("--output", dest="output_path", required=True)

    command = BootstrapSubcommand(
        name="export-openapi",
        help="export contract",
        configure=configure,
        handler=lambda arguments: seen.append(arguments) or 3,
    )
    assert (
        run_bootstrap_command(
            IDENTITY,
            ["export-openapi", "--output", "schema.json"],
            subcommands=(command,),
        )
        == 3
    )
    assert seen[0].output_path == "schema.json"
