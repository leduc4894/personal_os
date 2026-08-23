"""Strict sync rejection diagnostics models of the Web Admin surface.

Every model here is frozen and closed for extra fields and projects only the
closed rejection evidence of the small-file sync metrics sink: counter rows
keyed by the closed operation label and reason code with their counts, and
the bounded ring of recent rejection records carrying exactly the closed
error code, the epoch-millisecond timestamp and the closed operation label
that stands in for the design's route-template token. No path, locator,
device id, digest, token or free-form string can enter any payload member.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from personal_os.small_file_sync.contracts import SmallFileOperation
from personal_os.small_file_sync.metrics import SmallFileRejectionReason


class SmallFileRejectionRecordData(BaseModel):
    """One recent rejection of the bounded diagnostics ring.

    The closed error code mirrors the domain error registry, the timestamp is
    an epoch-millisecond integer and the closed operation label stands in for
    the design's route-template token: the metrics layer sits below the
    correlation plumbing that owns route templates, so the label localizes
    the rejecting operation without claiming route equivalence.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    error_code: SmallFileRejectionReason
    at_epoch_ms: int = Field(ge=0)
    operation: SmallFileOperation


class SmallFileRejectionCounterData(BaseModel):
    """One rejection counter: closed labels plus its count."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: SmallFileOperation
    error_code: SmallFileRejectionReason
    count: int = Field(ge=0)


class SmallFileRejectionDiagnosticsData(BaseModel):
    """The rejection evidence snapshot of the Admin diagnostics route."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rejection_counters: tuple[SmallFileRejectionCounterData, ...]
    recent_rejections: tuple[SmallFileRejectionRecordData, ...]
