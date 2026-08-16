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


@pytest.mark.parametrize(
    "subcommand", ("enroll-web-credential", "reset-web-authentication")
)
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


def _patch_getpass_sequence(
    monkeypatch: pytest.MonkeyPatch, responses: Iterator[str]
) -> list[str]:
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
    monkeypatch.setattr("builtins.input", lambda _prompt: next(typed_lines))
    with pytest.raises(credential_commands.CredentialCommandInputError) as rejected:
        credential_commands.read_emergency_reset_confirmation(username="owner")
    assert "owner-typo" not in str(rejected.value)

    typed_lines = iter(["owner"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(typed_lines))
    assert (
        credential_commands.read_emergency_reset_confirmation(username="owner") == "owner"
    )


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
