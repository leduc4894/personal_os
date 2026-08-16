"""Public API-contract exports: envelopes, error vocabulary, health and route values.

These contracts are framework-neutral: modules import Pydantic and existing
core contracts only, never FastAPI, Uvicorn, SQLAlchemy, Psycopg or a provider
package. The FastAPI composition root under ``api_runtime`` consumes them.
"""

from __future__ import annotations

from personal_os.api_contracts.envelopes import (
    ApiDetailValue,
    ApiEnvelope,
    ApiErrorBody,
    ApiWarning,
    error_envelope,
    success_envelope,
)
from personal_os.api_contracts.errors import (
    HTTP_ERROR_STATUSES,
    ApiTransportError,
)
from personal_os.api_contracts.health import (
    CanonicalDatabaseReadinessProbe,
    LivenessData,
    ReadinessChecks,
    ReadinessData,
)
from personal_os.api_contracts.request_values import (
    AUTHENTICATION_ROUTE_TEMPLATE_VALUES,
    AUTHENTICATION_ROUTE_TEMPLATES,
    ApiHttpMethod,
    ApiRouteTemplate,
    is_authentication_route_template,
)

__all__ = [
    "AUTHENTICATION_ROUTE_TEMPLATES",
    "AUTHENTICATION_ROUTE_TEMPLATE_VALUES",
    "HTTP_ERROR_STATUSES",
    "ApiDetailValue",
    "ApiEnvelope",
    "ApiErrorBody",
    "ApiHttpMethod",
    "ApiRouteTemplate",
    "ApiTransportError",
    "ApiWarning",
    "CanonicalDatabaseReadinessProbe",
    "LivenessData",
    "ReadinessChecks",
    "ReadinessData",
    "error_envelope",
    "is_authentication_route_template",
    "success_envelope",
]
