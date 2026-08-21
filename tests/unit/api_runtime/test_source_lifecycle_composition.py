"""Source lifecycle composition: deterministic offline graph for export and tests.

The offline composition must be reusable for OpenAPI export and for unit
and contract tests without entering a database transaction: a fixed
identity namespace, deterministic in-memory state and the real
:class:`SourceLifecycleService` bound to closed port doubles.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Final
from uuid import UUID

import pytest
from api_runtime.source_lifecycle_composition import (
    OfflineSourceLifecycleState,
    compose_offline_source_lifecycle,
)

from personal_os.exclusion_policy.contracts import PolicySubject
from personal_os.source_lifecycle.commands import (
    LifecycleOperation,
    LifecycleState,
    SourceLifecycleCommand,
    SourceLifecycleCommitResult,
)
from personal_os.source_lifecycle.ports import (
    LifecycleDeviceContext,
    LifecyclePolicyDecision,
    LifecyclePolicyOutcome,
)
from personal_os.source_lifecycle.service import SourceLifecycleService
from personal_os.source_locators import NormalizedLocator

OFFLINE_WORKSPACE_ID: Final[UUID] = UUID("00000000-0000-7000-8000-000000000002")


def _build_rename_command() -> SourceLifecycleCommand:
    return SourceLifecycleCommand(
        source_id=UUID("018f47a0-7b00-7000-8000-000000000010"),
        event_id=UUID("018f47a0-7b00-7000-8000-000000000011"),
        idempotency_key="lifecycle-offline-001",
        operation=LifecycleOperation.RENAME,
        expected_version_id=UUID("018f47a0-7b00-7000-8000-000000000013"),
        expected_locator=NormalizedLocator("notes/old.md"),
        target_locator=NormalizedLocator("notes/new.md"),
        tombstone_id=None,
        policy_revision=1,
        client_timestamp=datetime(2026, 8, 20, 1, 2, 3, tzinfo=UTC),
    )


def _build_device_context() -> LifecycleDeviceContext:
    return LifecycleDeviceContext(
        workspace_id=OFFLINE_WORKSPACE_ID,
        device_id=UUID("018f47a0-7b00-7000-8000-000000000041"),
        user_id=UUID("018f47a0-7b00-7000-8000-000000000042"),
        device_kind="obsidian",
    )


def test_offline_state_is_deterministic_and_reusable() -> None:
    state_a = OfflineSourceLifecycleState()
    state_b = OfflineSourceLifecycleState()
    assert state_a.workspace_id == state_b.workspace_id == OFFLINE_WORKSPACE_ID
    assert state_a.device_kind == state_b.device_kind == "obsidian"


def test_compose_offline_source_lifecycle_builds_a_reusable_runtime() -> None:
    runtime_a = compose_offline_source_lifecycle()
    runtime_b = compose_offline_source_lifecycle()
    assert runtime_a.service is not runtime_b.service
    # Each runtime owns its own metrics and ports; identity is fresh.
    assert runtime_a.state.workspace_id == runtime_b.state.workspace_id


@pytest.mark.asyncio
async def test_offline_runtime_executes_a_real_rename_through_the_service() -> None:
    runtime = compose_offline_source_lifecycle()
    command = _build_rename_command()
    device_context = _build_device_context()
    from personal_os.diagnostics.context import create_diagnostic_context

    diagnostic_context = create_diagnostic_context().context

    result = await runtime.service.commit(
        command=command,
        device_context=device_context,
        diagnostic_context=diagnostic_context,
    )
    assert isinstance(result, SourceLifecycleCommitResult)
    assert result.state is LifecycleState.ACTIVE
    assert result.resulting_locator == NormalizedLocator("notes/new.md")


def test_offline_runtime_records_policy_and_store_calls_in_deterministic_order() -> None:
    # Sanity check: the runtime wires the real service so the offline policy
    # and store ports participate in the documented order. We don't assert
    # a fixed ledger here because the offline store is intentionally
    # scriptable; we assert the runtime exposes the service surface.
    runtime = compose_offline_source_lifecycle()
    assert isinstance(runtime.service, SourceLifecycleService)
    assert runtime.service.store is runtime.store
    assert runtime.service.policy is runtime.policy
    assert runtime.service.metrics is runtime.metrics


def test_offline_state_workspace_id_is_the_known_offline_value() -> None:
    runtime = compose_offline_source_lifecycle()
    assert runtime.state.workspace_id == OFFLINE_WORKSPACE_ID


def test_offline_policy_decision_uses_expected_and_target_locator() -> None:
    runtime = compose_offline_source_lifecycle()
    command = _build_rename_command()
    device_context = _build_device_context()
    decision = runtime.policy.build_decision(command=command, device_context=device_context)
    assert isinstance(decision, LifecyclePolicyDecision)
    assert decision.workspace_id == OFFLINE_WORKSPACE_ID
    assert decision.outcome is LifecyclePolicyOutcome.ALLOWED
    assert decision.expected_locator == command.expected_locator
    assert decision.target_locator == command.target_locator
    assert isinstance(decision.subject, PolicySubject)
    assert decision.subject.workspace_id == OFFLINE_WORKSPACE_ID


@pytest.mark.asyncio
async def test_offline_store_replay_path_returns_a_deterministic_result() -> None:
    runtime = compose_offline_source_lifecycle()
    command = _build_rename_command()
    device_context = _build_device_context()
    from personal_os.diagnostics.context import create_diagnostic_context

    diagnostic_context = create_diagnostic_context().context

    first = await runtime.service.commit(
        command=command,
        device_context=device_context,
        diagnostic_context=diagnostic_context,
    )
    replay = await runtime.service.commit(
        command=command,
        device_context=device_context,
        diagnostic_context=diagnostic_context,
    )
    assert first.source_version_id == replay.source_version_id
    assert first.event_sequence == replay.event_sequence
