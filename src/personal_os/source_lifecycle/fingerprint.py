"""Deterministic lifecycle idempotency fingerprints without telemetry leakage."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from personal_os.source_lifecycle.commands import SourceLifecycleCommand

LIFECYCLE_REQUEST_CONTRACT: Final[str] = "source_lifecycle/v1"
_HEX_LOWER: Final[frozenset[str]] = frozenset("0123456789abcdef")


@dataclass(frozen=True, slots=True)
class LifecycleRequestFingerprint:
    """A lowercase SHA-256 digest, never a raw canonical envelope."""

    hexadecimal: str

    @classmethod
    def parse(cls, value: str) -> LifecycleRequestFingerprint:
        if len(value) != 64 or any(char not in _HEX_LOWER for char in value):
            raise ValueError("value does not satisfy the canonical fingerprint contract")
        return cls(value)

    def __str__(self) -> str:
        return self.hexadecimal


def _format_utc_timestamp(timestamp: datetime) -> str:
    utc_timestamp = timestamp.astimezone(UTC)
    return f"{utc_timestamp:%Y-%m-%dT%H:%M:%S.%f}Z"


def fingerprint_lifecycle_command(command: SourceLifecycleCommand) -> LifecycleRequestFingerprint:
    """Hash every replay-identity operand with sorted compact canonical JSON."""

    envelope: dict[str, object] = {
        "contract": LIFECYCLE_REQUEST_CONTRACT,
        "operation": command.operation.value,
        "source_id": str(command.source_id),
        "expected_version_id": str(command.expected_version_id),
        "expected_locator": (
            None if command.expected_locator is None else command.expected_locator.value
        ),
        "target_locator": None if command.target_locator is None else command.target_locator.value,
        "tombstone_id": None if command.tombstone_id is None else str(command.tombstone_id),
        "policy_revision": command.policy_revision,
        "event_id": str(command.event_id),
        "idempotency_key": command.idempotency_key,
        "client_timestamp": (
            None
            if command.client_timestamp is None
            else _format_utc_timestamp(command.client_timestamp)
        ),
    }
    encoded = json.dumps(
        envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")
    return LifecycleRequestFingerprint.parse(hashlib.sha256(encoded).hexdigest())
