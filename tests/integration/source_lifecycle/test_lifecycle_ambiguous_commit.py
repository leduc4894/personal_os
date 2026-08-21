"""Ambiguous-commit recovery through a fresh bounded evidence lookup.

The lost-acknowledgement case proves the adapter resolves an uncertain commit
through a fresh connection's evidence lookup: when a transaction commits but
the caller's acknowledgement is lost, a second commit returns the committed
replay without writing a duplicate graph. When the evidence lookup finds
nothing, the adapter returns the retryable
``source_lifecycle_commit_outcome_unknown`` without guessing a rollback.
"""

from __future__ import annotations

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


def _rename_command(
    *,
    source: SeededSourceLocator,
    expected: NormalizedLocator,
    target: NormalizedLocator,
    idempotency_key: str,
    event_id: UUID,
) -> SourceLifecycleCommand:
    return SourceLifecycleCommand(
        source_id=source.source_id,
        event_id=event_id,
        idempotency_key=idempotency_key,
        operation=LifecycleOperation.RENAME,
        expected_version_id=source.current_version_id,
        expected_locator=expected,
        target_locator=target,
        tombstone_id=None,
        policy_revision=1,
        client_timestamp=datetime(2026, 8, 20, 1, 2, 3, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_ambiguous_commit_returns_replay_when_evidence_exists(
    lifecycle_harness: LifecycleHarness,
    monkey: pytest.MonkeyPatch,
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
    command = _rename_command(
        source=seeded,
        expected=seeded.initial_locator,
        target=target,
        idempotency_key="lifecycle-ambiguous",
        event_id=UUID("018f47a0-7b00-7000-8000-0000000000a1"),
    )
    decision = _allowed_decision(
        workspace,
        source_id,
        expected=seeded.initial_locator.value,
        target=target.value,
    )
    fingerprint = fingerprint_lifecycle_command(command)

    # First commit succeeds and returns the canonical result.
    first = await lifecycle_harness.lifecycle_store.commit(
        command,
        _device_context(workspace),
        fingerprint,
        decision,
        _diagnostic_context(),
    )
    counts_after_first = await lifecycle_harness.table_row_counts()

    # Simulate an ambiguous acknowledgement by raising a connection-class
    # failure right before the commit acknowledgement. The bounded retry
    # must fall back to a fresh evidence lookup that returns the replay.
    real_commit_once = lifecycle_store.PostgresqlSourceLifecycleStore._commit_lifecycle_once

    state = {"calls": 0}

    async def injected_once(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        state["calls"] += 1
        if state["calls"] == 1:
            raise RuntimeError("simulated lost acknowledgement")
        return await real_commit_once(self, *args, **kwargs)

    monkey.setattr(
        lifecycle_store.PostgresqlSourceLifecycleStore,
        "_commit_lifecycle_once",
        injected_once,
    )

    second = await lifecycle_harness.lifecycle_store.commit(
        command,
        _device_context(workspace),
        fingerprint,
        decision,
        _diagnostic_context(),
    )

    counts_after_second = await lifecycle_harness.table_row_counts()
    assert second.event_id == first.event_id
    assert second.event_sequence == first.event_sequence
    assert second.source_version_id == first.source_version_id
    assert counts_after_second == counts_after_first


@pytest.mark.asyncio
async def test_ambiguous_commit_returns_commit_outcome_unknown_when_no_evidence(
    lifecycle_harness: LifecycleHarness,
    monkey: pytest.MonkeyPatch,
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
    command = _rename_command(
        source=seeded,
        expected=seeded.initial_locator,
        target=target,
        idempotency_key="lifecycle-ambiguous-empty",
        event_id=UUID("018f47a0-7b00-7000-8000-0000000000a2"),
    )
    decision = _allowed_decision(
        workspace,
        source_id,
        expected=seeded.initial_locator.value,
        target=target.value,
    )
    fingerprint = fingerprint_lifecycle_command(command)

    counts_before = await lifecycle_harness.table_row_counts()

    # Simulate the same ambiguous failure path with no committed evidence.
    state = {"calls": 0}

    async def injected_once(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        state["calls"] += 1
        if state["calls"] == 1:
            raise RuntimeError("simulated lost acknowledgement without commit")
        return await lifecycle_store.PostgresqlSourceLifecycleStore._commit_lifecycle_once(
            self, *args, **kwargs
        )

    monkey.setattr(
        lifecycle_store.PostgresqlSourceLifecycleStore,
        "_commit_lifecycle_once",
        injected_once,
    )

    with pytest.raises(SourceLifecycleError) as failure:
        await lifecycle_harness.lifecycle_store.commit(
            command,
            _device_context(workspace),
            fingerprint,
            decision,
            _diagnostic_context(),
        )
    assert failure.value.code is SourceLifecycleErrorCode.COMMIT_OUTCOME_UNKNOWN
    counts_after = await lifecycle_harness.table_row_counts()
    diff = {
        name: counts_after[name] - counts_before[name]
        for name in counts_after
    }
    assert diff["source_locators"] == 0
    assert diff["sync_events"] == 0
    assert diff["projection_intents"] == 0
    assert diff["audit_events"] == 0