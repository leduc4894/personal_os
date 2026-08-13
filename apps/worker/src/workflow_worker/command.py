from __future__ import annotations

from collections.abc import Sequence
from typing import NoReturn

from personal_os.command_shell import CommandIdentity, run_bootstrap_command

IDENTITY = CommandIdentity("personal-worker", "Temporal worker process shell")


def _check_runtime() -> int:
    from workflow_worker.runtime_check import run

    return run()


def run(argv: Sequence[str] | None = None) -> int:
    return run_bootstrap_command(IDENTITY, argv, runtime_check=_check_runtime)


def main() -> NoReturn:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
