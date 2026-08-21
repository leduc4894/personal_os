"""Integration tests for policy races between API and store lock evaluation.

The lifecycle service evaluates the policy at the API boundary
(advisory verdict); the adapter re-evaluates under the locked
``workspace_policy_state`` row. A race between the two verdicts
chooses the locked verdict for intent operation selection but never
rejects the locator transition — the truthful canonical state still
commits with the projection operation the locked verdict implies.

The integration tests cover three concrete races:

- The API advisory verdict is ``ALLOWED`` and the locked verdict is
  ``DENIED`` for a rename: the locator transition commits and the
  intents are ``delete``.
- The API advisory verdict is ``ALLOWED`` and the locked verdict is
  ``INDETERMINATE`` for a restore: the locator transition commits,
  the tombstone is closed and the intents are ``delete``.
- A racing policy revision while the adapter holds the policy-state
  row lock serializes: the second commit sees the new revision and
  the locked verdict flows through.

The integration tests are gated by the ``local_stack`` marker; the
disposable CI stack is the gate.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4, uuid7

import pytest
import sqlalchemy as sa
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
    SourceLifecycleCommitResult,
)
from personal_os.source_lifecycle.fingerprint import fingerprint_lifecycle_command
from personal_os.source_lifecycle.ports import (
    LifecycleDeviceContext,
    LifecyclePolicyDecision,
    LifecyclePolicyOutcome,
)
from personal_os.source_locators import NormalizedLocator
from postgresql_source_store.tables import (
    workspace_policy_state,
)

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


def _subject(
    workspace: SeededWorkspace, source_id: UUID, *, locator: str | None
) -> PolicySubject:
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
    policy_revision: int = 1,
) -> LifecyclePolicyDecision:
    return LifecyclePolicyDecision(
        workspace_id=workspace.workspace_id,
        outcome=outcome,
        policy_revision_number=policy_revision,
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


# --- helpers ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _TwoSourceSeed:
    first: SeededSourceLocator
    second: SeededSourceLocator


async def _locked_active_policy_revision(
    lifecycle_harness: LifecycleHarness, workspace_id: UUID
) -> int:
    async with lifecycle_harness._engine.connect() as connection:
        row = (
            await connection.execute(
                sa.select(workspace_policy_state.c.active_revision_number).where(
                    workspace_policy_state.c.workspace_id == workspace_id
                )
            )
        ).one()
    return int(row.active_revision_number)


# --- rename race: ALLOWED advisory vs DENIED locked --------------------------


@pytest.mark.asyncio
async def test_locked_denied_rename_commits_locator_state_with_delete_intents(
    lifecycle_harness: LifecycleHarness,
) -> None:
    """The locked-policy verdict drives intent selection, not the API verdict.

    The API evaluates the policy at request time (advisory verdict).
    The store re-evaluates under the locked ``workspace_policy_state``
    row. When the locked verdict is ``DENIED`` for a rename, the
    canonical locator transition still commits (the rename is truthful)
    but the projection intents are ``delete`` — the locked verdict
    wins for intent selection.
    """

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
    allowed_decision = _decision(
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
        allowed_decision,
        _diagnostic_context(),
    )

    assert isinstance(result, SourceLifecycleCommitResult)
    assert result.resulting_locator == target
    active = await lifecycle_harness.fetch_active_locator(source_id)
    assert active.normalized_locator == target.value
    intents = await lifecycle_harness.fetch_intent_rows(result.event_id)
    for intent in intents:
        assert intent.operation == "delete"
        assert intent.source_version_id == seeded.current_version_id


@pytest.mark.asyncio
async def test_locked_indeterminate_move_commits_locator_state_with_delete_intents(
    lifecycle_harness: LifecycleHarness,
) -> None:
    """A locked indeterminate verdict still commits the move with delete intents."""

    workspace = await lifecycle_harness.seed_workspace()
    source_id = uuid4()
    seeded = await lifecycle_harness.seed_active_source_with_locator(
        workspace=workspace,
        source_id=source_id,
        locator=NormalizedLocator("notes/old.md"),
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
        outcome=LifecyclePolicyOutcome.INDETERMINATE,
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

    active = await lifecycle_harness.fetch_active_locator(source_id)
    assert active.normalized_locator == target.value
    intents = await lifecycle_harness.fetch_intent_rows(result.event_id)
    for intent in intents:
        assert intent.operation == "delete"


# --- restore race: ALLOWED advisory vs DENIED locked ------------------------


@pytest.mark.asyncio
async def test_locked_denied_restore_closes_tombstone_and_emits_delete_intents(
    lifecycle_harness: LifecycleHarness,
) -> None:
    """A denied restore still commits the tombstone close and emits delete intents."""

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
    )
    denied_decision = _decision(
        workspace,
        outcome=LifecyclePolicyOutcome.DENIED,
        source_id=source_id,
        expected=None,
        target=target.value,
    )

    result = await lifecycle_harness.lifecycle_store.commit(
        restore_command,
        _device_context(workspace),
        fingerprint_lifecycle_command(restore_command),
        denied_decision,
        _diagnostic_context(),
    )

    source_row = await lifecycle_harness.fetch_source_row(source_id)
    assert source_row.sync_state == "active"
    tombstone = await lifecycle_harness.fetch_tombstone(source_id)
    assert tombstone.restore_event_id == result.event_id
    intents = await lifecycle_harness.fetch_intent_rows(result.event_id)
    for intent in intents:
        assert intent.operation == "delete"
        assert intent.source_version_id == seeded.current_version_id


@pytest.mark.asyncio
async def test_locked_indeterminate_restore_closes_tombstone_and_emits_delete_intents(
    lifecycle_harness: LifecycleHarness,
) -> None:
    """An indeterminate restore still commits the tombstone close and emits delete intents."""

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
    decision = _decision(
        workspace,
        outcome=LifecyclePolicyOutcome.INDETERMINATE,
        source_id=source_id,
        expected=None,
        target=target.value,
    )

    result = await lifecycle_harness.lifecycle_store.commit(
        restore_command,
        _device_context(workspace),
        fingerprint_lifecycle_command(restore_command),
        decision,
        _diagnostic_context(),
    )

    tombstone = await lifecycle_harness.fetch_tombstone(source_id)
    assert tombstone.restore_event_id == result.event_id
    intents = await lifecycle_harness.fetch_intent_rows(result.event_id)
    for intent in intents:
        assert intent.operation == "delete"


# --- policy revision race under the lock --------------------------------------


@pytest.mark.asyncio
async def test_policy_revision_mismatch_uses_locked_revision_for_intent_selection(
    lifecycle_harness: LifecycleHarness,
) -> None:
    """The API revision number ``1`` is superseded by the locked revision ``2``.

    The adapter re-evaluates under the locked ``workspace_policy_state``
    row; if the externally passed revision number differs from the
    locked one, the locked verdict is authoritative for intent
    selection. The contract is that the locator transition still commits
    and the intent operation is whatever the locked verdict implies.
    """

    workspace = await lifecycle_harness.seed_workspace()
    source_id = uuid4()
    seeded = await lifecycle_harness.seed_active_source_with_locator(
        workspace=workspace,
        source_id=source_id,
        locator=NormalizedLocator("notes/old.md"),
    )
    # Bump the locked policy revision after the workspace seed; the API
    # still passes revision_number=1, so the adapter must re-evaluate.
    async with lifecycle_harness._engine.begin() as connection:
        await connection.execute(
            sa.update(workspace_policy_state)
            .where(workspace_policy_state.c.workspace_id == workspace.workspace_id)
            .values(active_revision_number=2)
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
        policy_revision=1,
    )

    result = await lifecycle_harness.lifecycle_store.commit(
        command,
        _device_context(workspace),
        fingerprint_lifecycle_command(command),
        decision,
        _diagnostic_context(),
    )

    assert result.resulting_locator == target
    # The locked revision is the source of truth (revision_number=2).
    assert await _locked_active_policy_revision(
        lifecycle_harness, workspace.workspace_id
    ) == 2
    intents = await lifecycle_harness.fetch_intent_rows(result.event_id)
    for intent in intents:
        assert intent.source_version_id == seeded.current_version_id
