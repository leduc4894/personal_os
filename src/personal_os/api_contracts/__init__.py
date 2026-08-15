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
    ApiHttpMethod,
    ApiRouteTemplate,
)

__all__ = [
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
    "success_envelope",
]
