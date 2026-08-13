"""Public diagnostics API: events, context, redaction and structured logging."""

from personal_os.diagnostics.context import (
    DiagnosticContext,
    bind_diagnostic_context,
    create_diagnostic_context,
    current_diagnostic_context,
)
from personal_os.diagnostics.events import (
    EVENT_DEFINITIONS,
    DiagnosticEvent,
    DiagnosticLevel,
    EventDefinition,
    EventName,
    RejectedDiagnosticPayload,
    ResultCode,
    SafeDiagnosticValue,
    SafeToken,
    ShortDigest,
    build_registered_event,
)
from personal_os.diagnostics.logging import (
    DiagnosticLogger,
    RejectionCounterHook,
    configure_diagnostics,
    emit_emergency_application_error,
    emit_emergency_internal_error,
    reset_diagnostics_for_testing,
)
from personal_os.diagnostics.redaction import (
    fingerprint_stack,
    fingerprint_text,
    normalize_exception_type,
)

__all__ = [
    "EVENT_DEFINITIONS",
    "DiagnosticContext",
    "DiagnosticEvent",
    "DiagnosticLevel",
    "DiagnosticLogger",
    "EventDefinition",
    "EventName",
    "RejectedDiagnosticPayload",
    "RejectionCounterHook",
    "ResultCode",
    "SafeDiagnosticValue",
    "SafeToken",
    "ShortDigest",
    "bind_diagnostic_context",
    "build_registered_event",
    "configure_diagnostics",
    "create_diagnostic_context",
    "current_diagnostic_context",
    "emit_emergency_application_error",
    "emit_emergency_internal_error",
    "fingerprint_stack",
    "fingerprint_text",
    "normalize_exception_type",
    "reset_diagnostics_for_testing",
]
