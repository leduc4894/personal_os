"""Protected credential command surface: input boundary, laziness, closed errors.

These tests pin the shell contract of the three protected subcommands
(``enroll-web-credential``, ``web-credential-status``,
``reset-web-authentication``): their ``--help`` paths exit successfully
without prompting or touching the environment; missing arguments, password
text in argv and grammar-invalid usernames or password file names are closed
failures that never reach a prompt; and the interactive input boundary
confirms enrollment passwords, validates the emergency-reset confirmation
against the canonical username, and reads file-based passwords through the
secret-file boundary without ever echoing a rejected value.
"""

from __future__ import annotations

import getpass
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from api_runtime import command as api_command

PROTECTED_SUBCOMMANDS = (
    "enroll-web-credential",
    "web-credential-status",
    "reset-web-authentication",
)


def _fail_if_called(*_args: object, **_kwargs: object) -> str:
    raise AssertionError("a protected credential shell path issued a prompt")


def _fail_if_side_effect(*_args: object, **_kwargs: object) -> None:
    raise AssertionError("a protected credential shell path read the environment")


# --- shell-only paths never prompt or load settings --------------------------------


def test_enrollment_help_does_not_prompt_or_load_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(getpass, "getpass", _fail_if_called)
    monkeypatch.setattr(os, "getenv", _fail_if_side_effect)
    with pytest.raises(SystemExit) as exit_info:
        api_command.run(["enroll-web-credential", "--help"])
    assert exit_info.value.code == 0


@pytest.mark.parametrize("subcommand", PROTECTED_SUBCOMMANDS[1:], ids=PROTECTED_SUBCOMMANDS[1:])
def test_status_and_reset_help_do_not_prompt_or_load_settings(
    monkeypatch: pytest.MonkeyPatch, subcommand: str
) -> None:
    monkeypatch.setattr(getpass, "getpass", _fail_if_called)
    monkeypatch.setattr(os, "getenv", _fail_if_side_effect)
    with pytest.raises(SystemExit) as exit_info:
        api_command.run([subcommand, "--help"])
    assert exit_info.value.code == 0


# --- argument validation precedes every prompt --------------------------------------


@pytest.mark.parametrize("subcommand", PROTECTED_SUBCOMMANDS, ids=PROTECTED_SUBCOMMANDS)
def test_missing_username_exits_two_before_any_prompt(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], subcommand: str
) -> None:
    monkeypatch.setattr(getpass, "getpass", _fail_if_called)
    with pytest.raises(SystemExit) as exit_info:
        api_command.run([subcommand])
    assert exit_info.value.code == 2
    assert "--username" in capsys.readouterr().err


def test_password_text_is_never_an_accepted_argument(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(getpass, "getpass", _fail_if_called)
    with pytest.raises(SystemExit) as exit_info:
        api_command.run(
            ["enroll-web-credential", "--username", "owner", "--password", "argv-secret"]
        )
    assert exit_info.value.code == 2


@pytest.mark.parametrize("subcommand", PROTECTED_SUBCOMMANDS, ids=PROTECTED_SUBCOMMANDS)
def test_invalid_username_grammar_rejects_before_password_input(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], subcommand: str
) -> None:
    monkeypatch.setattr(getpass, "getpass", _fail_if_called)
    assert api_command.run([subcommand, "--username", "Not Valid!"]) == 2
    captured = capsys.readouterr()
    assert "Not Valid!" not in captured.out + captured.err


@pytest.mark.parametrize("subcommand", ("enroll-web-credential", "reset-web-authentication"))
def test_invalid_password_file_name_rejects_before_password_input(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], subcommand: str
) -> None:
    monkeypatch.setattr(getpass, "getpass", _fail_if_called)
    exit_code = api_command.run(
        [subcommand, "--username", "owner", "--password-file-name", "../escape"]
    )
    assert exit_code == 2
    captured = capsys.readouterr()
    assert "../escape" not in captured.out + captured.err


# --- the interactive and file input boundary ----------------------------------------


def _patch_getpass_sequence(monkeypatch: pytest.MonkeyPatch, responses: Iterator[str]) -> list[str]:
    prompts: list[str] = []

    def fake_getpass(prompt: str = "") -> str:
        prompts.append(prompt)
        return next(responses)

    monkeypatch.setattr(getpass, "getpass", fake_getpass)
    return prompts


def test_interactive_enrollment_rejects_mismatched_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from api_runtime import authentication_commands as credential_commands

    prompts = _patch_getpass_sequence(
        monkeypatch, iter(("first-password-value", "mismatched-password-value"))
    )
    with pytest.raises(credential_commands.CredentialCommandInputError) as error:
        credential_commands.read_password_interactively(should_confirm=True)
    message = str(error.value)
    assert "first-password-value" not in message
    assert "mismatched-password-value" not in message
    assert len(prompts) == 2


