from __future__ import annotations

from argparse import ArgumentParser
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from personal_os.package_metadata import distribution_version

type RuntimeCheck = Callable[[], int]


@dataclass(frozen=True, slots=True)
class CommandIdentity:
    program_name: str
    process_description: str


def run_bootstrap_command(
    identity: CommandIdentity,
    argv: Sequence[str] | None = None,
    *,
    runtime_check: RuntimeCheck | None = None,
) -> int:
    """Parse one process-shell invocation and dispatch the selected path.

    Shell-only paths (no arguments, ``--help``, ``--version`` and any invalid
    syntax) never invoke ``runtime_check``. The callback fires only after
    successful parsing selects the ``check-runtime`` subcommand. When no callback
    is supplied the selection remains a syntax failure (exit code ``2``) rather
    than silently succeeding.
    """
    parser = ArgumentParser(prog=identity.program_name, description=identity.process_description)
    parser.add_argument("--version", dest="should_show_version", action="store_true")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser(
        "check-runtime",
        help="load and validate runtime configuration, emitting one diagnostic line",
    )
    arguments = parser.parse_args(argv)
    if arguments.should_show_version:
        print(f"{identity.program_name} {distribution_version()}")
        return 0
    if arguments.command == "check-runtime":
        if runtime_check is None:
            parser.error("check-runtime is not wired for this entry point")
        return runtime_check()
    parser.print_help()
    return 0
