"""Authenticated read-only source lifecycle diagnostics endpoint.

The single endpoint of the lifecycle observability Admin surface is created
per composed runtime: the closure binds the source lifecycle runtime's
diagnostics source and the web session dependencies, so the application
factory only registers the semantic operation id and the response model.
The route resolves behind the strict active-session origin gate exactly like
the Admin device list — a plugin device credential is never a Web authority
— and renders one immutable snapshot of the metrics sink: the closed commit
counters and the bounded ring of the most recent rejection records, each
carrying only the closed error code, the epoch-millisecond timestamp and
the closed operation label standing in for the design's route-template
token. The response carries the canonical envelope and
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
from api_runtime.source_lifecycle_composition import SourceLifecycleRuntime
from api_runtime.source_lifecycle_diagnostics_models import (
    SourceLifecycleCommitCounterData,
    SourceLifecycleDiagnosticsData,
    SourceLifecycleRejectionRecordData,
)
from personal_os.api_contracts import ApiRouteTemplate, success_envelope
from personal_os.diagnostics.context import DiagnosticContext, current_diagnostic_context

#: Response headers every lifecycle diagnostics response carries.
_NO_STORE_HEADERS: Final[dict[str, str]] = {"cache-control": "no-store"}


@dataclass(frozen=True, slots=True)
class SourceLifecycleDiagnosticsAdminRouteEndpoints:
    """The one endpoint callable of the lifecycle diagnostics Admin route set."""

    get_rejection_diagnostics: Callable[..., Awaitable[JSONResponse]]


def _bound_diagnostic_context() -> DiagnosticContext:
    """Return the diagnostic context owned by the request correlation middleware."""
    context = current_diagnostic_context()
    if context is None:
        raise RuntimeError(
            "source lifecycle diagnostics admin routes require a bound request correlation context"
        )
    return context


def create_source_lifecycle_diagnostics_admin_route_endpoints(
    *,
    web_authentication: WebAuthenticationRuntime,
    source_lifecycle: SourceLifecycleRuntime,
) -> SourceLifecycleDiagnosticsAdminRouteEndpoints:
    """Build the one lifecycle diagnostics Admin endpoint over the composed runtimes."""

    dependencies = create_session_route_dependencies(web_authentication)
    diagnostics_source = source_lifecycle.lifecycle_diagnostics

    async def get_rejection_diagnostics(
        request: Request,
        authentication: AuthenticatedWebRequest = Depends(  # noqa: B008
            dependencies.require_session_request
        ),
    ) -> JSONResponse:
        """Serve the closed lifecycle evidence snapshot (read-only)."""
        del authentication  # the session gate is the whole authority: no body, no selector
        request.scope["route_template"] = ApiRouteTemplate.ADMIN_SOURCE_LIFECYCLE_REJECTIONS
        snapshot = diagnostics_source.lifecycle_diagnostics()
        data = SourceLifecycleDiagnosticsData(
            commit_counters=tuple(
                SourceLifecycleCommitCounterData(
                    operation=operation,
                    outcome=outcome,
                    count=count,
                )
                for (operation, outcome), count in sorted(
                    snapshot.commit_counters.items(),
                    key=lambda item: (item[0][0].value, item[0][1].value),
                )
            ),
            recent_rejections=tuple(
                SourceLifecycleRejectionRecordData(
                    error_code=record.error_code,
                    at_epoch_ms=record.at_epoch_ms,
                    operation=record.operation,
                )
                for record in snapshot.recent_rejections
            ),
        )
        envelope = success_envelope(request_id=_bound_diagnostic_context().request_id, data=data)
        return JSONResponse(content=envelope.model_dump(mode="json"), headers=_NO_STORE_HEADERS)

    return SourceLifecycleDiagnosticsAdminRouteEndpoints(
        get_rejection_diagnostics=get_rejection_diagnostics
    )
