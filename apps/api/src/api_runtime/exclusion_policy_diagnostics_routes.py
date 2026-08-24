"""Authenticated read-only exclusion-policy diagnostics endpoint.

The single endpoint of the policy observability Admin surface (spec
2026-08-24 C2) is created per composed runtime: the closure binds the
exclusion-policy runtime's metrics diagnostics source and the web session
dependencies, so the application factory only registers the semantic
operation id and the response model. The route resolves behind the strict
active-session origin gate exactly like the Admin device list — a plugin
device credential is never a Web authority — and renders one immutable
snapshot of the metrics sink: the closed evaluation counters by boundary and
decision (``failed`` included), the closed publication outcome counters, and
the bounded ring of the most recent policy system failures, each carrying
only the closed boundary label, the closed registry error code and the
epoch-millisecond timestamp. The response carries the canonical envelope and
``Cache-Control: no-store``; no path, locator, digest, operand, workspace,
revision or key identity or free-form string can appear in it.
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
from api_runtime.exclusion_policy_composition import ExclusionPolicyRuntime
from api_runtime.exclusion_policy_diagnostics_models import (
    ExclusionPolicyDiagnosticsData,
    PolicyEvaluationCounterData,
    PolicyFailureRecordData,
    PolicyPublicationCounterData,
)
from personal_os.api_contracts import ApiRouteTemplate, success_envelope
from personal_os.diagnostics.context import DiagnosticContext, current_diagnostic_context

#: Response headers every policy diagnostics response carries.
_NO_STORE_HEADERS: Final[dict[str, str]] = {"cache-control": "no-store"}


@dataclass(frozen=True, slots=True)
class PolicyDiagnosticsAdminRouteEndpoints:
    """The one endpoint callable of the policy diagnostics Admin route set."""

    get_policy_diagnostics: Callable[..., Awaitable[JSONResponse]]


def _bound_diagnostic_context() -> DiagnosticContext:
    """Return the diagnostic context owned by the request correlation middleware."""
    context = current_diagnostic_context()
    if context is None:
        raise RuntimeError(
            "exclusion policy diagnostics admin routes require a bound request correlation context"
        )
    return context


def create_policy_diagnostics_admin_route_endpoints(
    *,
    web_authentication: WebAuthenticationRuntime,
    exclusion_policy: ExclusionPolicyRuntime,
) -> PolicyDiagnosticsAdminRouteEndpoints:
    """Build the one policy diagnostics Admin endpoint over the composed runtime."""

    dependencies = create_session_route_dependencies(web_authentication)
    diagnostics_source = exclusion_policy.metrics_diagnostics

    async def get_policy_diagnostics(
        request: Request,
        authentication: AuthenticatedWebRequest = Depends(  # noqa: B008
            dependencies.require_session_request
        ),
    ) -> JSONResponse:
        """Serve the closed policy evidence snapshot (read-only)."""
        del authentication  # the session gate is the whole authority: no body, no selector
        request.scope["route_template"] = ApiRouteTemplate.ADMIN_EXCLUSION_POLICY_DIAGNOSTICS
        snapshot = diagnostics_source.policy_diagnostics()
        data = ExclusionPolicyDiagnosticsData(
            evaluation_counters=tuple(
                PolicyEvaluationCounterData(
                    boundary=boundary,
                    decision=decision,
                    count=count,
                )
                for (boundary, decision), count in sorted(
                    snapshot.evaluation_counters.items(),
                    key=lambda item: (item[0][0].value, item[0][1].value),
                )
            ),
            publication_counters=tuple(
                PolicyPublicationCounterData(outcome=outcome, count=count)
                for outcome, count in sorted(
                    snapshot.publication_counters.items(),
                    key=lambda item: item[0].value,
                )
            ),
            recent_failures=tuple(
                PolicyFailureRecordData(
                    boundary=record.boundary,
                    error_code=record.error_code,
                    at_epoch_ms=record.at_epoch_ms,
                )
                for record in snapshot.recent_failures
            ),
        )
        envelope = success_envelope(request_id=_bound_diagnostic_context().request_id, data=data)
        return JSONResponse(content=envelope.model_dump(mode="json"), headers=_NO_STORE_HEADERS)

    return PolicyDiagnosticsAdminRouteEndpoints(get_policy_diagnostics=get_policy_diagnostics)
