"""Structured JSON diagnostics: canonical serialization, stream routing and fallbacks.

This module owns the only diagnostic write boundary. Application events are
validated by the closed event registry and serialized as one JSON object per
line; unmarked dependency log records are reduced to a fingerprinted
``dependency_log`` event. Any validation, rendering, hook or serialization
failure enters a non-recursive constant fallback that never calls ``logging``
again, so a diagnostics failure can never replace an application error or exit
code.
"""

from __future__ import annotations

import contextlib
import json
import logging
import sys
import threading
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Final, TextIO, cast
from uuid import UUID

from personal_os.diagnostics.context import (
    DiagnosticContext,
    current_diagnostic_context,
)
from personal_os.diagnostics.events import (
    EVENT_DEFINITIONS,
    DiagnosticLevel,
    EventName,
    RejectedDiagnosticPayload,
    ResultCode,
    SafeToken,
    ShortDigest,
    build_registered_event,
)
from personal_os.diagnostics.redaction import (
    fingerprint_stack,
    fingerprint_text,
    normalize_exception_type,
)
from personal_os.error_contracts.exceptions import ApplicationError
from personal_os.runtime_configuration.models import RuntimeSettings, ServiceName

type RejectionCounterHook = Callable[[EventName], None]

_MARKER: Final[str] = "_diagnostic_schema_record"
_OWNED_ATTR: Final[str] = "_diagnostics_owned"
_UNKNOWN_DEPENDENCY: Final[SafeToken] = SafeToken.parse("unknown.dependency")
_UNSAFE_PAYLOAD_REASON: Final[SafeToken] = SafeToken.parse("unsafe_payload")
_REJECTION_COUNT: Final[int] = 1
_SCHEMA_VERSION: Final[int] = 1
_FIXED_FALLBACK_TIMESTAMP: Final[str] = "1970-01-01T00:00:00.000Z"

_DIAGNOSTIC_TO_STDLIB_LEVEL: Final[Mapping[DiagnosticLevel, int]] = MappingProxyType(
    {
        DiagnosticLevel.DEBUG: logging.DEBUG,
        DiagnosticLevel.INFO: logging.INFO,
        DiagnosticLevel.WARNING: logging.WARNING,
        DiagnosticLevel.ERROR: logging.ERROR,
        DiagnosticLevel.CRITICAL: logging.CRITICAL,
    }
)

_STATIC_FALLBACK_LINE: Final[str] = (
    '{"diagnostic_schema_version":1,'
    '"timestamp":"1970-01-01T00:00:00.000Z",'
    '"level":"warning","service":"unknown","environment":null,'
    '"event":"logging_payload_rejected","result_code":"rejected",'
    '"request_id":null,"trace_id":null,'
    '"reason":"unsafe_payload","count":1}\n'
)
_MINIMAL_EMERGENCY_LINE: Final[str] = (
    '{"diagnostic_schema_version":1,'
    '"event":"logging_payload_rejected","result_code":"rejected",'
    '"reason":"serializer_failure"}\n'
)

# Module-private configuration state. The clock seam (``_current_timestamp``) is
# patched by tests; the remaining globals are owned by ``configure_diagnostics``.
_prior_root_handlers: list[logging.Handler] | None = None
_prior_root_level: int | None = None
_active_logger: DiagnosticLogger | None = None
_active_snapshot: Mapping[str, object] | None = None
_active_stdout: TextIO | None = None
_active_stderr: TextIO | None = None
_active_fallback_line: str = _STATIC_FALLBACK_LINE
_fallback_guard: threading.local = threading.local()


def _current_timestamp() -> datetime:
    """Return the current UTC moment (private test seam)."""
    return datetime.now(UTC)


def _format_rfc3339_millis(moment: datetime) -> str:
    """Format a UTC moment as RFC 3339 with millisecond precision and a literal ``Z``."""
    utc = moment.astimezone(UTC) if moment.tzinfo is not None else moment.replace(tzinfo=UTC)
    return utc.strftime("%Y-%m-%dT%H:%M:%S") + f".{utc.microsecond // 1000:03d}Z"


