"""Typed device sync errors bound to the closed central error registry.

``DeviceSyncErrorCode`` is the closed domain failure vocabulary of the device
cursor and manifest reconciliation design (spec section 13). Each member maps
one-to-one through :data:`CENTRAL_ERROR_CODE_BY_DEVICE_CODE` onto a central
:class:`~personal_os.error_contracts.codes.ErrorCode` registry definition, so
``DeviceSyncError`` inherits the registry's category, retryability and safe
message while exposing the domain ``code`` for closed metric and diagnostic
reason labels. Locators, digests, credentials, object keys, provider details
and exception text never enter the typed error.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import ApplicationError


class DeviceSyncErrorCode(StrEnum):
    """The closed device sync failure tokens (spec section 13)."""

    CURSOR_GAP = "device_cursor_gap"
    CURSOR_REGRESSION = "device_cursor_regression"
    CURSOR_ACK_AHEAD = "device_cursor_ack_ahead"
    EVENT_UNAVAILABLE = "device_event_unavailable"
    EVENT_INTEGRITY_FAILED = "device_event_integrity_failed"
    MANIFEST_NOT_FOUND = "device_manifest_not_found"
    MANIFEST_EXPIRED = "device_manifest_expired"
    MANIFEST_STATE_INVALID = "device_manifest_state_invalid"
    MANIFEST_PAGE_INVALID = "device_manifest_page_invalid"
    MANIFEST_PAGE_REPLAY_MISMATCH = "device_manifest_page_replay_mismatch"
    MANIFEST_DIGEST_MISMATCH = "device_manifest_digest_mismatch"
    MANIFEST_POLICY_ADVANCED = "device_manifest_policy_advanced"
    DOWNLOAD_INTEGRITY_FAILED = "device_download_integrity_failed"
    DEPENDENCY_UNAVAILABLE = "device_sync_dependency_unavailable"


#: The one mapping from every domain code onto its central registry code.
#: Values match verbatim so a diagnostic reason token renders identically on
#: both sides of the boundary.
CENTRAL_ERROR_CODE_BY_DEVICE_CODE: Final[Mapping[DeviceSyncErrorCode, ErrorCode]] = (
    MappingProxyType(
        {
            DeviceSyncErrorCode.CURSOR_GAP: ErrorCode.DEVICE_CURSOR_GAP,
            DeviceSyncErrorCode.CURSOR_REGRESSION: ErrorCode.DEVICE_CURSOR_REGRESSION,
            DeviceSyncErrorCode.CURSOR_ACK_AHEAD: ErrorCode.DEVICE_CURSOR_ACK_AHEAD,
            DeviceSyncErrorCode.EVENT_UNAVAILABLE: ErrorCode.DEVICE_EVENT_UNAVAILABLE,
            DeviceSyncErrorCode.EVENT_INTEGRITY_FAILED: ErrorCode.DEVICE_EVENT_INTEGRITY_FAILED,
            DeviceSyncErrorCode.MANIFEST_NOT_FOUND: ErrorCode.DEVICE_MANIFEST_NOT_FOUND,
            DeviceSyncErrorCode.MANIFEST_EXPIRED: ErrorCode.DEVICE_MANIFEST_EXPIRED,
            DeviceSyncErrorCode.MANIFEST_STATE_INVALID: ErrorCode.DEVICE_MANIFEST_STATE_INVALID,
            DeviceSyncErrorCode.MANIFEST_PAGE_INVALID: ErrorCode.DEVICE_MANIFEST_PAGE_INVALID,
            DeviceSyncErrorCode.MANIFEST_PAGE_REPLAY_MISMATCH: (
                ErrorCode.DEVICE_MANIFEST_PAGE_REPLAY_MISMATCH
            ),
            DeviceSyncErrorCode.MANIFEST_DIGEST_MISMATCH: ErrorCode.DEVICE_MANIFEST_DIGEST_MISMATCH,
            DeviceSyncErrorCode.MANIFEST_POLICY_ADVANCED: ErrorCode.DEVICE_MANIFEST_POLICY_ADVANCED,
            DeviceSyncErrorCode.DOWNLOAD_INTEGRITY_FAILED: (
                ErrorCode.DEVICE_DOWNLOAD_INTEGRITY_FAILED
            ),
            DeviceSyncErrorCode.DEPENDENCY_UNAVAILABLE: (
                ErrorCode.DEVICE_SYNC_DEPENDENCY_UNAVAILABLE
            ),
        }
    )
)


class DeviceSyncError(ApplicationError):
    """Device cursor, event, manifest and download failures.

    Every code is terminal for the triggering request except the retryable
    dependency outage; none of them accepts a safe detail field, because the
    readable reason travels through the structured diagnostic events and the
    plugin trail instead of the error envelope.
    """

    allowed_codes = frozenset(CENTRAL_ERROR_CODE_BY_DEVICE_CODE.values())

    def __init__(self, code: DeviceSyncErrorCode) -> None:
        # The isinstance fence keeps the boundary closed at runtime even
        # though a foreign StrEnum member with a matching value would
        # otherwise hash and compare equal to a domain key.
        if not isinstance(code, DeviceSyncErrorCode):
            raise ValueError("error code is not valid for this exception type")
        super().__init__(CENTRAL_ERROR_CODE_BY_DEVICE_CODE[code])
        self.code: DeviceSyncErrorCode = code


__all__ = [
    "CENTRAL_ERROR_CODE_BY_DEVICE_CODE",
    "DeviceSyncError",
    "DeviceSyncErrorCode",
]
