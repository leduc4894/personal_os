"""Diagnostic event contracts: safe scalar values, enums and the closed event registry."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final
from uuid import UUID

_SAFE_TOKEN_PATTERN: Final = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,63}$")
_SHORT_DIGEST_PATTERN: Final = re.compile(r"^[0-9a-f]{16}$")


@dataclass(frozen=True, slots=True)
class SafeToken:
    """Bounded ASCII token matching the registered diagnostic field grammar."""

    value: str

    @classmethod
    def parse(cls, value: str) -> SafeToken:
        if _SAFE_TOKEN_PATTERN.fullmatch(value) is None:
            raise ValueError("value does not satisfy the safe token contract")
        return cls(value)

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class ShortDigest:
    """Lowercase hexadecimal digest of the registered fixed length."""

    value: str

    @classmethod
    def parse(cls, value: str) -> ShortDigest:
        if _SHORT_DIGEST_PATTERN.fullmatch(value) is None:
            raise ValueError("value does not satisfy the short digest contract")
        return cls(value)

    def __str__(self) -> str:
        return self.value


class DiagnosticLevel(StrEnum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class ResultCode(StrEnum):
    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DEGRADED = "degraded"
    REJECTED = "rejected"


class EventName(StrEnum):
    RUNTIME_CONFIGURATION_VALIDATED = "runtime_configuration_validated"
    RUNTIME_CONFIGURATION_FAILED = "runtime_configuration_failed"
    CLIENT_REQUEST_ID_REJECTED = "client_request_id_rejected"
    TRACE_CONTEXT_REPLACED = "trace_context_replaced"
    LOGGING_PAYLOAD_REJECTED = "logging_payload_rejected"
    DEPENDENCY_LOG = "dependency_log"
    INTERNAL_ERROR = "internal_error"


type SafeDiagnosticScalar = bool | int | StrEnum | UUID | SafeToken | ShortDigest
type SafeDiagnosticValue = SafeDiagnosticScalar | tuple[SafeDiagnosticScalar, ...]


@dataclass(frozen=True, slots=True)
class EventDefinition:
    """Fixed level, result code and caller field rules for one registered event."""

    level: DiagnosticLevel | None
    result_code: ResultCode
    required_fields: frozenset[str]
    allowed_fields: frozenset[str]


EVENT_DEFINITIONS: Final[Mapping[EventName, EventDefinition]] = MappingProxyType(
    {
        EventName.RUNTIME_CONFIGURATION_VALIDATED: EventDefinition(
            level=DiagnosticLevel.INFO,
            result_code=ResultCode.SUCCEEDED,
            required_fields=frozenset({"configured_log_level"}),
            allowed_fields=frozenset({"configured_log_level"}),
        ),
        EventName.RUNTIME_CONFIGURATION_FAILED: EventDefinition(
            level=DiagnosticLevel.ERROR,
            result_code=ResultCode.FAILED,
            required_fields=frozenset({"error_code", "error_category", "is_retryable"}),
            allowed_fields=frozenset(
                {"error_code", "error_category", "is_retryable", "reason", "count"}
            ),
        ),
        EventName.CLIENT_REQUEST_ID_REJECTED: EventDefinition(
            level=DiagnosticLevel.WARNING,
            result_code=ResultCode.REJECTED,
            required_fields=frozenset({"reason"}),
            allowed_fields=frozenset({"reason"}),
        ),
        EventName.TRACE_CONTEXT_REPLACED: EventDefinition(
            level=DiagnosticLevel.WARNING,
            result_code=ResultCode.DEGRADED,
            required_fields=frozenset({"reason"}),
            allowed_fields=frozenset({"reason"}),
        ),
        EventName.LOGGING_PAYLOAD_REJECTED: EventDefinition(
            level=DiagnosticLevel.WARNING,
            result_code=ResultCode.REJECTED,
            required_fields=frozenset({"reason", "count"}),
            allowed_fields=frozenset({"reason", "count"}),
        ),
        EventName.DEPENDENCY_LOG: EventDefinition(
            level=None,
            result_code=ResultCode.DEGRADED,
            required_fields=frozenset({"logger_name", "message_fingerprint"}),
            allowed_fields=frozenset({"logger_name", "message_fingerprint"}),
        ),
        EventName.INTERNAL_ERROR: EventDefinition(
            level=DiagnosticLevel.ERROR,
            result_code=ResultCode.FAILED,
            required_fields=frozenset(
                {
                    "error_code",
                    "error_category",
                    "is_retryable",
                    "exception_type",
                    "stack_fingerprint",
                }
            ),
            allowed_fields=frozenset(
                {
                    "error_code",
                    "error_category",
                    "is_retryable",
                    "exception_type",
                    "stack_fingerprint",
                }
            ),
        ),
    }
)


if set(EVENT_DEFINITIONS) != set(EventName):
    raise RuntimeError("event definition registry is incomplete")
