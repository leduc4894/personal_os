"""Composition-boundary orchestration for the ``check-runtime`` command.

Performs one linear sequence -- create and bind a diagnostic context, load the
validated runtime settings, configure structured diagnostics, then emit a single
``runtime_configuration_validated`` line -- and maps every failure to exactly one
safe emergency record plus a stable exit code. Application (configuration) errors
before a settings snapshot exists exit ``78``; any other unexpected exception at
that stage exits ``70``. Failures once settings exist (logger configuration or
emission) reuse the emergency internal serializer with the validated environment
and exit ``70``. ``KeyboardInterrupt``, ``SystemExit`` and ``GeneratorExit`` are
never caught.
"""

from __future__ import annotations

from personal_os.diagnostics.context import (
    bind_diagnostic_context,
    create_diagnostic_context,
)
from personal_os.diagnostics.events import EventName
from personal_os.diagnostics.logging import (
    configure_diagnostics,
    emit_emergency_application_error,
    emit_emergency_internal_error,
)
from personal_os.error_contracts.exceptions import ApplicationError
from personal_os.runtime_configuration.loading import load_runtime_settings
from personal_os.runtime_configuration.models import ServiceName

__all__ = ["run_runtime_check"]


def run_runtime_check(service: ServiceName) -> int:
    """Run the linear runtime-configuration check for one composition root."""
    resolution = create_diagnostic_context()
    context = resolution.context
    with bind_diagnostic_context(context):
        try:
            settings = load_runtime_settings(service_name=service)
        except ApplicationError as error:
            emit_emergency_application_error(service, context, error)
            return 78
        except Exception as error:
            emit_emergency_internal_error(service, context, error)
            return 70

        try:
            logger = configure_diagnostics(settings)
            logger.emit(
                EventName.RUNTIME_CONFIGURATION_VALIDATED,
                {"configured_log_level": settings.log_level},
            )
        except Exception as error:
            emit_emergency_internal_error(service, context, error)
            return 70

    return 0
