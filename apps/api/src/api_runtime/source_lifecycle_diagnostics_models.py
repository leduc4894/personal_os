"""Strict source lifecycle diagnostics models of the Web Admin surface.

Every model here is frozen and closed for extra fields and projects only the
closed evidence of the lifecycle metrics sink: commit counter rows keyed by
the closed operation and outcome labels with their counts, and the bounded
ring of recent rejection records carrying exactly the closed error code,
the epoch-millisecond timestamp and the closed operation label that stands
in for the design's route-template token. No path, locator, device id,
digest, token or free-form string can enter any payload member.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from personal_os.source_lifecycle.commands import LifecycleOperation
from personal_os.source_lifecycle.errors import SourceLifecycleErrorCode
from personal_os.source_lifecycle.metrics import LifecycleMetricOutcome


class SourceLifecycleRejectionRecordData(BaseModel):
    """One recent rejection of the bounded diagnostics ring.

    The closed error code mirrors the domain error registry, the timestamp is
    an epoch-millisecond integer and the closed operation label stands in for
    the design's route-template token: the metrics layer sits below the
    correlation plumbing that owns route templates, so the label localizes
    the rejecting operation without claiming route equivalence.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    error_code: SourceLifecycleErrorCode
    at_epoch_ms: int = Field(ge=0)
    operation: LifecycleOperation


class SourceLifecycleCommitCounterData(BaseModel):
    """One commit counter: closed labels plus its count."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: LifecycleOperation
    outcome: LifecycleMetricOutcome
    count: int = Field(ge=0)


class SourceLifecycleDiagnosticsData(BaseModel):
    """The lifecycle evidence snapshot of the Admin diagnostics route."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    commit_counters: tuple[SourceLifecycleCommitCounterData, ...]
    recent_rejections: tuple[SourceLifecycleRejectionRecordData, ...]
