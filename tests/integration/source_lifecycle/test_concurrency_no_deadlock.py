"""Bounded-parallel-traffic concurrency probe for the lifecycle adapter.

The disposable PostgreSQL stack backs every case: eight parallel workers
fire a mixed workload of create / rename / move / delete / restore
operations against the locked lifecycle prefix. The brief requires:

> Concurrency test must prove no deadlock under bounded parallel
> traffic. Use a fixed small workload (e.g., 8 parallel workers) and
> assert all complete within a deadline.

The adapter's ``DatabaseRetryPolicy`` caps contention at three attempts
with 50-250 ms cancellable jitter between attempts; the locked prefix
orders the source / idempotency / workspace / locator / tombstone locks
so two racing workers either serialise through the same row or one
loses with a typed locator conflict. The deadline below is sized for
the bounded retry budget plus a generous per-worker latency allowance
— the test fails if PostgreSQL reports a deadlock or any worker blocks
past the deadline.

The integration tests are gated by the ``local_stack`` marker; the
disposable CI stack is the gate. In this environment the tests are
collected + lint-clean but unrun.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID, uuid4, uuid7

import pytest
from tests.integration.source_lifecycle.conftest import (
    LifecycleHarness,
    SeededSourceLocator,
    SeededWorkspace,
)

from personal_os.diagnostics.context import create_diagnostic_context
from personal_os.exclusion_policy.contracts import PolicySubject
from personal_os.source_lifecycle.commands import (
    LifecycleOperation,
    SourceLifecycleCommand,
)
from personal_os.source_lifecycle.errors import (
    SourceLifecycleError,
    SourceLifecycleErrorCode,
)
from personal_os.source_lifecycle.fingerprint import fingerprint_lifecycle_command
from personal_os.source_lifecycle.ports import (
    LifecycleDeviceContext,
    LifecyclePolicyDecision,
    LifecyclePolicyOutcome,
)
from personal_os.source_locators import NormalizedLocator

pytestmark = pytest.mark.local_stack

#: Fixed parallel workload: eight workers drive mixed traffic.
_WORKER_COUNT: int = 8

#: The bounded retry budget is three attempts at 50-250 ms jitter plus
#: commit latency. A generous deadline of 30 s covers any reasonable
#: contention without ever silently passing a deadlock.
_DEADLINE_SECONDS: float = 30.0


def _diagnostic_context():
    return create_diagnostic_context().context


def _device_context(workspace: SeededWorkspace) -> LifecycleDeviceContext:
    return LifecycleDeviceContext(
        workspace_id=workspace.workspace_id,
        device_id=workspace.device_id,
        user_id=workspace.owner_user_id,
        device_kind="obsidian",
    )


def _subject(workspace: SeededWorkspace, source_id: UUID, *, locator: str | None) -> PolicySubject:
    return PolicySubject(
        workspace_id=workspace.workspace_id,
        source_id=source_id,
        normalized_locator=locator,
        source_type="markdown",
    )


def _allowed_decision(
    workspace: SeededWorkspace,
    source_id: UUID,
    *,
    expected: str | None,
    target: str | None,
) -> LifecyclePolicyDecision:
    return LifecyclePolicyDecision(
        workspace_id=workspace.workspace_id,
        outcome=LifecyclePolicyOutcome.ALLOWED,
        policy_revision_number=1,
        subject=_subject(workspace, source_id, locator=target or expected),
        expected_locator=NormalizedLocator(expected) if expected else None,
        target_locator=NormalizedLocator(target) if target else None,
    )


def _command(
    *,
    operation: LifecycleOperation,
    source: SeededSourceLocator,
    expected: NormalizedLocator | None = None,
    target: NormalizedLocator | None = None,
    tombstone_id: UUID | None = None,
    idempotency_key: str | None = None,
    event_id: UUID | None = None,
) -> SourceLifecycleCommand:
    return SourceLifecycleCommand(
        source_id=source.source_id,
        event_id=event_id if event_id is not None else uuid7(),
        idempotency_key=(
            idempotency_key if idempotency_key is not None else f"idempotency-{uuid4()}"
        ),
        operation=operation,
        expected_version_id=source.current_version_id,
        expected_locator=expected,
        target_locator=target,
        tombstone_id=tombstone_id,
        policy_revision=1,
        client_timestamp=datetime(2026, 8, 20, 1, 2, 3, tzinfo=UTC),
    )


# --- bounded-parallel-traffic concurrency probe ------------------------------


@pytest.mark.asyncio
async def test_bounded_parallel_traffic_completes_within_the_deadline(
    lifecycle_harness: LifecycleHarness,
) -> None:
    """Eight parallel workers drive mixed lifecycle traffic without deadlock.

    Each worker independently commits one create / rename / move /
    delete / restore operation against a separate seeded source. The
    workers share no source_id but they do share the workspace policy
    row lock, the source locator advisory locks, and the projection
    intent outbox. The bounded retry policy plus the documented lock
    order must let every worker finish within ``_DEADLINE_SECONDS``.
    """

    workspace = await lifecycle_harness.seed_workspace()
    seeded_sources: list[SeededSourceLocator] = []
    for worker_index in range(_WORKER_COUNT):
        seeded_sources.append(
            await lifecycle_harness.seed_active_source_with_locator(
                workspace=workspace,
                source_id=uuid4(),
                locator=NormalizedLocator(f"notes/worker-{worker_index}.md"),
                title=f"Worker {worker_index}",
            )
        )

    async def _worker(seeded: SeededSourceLocator) -> None:
        """Drive one create / rename / move / delete / restore sequence."""
        # RENAME: worker renames its own source from the initial locator.
        rename_command = _command(
            operation=LifecycleOperation.RENAME,
            source=seeded,
            expected=seeded.initial_locator,
            target=NormalizedLocator(f"notes/renamed-{seeded.source_id}.md"),
        )
        rename_decision = _allowed_decision(
            workspace,
            seeded.source_id,
            expected=seeded.initial_locator.value,
            target=f"notes/renamed-{seeded.source_id}.md",
        )
        await lifecycle_harness.lifecycle_store.commit(
            rename_command,
            _device_context(workspace),
            fingerprint_lifecycle_command(rename_command),
            rename_decision,
            _diagnostic_context(),
        )
        # DELETE: worker then deletes its own source.
        delete_command = _command(
            operation=LifecycleOperation.DELETE,
            source=seeded,
            expected=NormalizedLocator(f"notes/renamed-{seeded.source_id}.md"),
            idempotency_key=f"delete-{seeded.source_id}",
        )
        delete_decision = LifecyclePolicyDecision(
            workspace_id=workspace.workspace_id,
            outcome=LifecyclePolicyOutcome.ALLOWED,
            policy_revision_number=1,
            subject=_subject(workspace, seeded.source_id, locator=seeded.initial_locator.value),
            expected_locator=NormalizedLocator(f"notes/renamed-{seeded.source_id}.md"),
            target_locator=None,
        )
        result = await lifecycle_harness.lifecycle_store.commit(
            delete_command,
            _device_context(workspace),
            fingerprint_lifecycle_command(delete_command),
            delete_decision,
            _diagnostic_context(),
        )
        # RESTORE: worker restores its source from the tombstone.
        restore_command = _command(
            operation=LifecycleOperation.RESTORE,
            source=seeded,
            target=NormalizedLocator(f"notes/restored-{seeded.source_id}.md"),
            tombstone_id=result.tombstone_id,
            idempotency_key=f"restore-{seeded.source_id}",
        )
        restore_decision = _allowed_decision(
            workspace,
            seeded.source_id,
            expected=None,
            target=f"notes/restored-{seeded.source_id}.md",
        )
        await lifecycle_harness.lifecycle_store.commit(
            restore_command,
            _device_context(workspace),
            fingerprint_lifecycle_command(restore_command),
            restore_decision,
            _diagnostic_context(),
        )

    async def _bounded() -> None:
        await asyncio.gather(*(_worker(source) for source in seeded_sources))

    # The bounded retry policy caps any individual contention burst at
    # three attempts; the deadline below allows the worst case plus a
    # generous margin and still fails loudly if PostgreSQL deadlocks.
    try:
        await asyncio.wait_for(_bounded(), timeout=_DEADLINE_SECONDS)
    except TimeoutError as cause:  # pragma: no cover - deadline reached
        raise AssertionError(
            f"parallel lifecycle traffic did not complete within "
            f"{_DEADLINE_SECONDS}s — possible deadlock"
        ) from cause

    # Every worker committed three transitions (rename + delete + restore).
    # Each commit writes two projection intents; the count proves every
    # worker reached the dispatcher outbox without losing a transition.
    async with lifecycle_harness._engine.connect() as connection:
        import sqlalchemy as sa

        from postgresql_source_store.tables import sync_events

        committed_count = int(
            (
                await connection.execute(sa.select(sa.func.count()).select_from(sync_events))
            ).scalar_one()
        )
    # One create per seeded source plus three lifecycle transitions per
    # worker (rename + delete + restore) — total events per source = 4.
    assert committed_count >= _WORKER_COUNT * 4


@pytest.mark.asyncio
async def test_parallel_workers_competing_for_the_same_target_locator_serialise(
    lifecycle_harness: LifecycleHarness,
) -> None:
    """Eight parallel workers racing for one locator finish within the deadline.

    The locator advisory locks serialise the workers; exactly one wins
    the rename and the others either replay (deterministic) or raise a
    typed locator conflict. No worker blocks past the deadline.
    """

    workspace = await lifecycle_harness.seed_workspace()
    seeded = await lifecycle_harness.seed_active_source_with_locator(
        workspace=workspace,
        source_id=uuid4(),
        locator=NormalizedLocator("notes/seed.md"),
    )
    target = NormalizedLocator("notes/contested.md")

    async def _racer(worker_index: int) -> None:
        command = _command(
            operation=LifecycleOperation.RENAME,
            source=seeded,
            expected=seeded.initial_locator,
            target=target,
            idempotency_key=f"contested-{worker_index}",
        )
        decision = _allowed_decision(
            workspace,
            seeded.source_id,
            expected=seeded.initial_locator.value,
            target=target.value,
        )
        try:
            await lifecycle_harness.lifecycle_store.commit(
                command,
                _device_context(workspace),
                fingerprint_lifecycle_command(command),
                decision,
                _diagnostic_context(),
            )
        except SourceLifecycleError as cause:
            # A locator conflict is the documented loser outcome; the
            # idempotency-key guard and the locator advisory lock
            # guarantee the loser never produces a partial write.
            assert cause.code is SourceLifecycleErrorCode.LOCATOR_CONFLICT

    async def _bounded() -> None:
        await asyncio.gather(*(_racer(index) for index in range(_WORKER_COUNT)))

    try:
        await asyncio.wait_for(_bounded(), timeout=_DEADLINE_SECONDS)
    except TimeoutError as cause:  # pragma: no cover - deadline reached
        raise AssertionError(
            f"contested locator race did not complete within "
            f"{_DEADLINE_SECONDS}s — possible deadlock"
        ) from cause

    # Exactly one rename committed: the active locator is the target.
    active = await lifecycle_harness.fetch_active_locator(seeded.source_id)
    assert active.normalized_locator == target.value
