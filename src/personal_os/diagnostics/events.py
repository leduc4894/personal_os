"""Diagnostic event contracts: safe scalar values, enums and the closed event registry."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final, Protocol, cast
from uuid import UUID

_SAFE_TOKEN_PATTERN: Final = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,63}$")
_SHORT_DIGEST_PATTERN: Final = re.compile(r"^[0-9a-f]{16}$")
_OBJECT_DIGEST_PREFIX_PATTERN: Final = re.compile(r"^[0-9a-f]{12}$")


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


@dataclass(frozen=True, slots=True)
class ObjectDigestPrefix:
    """Lowercase hexadecimal object digest prefix of the registered fixed length.

    Accepts exactly 12 lowercase hexadecimal characters. It is a distinct contract
    from :class:`ShortDigest` (16 characters) and is the only registered shape for
    the ``object_digest_prefix`` log field; metrics never accept it.
    """

    value: str

    @classmethod
    def parse(cls, value: str) -> ObjectDigestPrefix:
        if _OBJECT_DIGEST_PREFIX_PATTERN.fullmatch(value) is None:
            raise ValueError("value does not satisfy the object digest prefix contract")
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
    OBJECT_STORAGE_OPERATION_SUCCEEDED = "object_storage_operation_succeeded"
    OBJECT_STORAGE_OPERATION_FAILED = "object_storage_operation_failed"
    OBJECT_STORAGE_OBJECT_DEDUPLICATED = "object_storage_object_deduplicated"
    OBJECT_STORAGE_INTEGRITY_FAILED = "object_storage_integrity_failed"
    OBJECT_STORAGE_SPOOL_CLEANUP_DEGRADED = "object_storage_spool_cleanup_degraded"
    SOURCE_VERSION_PUBLISH_SUCCEEDED = "source_version_publish_succeeded"
    SOURCE_VERSION_PUBLISH_REPLAYED = "source_version_publish_replayed"
    SOURCE_VERSION_PUBLISH_REJECTED = "source_version_publish_rejected"
    PROJECTION_INTENT_DISPATCHED = "projection_intent_dispatched"
    PROJECTION_INTENT_DISPATCH_FAILED = "projection_intent_dispatch_failed"
    PROJECTION_INTENT_LEASE_RECLAIMED = "projection_intent_lease_reclaimed"
    IDENTITY_BOOTSTRAP_SUCCEEDED = "identity_bootstrap_succeeded"
    IDENTITY_BOOTSTRAP_REPLAYED = "identity_bootstrap_replayed"
    IDENTITY_BOOTSTRAP_REJECTED = "identity_bootstrap_rejected"
    CANONICAL_SOURCE_READ_SUCCEEDED = "canonical_source_read_succeeded"
    CANONICAL_SOURCE_READ_FAILED = "canonical_source_read_failed"
    CANONICAL_BACKUP_CREATED = "canonical_backup_created"
    CANONICAL_BACKUP_VERIFIED = "canonical_backup_verified"
    CANONICAL_BACKUP_FAILED = "canonical_backup_failed"
    CANONICAL_RESTORE_SUCCEEDED = "canonical_restore_succeeded"
    CANONICAL_RESTORE_FAILED = "canonical_restore_failed"
    CANONICAL_ACCEPTANCE_COMPLETED = "canonical_acceptance_completed"
    CANONICAL_ACCEPTANCE_FAILED = "canonical_acceptance_failed"
    API_REQUEST_COMPLETED = "api_request_completed"
    API_REQUEST_REJECTED = "api_request_rejected"
    API_REQUEST_FAILED = "api_request_failed"
    EXCLUSION_POLICY_EVALUATION_COMPLETED = "exclusion_policy_evaluation_completed"
    EXCLUSION_POLICY_EVALUATION_REJECTED = "exclusion_policy_evaluation_rejected"


type SafeDiagnosticScalar = (
    bool | int | StrEnum | UUID | SafeToken | ShortDigest | ObjectDigestPrefix
)
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
        EventName.OBJECT_STORAGE_OPERATION_SUCCEEDED: EventDefinition(
            level=DiagnosticLevel.INFO,
            result_code=ResultCode.SUCCEEDED,
            required_fields=frozenset(
                {"operation", "duration_ms", "size_bytes", "attempt_count", "provider"}
            ),
            allowed_fields=frozenset(
                {"operation", "duration_ms", "size_bytes", "attempt_count", "provider"}
            ),
        ),
        EventName.OBJECT_STORAGE_OPERATION_FAILED: EventDefinition(
            level=DiagnosticLevel.ERROR,
            result_code=ResultCode.FAILED,
            required_fields=frozenset(
                {
                    "operation",
                    "duration_ms",
                    "attempt_count",
                    "provider",
                    "error_code",
                    "error_category",
                    "is_retryable",
                }
            ),
            allowed_fields=frozenset(
                {
                    "operation",
                    "duration_ms",
                    "attempt_count",
                    "provider",
                    "error_code",
                    "error_category",
                    "is_retryable",
                    "size_bytes",
                    "object_digest_prefix",
                }
            ),
        ),
        EventName.OBJECT_STORAGE_OBJECT_DEDUPLICATED: EventDefinition(
            level=DiagnosticLevel.INFO,
            result_code=ResultCode.SUCCEEDED,
            required_fields=frozenset(
                {"operation", "duration_ms", "size_bytes", "attempt_count", "provider"}
            ),
            allowed_fields=frozenset(
                {
                    "operation",
                    "duration_ms",
                    "size_bytes",
                    "attempt_count",
                    "provider",
                    "object_digest_prefix",
                }
            ),
        ),
        EventName.OBJECT_STORAGE_INTEGRITY_FAILED: EventDefinition(
            level=DiagnosticLevel.ERROR,
            result_code=ResultCode.FAILED,
            required_fields=frozenset(
                {
                    "operation",
                    "duration_ms",
                    "attempt_count",
                    "provider",
                    "error_code",
                    "error_category",
                    "is_retryable",
                }
            ),
            allowed_fields=frozenset(
                {
                    "operation",
                    "duration_ms",
                    "attempt_count",
                    "provider",
                    "error_code",
                    "error_category",
                    "is_retryable",
                    "size_bytes",
                    "object_digest_prefix",
                }
            ),
        ),
        EventName.OBJECT_STORAGE_SPOOL_CLEANUP_DEGRADED: EventDefinition(
            level=DiagnosticLevel.WARNING,
            result_code=ResultCode.DEGRADED,
            required_fields=frozenset({"operation", "count"}),
            allowed_fields=frozenset(
                {
                    "operation",
                    "count",
                    "error_code",
                    "error_category",
                    "is_retryable",
                }
            ),
        ),
        EventName.SOURCE_VERSION_PUBLISH_SUCCEEDED: EventDefinition(
            level=DiagnosticLevel.INFO,
            result_code=ResultCode.SUCCEEDED,
            required_fields=frozenset(
                {
                    "operation",
                    "outcome",
                    "duration_ms",
                    "attempt_count",
                    "content_version",
                    "source_id",
                    "source_version_id",
                    "event_id",
                }
            ),
            allowed_fields=frozenset(
                {
                    "operation",
                    "outcome",
                    "duration_ms",
                    "attempt_count",
                    "content_version",
                    "source_id",
                    "source_version_id",
                    "event_id",
                }
            ),
        ),
        EventName.SOURCE_VERSION_PUBLISH_REPLAYED: EventDefinition(
            level=DiagnosticLevel.INFO,
            result_code=ResultCode.SUCCEEDED,
            required_fields=frozenset(
                {
                    "operation",
                    "outcome",
                    "duration_ms",
                    "attempt_count",
                    "content_version",
                    "source_id",
                    "source_version_id",
                    "event_id",
                }
            ),
            allowed_fields=frozenset(
                {
                    "operation",
                    "outcome",
                    "duration_ms",
                    "attempt_count",
                    "content_version",
                    "source_id",
                    "source_version_id",
                    "event_id",
                }
            ),
        ),
        EventName.SOURCE_VERSION_PUBLISH_REJECTED: EventDefinition(
            level=DiagnosticLevel.WARNING,
            result_code=ResultCode.REJECTED,
            required_fields=frozenset(
                {
                    "operation",
                    "outcome",
                    "duration_ms",
                    "error_code",
                    "error_category",
                    "is_retryable",
                }
            ),
            allowed_fields=frozenset(
                {
                    "operation",
                    "outcome",
                    "duration_ms",
                    "error_code",
                    "error_category",
                    "is_retryable",
                    "source_id",
                    "event_id",
                    "reason_code",
                }
            ),
        ),
        EventName.PROJECTION_INTENT_DISPATCHED: EventDefinition(
            level=DiagnosticLevel.INFO,
            result_code=ResultCode.SUCCEEDED,
            required_fields=frozenset(
                {
                    "projection_kind",
                    "outcome",
                    "duration_ms",
                    "attempt_count",
                    "intent_id",
                }
            ),
            allowed_fields=frozenset(
                {
                    "projection_kind",
                    "outcome",
                    "duration_ms",
                    "attempt_count",
                    "intent_id",
                }
            ),
        ),
        EventName.PROJECTION_INTENT_DISPATCH_FAILED: EventDefinition(
            level=DiagnosticLevel.ERROR,
            result_code=ResultCode.FAILED,
            required_fields=frozenset(
                {
                    "projection_kind",
                    "outcome",
                    "duration_ms",
                    "attempt_count",
                    "intent_id",
                    "error_code",
                    "error_category",
                    "is_retryable",
                }
            ),
            allowed_fields=frozenset(
                {
                    "projection_kind",
                    "outcome",
                    "duration_ms",
                    "attempt_count",
                    "intent_id",
                    "error_code",
                    "error_category",
                    "is_retryable",
                }
            ),
        ),
        EventName.PROJECTION_INTENT_LEASE_RECLAIMED: EventDefinition(
            level=DiagnosticLevel.WARNING,
            result_code=ResultCode.DEGRADED,
            required_fields=frozenset({"projection_kind", "count"}),
            allowed_fields=frozenset({"projection_kind", "count", "attempt_count"}),
        ),
        EventName.IDENTITY_BOOTSTRAP_SUCCEEDED: EventDefinition(
            level=DiagnosticLevel.INFO,
            result_code=ResultCode.SUCCEEDED,
            required_fields=frozenset({"outcome", "workspace_id"}),
            allowed_fields=frozenset({"outcome", "user_id", "workspace_id", "device_id"}),
        ),
        EventName.IDENTITY_BOOTSTRAP_REPLAYED: EventDefinition(
            level=DiagnosticLevel.INFO,
            result_code=ResultCode.SUCCEEDED,
            required_fields=frozenset({"workspace_id"}),
            allowed_fields=frozenset({"user_id", "workspace_id", "device_id"}),
        ),
        EventName.IDENTITY_BOOTSTRAP_REJECTED: EventDefinition(
            level=DiagnosticLevel.WARNING,
            result_code=ResultCode.REJECTED,
            required_fields=frozenset({"error_code"}),
            allowed_fields=frozenset({"workspace_id", "error_code"}),
        ),
        EventName.CANONICAL_SOURCE_READ_SUCCEEDED: EventDefinition(
            level=DiagnosticLevel.INFO,
            result_code=ResultCode.SUCCEEDED,
            required_fields=frozenset({"source_id", "workspace_id", "source_version_id"}),
            allowed_fields=frozenset({"source_id", "workspace_id", "source_version_id"}),
        ),
        EventName.CANONICAL_SOURCE_READ_FAILED: EventDefinition(
            level=DiagnosticLevel.ERROR,
            result_code=ResultCode.FAILED,
            required_fields=frozenset({"error_code"}),
            allowed_fields=frozenset({"source_id", "workspace_id", "error_code"}),
        ),
        EventName.CANONICAL_BACKUP_CREATED: EventDefinition(
            level=DiagnosticLevel.INFO,
            result_code=ResultCode.SUCCEEDED,
            required_fields=frozenset({"operation", "outcome", "duration_ms", "bundle_id"}),
            allowed_fields=frozenset(
                {"operation", "outcome", "duration_ms", "bundle_id", "object_count", "byte_total"}
            ),
        ),
        EventName.CANONICAL_BACKUP_VERIFIED: EventDefinition(
            level=DiagnosticLevel.INFO,
            result_code=ResultCode.SUCCEEDED,
            required_fields=frozenset({"operation", "outcome", "duration_ms", "bundle_id"}),
            allowed_fields=frozenset(
                {"operation", "outcome", "duration_ms", "bundle_id", "object_count", "byte_total"}
            ),
        ),
        EventName.CANONICAL_BACKUP_FAILED: EventDefinition(
            level=DiagnosticLevel.ERROR,
            result_code=ResultCode.FAILED,
            required_fields=frozenset({"error_code"}),
            allowed_fields=frozenset(
                {"operation", "outcome", "duration_ms", "bundle_id", "error_code"}
            ),
        ),
        EventName.CANONICAL_RESTORE_SUCCEEDED: EventDefinition(
            level=DiagnosticLevel.INFO,
            result_code=ResultCode.SUCCEEDED,
            required_fields=frozenset({"operation", "outcome", "duration_ms", "bundle_id"}),
            allowed_fields=frozenset(
                {"operation", "outcome", "duration_ms", "bundle_id", "object_count", "byte_total"}
            ),
        ),
        EventName.CANONICAL_RESTORE_FAILED: EventDefinition(
            level=DiagnosticLevel.ERROR,
            result_code=ResultCode.FAILED,
            required_fields=frozenset({"error_code"}),
            allowed_fields=frozenset(
                {"operation", "outcome", "duration_ms", "bundle_id", "error_code"}
            ),
        ),
        EventName.CANONICAL_ACCEPTANCE_COMPLETED: EventDefinition(
            level=DiagnosticLevel.INFO,
            result_code=ResultCode.SUCCEEDED,
            required_fields=frozenset({"outcome", "duration_ms"}),
            allowed_fields=frozenset(
                {
                    "outcome",
                    "duration_ms",
                    "workspace_id",
                    "source_version_id",
                    "event_id",
                    "intent_count",
                }
            ),
        ),
        EventName.CANONICAL_ACCEPTANCE_FAILED: EventDefinition(
            level=DiagnosticLevel.ERROR,
            result_code=ResultCode.FAILED,
            required_fields=frozenset({"error_code"}),
            allowed_fields=frozenset(
                {
                    "error_code",
                    "error_category",
                    "is_retryable",
                    "operation",
                    "outcome",
                    "duration_ms",
                }
            ),
        ),
        EventName.API_REQUEST_COMPLETED: EventDefinition(
            level=DiagnosticLevel.INFO,
            result_code=ResultCode.SUCCEEDED,
            required_fields=frozenset({"http_method", "route", "status_code", "duration_ms"}),
            allowed_fields=frozenset({"http_method", "route", "status_code", "duration_ms"}),
        ),
        EventName.API_REQUEST_REJECTED: EventDefinition(
            level=DiagnosticLevel.WARNING,
            result_code=ResultCode.REJECTED,
            required_fields=frozenset({"http_method", "route", "status_code", "duration_ms"}),
            allowed_fields=frozenset({"http_method", "route", "status_code", "duration_ms"}),
        ),
        EventName.API_REQUEST_FAILED: EventDefinition(
            level=DiagnosticLevel.ERROR,
            result_code=ResultCode.FAILED,
            required_fields=frozenset({"http_method", "route", "status_code", "duration_ms"}),
            allowed_fields=frozenset({"http_method", "route", "status_code", "duration_ms"}),
        ),
        # Spec 21 evaluation events: per-source evaluations use metrics rather
        # than one audit row each, so these carry only the closed boundary and
        # decision labels plus counts — never a locator, operand, path or
        # subject fingerprint.
        EventName.EXCLUSION_POLICY_EVALUATION_COMPLETED: EventDefinition(
            level=DiagnosticLevel.INFO,
            result_code=ResultCode.SUCCEEDED,
            required_fields=frozenset({"boundary", "decision", "rule_count"}),
            allowed_fields=frozenset(
                {
                    "boundary",
                    "decision",
                    "rule_count",
                    "revision_number",
                    "duration_ms",
                    "matched_rule_count",
                    "missing_field_count",
                }
            ),
        ),
        EventName.EXCLUSION_POLICY_EVALUATION_REJECTED: EventDefinition(
            level=DiagnosticLevel.WARNING,
            result_code=ResultCode.REJECTED,
            required_fields=frozenset({"boundary", "error_code"}),
            allowed_fields=frozenset(
                {
                    "boundary",
                    "error_code",
                    "error_category",
                    "is_retryable",
                }
            ),
        ),
    }
)


if set(EVENT_DEFINITIONS) != set(EventName):
    raise RuntimeError("event definition registry is incomplete")

_REJECT_UNKNOWN_FIELD: Final = SafeToken.parse("unknown_field")
_REJECT_MISSING_FIELD: Final = SafeToken.parse("missing_field")
_REJECT_UNSAFE_VALUE: Final = SafeToken.parse("unsafe_value")


@dataclass(frozen=True, slots=True)
class DiagnosticEvent:
    """Accepted diagnostic event bound to its registry definition and frozen fields."""

    definition: EventDefinition
    fields: Mapping[str, SafeDiagnosticValue]


@dataclass(frozen=True, slots=True)
class RejectedDiagnosticPayload:
    """Constant rejection summary emitted when an untrusted payload fails validation.

    Only the rejection reason and offending count survive; the offending key,
    value, type representation or exception message is never retained.
    """

    reason: SafeToken
    count: int


class DiagnosticEventSink(Protocol):
    """Structural sink a composition root satisfies with its diagnostic logger.

    One narrow core protocol shared by the domain services that build and
    validate registered events: when the composition provides a sink, the
    service delivers the validated event; when it does not, the built payload
    is validated and discarded (build-only behavior). The validating
    ``DiagnosticLogger`` satisfies this protocol structurally.
    """

    def emit(self, event_name: EventName, fields: Mapping[str, object] | None = None) -> None: ...


def build_registered_event(
    event_name: EventName,
    fields: Mapping[str, object],
) -> DiagnosticEvent | RejectedDiagnosticPayload:
    """Validate an untrusted payload against the registered event contract.

    The caller field set must equal the union of the definition's required and
    allowed fields, and every value must conform to the closed safe-value union
    without forbidden keys, sensitive shapes or unbounded structure. The
    function never raises for untrusted diagnostic data, never retains the
    caller's mutable mapping and never copies offending data into the result.
    """
    from personal_os.diagnostics.redaction import _is_safe_diagnostic_value

    definition = EVENT_DEFINITIONS[event_name]
    allowed = definition.required_fields | definition.allowed_fields
    caller_keys = set(fields.keys())
    unknown = caller_keys - allowed
    if unknown:
        return RejectedDiagnosticPayload(reason=_REJECT_UNKNOWN_FIELD, count=len(unknown))
    missing = definition.required_fields - caller_keys
    if missing:
        return RejectedDiagnosticPayload(reason=_REJECT_MISSING_FIELD, count=len(missing))

    accepted: dict[str, SafeDiagnosticValue] = {}
    unsafe_count = 0
    for key, value in fields.items():
        if _is_safe_diagnostic_value(value):
            accepted[key] = cast(SafeDiagnosticValue, value)
        else:
            unsafe_count += 1
    if unsafe_count:
        return RejectedDiagnosticPayload(reason=_REJECT_UNSAFE_VALUE, count=unsafe_count)
    return DiagnosticEvent(definition=definition, fields=MappingProxyType(accepted))
