"""Golden lifecycle request fingerprint tests."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from personal_os.source_lifecycle.commands import LifecycleOperation, SourceLifecycleCommand
from personal_os.source_lifecycle.fingerprint import fingerprint_lifecycle_command
from personal_os.source_locators import NormalizedLocator


def _command(**overrides: object) -> SourceLifecycleCommand:
    values: dict[str, object] = {
        "source_id": UUID("018f47a0-7b00-7000-8000-000000000002"),
        "event_id": UUID("018f47a0-7b00-7000-8000-000000000003"),
        "idempotency_key": "lifecycle-001",
        "operation": LifecycleOperation.RENAME,
        "expected_version_id": UUID("018f47a0-7b00-7000-8000-000000000005"),
        "expected_locator": NormalizedLocator("notes/old.md"),
        "target_locator": NormalizedLocator("notes/new.md"),
        "tombstone_id": None,
        "policy_revision": 7,
        "client_timestamp": datetime(2026, 8, 20, 1, 2, 3, 123456, tzinfo=UTC),
    }
    values.update(overrides)
    return SourceLifecycleCommand(**values)  # type: ignore[arg-type]


def test_lifecycle_fingerprint_has_stable_golden_value() -> None:
    assert fingerprint_lifecycle_command(_command()).hexadecimal == (
        "54b3e7cdc921d285b6dcf0beaa896bdf0e56c8d60bb4bac218e1452390264f3a"
    )


def test_lifecycle_fingerprint_binds_every_lifecycle_operand() -> None:
    baseline = fingerprint_lifecycle_command(_command())
    alternatives = [
        _command(operation=LifecycleOperation.MOVE),
        _command(source_id=UUID("018f47a0-7b00-7000-8000-000000000009")),
        _command(expected_version_id=UUID("018f47a0-7b00-7000-8000-000000000010")),
        _command(expected_locator=NormalizedLocator("notes/other.md")),
        _command(target_locator=NormalizedLocator("notes/renamed.md")),
        _command(policy_revision=8),
        _command(event_id=UUID("018f47a0-7b00-7000-8000-000000000011")),
        _command(idempotency_key="lifecycle-002"),
    ]

    assert all(fingerprint_lifecycle_command(command) != baseline for command in alternatives)


def test_lifecycle_fingerprint_binds_restore_tombstone_identity() -> None:
    first = _command(
        operation=LifecycleOperation.RESTORE,
        expected_locator=None,
        tombstone_id=UUID("018f47a0-7b00-7000-8000-000000000012"),
    )
    second = _command(
        operation=LifecycleOperation.RESTORE,
        expected_locator=None,
        tombstone_id=UUID("018f47a0-7b00-7000-8000-000000000013"),
    )

    assert fingerprint_lifecycle_command(first) != fingerprint_lifecycle_command(second)
