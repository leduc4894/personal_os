"""Closed lifecycle domain error tokens, safe outside canonical state."""

from __future__ import annotations

from enum import StrEnum
from typing import Final

from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import ApplicationError


class SourceLifecycleErrorCode(StrEnum):
    """The complete externally safe lifecycle error vocabulary."""

    INPUT_INVALID = "source_lifecycle_input_invalid"
    LOCATOR_MISSING = "source_locator_missing"
    LOCATOR_CONFLICT = "source_locator_conflict"
    TOMBSTONE_NOT_FOUND = "source_tombstone_not_found"
    TOMBSTONE_CLOSED = "source_tombstone_closed"
    VERSION_CONFLICT = "source_lifecycle_version_conflict"
    COMMIT_OUTCOME_UNKNOWN = "source_lifecycle_commit_outcome_unknown"


class SourceLifecycleError(ValueError):
    """Domain failure carrying only a registered safe error code."""

    def __init__(self, code: SourceLifecycleErrorCode) -> None:
        self.code = code
        super().__init__(code.value)


#: The exact mapping from the closed domain vocabulary to the public error
#: registry. The keys never gain a member beyond ``SourceLifecycleErrorCode``;
#: the registry codes own the wire-stable spelling and the HTTP status table.
LIFECYCLE_ERROR_CODE_TO_REGISTRY: Final[dict[SourceLifecycleErrorCode, ErrorCode]] = {
    SourceLifecycleErrorCode.INPUT_INVALID: ErrorCode.SOURCE_LIFECYCLE_INPUT_INVALID,
    SourceLifecycleErrorCode.LOCATOR_MISSING: ErrorCode.SOURCE_LOCATOR_MISSING,
    SourceLifecycleErrorCode.LOCATOR_CONFLICT: ErrorCode.SOURCE_LOCATOR_CONFLICT,
    SourceLifecycleErrorCode.TOMBSTONE_NOT_FOUND: ErrorCode.SOURCE_TOMBSTONE_NOT_FOUND,
    SourceLifecycleErrorCode.TOMBSTONE_CLOSED: ErrorCode.SOURCE_TOMBSTONE_CLOSED,
    SourceLifecycleErrorCode.VERSION_CONFLICT: ErrorCode.SOURCE_LIFECYCLE_VERSION_CONFLICT,
    SourceLifecycleErrorCode.COMMIT_OUTCOME_UNKNOWN:
        ErrorCode.SOURCE_LIFECYCLE_COMMIT_OUTCOME_UNKNOWN,
}


class SourceLifecycleApplicationError(ApplicationError):
    """The API-bound lifecycle error carrying only the registered safe payload.

    Every lifecycle rejection at the route boundary translates a typed
    :class:`SourceLifecycleError` into the registry-bound
    :class:`ApplicationError` the canonical envelope consumes. The mapping is
    closed: only the seven ``source_lifecycle_*`` / ``source_locator_*`` /
    ``source_tombstone_*`` codes above are accepted; locator, title, identity
    and content stay out of the safe detail so the envelope never echoes a
    rejected value.
    """

    allowed_codes = frozenset(LIFECYCLE_ERROR_CODE_TO_REGISTRY.values())


def lifecycle_application_error_for(
    code: SourceLifecycleErrorCode,
) -> SourceLifecycleApplicationError:
    """Render the typed :class:`ApplicationError` for one closed domain code."""

    return SourceLifecycleApplicationError(LIFECYCLE_ERROR_CODE_TO_REGISTRY[code])
