"""Typed object-storage errors and the closed input/busy reason token sets.

``ObjectStorageError`` binds the object-storage subsystem to the closed error
registry. Provider exception classes, response bodies, request IDs, headers and
messages remain chained only as internal causes and are never copied into the
typed error, its safe details or diagnostics; that contract is enforced by
:class:`personal_os.error_contracts.exceptions.ApplicationError`.

The reason tokens are closed ``SafeToken`` constants. The adapter supplies
``provider=SafeToken.parse("r2")`` and a registered operation token, never caller
text; ``object_storage_input_invalid`` and ``object_storage_busy`` are the only
codes that accept a ``reason`` detail, each only from its closed set above: the
input set for malformed or mismatched receive input, and the spool set that
distinguishes the three busy causes (free-space reserve, admission-window
expiry, permit/budget exhaustion).
"""

from __future__ import annotations

from personal_os.diagnostics.events import SafeToken
from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import ApplicationError

SIZE_OUT_OF_RANGE: SafeToken = SafeToken.parse("size_out_of_range")
SIZE_MISMATCH: SafeToken = SafeToken.parse("size_mismatch")
DIGEST_MISMATCH: SafeToken = SafeToken.parse("digest_mismatch")
MEDIA_TYPE_INVALID: SafeToken = SafeToken.parse("media_type_invalid")
STREAM_INVALID: SafeToken = SafeToken.parse("stream_invalid")
SPOOL_FREE_SPACE: SafeToken = SafeToken.parse("spool_free_space")
SPOOL_ADMISSION_WINDOW_EXPIRED: SafeToken = SafeToken.parse("spool_admission_window_expired")
SPOOL_PERMITS_EXHAUSTED: SafeToken = SafeToken.parse("spool_permits_exhausted")


class ObjectStorageError(ApplicationError):
    """Object-storage configuration, input, dependency, integrity and metadata failures.

    The closed code set covers configuration shape, input validation, transient
    capacity or availability, authorization, contract/integrity failures and
    metadata conflicts. Safe detail fields are registry-bound: configuration
    failures accept ``count`` and ``field_names``, invalid input and the busy
    capacity code each accept a single ``reason`` from their closed token sets,
    and every other code accepts no detail.
    """

    allowed_codes = frozenset(
        {
            ErrorCode.OBJECT_STORAGE_CONFIGURATION_INVALID,
            ErrorCode.OBJECT_STORAGE_INPUT_INVALID,
            ErrorCode.OBJECT_STORAGE_BUSY,
            ErrorCode.OBJECT_STORAGE_UNAVAILABLE,
            ErrorCode.OBJECT_STORAGE_ACCESS_DENIED,
            ErrorCode.OBJECT_STORAGE_CONTRACT_INVALID,
            ErrorCode.OBJECT_STORAGE_OBJECT_MISSING,
            ErrorCode.OBJECT_STORAGE_INTEGRITY_FAILED,
            ErrorCode.OBJECT_STORAGE_METADATA_CONFLICT,
        }
    )
