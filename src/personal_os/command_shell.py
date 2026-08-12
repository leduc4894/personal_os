from __future__ import annotations

from argparse import ArgumentParser
from collections.abc import Sequence
from dataclasses import dataclass

from personal_os.package_metadata import distribution_version


@dataclass(frozen=True, slots=True)
class CommandIdentity:
    program_name: str
    process_description: str


def run_bootstrap_command(identity: CommandIdentity, argv: Sequence[str] | None = None) -> int:
    parser = ArgumentParser(prog=identity.program_name, description=identity.process_description)
    parser.add_argument("--version", dest="should_show_version", action="store_true")
    arguments = parser.parse_args(argv)
    if arguments.should_show_version:
        print(f"{identity.program_name} {distribution_version()}")
        return 0
    parser.print_help()
    return 0
