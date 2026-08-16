"""API process shell: lazy composition callbacks behind a shared parser.

The module stays import-light so every shell-only invocation (``--help``,
``--version``, no arguments, invalid syntax, ``check-runtime``) parses without
touching FastAPI, Uvicorn or any settings loader. The server, OpenAPI export
and protected web-credential implementations are imported inside their
handlers only, so the heavy runtime loads exactly when the matching
subcommand is selected and parsed.
"""

from __future__ import annotations

from argparse import ArgumentParser, Namespace
from collections.abc import Sequence
from typing import Final, NoReturn

from personal_os.command_shell import (
    BootstrapSubcommand,
    CommandIdentity,
    run_bootstrap_command,
)

IDENTITY = CommandIdentity("personal-api", "API process shell")


def _check_runtime() -> int:
    from api_runtime.runtime_check import run

    return run()


def _serve(_arguments: Namespace) -> int:
    from api_runtime.server import run_server

    return run_server()


def _export_openapi(arguments: Namespace) -> int:
    from api_runtime.openapi_export import export_openapi

    return export_openapi(arguments.output)


def _enroll_web_credential(arguments: Namespace) -> int:
    from api_runtime.authentication_commands import run_enroll_web_credential

    return run_enroll_web_credential(arguments)


def _web_credential_status(arguments: Namespace) -> int:
    from api_runtime.authentication_commands import run_web_credential_status

    return run_web_credential_status(arguments)


def _reset_web_authentication(arguments: Namespace) -> int:
    from api_runtime.authentication_commands import run_reset_web_authentication

    return run_reset_web_authentication(arguments)


def _configure_serve(parser: ArgumentParser) -> None:
    """Declare the serve surface: bind settings come from the environment."""


def _configure_export_openapi(parser: ArgumentParser) -> None:
    parser.add_argument("--output", required=True)


def _configure_enroll_web_credential(parser: ArgumentParser) -> None:
    """Declare the enrollment surface: the password never travels in argv.

    Abbreviation stays off so ``--password`` cannot alias
    ``--password-file-name``: password text in argv remains a syntax failure.
    """
    parser.allow_abbrev = False
    parser.add_argument("--username", required=True)
    parser.add_argument("--password-file-name")


def _configure_web_credential_status(parser: ArgumentParser) -> None:
    parser.allow_abbrev = False
    parser.add_argument("--username", required=True)


def _configure_reset_web_authentication(parser: ArgumentParser) -> None:
    """Declare the reset surface: password file or prompt, typed confirmation."""
    parser.allow_abbrev = False
    parser.add_argument("--username", required=True)
    parser.add_argument("--password-file-name")


API_SUBCOMMANDS: Final[tuple[BootstrapSubcommand, ...]] = (
    BootstrapSubcommand(
        name="serve",
        help="run the API server with settings from the environment",
        configure=_configure_serve,
        handler=_serve,
    ),
    BootstrapSubcommand(
        name="export-openapi",
        help="export the OpenAPI contract document to one file",
        configure=_configure_export_openapi,
        handler=_export_openapi,
    ),
    BootstrapSubcommand(
        name="enroll-web-credential",
        help="enroll the initial web credential of one canonical username",
        configure=_configure_enroll_web_credential,
        handler=_enroll_web_credential,
    ),
    BootstrapSubcommand(
        name="web-credential-status",
        help="report the web credential enrollment and credential revision",
        configure=_configure_web_credential_status,
        handler=_web_credential_status,
    ),
    BootstrapSubcommand(
        name="reset-web-authentication",
        help="emergency-reset every web authentication surface of one username",
        configure=_configure_reset_web_authentication,
        handler=_reset_web_authentication,
    ),
)


def run(argv: Sequence[str] | None = None) -> int:
    return run_bootstrap_command(
        IDENTITY,
        argv,
        runtime_check=_check_runtime,
        subcommands=API_SUBCOMMANDS,
    )


def main() -> NoReturn:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
