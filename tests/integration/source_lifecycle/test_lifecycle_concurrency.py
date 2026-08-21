"""Concurrency and replay coverage for the lifecycle atomic transition adapter.

The replay and same-identity reuse cases prove the exact-replay lookup is
identical to the original commit and that a same identity with a different
fingerprint fails closed with exactly one rejection audit. The racing cases
prove the locked prefix produces deterministic outcomes: two devices racing
for one target locator, a delete racing an update (the update loses through
the source advisory lock), and a restore racing a move (the restore wins
through the tombstone row lock).
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
from personal_os.source_lifecycle.errors import SourceLifecycleError, SourceLifecycleErrorCode
from personal_os.source_lifecycle.fingerprint import fingerprint_lifecycle_command
from personal_os.source_lifecycle.ports import (
    LifecycleDeviceContext,
    LifecyclePolicyDecision,
    LifecyclePolicyOutcome,
)
from personal_os.source_locators import NormalizedLocator

pytestmark = pytest.mark.local_stack


def _diagnostic_context():
    return create_diagnostic_context().context


def _device_context(
    workspace: SeededWorkspace,
    *,
    device_id: UUID | None = None,
) -> LifecycleDeviceContext:
    return LifecycleDeviceContext(
        workspace_id=workspace.workspace_id,
        device_id=device_id if device_id is not None else workspace.device_id,
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


# --- exact replay ----------------------------------------------------------


@pytest.mark.asyncio
async def test_exact_replay_returns_identical_result_without_writing_again(
    lifecycle_harness: LifecycleHarness,
) -> None:
    workspace = await lifecycle_harness.seed_workspace()
    source_id = uuid4()
    seeded = await lifecycle_harness.seed_active_source_with_locator(
        workspace=workspace,
        source_id=source_id,
        locator=NormalizedLocator("notes/old.md"),
    )
    target = NormalizedLocator("notes/new.md")
    command = _command(
        operation=LifecycleOperation.RENAME,
        source=seeded,
        expected=seeded.initial_locator,
        target=target,
    )
    decision = _allowed_decision(
        workspace,
        source_id,
        expected=seeded.initial_locator.value,
        target=target.value,
    )
    fingerprint = fingerprint_lifecycle_command(command)

    first = await lifecycle_harness.lifecycle_store.commit(
        command,
        _device_context(workspace),
        fingerprint,
        decision,
        _diagnostic_context(),
    )
    counts_after_first = await lifecycle_harness.table_row_counts()

    replay = await lifecycle_harness.lifecycle_store.commit(
        command,
        _device_context(workspace),
        fingerprint,
        decision,
        _diagnostic_context(),
    )
    counts_after_replay = await lifecycle_harness.table_row_counts()

    assert first.event_id == replay.event_id
    assert first.source_version_id == replay.source_version_id
    assert first.event_sequence == replay.event_sequence
    assert first.committed_at == replay.committed_at
    assert counts_after_replay == counts_after_first


# --- same idempotency key with a different fingerprint ----------------------


@pytest.mark.asyncio
async def test_same_idempotency_key_with_changed_fingerprint_rejects_with_one_audit(
    lifecycle_harness: LifecycleHarness,
) -> None:
    workspace = await lifecycle_harness.seed_workspace()
    source_id = uuid4()
    seeded = await lifecycle_harness.seed_active_source_with_locator(
        workspace=workspace,
        source_id=source_id,
        locator=NormalizedLocator("notes/old.md"),
    )
    idempotency_key = "lifecycle-shared-key"
    command_first = _command(
        operation=LifecycleOperation.RENAME,
        source=seeded,
        expected=seeded.initial_locator,
        target=NormalizedLocator("notes/new.md"),
        idempotency_key=idempotency_key,
    )
    decision = _allowed_decision(
        workspace,
        source_id,
        expected=seeded.initial_locator.value,
        target="notes/new.md",
    )
    await lifecycle_harness.lifecycle_store.commit(
        command_first,
        _device_context(workspace),
        fingerprint_lifecycle_command(command_first),
        decision,
        _diagnostic_context(),
    )

    command_drift = _command(
        operation=LifecycleOperation.RENAME,
        source=seeded,
        expected=NormalizedLocator("notes/old.md"),
        target=NormalizedLocator("notes/new.md"),
        idempotency_key=idempotency_key,
        event_id=uuid7(),
    )
    counts_before = await lifecycle_harness.table_row_counts()

    with pytest.raises(SourceLifecycleError) as failure:
        await lifecycle_harness.lifecycle_store.commit(
            command_drift,
            _device_context(workspace),
            fingerprint_lifecycle_command(command_drift),
            decision,
            _diagnostic_context(),
        )
    assert failure.value.code is SourceLifecycleErrorCode.LOCATOR_CONFLICT
    counts_after = await lifecycle_harness.table_row_counts()
    assert counts_after["audit_events"] - counts_before["audit_events"] == 1


# --- two devices racing for one target locator -----------------------------


@pytest.mark.asyncio
async def test_two_devices_racing_for_one_target_locator_only_one_wins(
    lifecycle_harness: LifecycleHarness,
) -> None:
    workspace = await lifecycle_harness.seed_workspace()
    first_id = uuid4()
    second_id = uuid4()
    first = await lifecycle_harness.seed_active_source_with_locator(
        workspace=workspace,
        source_id=first_id,
        locator=NormalizedLocator("notes/first.md"),
        title="first",
    )
    second = await lifecycle_harness.seed_active_source_with_locator(
        workspace=workspace,
        source_id=second_id,
        locator=NormalizedLocator("notes/second.md"),
        title="second",
    )
    target = NormalizedLocator("notes/shared.md")
    command_a = _command(
        operation=LifecycleOperation.RENAME,
        source=first,
        expected=first.initial_locator,
        target=target,
    )
    command_b = _command(
        operation=LifecycleOperation.RENAME,
        source=second,
        expected=second.initial_locator,
        target=target,
    )
    decision_a = _allowed_decision(
        workspace,
        first_id,
        expected=first.initial_locator.value,
        target=target.value,
    )
    decision_b = _allowed_decision(
        workspace,
        second_id,
        expected=second.initial_locator.value,
        target=target.value,
    )

    async def run_first() -> None:
        await lifecycle_harness.lifecycle_store.commit(
            command_a,
            _device_context(workspace),
            fingerprint_lifecycle_command(command_a),
            decision_a,
            _diagnostic_context(),
        )

    async def run_second() -> None:
        try:
            await lifecycle_harness.lifecycle_store.commit(
                command_b,
                _device_context(workspace),
                fingerprint_lifecycle_command(command_b),
                decision_b,
                _diagnostic_context(),
            )
        except SourceLifecycleError:
            return

    await asyncio.gather(run_first(), run_second())

    history_first = await lifecycle_harness.fetch_locator_history(first_id)
    history_second = await lifecycle_harness.fetch_locator_history(second_id)
    winners = [
        history
        for history in (history_first, history_second)
        if any(row.normalized_locator == target.value for row in history)
    ]
    assert len(winners) == 1


# --- delete versus update --------------------------------------------------


@pytest.mark.asyncio
async def test_delete_vs_update_delete_wins_through_source_row_lock(
    lifecycle_harness: LifecycleHarness,
) -> None:
    workspace = await lifecycle_harness.seed_workspace()
    source_id = uuid4()
    seeded = await lifecycle_harness.seed_active_source_with_locator(
        workspace=workspace,
        source_id=source_id,
        locator=NormalizedLocator("notes/old.md"),
    )
    delete_command = _command(
        operation=LifecycleOperation.DELETE,
        source=seeded,
        expected=seeded.initial_locator,
    )
    delete_decision = LifecyclePolicyDecision(
        workspace_id=workspace.workspace_id,
        outcome=LifecyclePolicyOutcome.ALLOWED,
        policy_revision_number=1,
        subject=_subject(workspace, source_id, locator=seeded.initial_locator.value),
        expected_locator=seeded.initial_locator,
        target_locator=None,
    )
    # Second command models a would-be rename after a stale view.
    rename_command = _command(
        operation=LifecycleOperation.RENAME,
        source=seeded,
        expected=seeded.initial_locator,
        target=NormalizedLocator("notes/new.md"),
    )
    rename_decision = _allowed_decision(
        workspace,
        source_id,
        expected=seeded.initial_locator.value,
        target="notes/new.md",
    )

    async def run_delete() -> None:
        await lifecycle_harness.lifecycle_store.commit(
            delete_command,
            _device_context(workspace),
            fingerprint_lifecycle_command(delete_command),
            delete_decision,
            _diagnostic_context(),
        )

    async def run_rename() -> None:
        try:
            await lifecycle_harness.lifecycle_store.commit(
                rename_command,
                _device_context(workspace),
                fingerprint_lifecycle_command(rename_command),
                rename_decision,
                _diagnostic_context(),
            )
        except SourceLifecycleError as cause:
            assert cause.code is SourceLifecycleErrorCode.LOCATOR_MISSING

    await asyncio.gather(run_delete(), run_rename())

    source_row = await lifecycle_harness.fetch_source_row(source_id)
    assert source_row.sync_state == "deleted"
    tombstone = await lifecycle_harness.fetch_tombstone(source_id)
    assert tombstone.delete_event_id == delete_command.event_id


# --- restore versus move ----------------------------------------------------


@pytest.mark.asyncio
async def test_restore_vs_move_restore_wins_through_tombstone_row_lock(
    lifecycle_harness: LifecycleHarness,
) -> None:
    workspace = await lifecycle_harness.seed_workspace()
    source_id = uuid4()
    seeded = await lifecycle_harness.seed_active_source_with_locator(
        workspace=workspace,
        source_id=source_id,
        locator=NormalizedLocator("notes/old.md"),
    )
    delete_command = _command(
        operation=LifecycleOperation.DELETE,
        source=seeded,
        expected=seeded.initial_locator,
        event_id=UUID("018f47a0-7b00-7000-8000-0000000000e1"),
    )
    delete_decision = LifecyclePolicyDecision(
        workspace_id=workspace.workspace_id,
        outcome=LifecyclePolicyOutcome.ALLOWED,
        policy_revision_number=1,
        subject=_subject(workspace, source_id, locator=seeded.initial_locator.value),
        expected_locator=seeded.initial_locator,
        target_locator=None,
    )
    delete_result = await lifecycle_harness.lifecycle_store.commit(
        delete_command,
        _device_context(workspace),
        fingerprint_lifecycle_command(delete_command),
        delete_decision,
        _diagnostic_context(),
    )
    target = NormalizedLocator("notes/restored.md")
    restore_command = _command(
        operation=LifecycleOperation.RESTORE,
        source=seeded,
        target=target,
        tombstone_id=delete_result.tombstone_id,
    )
    restore_decision = _allowed_decision(
        workspace,
        source_id,
        expected=None,
        target=target.value,
    )
    move_command = _command(
        operation=LifecycleOperation.MOVE,
        source=seeded,
        expected=seeded.initial_locator,
        target=target,
    )
    move_decision = _allowed_decision(
        workspace,
        source_id,
        expected=seeded.initial_locator.value,
        target=target.value,
    )

    async def run_restore() -> None:
        await lifecycle_harness.lifecycle_store.commit(
            restore_command,
            _device_context(workspace),
            fingerprint_lifecycle_command(restore_command),
            restore_decision,
            _diagnostic_context(),
        )

    async def run_move() -> None:
        try:
            await lifecycle_harness.lifecycle_store.commit(
                move_command,
                _device_context(workspace),
                fingerprint_lifecycle_command(move_command),
                move_decision,
                _diagnostic_context(),
            )
        except SourceLifecycleError as cause:
            assert cause.code in {
                SourceLifecycleErrorCode.LOCATOR_MISSING,
                SourceLifecycleErrorCode.TOMBSTONE_CLOSED,
                SourceLifecycleErrorCode.LOCATOR_CONFLICT,
            }

    await asyncio.gather(run_restore(), run_move())

    source_row = await lifecycle_harness.fetch_source_row(source_id)
    assert source_row.sync_state == "active"
    tombstone = await lifecycle_harness.fetch_tombstone(source_id)
    assert tombstone.restore_event_id == restore_command.event_id
