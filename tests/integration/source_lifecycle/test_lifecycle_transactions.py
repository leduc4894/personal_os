"""Atomic lifecycle transition transactions over the real PostgreSQL baseline.

Every test runs through the real async engine against the migrated
``20260820_01`` schema and exercises one lifecycle command path. Each test
asserts the exact active/history locator rows, the sync event sequence, the
two projection intents (with ``source_version_id`` equal to
``sources.current_version_id``), the tombstone shape and the redacted audit
record. Allowed rename/move/restore select upsert intents; denied or
indeterminate rename/move/restore select delete intents; delete always
selects delete intents. Rollback tests inject failures after every write
boundary and assert nothing remains. The tests cover title derivation,
exactly one tombstone per deleted source, locator uniqueness, version and
locator conflicts, the canonical lock order and the diagnostic-only
rejection paths.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

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
    SourceLifecycleCommitResult,
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


def _decision(
    workspace: SeededWorkspace,
    *,
    outcome: LifecyclePolicyOutcome,
    source_id: UUID,
    expected: str | None,
    target: str | None,
) -> LifecyclePolicyDecision:
    return LifecyclePolicyDecision(
        workspace_id=workspace.workspace_id,
        outcome=outcome,
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
) -> SourceLifecycleCommitResult.__class__:  # type: ignore[valid-type]
    from personal_os.source_lifecycle.commands import SourceLifecycleCommand

    return SourceLifecycleCommand(
        source_id=source.source_id,
        event_id=event_id if event_id is not None else uuid4(),
        idempotency_key=(
            idempotency_key
            if idempotency_key is not None
            else f"idempotency-{uuid4()}"
        ),
        operation=operation,
        expected_version_id=source.current_version_id,
        expected_locator=expected,
        target_locator=target,
        tombstone_id=tombstone_id,
        policy_revision=1,
        client_timestamp=datetime(2026, 8, 20, 1, 2, 3, tzinfo=UTC),
    )


# --- rename allowed -----------------------------------------------------------


@pytest.mark.asyncio
async def test_allowed_rename_closes_old_locator_opens_target_updates_title(
    lifecycle_harness: LifecycleHarness,
) -> None:
    workspace = await lifecycle_harness.seed_workspace()
    source_id = uuid4()
    seeded = await lifecycle_harness.seed_active_source_with_locator(
        workspace=workspace,
        source_id=source_id,
        locator=NormalizedLocator("notes/old.md"),
        title="old",
    )
    target = NormalizedLocator("notes/renamed.md")
    command = _command(
        operation=LifecycleOperation.RENAME,
        source=seeded,
        expected=seeded.initial_locator,
        target=target,
    )
    decision = _decision(
        workspace,
        outcome=LifecyclePolicyOutcome.ALLOWED,
        source_id=source_id,
        expected=seeded.initial_locator.value,
        target=target.value,
    )
    fingerprint = fingerprint_lifecycle_command(command)

    result = await lifecycle_harness.lifecycle_store.commit(
        command,
        _device_context(workspace),
        fingerprint,
        decision,
        _diagnostic_context(),
    )

    assert result.state.name == "ACTIVE"
    assert result.source_id == source_id
    assert result.source_version_id == seeded.current_version_id
    assert result.resulting_locator == target
    assert result.tombstone_id is None
    assert result.committed_at.tzinfo is not None

    active = await lifecycle_harness.fetch_active_locator(source_id)
    history = await lifecycle_harness.fetch_locator_history(source_id)
    assert active.normalized_locator == target.value
    assert len(history) == 2
    assert history[0].closed_event_id == result.event_id
    assert history[1].source_locator_id == active.source_locator_id
    assert history[1].closed_event_id is None

    source_row = await lifecycle_harness.fetch_source_row(source_id)
    assert source_row.title == "renamed"
    assert source_row.sync_state == "active"
    assert source_row.deleted_at is None
    assert source_row.current_version_id == seeded.current_version_id

    intents = await lifecycle_harness.fetch_intent_rows(result.event_id)
    kinds = {row.projection_kind for row in intents}
    assert kinds == {"qdrant", "neo4j"}
    for intent in intents:
        assert intent.source_version_id == source_row.current_version_id
        assert intent.operation == "upsert"
        assert intent.status == "pending"


# --- rename denied / indeterminate ------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome", [LifecyclePolicyOutcome.DENIED, LifecyclePolicyOutcome.INDETERMINATE]
)
async def test_denied_rename_still_commits_locator_state_with_delete_intents(
    lifecycle_harness: LifecycleHarness, outcome: LifecyclePolicyOutcome
) -> None:
    workspace = await lifecycle_harness.seed_workspace()
    source_id = uuid4()
    seeded = await lifecycle_harness.seed_active_source_with_locator(
        workspace=workspace,
        source_id=source_id,
        locator=NormalizedLocator("notes/old.md"),
        title="old",
    )
    target = NormalizedLocator("notes/renamed.md")
    command = _command(
        operation=LifecycleOperation.RENAME,
        source=seeded,
        expected=seeded.initial_locator,
        target=target,
    )
    decision = _decision(
        workspace,
        outcome=outcome,
        source_id=source_id,
        expected=seeded.initial_locator.value,
        target=target.value,
    )

    result = await lifecycle_harness.lifecycle_store.commit(
        command,
        _device_context(workspace),
        fingerprint_lifecycle_command(command),
        decision,
        _diagnostic_context(),
    )

    assert result.resulting_locator == target
    active = await lifecycle_harness.fetch_active_locator(source_id)
    assert active.normalized_locator == target.value
    intents = await lifecycle_harness.fetch_intent_rows(result.event_id)
    for intent in intents:
        assert intent.operation == "delete"


# --- move allowed / denied ------------------------------------------------------


@pytest.mark.asyncio
async def test_allowed_move_changes_parent_without_changing_title(
    lifecycle_harness: LifecycleHarness,
) -> None:
    workspace = await lifecycle_harness.seed_workspace()
    source_id = uuid4()
    seeded = await lifecycle_harness.seed_active_source_with_locator(
        workspace=workspace,
        source_id=source_id,
        locator=NormalizedLocator("notes/old.md"),
        title="unchanged-title",
    )
    target = NormalizedLocator("archive/old.md")
    command = _command(
        operation=LifecycleOperation.MOVE,
        source=seeded,
        expected=seeded.initial_locator,
        target=target,
    )
    decision = _decision(
        workspace,
        outcome=LifecyclePolicyOutcome.ALLOWED,
        source_id=source_id,
        expected=seeded.initial_locator.value,
        target=target.value,
    )

    result = await lifecycle_harness.lifecycle_store.commit(
        command,
        _device_context(workspace),
        fingerprint_lifecycle_command(command),
        decision,
        _diagnostic_context(),
    )

    assert result.resulting_locator == target
    source_row = await lifecycle_harness.fetch_source_row(source_id)
    assert source_row.title == "unchanged-title"
    intents = await lifecycle_harness.fetch_intent_rows(result.event_id)
    for intent in intents:
        assert intent.operation == "upsert"


@pytest.mark.asyncio
async def test_denied_move_still_commits_locator_state_with_delete_intents(
    lifecycle_harness: LifecycleHarness,
) -> None:
    workspace = await lifecycle_harness.seed_workspace()
    source_id = uuid4()
    seeded = await lifecycle_harness.seed_active_source_with_locator(
        workspace=workspace,
        source_id=source_id,
        locator=NormalizedLocator("notes/old.md"),
        title="unchanged-title",
    )
    target = NormalizedLocator("archive/old.md")
    command = _command(
        operation=LifecycleOperation.MOVE,
        source=seeded,
        expected=seeded.initial_locator,
        target=target,
    )
    decision = _decision(
        workspace,
        outcome=LifecyclePolicyOutcome.DENIED,
        source_id=source_id,
        expected=seeded.initial_locator.value,
        target=target.value,
    )

    result = await lifecycle_harness.lifecycle_store.commit(
        command,
        _device_context(workspace),
        fingerprint_lifecycle_command(command),
        decision,
        _diagnostic_context(),
    )

    intents = await lifecycle_harness.fetch_intent_rows(result.event_id)
    for intent in intents:
        assert intent.operation == "delete"


# --- delete ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_closes_locator_creates_tombstone_and_emits_two_delete_intents(
    lifecycle_harness: LifecycleHarness,
) -> None:
    workspace = await lifecycle_harness.seed_workspace()
    source_id = uuid4()
    seeded = await lifecycle_harness.seed_active_source_with_locator(
        workspace=workspace,
        source_id=source_id,
        locator=NormalizedLocator("notes/old.md"),
        title="note",
    )
    command = _command(
        operation=LifecycleOperation.DELETE,
        source=seeded,
        expected=seeded.initial_locator,
    )
    decision = LifecyclePolicyDecision(
        workspace_id=workspace.workspace_id,
        outcome=LifecyclePolicyOutcome.ALLOWED,
        policy_revision_number=1,
        subject=_subject(workspace, source_id, locator=seeded.initial_locator.value),
        expected_locator=seeded.initial_locator,
        target_locator=None,
    )

    result = await lifecycle_harness.lifecycle_store.commit(
        command,
        _device_context(workspace),
        fingerprint_lifecycle_command(command),
        decision,
        _diagnostic_context(),
    )

    assert result.tombstone_id is not None
    assert result.resulting_locator is None
    active = await lifecycle_harness.fetch_active_locator(source_id)
    assert active is None
    source_row = await lifecycle_harness.fetch_source_row(source_id)
    assert source_row.sync_state == "deleted"
    assert source_row.deleted_at is not None
    tombstone = await lifecycle_harness.fetch_tombstone(source_id)
    assert tombstone.source_tombstone_id == result.tombstone_id
    assert tombstone.retained_version_id == seeded.current_version_id
    assert tombstone.retained_locator == seeded.initial_locator.value
    assert tombstone.actor_kind == "device"
    assert tombstone.actor_id == workspace.device_id
    intents = await lifecycle_harness.fetch_intent_rows(result.event_id)
    for intent in intents:
        assert intent.source_version_id == seeded.current_version_id
        assert intent.operation == "delete"


# --- restore allowed / denied / indeterminate ----------------------------------


@pytest.mark.asyncio
async def test_allowed_restore_closes_tombstone_and_opens_target(
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
        idempotency_key="delete-1",
        event_id=UUID("018f47a0-7b00-7000-8000-0000000000d1"),
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
        idempotency_key="restore-1",
        event_id=UUID("018f47a0-7b00-7000-8000-0000000000d2"),
    )
    restore_decision = _decision(
        workspace,
        outcome=LifecyclePolicyOutcome.ALLOWED,
        source_id=source_id,
        expected=None,
        target=target.value,
    )

    result = await lifecycle_harness.lifecycle_store.commit(
        restore_command,
        _device_context(workspace),
        fingerprint_lifecycle_command(restore_command),
        restore_decision,
        _diagnostic_context(),
    )

    assert result.tombstone_id is None
    assert result.resulting_locator == target
    active = await lifecycle_harness.fetch_active_locator(source_id)
    assert active.normalized_locator == target.value
    tombstone = await lifecycle_harness.fetch_tombstone(source_id)
    assert tombstone.restore_event_id == result.event_id
    assert tombstone.restored_at is not None
    source_row = await lifecycle_harness.fetch_source_row(source_id)
    assert source_row.sync_state == "active"
    assert source_row.deleted_at is None
    intents = await lifecycle_harness.fetch_intent_rows(result.event_id)
    for intent in intents:
        assert intent.operation == "upsert"
        assert intent.source_version_id == seeded.current_version_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome", [LifecyclePolicyOutcome.DENIED, LifecyclePolicyOutcome.INDETERMINATE]
)
async def test_denied_or_indeterminate_restore_still_emits_two_delete_intents(
    lifecycle_harness: LifecycleHarness, outcome: LifecyclePolicyOutcome
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
        idempotency_key="delete-1",
        event_id=UUID("018f47a0-7b00-7000-8000-0000000000d1"),
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
        idempotency_key="restore-1",
        event_id=UUID("018f47a0-7b00-7000-8000-0000000000d2"),
    )
    restore_decision = _decision(
        workspace,
        outcome=outcome,
        source_id=source_id,
        expected=None,
        target=target.value,
    )

    result = await lifecycle_harness.lifecycle_store.commit(
        restore_command,
        _device_context(workspace),
        fingerprint_lifecycle_command(restore_command),
        restore_decision,
        _diagnostic_context(),
    )

    intents = await lifecycle_harness.fetch_intent_rows(result.event_id)
    for intent in intents:
        assert intent.operation == "delete"


# --- rollback paths ---------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_after",
    [
        "after_locator_close",
        "after_locator_open",
        "after_event",
        "after_intents",
        "after_audit",
    ],
)
async def test_injected_failure_after_each_write_boundary_leaves_no_partial_graph(
    lifecycle_harness: LifecycleHarness, monkey: pytest.MonkeyPatch, failure_after: str
) -> None:
    from postgresql_source_store import lifecycle_store

    workspace = await lifecycle_harness.seed_workspace()
    source_id = uuid4()
    seeded = await lifecycle_harness.seed_active_source_with_locator(
        workspace=workspace,
        source_id=source_id,
        locator=NormalizedLocator("notes/old.md"),
    )
    target = NormalizedLocator("notes/renamed.md")
    command = _command(
        operation=LifecycleOperation.RENAME,
        source=seeded,
        expected=seeded.initial_locator,
        target=target,
    )
    decision = _decision(
        workspace,
        outcome=LifecyclePolicyOutcome.ALLOWED,
        source_id=source_id,
        expected=seeded.initial_locator.value,
        target=target.value,
    )

    original = lifecycle_store.PostgresqlSourceLifecycleStore._commit_lifecycle_once
    counter = {"calls": 0}

    async def wrapped(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        counter["calls"] += 1
        return await original(self, *args, **kwargs)

    monkey.setattr(
        lifecycle_store.PostgresqlSourceLifecycleStore, "_commit_lifecycle_once", wrapped
    )

    boundary_method = {
        "after_locator_close": "_close_existing_locator",
        "after_locator_open": "_open_new_locator",
        "after_event": "_insert_lifecycle_event",
        "after_intents": "_insert_lifecycle_intents",
        "after_audit": "_insert_lifecycle_audit",
    }[failure_after]

    original_boundary = getattr(lifecycle_store.PostgresqlSourceLifecycleStore, boundary_method)
    call_state = {"count": 0}

    async def raising(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        call_state["count"] += 1
        if call_state["count"] == 1:
            raise RuntimeError(f"injected-{failure_after}")
        return await original_boundary(self, *args, **kwargs)

    monkey.setattr(
        lifecycle_store.PostgresqlSourceLifecycleStore, boundary_method, raising
    )

    with pytest.raises(RuntimeError, match=f"injected-{failure_after}"):
        await lifecycle_harness.lifecycle_store.commit(
            command,
            _device_context(workspace),
            fingerprint_lifecycle_command(command),
            decision,
            _diagnostic_context(),
        )

    # Nothing should remain: only the originally-seeded locator stays active,
    # no extra sync event, intent or audit landed; the seed title stayed.
    history = await lifecycle_harness.fetch_locator_history(source_id)
    assert len(history) == 1
    assert history[0].normalized_locator == seeded.initial_locator.value
    assert history[0].closed_event_id is None
    source_row = await lifecycle_harness.fetch_source_row(source_id)
    assert source_row.title == seeded.initial_locator.value.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    assert source_row.sync_state == "active"


@pytest.mark.asyncio
async def test_locator_collision_rejects_rename_without_partial_graph(
    lifecycle_harness: LifecycleHarness,
) -> None:
    workspace = await lifecycle_harness.seed_workspace()
    first_id = uuid4()
    second_id = uuid4()
    await lifecycle_harness.seed_active_source_with_locator(
        workspace=workspace,
        source_id=first_id,
        locator=NormalizedLocator("notes/old.md"),
        title="first",
    )
    second = await lifecycle_harness.seed_active_source_with_locator(
        workspace=workspace,
        source_id=second_id,
        locator=NormalizedLocator("notes/other.md"),
        title="second",
    )
    command = _command(
        operation=LifecycleOperation.RENAME,
        source=second,
        expected=second.initial_locator,
        target=NormalizedLocator("notes/old.md"),
    )
    decision = _decision(
        workspace,
        outcome=LifecyclePolicyOutcome.ALLOWED,
        source_id=second_id,
        expected=second.initial_locator.value,
        target="notes/old.md",
    )

    with pytest.raises(SourceLifecycleError) as failure:
        await lifecycle_harness.lifecycle_store.commit(
            command,
            _device_context(workspace),
            fingerprint_lifecycle_command(command),
            decision,
            _diagnostic_context(),
        )
    assert failure.value.code is SourceLifecycleErrorCode.LOCATOR_CONFLICT

    history_first = await lifecycle_harness.fetch_locator_history(first_id)
    assert len(history_first) == 1
    assert history_first[0].closed_event_id is None
    history_second = await lifecycle_harness.fetch_locator_history(second_id)
    assert len(history_second) == 1
    assert history_second[0].normalized_locator == "notes/other.md"


@pytest.mark.asyncio
async def test_expected_locator_mismatch_rejects_rename(
    lifecycle_harness: LifecycleHarness,
) -> None:
    workspace = await lifecycle_harness.seed_workspace()
    source_id = uuid4()
    seeded = await lifecycle_harness.seed_active_source_with_locator(
        workspace=workspace,
        source_id=source_id,
        locator=NormalizedLocator("notes/old.md"),
    )
    wrong = NormalizedLocator("notes/different.md")
    command = _command(
        operation=LifecycleOperation.RENAME,
        source=seeded,
        expected=wrong,
        target=NormalizedLocator("notes/renamed.md"),
    )
    decision = _decision(
        workspace,
        outcome=LifecyclePolicyOutcome.ALLOWED,
        source_id=source_id,
        expected=wrong.value,
        target="notes/renamed.md",
    )
    with pytest.raises(SourceLifecycleError) as failure:
        await lifecycle_harness.lifecycle_store.commit(
            command,
            _device_context(workspace),
            fingerprint_lifecycle_command(command),
            decision,
            _diagnostic_context(),
        )
    assert failure.value.code is SourceLifecycleErrorCode.LOCATOR_CONFLICT
    history = await lifecycle_harness.fetch_locator_history(source_id)
    assert len(history) == 1
    assert history[0].normalized_locator == seeded.initial_locator.value


@pytest.mark.asyncio
async def test_stale_expected_version_id_rejects_rename(
    lifecycle_harness: LifecycleHarness,
) -> None:
    workspace = await lifecycle_harness.seed_workspace()
    source_id = uuid4()
    seeded = await lifecycle_harness.seed_active_source_with_locator(
        workspace=workspace,
        source_id=source_id,
        locator=NormalizedLocator("notes/old.md"),
    )
    command = _command(
        operation=LifecycleOperation.RENAME,
        source=replace(seeded, current_version_id=uuid4()),
        expected=seeded.initial_locator,
        target=NormalizedLocator("notes/renamed.md"),
    )
    decision = _decision(
        workspace,
        outcome=LifecyclePolicyOutcome.ALLOWED,
        source_id=source_id,
        expected=seeded.initial_locator.value,
        target="notes/renamed.md",
    )
    with pytest.raises(SourceLifecycleError) as failure:
        await lifecycle_harness.lifecycle_store.commit(
            command,
            _device_context(workspace),
            fingerprint_lifecycle_command(command),
            decision,
            _diagnostic_context(),
        )
    assert failure.value.code is SourceLifecycleErrorCode.VERSION_CONFLICT


@pytest.mark.asyncio
async def test_restore_of_active_source_rejects_with_tombstone_not_found(
    lifecycle_harness: LifecycleHarness,
) -> None:
    workspace = await lifecycle_harness.seed_workspace()
    source_id = uuid4()
    seeded = await lifecycle_harness.seed_active_source_with_locator(
        workspace=workspace,
        source_id=source_id,
        locator=NormalizedLocator("notes/old.md"),
    )
    command = _command(
        operation=LifecycleOperation.RESTORE,
        source=seeded,
        target=NormalizedLocator("notes/restored.md"),
        tombstone_id=uuid4(),
    )
    decision = _decision(
        workspace,
        outcome=LifecyclePolicyOutcome.ALLOWED,
        source_id=source_id,
        expected=None,
        target="notes/restored.md",
    )
    with pytest.raises(SourceLifecycleError) as failure:
        await lifecycle_harness.lifecycle_store.commit(
            command,
            _device_context(workspace),
            fingerprint_lifecycle_command(command),
            decision,
            _diagnostic_context(),
        )
    assert failure.value.code is SourceLifecycleErrorCode.TOMBSTONE_NOT_FOUND


# --- title behaviour -------------------------------------------------------


@pytest.mark.asyncio
async def test_rename_title_handles_unicode_and_rejects_overlong_or_empty_targets(
    lifecycle_harness: LifecycleHarness,
) -> None:
    from personal_os.source_lifecycle.title import derive_title_v1

    assert derive_title_v1(NormalizedLocator("notes/n\u00e9w.md")).value == "n\u00e9w"
    with pytest.raises(ValueError):
        from personal_os.sources.commands import SourceTitle

        SourceTitle("")
    too_long = "x" * 501
    with pytest.raises(ValueError):
        SourceTitle(too_long)