def _serialize(record: Mapping[str, object]) -> str:
    """Canonical single-line JSON encoding plus exactly one trailing newline."""
    return (
        json.dumps(
            record,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    )


def _to_json_value(value: object) -> object:
    """Reduce a validated safe diagnostic value to a plain JSON scalar or list."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, SafeToken | ShortDigest):
        return value.value
    if isinstance(value, tuple):
        return [_to_json_value(item) for item in value]
    return value


def _normalize_level(levelno: int) -> DiagnosticLevel:
    if levelno <= logging.DEBUG:
        return DiagnosticLevel.DEBUG
    if levelno <= logging.INFO:
        return DiagnosticLevel.INFO
    if levelno <= logging.WARNING:
        return DiagnosticLevel.WARNING
    if levelno <= logging.ERROR:
        return DiagnosticLevel.ERROR
    return DiagnosticLevel.CRITICAL


def _validate_dependency_logger_name(name: str) -> SafeToken:
    try:
        return SafeToken.parse(name)
    except ValueError, TypeError:
        return _UNKNOWN_DEPENDENCY


def _fallback_in_progress() -> bool:
    return bool(getattr(_fallback_guard, "active", False))


def _fallback_set(active: bool) -> None:
    _fallback_guard.active = active


def _is_owned(handler: logging.Handler) -> bool:
    return bool(getattr(handler, _OWNED_ATTR, False))


def _remove_owned_handlers(root: logging.Logger) -> None:
    owned = [handler for handler in root.handlers if _is_owned(handler)]
    for handler in owned:
        root.removeHandler(handler)
        handler.close()


def _build_base_record(snapshot: Mapping[str, object]) -> dict[str, object]:
    """Build the always-present schema fields from the snapshot and current context."""
    context = current_diagnostic_context()
    record: dict[str, object] = {
        "diagnostic_schema_version": _SCHEMA_VERSION,
        "timestamp": _format_rfc3339_millis(_current_timestamp()),
        "service": snapshot["service"],
        "environment": snapshot["environment"],
        "request_id": str(context.request_id) if context is not None else None,
        "trace_id": str(context.trace.trace_id) if context is not None else None,
    }
    if context is not None:
        if context.client_request_id is not None:
            record["client_request_id"] = str(context.client_request_id)
        if context.workflow_id is not None:
            record["workflow_id"] = context.workflow_id.value
    return record


def _build_fallback_line(snapshot: Mapping[str, object]) -> str:
    """Precompute a constant rejection line capturing the active service/environment."""
    try:
        return _serialize(
            {
                "diagnostic_schema_version": _SCHEMA_VERSION,
                "timestamp": _FIXED_FALLBACK_TIMESTAMP,
                "level": DiagnosticLevel.WARNING.value,
                "service": snapshot["service"],
                "environment": snapshot["environment"],
                "event": EventName.LOGGING_PAYLOAD_REJECTED.value,
                "result_code": ResultCode.REJECTED.value,
                "request_id": None,
                "trace_id": None,
                "reason": _UNSAFE_PAYLOAD_REASON.value,
                "count": _REJECTION_COUNT,
            }
        )
    except Exception:
        return _STATIC_FALLBACK_LINE


def _emit_fallback_line() -> None:
    """Non-recursive tier-1 fallback: write the precomputed rejection line directly."""
    if _fallback_in_progress():
        return
    _fallback_set(True)
    try:
        target = _active_stderr if _active_stderr is not None else sys.stderr
        target.write(_active_fallback_line)
        target.flush()
    except Exception:
        pass
    finally:
        _fallback_set(False)


class _MaxLevelFilter(logging.Filter):
    """Accept only records at or below a ceiling level."""

    def __init__(self, ceiling: int) -> None:
        super().__init__()
        self.ceiling = ceiling

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno <= self.ceiling


class _MinLevelFilter(logging.Filter):
    """Accept only records at or above a floor level."""

    def __init__(self, floor: int) -> None:
        super().__init__()
        self.floor = floor

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno >= self.floor


class _DiagnosticFormatter(logging.Formatter):
    """Serialize marked application records directly; reduce dependency records."""

    def __init__(self, snapshot: Mapping[str, object], fallback_line: str) -> None:
        super().__init__()
        self._snapshot = snapshot
        self._fallback_line = fallback_line

    def format(self, record: logging.LogRecord) -> str:
        try:
            schema = getattr(record, _MARKER, None)
            if schema is not None:
                return _serialize(cast("Mapping[str, object]", schema))
            return _serialize(self._build_dependency_record(record))
        except Exception:
            return self._fallback_line

    def _build_dependency_record(self, record: logging.LogRecord) -> dict[str, object]:
        level = _normalize_level(record.levelno)
        logger_name = _validate_dependency_logger_name(record.name)
        message = record.getMessage()
        fingerprint = fingerprint_text(message)
        base = _build_base_record(self._snapshot)
        base["level"] = level.value
        base["event"] = EventName.DEPENDENCY_LOG.value
        base["result_code"] = ResultCode.DEGRADED.value
        base["logger_name"] = logger_name.value
        base["message_fingerprint"] = fingerprint.value
        return base


class _DiagnosticStreamHandler(logging.StreamHandler[TextIO]):
    """Owned stream handler with a non-recursive emergency write fallback."""

    _diagnostics_owned: bool = False

    def __init__(self, stream: TextIO, level_filter: logging.Filter) -> None:
        super().__init__(stream)
        self.addFilter(level_filter)
        self.setLevel(logging.DEBUG)
        self._diagnostics_owned = True

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
            stream = self.stream
            stream.write(message)
            stream.flush()
        except Exception:
            self._emit_minimal_emergency()

    def _emit_minimal_emergency(self) -> None:
        if _fallback_in_progress():
            return
        _fallback_set(True)
        try:
            target = sys.stderr
            target.write(_MINIMAL_EMERGENCY_LINE)
            target.flush()
        except Exception:
            pass
        finally:
            _fallback_set(False)


class DiagnosticLogger:
    """Validating facade over stdlib logging that emits schema-v1 diagnostic lines."""

    def __init__(self, snapshot: Mapping[str, object]) -> None:
        self._snapshot = snapshot
        self._logger = logging.getLogger()
        self._hook: RejectionCounterHook | None = None

    def set_rejection_hook(self, hook: RejectionCounterHook | None) -> None:
        self._hook = hook

    def emit(
        self,
        event_name: EventName,
        fields: Mapping[str, object] | None = None,
    ) -> None:
        if _fallback_in_progress():
            return
        try:
            built = build_registered_event(event_name, fields or {})
            if isinstance(built, RejectedDiagnosticPayload):
                self._invoke_hook(event_name)
                self._emit_validated(
                    EventName.LOGGING_PAYLOAD_REJECTED,
                    {"reason": _UNSAFE_PAYLOAD_REASON, "count": _REJECTION_COUNT},
                )
                return
            self._emit_validated(event_name, built.fields)
        except Exception:
            _emit_fallback_line()

    def _invoke_hook(self, event_name: EventName) -> None:
        hook = self._hook
        if hook is None:
            return
        with contextlib.suppress(Exception):
            hook(event_name)

    def _emit_validated(self, event_name: EventName, fields: Mapping[str, object]) -> None:
        definition = EVENT_DEFINITIONS[event_name]
        level = definition.level or DiagnosticLevel.INFO
        record = _build_base_record(self._snapshot)
        record["level"] = level.value
        record["event"] = event_name.value
        record["result_code"] = definition.result_code.value
        for key, value in fields.items():
            record[key] = _to_json_value(value)
        self._logger.log(_DIAGNOSTIC_TO_STDLIB_LEVEL[level], "", extra={_MARKER: record})

    def emit_application_error(self, error: ApplicationError) -> None:
        fields: dict[str, object] = {
            "error_code": error.error_code,
            "error_category": error.category,
            "is_retryable": error.is_retryable,
        }
        details = error.safe_details
        if "reason" in details:
            fields["reason"] = details["reason"]
        if "count" in details:
            fields["count"] = details["count"]
        self.emit(EventName.RUNTIME_CONFIGURATION_FAILED, fields)

    def emit_internal_error(self, exception: BaseException) -> None:
        self.emit(
            EventName.INTERNAL_ERROR,
            {
                "error_code": SafeToken.parse("internal_error"),
                "error_category": SafeToken.parse("internal"),
                "is_retryable": False,
                "exception_type": normalize_exception_type(exception),
                "stack_fingerprint": fingerprint_stack(exception),
            },
        )


def configure_diagnostics(
    settings: RuntimeSettings,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    rejection_counter_hook: RejectionCounterHook | None = None,
) -> DiagnosticLogger:
    """Install the diagnostic root handlers and return the validating logger."""
    global _prior_root_handlers, _prior_root_level, _active_logger, _active_snapshot
    global _active_stdout, _active_stderr, _active_fallback_line

    snapshot: Mapping[str, object] = MappingProxyType(
        {
            "service": settings.service_name.value,
            "environment": settings.environment.value,
        }
    )
    out_stream = stdout if stdout is not None else sys.stdout
    err_stream = stderr if stderr is not None else sys.stderr
    fallback_line = _build_fallback_line(snapshot)
    formatter = _DiagnosticFormatter(snapshot, fallback_line)

    stdout_handler = _DiagnosticStreamHandler(out_stream, _MaxLevelFilter(logging.WARNING))
    stderr_handler = _DiagnosticStreamHandler(err_stream, _MinLevelFilter(logging.ERROR))
    for handler in (stdout_handler, stderr_handler):
        handler.setFormatter(formatter)

    root = logging.getLogger()
    if _prior_root_handlers is None:
        _prior_root_handlers = list(root.handlers)
        _prior_root_level = root.level
        root.handlers.clear()
    else:
        _remove_owned_handlers(root)
    root.addHandler(stdout_handler)
    root.addHandler(stderr_handler)
    root.setLevel(logging.DEBUG)

    logger = DiagnosticLogger(snapshot)
    logger.set_rejection_hook(rejection_counter_hook)
    _active_logger = logger
    _active_snapshot = snapshot
    _active_stdout = out_stream
    _active_stderr = err_stream
    _active_fallback_line = fallback_line
    return logger


def reset_diagnostics_for_testing() -> None:
    """Remove owned handlers and restore the prior root logger state."""
    global _prior_root_handlers, _prior_root_level, _active_logger, _active_snapshot
    global _active_stdout, _active_stderr, _active_fallback_line

    root = logging.getLogger()
    if _prior_root_handlers is not None:
        _remove_owned_handlers(root)
        root.handlers.extend(_prior_root_handlers)
        if _prior_root_level is not None:
            root.setLevel(_prior_root_level)
    _prior_root_handlers = None
    _prior_root_level = None
    _active_logger = None
    _active_snapshot = None
    _active_stdout = None
    _active_stderr = None
    _active_fallback_line = _STATIC_FALLBACK_LINE


def _correlation_from_context(context: DiagnosticContext, record: dict[str, object]) -> None:
    record["request_id"] = str(context.request_id)
    record["trace_id"] = str(context.trace.trace_id)
    if context.client_request_id is not None:
        record["client_request_id"] = str(context.client_request_id)
    if context.workflow_id is not None:
        record["workflow_id"] = context.workflow_id.value


def emit_emergency_application_error(
    service: ServiceName,
    context: DiagnosticContext,
    error: ApplicationError,
    *,
    stderr: TextIO | None = None,
) -> None:
    """Construct the configuration-failure schema directly and write it once."""
    try:
        record: dict[str, object] = {
            "diagnostic_schema_version": _SCHEMA_VERSION,
            "timestamp": _format_rfc3339_millis(_current_timestamp()),
            "service": service.value,
            "environment": None,
            "level": DiagnosticLevel.ERROR.value,
            "event": EventName.RUNTIME_CONFIGURATION_FAILED.value,
            "result_code": ResultCode.FAILED.value,
        }
        _correlation_from_context(context, record)
        record["error_code"] = error.error_code.value
        record["error_category"] = error.category.value
        record["is_retryable"] = error.is_retryable
        details = error.safe_details
        if "reason" in details:
            record["reason"] = _to_json_value(details["reason"])
        if "count" in details:
            record["count"] = _to_json_value(details["count"])
        (stderr if stderr is not None else sys.stderr).write(_serialize(record))
    except Exception:
        return


def emit_emergency_internal_error(
    service: ServiceName,
    context: DiagnosticContext,
    exception: BaseException,
    *,
    stderr: TextIO | None = None,
) -> None:
    """Construct the internal-error schema directly and write it once."""
    environment = _active_snapshot["environment"] if _active_snapshot is not None else None
    try:
        record: dict[str, object] = {
            "diagnostic_schema_version": _SCHEMA_VERSION,
            "timestamp": _format_rfc3339_millis(_current_timestamp()),
            "service": service.value,
            "environment": environment,
            "level": DiagnosticLevel.ERROR.value,
            "event": EventName.INTERNAL_ERROR.value,
            "result_code": ResultCode.FAILED.value,
        }
        _correlation_from_context(context, record)
        record["error_code"] = "internal_error"
        record["error_category"] = "internal"
        record["is_retryable"] = False
        record["exception_type"] = normalize_exception_type(exception).value
        record["stack_fingerprint"] = fingerprint_stack(exception).value
        (stderr if stderr is not None else sys.stderr).write(_serialize(record))
    except Exception:
        return
