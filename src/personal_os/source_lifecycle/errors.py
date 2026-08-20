"""Closed lifecycle domain error tokens, safe outside canonical state."""

from __future__ import annotations

from enum import StrEnum


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
