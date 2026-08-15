"""API transport error vocabulary and the closed HTTP status table.

HTTP status is selected only by this closed per-code table, never inferred
from an error category, an exception type or a provider response.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import (
    ApiTransportError as ApiTransportError,
)

_APPROVED_HTTP_STATUS_CODES: Final[frozenset[ErrorCode]] = frozenset(
    {
        ErrorCode.API_REQUEST_MALFORMED,
        ErrorCode.API_REQUEST_VALIDATION_FAILED,
        ErrorCode.API_ROUTE_NOT_FOUND,
        ErrorCode.API_METHOD_NOT_ALLOWED,
        ErrorCode.DATABASE_CONNECTION_UNAVAILABLE,
        ErrorCode.DATABASE_SCHEMA_CONTRACT_INVALID,
        ErrorCode.INTERNAL_ERROR,
    }
)


def _build_closed_http_status_map(
    status_by_code: Mapping[ErrorCode, int],
) -> dict[ErrorCode, int]:
    """Validate one status-table draft against the approved seven-code set.

    Rejects drafts that map a code outside the approved table and drafts that
    miss an approved code, so the public map is closed by construction.
    """
    unlisted_codes = set(status_by_code) - _APPROVED_HTTP_STATUS_CODES
    if unlisted_codes:
        raise ValueError("http status mapping contains codes outside the approved table")
    missing_codes = _APPROVED_HTTP_STATUS_CODES - set(status_by_code)
    if missing_codes:
        raise ValueError("http status mapping misses approved codes")
    return dict(status_by_code)


HTTP_ERROR_STATUSES: Final[Mapping[ErrorCode, int]] = MappingProxyType(
    _build_closed_http_status_map(
        {
            ErrorCode.API_REQUEST_MALFORMED: 400,
            ErrorCode.API_REQUEST_VALIDATION_FAILED: 422,
            ErrorCode.API_ROUTE_NOT_FOUND: 404,
            ErrorCode.API_METHOD_NOT_ALLOWED: 405,
            ErrorCode.DATABASE_CONNECTION_UNAVAILABLE: 503,
            ErrorCode.DATABASE_SCHEMA_CONTRACT_INVALID: 503,
            ErrorCode.INTERNAL_ERROR: 500,
        }
    )
)
