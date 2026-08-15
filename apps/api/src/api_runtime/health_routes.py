"""Liveness and readiness endpoints returning the strict API envelopes.

Liveness implies no I/O by construction: it never touches the readiness probe.
Readiness runs the complete canonical database probe exactly once inside the
two-second overall deadline owned here (kept out of the PostgreSQL adapter),
maps deadline expiry to ``database_connection_unavailable`` and lets the
probe's own registry error surface to the application error handler, which
selects its registered status. Both endpoints publish their closed
:class:`ApiRouteTemplate` on the ASGI scope before returning so the request
correlation middleware can classify the exchange without retaining raw paths.
"""

from __future__ import annotations

import asyncio
from typing import Final
from uuid import UUID

from fastapi import Request
from fastapi.responses import JSONResponse

from personal_os.api_contracts import (
    HTTP_ERROR_STATUSES,
    ApiRouteTemplate,
    CanonicalDatabaseReadinessProbe,
    LivenessData,
    ReadinessData,
    error_envelope,
    success_envelope,
)
from personal_os.diagnostics.context import current_diagnostic_context
from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import ApplicationError, DatabaseMigrationError

READINESS_DEADLINE_SECONDS: Final = 2.0

type HealthData = LivenessData | ReadinessData


def _bound_request_id() -> UUID:
    """Return the server request id owned by the request correlation middleware."""
    context = current_diagnostic_context()
    if context is None:
        raise RuntimeError("api handlers require a bound request correlation context")
    return context.request_id


def _success_json(request_id: UUID, data: HealthData) -> JSONResponse:
    """Render one health success envelope as the response body."""
    envelope = success_envelope(request_id=request_id, data=data)
    return JSONResponse(content=envelope.model_dump(mode="json"))


def _error_json(request_id: UUID, error: ApplicationError) -> JSONResponse:
    """Render one health error envelope with its closed-table status."""
    envelope = error_envelope(request_id=request_id, error=error)
    status_code = HTTP_ERROR_STATUSES[error.error_code]
    return JSONResponse(content=envelope.model_dump(mode="json"), status_code=status_code)


async def liveness(request: Request) -> JSONResponse:
    """Report process liveness: a constant payload, no dependency consulted."""
    request.scope["route_template"] = ApiRouteTemplate.HEALTH_LIVE
    return _success_json(_bound_request_id(), LivenessData())


async def readiness(
    request: Request,
    readiness_probe: CanonicalDatabaseReadinessProbe,
) -> JSONResponse:
    """Report canonical readiness: one probe call under the overall deadline.

    The response is built only after the deadline scope has fully exited. The
    probe is never retried: one call either succeeds, misses the deadline or
    raises its own safe registry error, which propagates to the registered
    application error handler for status selection.
    """
    request.scope["route_template"] = ApiRouteTemplate.HEALTH_READY
    request_id = _bound_request_id()
    try:
        async with asyncio.timeout(READINESS_DEADLINE_SECONDS):
            await readiness_probe.check()
    except TimeoutError:
        return _error_json(
            request_id,
            DatabaseMigrationError(ErrorCode.DATABASE_CONNECTION_UNAVAILABLE),
        )
    return _success_json(request_id, ReadinessData())
