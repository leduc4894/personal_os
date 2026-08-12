from __future__ import annotations

from collections.abc import Sequence
from typing import NoReturn

from personal_os.command_shell import CommandIdentity, run_bootstrap_command

IDENTITY = CommandIdentity("personal-mcp", "MCP process shell")


def run(argv: Sequence[str] | None = None) -> int:
    return run_bootstrap_command(IDENTITY, argv)


def main() -> NoReturn:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
