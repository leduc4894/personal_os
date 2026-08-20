"""Lifecycle command validation proves the closed state-machine inputs."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

import pytest

from personal_os.source_lifecycle.commands import LifecycleOperation, SourceLifecycleCommand
from personal_os.source_locators import NormalizedLocator

_EVENT_ID = UUID("018f47a0-7b00-7000-8000-000000000003")


def _command(**overrides: object) -> SourceLifecycleCommand:
    values: dict[str, object] = {
        "source_id": uuid4(),
        "event_id": _EVENT_ID,
        "idempotency_key": "lifecycle-001",
        "operation": LifecycleOperation.RENAME,
        "expected_version_id": uuid4(),
        "expected_locator": NormalizedLocator("notes/old.md"),
        "target_locator": NormalizedLocator("notes/new.md"),
        "tombstone_id": None,
        "policy_revision": 1,
        "client_timestamp": datetime(2026, 8, 20, 1, 2, 3),
    }
    values.update(overrides)
    return SourceLifecycleCommand(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize("operation", [LifecycleOperation.RENAME, LifecycleOperation.MOVE])
def test_rename_and_move_require_both_locator_operands(operation: LifecycleOperation) -> None:
    with pytest.raises(ValueError, match="expected_locator"):
        _command(operation=operation, expected_locator=None)
    with pytest.raises(ValueError, match="target_locator"):
        _command(operation=operation, target_locator=None)


def test_delete_requires_expected_locator_and_forbids_target_or_tombstone() -> None:
    with pytest.raises(ValueError, match="expected_locator"):
        _command(operation=LifecycleOperation.DELETE, expected_locator=None, target_locator=None)
    with pytest.raises(ValueError, match="target_locator"):
        _command(
            operation=LifecycleOperation.DELETE,
            target_locator=NormalizedLocator("notes/new.md"),
        )
    with pytest.raises(ValueError, match="tombstone_id"):
        _command(operation=LifecycleOperation.DELETE, target_locator=None, tombstone_id=uuid4())


def test_restore_requires_target_and_tombstone_but_forbids_expected_locator() -> None:
    with pytest.raises(ValueError, match="target_locator"):
        _command(operation=LifecycleOperation.RESTORE, expected_locator=None, target_locator=None)
    with pytest.raises(ValueError, match="tombstone_id"):
        _command(operation=LifecycleOperation.RESTORE, expected_locator=None, tombstone_id=None)
    with pytest.raises(ValueError, match="expected_locator"):
        _command(operation=LifecycleOperation.RESTORE, tombstone_id=uuid4())


def test_lifecycle_command_rejects_same_expected_and_target_locator() -> None:
    locator = NormalizedLocator("notes/unchanged.md")
    with pytest.raises(ValueError, match="must differ"):
        _command(expected_locator=locator, target_locator=locator)


def test_lifecycle_command_requires_uuid7_event_and_positive_policy_revision() -> None:
    with pytest.raises(ValueError, match="UUIDv7"):
        _command(event_id=uuid4())
    with pytest.raises(ValueError, match="policy_revision"):
        _command(policy_revision=0)


def test_lifecycle_command_fails_closed_for_unknown_operation() -> None:
    with pytest.raises(ValueError, match="operation"):
        _command(operation="rename")
