from __future__ import annotations

from collections.abc import Sequence
from typing import NoReturn

from personal_os.command_shell import CommandIdentity, run_bootstrap_command

IDENTITY = CommandIdentity("personal-worker", "Temporal worker process shell")

#: The exact process-shell argument selecting the projection dispatcher loop.
DISPATCH_PROJECTIONS_ARGUMENT = "dispatch-projections"

#: The exact process-shell argument selecting the policy preview worker loop:
#: the registered Temporal preview worker plus the leased preview dispatcher.
RUN_POLICY_PREVIEWS_ARGUMENT = "run-policy-previews"

#: The exact process-shell argument selecting the policy reconciliation worker
#: loop: the registered Temporal reconciliation worker plus the leased
#: reconciliation dispatcher.
RUN_POLICY_RECONCILIATIONS_ARGUMENT = "run-policy-reconciliations"


def _check_runtime() -> int:
    from workflow_worker.runtime_check import run

    return run()


def _dispatch_projections() -> int:
    import asyncio

    from personal_os.error_contracts.exceptions import ApplicationError
    from workflow_worker.projection_dispatch_runtime import run_projection_dispatcher_process

    try:
        asyncio.run(run_projection_dispatcher_process())
    except ApplicationError as error:
        print(f"projection_dispatcher_failed {error.error_code.value}")
        return 78
    return 0


def _run_policy_previews() -> int:
    import asyncio

    from personal_os.error_contracts.exceptions import ApplicationError
    from workflow_worker.policy_workflow_runtime import run_policy_preview_process

    try:
        asyncio.run(run_policy_preview_process())
    except ApplicationError as error:
        print(f"policy_preview_worker_failed {error.error_code.value}")
        return 78
    return 0


def _run_policy_reconciliations() -> int:
    import asyncio

    from personal_os.error_contracts.exceptions import ApplicationError
    from workflow_worker.policy_workflow_runtime import run_policy_reconciliation_process

    try:
        asyncio.run(run_policy_reconciliation_process())
    except ApplicationError as error:
        print(f"policy_reconciliation_worker_failed {error.error_code.value}")
        return 78
    return 0


def _selected_arguments(argv: Sequence[str] | None) -> list[str]:
    import sys

    return list(sys.argv[1:] if argv is None else argv)


def run(argv: Sequence[str] | None = None) -> int:
    selected = _selected_arguments(argv)
    if selected == [DISPATCH_PROJECTIONS_ARGUMENT]:
        return _dispatch_projections()
    if selected == [RUN_POLICY_PREVIEWS_ARGUMENT]:
        return _run_policy_previews()
    if selected == [RUN_POLICY_RECONCILIATIONS_ARGUMENT]:
        return _run_policy_reconciliations()
    return run_bootstrap_command(IDENTITY, argv, runtime_check=_check_runtime)


def main() -> NoReturn:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
