from __future__ import annotations

from argparse import ArgumentParser, Namespace
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import cast

from personal_os.package_metadata import distribution_version

type RuntimeCheck = Callable[[], int]


@dataclass(frozen=True, slots=True)
class CommandIdentity:
    program_name: str
    process_description: str


@dataclass(frozen=True, slots=True)
class BootstrapSubcommand:
    """One process subcommand declared by a composition root.

    ``configure`` owns the subparser surface, ``handler`` runs only after
    successful parsing selected this subcommand. Composition roots keep heavy
    imports inside their handlers so shell-only paths stay lazy.
    """

    name: str
    help: str
    configure: Callable[[ArgumentParser], None]
    handler: Callable[[Namespace], int]


def run_bootstrap_command(
    identity: CommandIdentity,
    argv: Sequence[str] | None = None,
    *,
    runtime_check: RuntimeCheck | None = None,
    subcommands: Sequence[BootstrapSubcommand] = (),
) -> int:
    """Parse one process-shell invocation and dispatch the selected path.

    Shell-only paths (no arguments, ``--help``, ``--version`` and any invalid
    syntax) never invoke ``runtime_check`` or any subcommand handler. Both fire
    only after successful parsing selects them. When no ``runtime_check``
    callback is supplied the ``check-runtime`` selection remains a syntax
    failure (exit code ``2``) rather than silently succeeding. Composition
    roots with no extra subcommands retain byte-equivalent behavior.
    """
    parser = ArgumentParser(prog=identity.program_name, description=identity.process_description)
    parser.add_argument("--version", dest="should_show_version", action="store_true")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser(
        "check-runtime",
        help="load and validate runtime configuration, emitting one diagnostic line",
    )
    for subcommand in subcommands:
        subparser = subparsers.add_parser(subcommand.name, help=subcommand.help)
        subcommand.configure(subparser)
        subparser.set_defaults(handler=subcommand.handler)
    arguments = parser.parse_args(argv)
    if arguments.should_show_version:
        print(f"{identity.program_name} {distribution_version()}")
        return 0
    if arguments.command == "check-runtime":
        if runtime_check is None:
            parser.error("check-runtime is not wired for this entry point")
        return runtime_check()
    # ``set_defaults`` attaches the handler only to the subparser namespace it
    # was declared on, so its absence means no subcommand was selected.
    selected_handler = getattr(arguments, "handler", None)
    if selected_handler is not None:
        return cast("Callable[[Namespace], int]", selected_handler)(arguments)
    parser.print_help()
    return 0
