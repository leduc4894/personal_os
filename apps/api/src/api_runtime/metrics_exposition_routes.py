"""Authenticated read-only Prometheus text exposition of the policy counters.

The single endpoint of the policy metrics sink surface (sink plan
2026-08-31) is created per composed runtime: the closure binds the
exclusion-policy runtime's metrics diagnostics source — the read side of
the one shared recorder the serve graph already binds at both composition
sites — and the web session dependencies, so the application factory only
registers the semantic operation id and the text response. The route
resolves behind the strict active-session origin gate exactly like the
sibling policy diagnostics route — a plugin device credential is never a
Web authority — and renders the immutable counter snapshot in the
Prometheus text exposition format (version 0.0.4): counters and closed
boundary/decision/outcome tokens only, never a path, locator, digest,
operand, workspace, revision or key identity or free-form string. The
response carries ``Cache-Control: no-store``. The sink renders only: a
failure to read or render the snapshot answers the typed retryable
``exclusion_policy_metrics_unavailable`` dependency error and never
blocks any evaluation path — the recorder keeps recording regardless.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final

from fastapi import Depends, Request
from fastapi.responses import Response

from api_runtime.authentication_composition import WebAuthenticationRuntime
from api_runtime.authentication_dependencies import (
    AuthenticatedWebRequest,
    create_session_route_dependencies,
)
from api_runtime.exclusion_policy_composition import ExclusionPolicyRuntime
from api_runtime.metrics_exposition import render_policy_diagnostics_prometheus
from personal_os.api_contracts import ApiRouteTemplate
from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import ApplicationError
from personal_os.exclusion_policy.metrics import ExclusionPolicyDiagnosticsSource

#: The exact media type of the served exposition and its document entry:
#: the Prometheus text format version 0.0.4 rendered as UTF-8 text.
PROMETHEUS_TEXT_CONTENT_TYPE: Final[str] = "text/plain; version=0.0.4; charset=utf-8"

#: Response headers every metrics exposition response carries.
_NO_STORE_HEADERS: Final[dict[str, str]] = {"cache-control": "no-store"}


@dataclass(frozen=True, slots=True)
class MetricsExpositionRouteEndpoints:
    """The one endpoint callable of the policy metrics exposition route set."""

    get_metrics_exposition: Callable[..., Awaitable[Response]]


def create_metrics_exposition_route_endpoints(
    *,
    web_authentication: WebAuthenticationRuntime,
    exclusion_policy: ExclusionPolicyRuntime,
) -> MetricsExpositionRouteEndpoints:
    """Build the one metrics exposition endpoint over the composed runtime."""

    dependencies = create_session_route_dependencies(web_authentication)
    diagnostics_source: ExclusionPolicyDiagnosticsSource = exclusion_policy.metrics_diagnostics

    async def get_metrics_exposition(
        request: Request,
        authentication: AuthenticatedWebRequest = Depends(  # noqa: B008
            dependencies.require_session_request
        ),
    ) -> Response:
        """Render the closed policy counters in Prometheus text format."""
        del authentication  # the session gate is the whole authority: no body, no selector
        request.scope["route_template"] = ApiRouteTemplate.ADMIN_POLICY_METRICS
        try:
            exposition = render_policy_diagnostics_prometheus(
                diagnostics_source.policy_diagnostics()
            )
        except ApplicationError:
            # A sink that already failed with its own typed registry error
            # keeps that exact reason token; only unexpected failures map.
            raise
        except Exception as error:
            raise ApplicationError(ErrorCode.EXCLUSION_POLICY_METRICS_UNAVAILABLE) from error
        return Response(
            content=exposition,
            # The wire contract carries the parameterized media type verbatim
            # (Starlette's ``media_type`` helper would append its own charset
            # to text/* types), so the header is set explicitly alongside the
            # no-store posture.
            headers={"content-type": PROMETHEUS_TEXT_CONTENT_TYPE, **_NO_STORE_HEADERS},
        )

    return MetricsExpositionRouteEndpoints(get_metrics_exposition=get_metrics_exposition)


__all__ = [
    "PROMETHEUS_TEXT_CONTENT_TYPE",
    "MetricsExpositionRouteEndpoints",
    "create_metrics_exposition_route_endpoints",
]
