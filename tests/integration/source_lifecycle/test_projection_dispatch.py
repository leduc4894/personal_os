"""Integration tests for the projection dispatcher over lifecycle intents.

The disposable PostgreSQL stack backs every case: the dispatcher/Temporal
ingestion accepts delete intents unchanged (lifecycle delete and
rename/move/restore with a denied policy all produce ``delete`` intent
operations), every lifecycle intent carries a non-null
``source_version_id`` equal to the source's ``current_version_id`` at the
moment of commit, and the two intents of one lifecycle event share the
event identity the deterministic workflow id is derived from. The
integration tests are gated by the ``local_stack`` marker; the
disposable CI stack is the gate.
"""

from __future__ import annotations

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


# --- lifecycle intents are dispatched unchanged ------------------------------


@pytest.mark.asyncio
async def test_lifecycle_delete_intent_persists_with_non_null_source_version_id(
    lifecycle_harness: LifecycleHarness,
) -> None:
    """A lifecycle delete commit writes two ``delete`` intents with the source version id."""

    workspace = await lifecycle_harness.seed_workspace()
    source_id = uuid4()
    seeded = await lifecycle_harness.seed_active_source_with_locator(
        workspace=workspace,
        source_id=source_id,
        locator=NormalizedLocator("notes/old.md"),
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

    assert result.source_version_id == seeded.current_version_id
    intents = await lifecycle_harness.fetch_intent_rows(result.event_id)
    kinds = {row.projection_kind for row in intents}
    assert kinds == {"qdrant", "neo4j"}
    for intent in intents:
        assert intent.source_version_id == seeded.current_version_id
        assert intent.operation == "delete"
        assert intent.status == "pending"


@pytest.mark.asyncio
async def test_lifecycle_rename_intent_preserves_event_identity_for_dispatch(
    lifecycle_harness: LifecycleHarness,
) -> None:
    """The two intents of one rename share the lifecycle event identity."""

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
        event_id=UUID("018f47a0-7b00-7000-8000-0000000000a1"),
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

    intents = await lifecycle_harness.fetch_intent_rows(result.event_id)
    kinds = {row.projection_kind for row in intents}
    assert kinds == {"qdrant", "neo4j"}
    for intent in intents:
        assert intent.event_id == result.event_id
        assert intent.source_id == source_id
        assert intent.source_version_id == seeded.current_version_id
        assert intent.operation == "upsert"


@pytest.mark.asyncio
async def test_lifecycle_restore_intent_emits_two_upsert_intents_via_dispatch(
    lifecycle_harness: LifecycleHarness,
) -> None:
    """An allowed restore writes two upsert intents with the post-restore source version."""

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
    restore_decision = _decision(
        workspace,
        outcome=LifecyclePolicyOutcome.ALLOWED,
        source_id=source_id,
        expected=None,
        target=target.value,
    )

    restore_result = await lifecycle_harness.lifecycle_store.commit(
        restore_command,
        _device_context(workspace),
        fingerprint_lifecycle_command(restore_command),
        restore_decision,
        _diagnostic_context(),
    )

    intents = await lifecycle_harness.fetch_intent_rows(restore_result.event_id)
    assert {row.projection_kind for row in intents} == {"qdrant", "neo4j"}
    for intent in intents:
        assert intent.source_version_id == seeded.current_version_id
        assert intent.operation == "upsert"
        assert intent.status == "pending"


@pytest.mark.asyncio
async def test_lifecycle_denied_rename_intent_dispatch_row_carries_delete_operation(
    lifecycle_harness: LifecycleHarness,
) -> None:
    """A denied rename still commits the locator transition and writes ``delete`` intents."""

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
        assert intent.source_version_id == seeded.current_version_id
        assert intent.status == "pending"
