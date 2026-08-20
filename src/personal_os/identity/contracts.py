"""Identity bootstrap command/result contracts and validation.

The validation grammar mirrors the canonical PostgreSQL baseline exactly
(design spec 5.1): keys are ``^[a-z0-9][a-z0-9._-]{0,63}$``, free-text
fields are exact-trimmed Unicode of 1..200 code points without control
characters, values are never normalized or case-folded, and the device
kind vocabulary is closed. No UUID is accepted from any caller.
"""

from __future__ import annotations

import re
import unicodedata
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Final, NoReturn, Protocol, runtime_checkable
from uuid import UUID

from personal_os.diagnostics.events import SafeToken
from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import ApplicationError

IDENTITY_KEY_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
FREE_TEXT_MAXIMUM_LENGTH: Final[int] = 200

#: Maximum number of retained bootstrap outcome records. The recorder is a bounded
#: ring buffer for tests and standalone runs, never an unbounded audit log.
_MAXIMUM_BOOTSTRAP_RECORDS: Final[int] = 4096


class BootstrapDeviceKind(StrEnum):
    """The closed device-kind vocabulary accepted by identity bootstrap."""

    OBSIDIAN = "obsidian"
    WEB = "web"
    SYSTEM = "system"


class BootstrapIdentityOutcome(StrEnum):
    """The closed bootstrap outcomes used as result and metric labels."""

    CREATED = "created"
    EXISTING = "existing"


class BootstrapInputReason(StrEnum):
    """The closed input-rejection reason tokens (spec 5.1).

    Members are the only ``reason`` detail ever attached to
    ``identity_bootstrap_input_invalid``; being ``StrEnum`` members they are
    safe scalar diagnostic values, never caller text.
    """

    USERNAME_INVALID = "username_invalid"
    WORKSPACE_KEY_INVALID = "workspace_key_invalid"
    DISPLAY_NAME_INVALID = "display_name_invalid"
    DEVICE_NAME_INVALID = "device_name_invalid"
    DEVICE_KIND_INVALID = "device_kind_invalid"


#: Closed reason tokens accepted by ``identity_bootstrap_input_invalid``.
BOOTSTRAP_INPUT_REASONS: Final[frozenset[str]] = frozenset(
    member.value for member in BootstrapInputReason
)

#: Safe-token grammar guard: every closed reason parses as a registered
#: diagnostic token, so no reason can smuggle caller text into safe details.
BOOTSTRAP_INPUT_REASON_TOKENS: Final[tuple[SafeToken, ...]] = tuple(
    SafeToken.parse(member.value) for member in BootstrapInputReason
)


class IdentityBootstrapError(ApplicationError):
    """Typed identity bootstrap error with the closed identity code set."""

    allowed_codes: frozenset[ErrorCode] = frozenset(
        {ErrorCode.IDENTITY_BOOTSTRAP_INPUT_INVALID, ErrorCode.IDENTITY_BOOTSTRAP_STATE_CONFLICT}
    )


@dataclass(frozen=True, slots=True)
class BootstrapIdentityCommand:
    """Validated, exact-trimmed identity bootstrap command; carries no UUID."""

    username: str
    user_display_name: str
    workspace_key: str
    workspace_display_name: str
    device_name: str
    device_kind: BootstrapDeviceKind


@dataclass(frozen=True, slots=True)
class BootstrapIdentityResult:
    """Server-assigned canonical identity returned by the bootstrap store."""

    user_id: UUID
    workspace_id: UUID
    device_id: UUID
    outcome: BootstrapIdentityOutcome
    committed_at: datetime


def _reject(reason: BootstrapInputReason) -> NoReturn:
    raise IdentityBootstrapError(
        ErrorCode.IDENTITY_BOOTSTRAP_INPUT_INVALID,
        safe_details={"reason": reason},
    )


def _has_control_character(value: str) -> bool:
    return any(unicodedata.category(character) == "Cc" for character in value)


def _validate_key(value: str, reason: BootstrapInputReason) -> str:
    if not isinstance(value, str):
        _reject(reason)
    if IDENTITY_KEY_PATTERN.fullmatch(value) is None:
        _reject(reason)
    return value


def _validate_free_text(value: str, reason: BootstrapInputReason) -> str:
    if not isinstance(value, str):
        _reject(reason)
    trimmed = value.strip()
    if not trimmed or len(trimmed) > FREE_TEXT_MAXIMUM_LENGTH:
        _reject(reason)
    if _has_control_character(trimmed):
        _reject(reason)
    return trimmed


def validate_bootstrap_identity_command(
    *,
    username: str,
    user_display_name: str,
    workspace_key: str,
    workspace_display_name: str,
    device_name: str,
    device_kind: str,
) -> BootstrapIdentityCommand:
    """Validate and exact-trim one bootstrap command before any I/O (spec 5.1)."""
    validated_username = _validate_key(username, BootstrapInputReason.USERNAME_INVALID)
    validated_workspace_key = _validate_key(
        workspace_key, BootstrapInputReason.WORKSPACE_KEY_INVALID
    )
    validated_user_display_name = _validate_free_text(
        user_display_name, BootstrapInputReason.DISPLAY_NAME_INVALID
    )
    validated_workspace_display_name = _validate_free_text(
        workspace_display_name, BootstrapInputReason.DISPLAY_NAME_INVALID
    )
    validated_device_name = _validate_free_text(
        device_name, BootstrapInputReason.DEVICE_NAME_INVALID
    )
    try:
        parsed_kind = BootstrapDeviceKind(device_kind)
    except ValueError:
        _reject(BootstrapInputReason.DEVICE_KIND_INVALID)
    return BootstrapIdentityCommand(
        username=validated_username,
        user_display_name=validated_user_display_name,
        workspace_key=validated_workspace_key,
        workspace_display_name=validated_workspace_display_name,
        device_name=validated_device_name,
        device_kind=parsed_kind,
    )


#: The exact required metric name and its label dimension. IDs, keys, names and
#: free text are never metric labels, so no dimension names one.
IDENTITY_METRIC_CONTRACTS: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {"identity_bootstrap_total": frozenset({"outcome"})}
)


@runtime_checkable
class IdentityBootstrapMetrics(Protocol):
    """Low-cardinality identity bootstrap metric sink (spec 16.2)."""

    def record_bootstrap(self, outcome: BootstrapIdentityOutcome) -> None:
        """Record one completed bootstrap outcome."""
        ...


class InMemoryIdentityBootstrapMetrics:
    """Bounded in-memory sink implementing :class:`IdentityBootstrapMetrics`.

    Sufficient for tests and local acceptance runs without introducing
    Prometheus. It keeps at most :data:`_MAXIMUM_BOOTSTRAP_RECORDS` outcome
    records in a ring buffer, keyed only by the closed outcome enum, and
    rejects any non-enum label so a UUID, name or free-text value can never
    become a label.
    """

    def __init__(self) -> None:
        self.outcomes: deque[BootstrapIdentityOutcome] = deque(maxlen=_MAXIMUM_BOOTSTRAP_RECORDS)

    def record_bootstrap(self, outcome: BootstrapIdentityOutcome) -> None:
        if not isinstance(outcome, BootstrapIdentityOutcome):
            raise ValueError("outcome label must be a closed enum member")
        self.outcomes.append(outcome)

    def bootstrap_count(self, outcome: BootstrapIdentityOutcome) -> int:
        return sum(1 for recorded in self.outcomes if recorded is outcome)

    def __repr__(self) -> str:
        return "InMemoryIdentityBootstrapMetrics(redacted)"
