"""Authenticated read-only sync rejection diagnostics endpoint.

The single endpoint of the sync observability Admin surface is created per
composed runtime: the closure binds the small-file sync runtime's rejection
diagnostics source and the web session dependencies, so the application
factory only registers the semantic operation id and the response model.
The route resolves behind the strict active-session origin gate exactly like
the Admin device list — a plugin device credential is never a Web authority
— and renders one immutable snapshot of the metrics sink: the closed
rejection counters and the bounded ring of the most recent rejection
records, each carrying only the closed error code, the epoch-millisecond
timestamp and the closed operation label standing in for the design's
route-template token. The response carries the canonical envelope and
``Cache-Control: no-store``; no path, locator, device id, digest or
free-form string can appear in it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final

from fastapi import Depends, Request
from fastapi.responses import JSONResponse

from api_runtime.authentication_composition import WebAuthenticationRuntime
from api_runtime.authentication_dependencies import (
    AuthenticatedWebRequest,
    create_session_route_dependencies,
)
from api_runtime.small_file_sync_composition import SmallFileSyncRuntime
from api_runtime.small_file_sync_diagnostics_models import (
    SmallFileRejectionCounterData,
    SmallFileRejectionDiagnosticsData,
    SmallFileRejectionRecordData,
)
from personal_os.api_contracts import ApiRouteTemplate, success_envelope
from personal_os.diagnostics.context import DiagnosticContext, current_diagnostic_context

#: Response headers every sync diagnostics response carries.
_NO_STORE_HEADERS: Final[dict[str, str]] = {"cache-control": "no-store"}


@dataclass(frozen=True, slots=True)
class SyncDiagnosticsAdminRouteEndpoints:
    """The one endpoint callable of the sync diagnostics Admin route set."""

    get_rejection_diagnostics: Callable[..., Awaitable[JSONResponse]]


def _bound_diagnostic_context() -> DiagnosticContext:
    """Return the diagnostic context owned by the request correlation middleware."""
    context = current_diagnostic_context()
    if context is None:
        raise RuntimeError(
            "sync diagnostics admin routes require a bound request correlation context"
        )
    return context


def create_sync_diagnostics_admin_route_endpoints(
    *,
    web_authentication: WebAuthenticationRuntime,
    small_file_sync: SmallFileSyncRuntime,
) -> SyncDiagnosticsAdminRouteEndpoints:
    """Build the one sync diagnostics Admin endpoint over the composed runtimes."""

    dependencies = create_session_route_dependencies(web_authentication)

    async def get_rejection_diagnostics(
        request: Request,
        authentication: AuthenticatedWebRequest = Depends(  # noqa: B008
            dependencies.require_session_request
        ),
    ) -> JSONResponse:
        """Serve the closed rejection evidence snapshot (read-only)."""
        del authentication  # the session gate is the whole authority: no body, no selector
        request.scope["route_template"] = ApiRouteTemplate.ADMIN_SYNC_REJECTIONS
        snapshot = small_file_sync.rejection_diagnostics.rejection_diagnostics()
        data = SmallFileRejectionDiagnosticsData(
            rejection_counters=tuple(
                SmallFileRejectionCounterData(
                    operation=operation,
                    error_code=reason_code,
                    count=count,
                )
                for (operation, reason_code), count in sorted(
                    snapshot.rejection_counters.items(),
                    key=lambda item: (item[0][0].value, item[0][1].value),
                )
            ),
            recent_rejections=tuple(
                SmallFileRejectionRecordData(
                    error_code=record.error_code,
                    at_epoch_ms=record.at_epoch_ms,
                    operation=record.operation,
                )
                for record in snapshot.recent_rejections
            ),
        )
        envelope = success_envelope(request_id=_bound_diagnostic_context().request_id, data=data)
        return JSONResponse(content=envelope.model_dump(mode="json"), headers=_NO_STORE_HEADERS)

    return SyncDiagnosticsAdminRouteEndpoints(get_rejection_diagnostics=get_rejection_diagnostics)
