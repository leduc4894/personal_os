"""Worker process-shell event-loop contract tests.

The four long-running worker processes (projection dispatcher, policy
preview, policy reconciliation, multipart cleanup) open PostgreSQL
connections through SQLAlchemy's psycopg async driver inside their
coroutines. psycopg async refuses the Windows Proactor loop that a bare
``asyncio.run`` selects on win32, so every process entrypoint must run its
coroutine on a SelectorEventLoop runner — the same contract the API CLI
commands follow. On Unix the default loop is already selector-backed, so
the assertions hold everywhere.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[3]))

from workflow_worker import command


@pytest.mark.parametrize(
    ("argument", "module", "function"),
    [
        (
            command.DISPATCH_PROJECTIONS_ARGUMENT,
            "projection_dispatch_runtime",
            "run_projection_dispatcher_process",
        ),
        (
            command.RUN_POLICY_PREVIEWS_ARGUMENT,
            "policy_workflow_runtime",
            "run_policy_preview_process",
        ),
        (
            command.RUN_POLICY_RECONCILIATIONS_ARGUMENT,
            "policy_workflow_runtime",
            "run_policy_reconciliation_process",
        ),
        (
            command.RUN_MULTIPART_CLEANUP_ARGUMENT,
            "multipart_cleanup_workflow",
            "run_multipart_cleanup_process",
        ),
    ],
)
def test_worker_process_runs_on_a_selector_event_loop(
    monkeypatch: pytest.MonkeyPatch,
    argument: str,
    module: str,
    function: str,
) -> None:
    observed: dict[str, asyncio.AbstractEventLoop] = {}

    async def _record() -> None:
        observed["loop"] = asyncio.get_running_loop()

    worker_module = importlib.import_module(f"workflow_worker.{module}")
    monkeypatch.setattr(worker_module, function, _record, raising=True)

    exit_code = command.run([argument])

    assert exit_code == 0
    assert isinstance(observed["loop"], asyncio.SelectorEventLoop)