def test_interactive_enrollment_returns_the_confirmed_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from api_runtime import authentication_commands as credential_commands

    prompts = _patch_getpass_sequence(
        monkeypatch, iter(("repeated-password-value", "repeated-password-value"))
    )
    password = credential_commands.read_password_interactively(should_confirm=True)
    assert password == "repeated-password-value"
    assert len(prompts) == 2


def test_interactive_reset_password_reads_once_without_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from api_runtime import authentication_commands as credential_commands

    prompts = _patch_getpass_sequence(monkeypatch, iter(("reset-password-value",)))
    password = credential_commands.read_password_interactively(should_confirm=False)
    assert password == "reset-password-value"
    assert len(prompts) == 1


def test_reset_confirmation_must_equal_the_canonical_username(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from api_runtime import authentication_commands as credential_commands

    typed_lines = iter(["owner-typo"])
    monkeypatch.setattr(getpass, "getpass", lambda _prompt: next(typed_lines))
    with pytest.raises(credential_commands.CredentialCommandInputError) as rejected:
        credential_commands.read_emergency_reset_confirmation(username="owner")
    assert "owner-typo" not in str(rejected.value)

    typed_lines = iter(["owner"])
    monkeypatch.setattr(getpass, "getpass", lambda _prompt: next(typed_lines))
    assert credential_commands.read_emergency_reset_confirmation(username="owner") == "owner"


def test_confirmation_prompt_does_not_echo(monkeypatch: pytest.MonkeyPatch) -> None:
    from api_runtime import authentication_commands as credential_commands

    def _fail_if_echoing_input(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("the emergency-reset confirmation was read through echoing input()")

    prompts: list[str] = []

    def non_echoing_getpass(prompt: str = "") -> str:
        prompts.append(prompt)
        return "owner"

    monkeypatch.setattr("builtins.input", _fail_if_echoing_input)
    monkeypatch.setattr(getpass, "getpass", non_echoing_getpass)
    assert credential_commands.read_emergency_reset_confirmation(username="owner") == "owner"
    assert prompts == ["type the username to confirm the emergency reset: "]


def test_stdin_eof_maps_to_a_typed_abort(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from api_runtime import authentication_commands as credential_commands

    def closed_stdin_getpass(prompt: str = "") -> str:
        del prompt
        raise EOFError

    monkeypatch.setattr(getpass, "getpass", closed_stdin_getpass)
    with pytest.raises(credential_commands.CredentialCommandInputError) as rejected:
        credential_commands.read_emergency_reset_confirmation(username="owner")
    assert rejected.value.reason == "reset confirmation input closed"

    def prompt_then_abort() -> int:
        credential_commands.read_emergency_reset_confirmation(username="owner")
        raise AssertionError("a closed confirmation prompt must abort the command")

    assert credential_commands._run_protected_command(prompt_then_abort) == 2
    captured = capsys.readouterr()
    assert "reset confirmation input closed" in captured.err
    assert "internal_error" not in captured.err
    assert "70" not in captured.err


def test_password_file_names_outside_the_closed_grammar_are_rejected() -> None:
    from api_runtime import authentication_commands as credential_commands

    for rejected_name in ("../escape", "/absolute/password", "C:\\secret", "", "a//b"):
        with pytest.raises(credential_commands.CredentialCommandInputError):
            credential_commands.validate_password_file_name(rejected_name)
    assert credential_commands.validate_password_file_name("web-credential-password") == (
        "web-credential-password"
    )


def test_password_file_read_never_prompts_and_strips_line_endings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from api_runtime import authentication_commands as credential_commands

    monkeypatch.setattr(getpass, "getpass", _fail_if_called)
    (tmp_path / "cli-password").write_text("file-password-value\r\n", encoding="utf-8")
    assert (
        credential_commands.read_password_from_file_name(tmp_path, "cli-password")
        == "file-password-value"
    )


# --- the emergency internal-error line carries the closed exception-class token --------


def test_unexpected_exception_failure_line_carries_the_closed_class_token(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from api_runtime import authentication_commands as credential_commands

    def _explode() -> int:
        raise TimeoutError("operator-secret diagnostic message")

    assert credential_commands._run_protected_command(_explode) == 70
    captured = capsys.readouterr()
    assert captured.err.strip() == "personal-api: internal_error: timeout_error"
    assert "operator-secret diagnostic message" not in captured.out + captured.err


def test_exception_class_token_collapses_adversarial_class_names(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from api_runtime import authentication_commands as credential_commands

    def _explode() -> int:
        raise type("Weird-Name 42!", (Exception,), {})("secret text")

    assert credential_commands._run_protected_command(_explode) == 70
    captured = capsys.readouterr()
    assert captured.err.strip() == "personal-api: internal_error: weirdname42"
    for smuggled in ("Weird", "secret text", "!", "42!"):
        assert smuggled not in captured.err


def test_exception_class_token_falls_back_when_nothing_survives_sanitization() -> None:
    from api_runtime import authentication_commands as credential_commands

    hostile_error = type("パスワード洩れ", (Exception,), {})("nope")
    assert credential_commands._exception_class_token(hostile_error) == "unknown_error"
