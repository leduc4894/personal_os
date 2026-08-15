"""Closed HTTP method and route-template values used by safe access diagnostics.

These closed enum values are the only method and route scalars access
observations may carry; raw paths, query strings and headers never enter
diagnostics. Route values contain ``/`` but remain safe closed enum values.
"""

from __future__ import annotations

from enum import StrEnum


class ApiHttpMethod(StrEnum):
    """Closed HTTP method vocabulary; ``OTHER`` buckets every non-GET method."""

    GET = "GET"
    OTHER = "OTHER"


class ApiRouteTemplate(StrEnum):
    """Closed route-template vocabulary; ``UNMATCHED`` marks unknown routes."""

    HEALTH_LIVE = "/api/health/live"
    HEALTH_READY = "/api/health/ready"
    OPENAPI_DOCUMENT = "/api/openapi.json"
    UNMATCHED = "unmatched"
