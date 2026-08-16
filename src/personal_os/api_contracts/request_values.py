"""Closed HTTP method and route-template values used by safe access diagnostics.

These closed enum values are the only method and route scalars access
observations may carry; raw paths, query strings and headers never enter
diagnostics. Route values contain ``/`` but remain safe closed enum values.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final


class ApiHttpMethod(StrEnum):
    """Closed HTTP method vocabulary; ``OTHER`` buckets every non-GET method."""

    GET = "GET"
    OTHER = "OTHER"


class ApiRouteTemplate(StrEnum):
    """Closed route-template vocabulary; ``UNMATCHED`` marks unknown routes."""

    HEALTH_LIVE = "/api/health/live"
    HEALTH_READY = "/api/health/ready"
    AUTH_LOGIN = "/api/auth/login"
    AUTH_SESSION = "/api/auth/session"
    AUTH_LOGOUT = "/api/auth/logout"
    AUTH_REAUTHENTICATE = "/api/auth/reauthenticate"
    AUTH_PASSWORD = "/api/auth/password"
    AUTH_TOTP_VERIFY = "/api/auth/totp/verify"
    AUTH_TOTP_ENROLLMENTS = "/api/auth/totp/enrollments"
    AUTH_TOTP_ENROLLMENT_VERIFY = "/api/auth/totp/enrollments/{enrollment_id}/verify"
    AUTH_TOTP_RECOVERY = "/api/auth/totp/recovery"
    AUTH_TOTP_RECOVERY_CODES_REGENERATE = "/api/auth/totp/recovery-codes/regenerate"
    AUTH_TOTP_DISABLE = "/api/auth/totp"
    OPENAPI_DOCUMENT = "/api/openapi.json"
    UNMATCHED = "unmatched"


#: Every authentication-bound route template of the closed session/password
#: and TOTP/recovery route sets (spec 16.1, 16.2). Responses on these routes
#: carry the authentication cache-suppression and privacy posture, so the
#: error handlers need the same closed membership the correlation middleware
#: uses.
AUTHENTICATION_ROUTE_TEMPLATES: Final[frozenset[ApiRouteTemplate]] = frozenset(
    {
        ApiRouteTemplate.AUTH_LOGIN,
        ApiRouteTemplate.AUTH_SESSION,
        ApiRouteTemplate.AUTH_LOGOUT,
        ApiRouteTemplate.AUTH_REAUTHENTICATE,
        ApiRouteTemplate.AUTH_PASSWORD,
        ApiRouteTemplate.AUTH_TOTP_VERIFY,
        ApiRouteTemplate.AUTH_TOTP_ENROLLMENTS,
        ApiRouteTemplate.AUTH_TOTP_ENROLLMENT_VERIFY,
        ApiRouteTemplate.AUTH_TOTP_RECOVERY,
        ApiRouteTemplate.AUTH_TOTP_RECOVERY_CODES_REGENERATE,
        ApiRouteTemplate.AUTH_TOTP_DISABLE,
    }
)

#: Immutable view of the template values themselves, for classifying a raw
#: matched route path without retaining it.
AUTHENTICATION_ROUTE_TEMPLATE_VALUES: Final[frozenset[str]] = frozenset(
    template.value for template in AUTHENTICATION_ROUTE_TEMPLATES
)


def is_authentication_route_template(template: ApiRouteTemplate) -> bool:
    """Return whether one route template belongs to the authentication set."""
    return template in AUTHENTICATION_ROUTE_TEMPLATES
