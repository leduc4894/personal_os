"""API process shell: lazy composition callbacks behind a shared parser.

The module stays import-light so every shell-only invocation (``--help``,
``--version``, no arguments, invalid syntax, ``check-runtime``) parses without
touching FastAPI, Uvicorn or any settings loader. The server and OpenAPI export
implementations are imported inside their handlers only, so the heavy runtime
loads exactly when the matching subcommand is selected and parsed.
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


def _configure_serve(parser: ArgumentParser) -> None:
    """Declare the serve surface: bind settings come from the environment."""


def _configure_export_openapi(parser: ArgumentParser) -> None:
    parser.add_argument("--output", required=True)


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
