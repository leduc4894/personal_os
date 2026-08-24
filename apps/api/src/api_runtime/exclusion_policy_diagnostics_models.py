"""Strict exclusion-policy diagnostics models of the Web Admin surface.

Every model here is frozen and closed for extra fields and projects only the
closed policy evidence of the exclusion-policy metrics sink: evaluation
counter rows keyed by the closed boundary and decision labels with their
counts (the closed ``failed`` decision included), publication counter rows
keyed by the closed outcome label, and the bounded ring of recent policy
system failures carrying exactly the closed boundary label, the closed
registry error code and the epoch-millisecond timestamp. No path, locator,
digest, operand, workspace, revision or key identity or free-form string can
enter any payload member.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from personal_os.error_contracts.codes import ErrorCode
from personal_os.exclusion_policy.metrics import (
    EvaluationMetricOutcome,
    PolicyBoundary,
    PublicationMetricOutcome,
)


class PolicyEvaluationCounterData(BaseModel):
    """One evaluation counter: closed boundary and decision labels plus count."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    boundary: PolicyBoundary
    decision: EvaluationMetricOutcome
    count: int = Field(ge=0)


class PolicyPublicationCounterData(BaseModel):
    """One publication counter: the closed outcome label plus its count."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: PublicationMetricOutcome
    count: int = Field(ge=0)


class PolicyFailureRecordData(BaseModel):
    """One recent policy system failure of the bounded diagnostics ring.

    The closed registry error code names the policy system failure, the
    closed boundary label stands in for the design's route-template token
    (the metrics layer sits below the correlation plumbing that owns route
    templates) and the timestamp is an epoch-millisecond integer.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    boundary: PolicyBoundary
    error_code: ErrorCode
    at_epoch_ms: int = Field(ge=0)


class ExclusionPolicyDiagnosticsData(BaseModel):
    """The policy evidence snapshot of the Admin diagnostics route."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    evaluation_counters: tuple[PolicyEvaluationCounterData, ...]
    publication_counters: tuple[PolicyPublicationCounterData, ...]
    recent_failures: tuple[PolicyFailureRecordData, ...]
