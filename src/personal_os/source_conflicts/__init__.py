"""Public source-conflict domain contracts (Child 8 spec 4).

Framework-neutral closed vocabularies, the immutable candidate and aggregate
read-model values, the capture and resolve commands with the frozen
resolution result, the typed error bound to the closed registry, the
low-cardinality metric contracts, the store, policy-guard and
evidence-reader ports, and the capture/resolution orchestration service
binding them. The package imports no FastAPI, SQLAlchemy, R2 or
request type.
"""

from personal_os.source_conflicts.commands import (
    CaptureConflictCommand,
    ConflictResolutionResult,
    ResolveConflictCommand,
)
from personal_os.source_conflicts.contracts import (
    TERMINAL_CONFLICT_STATUSES,
    VERSION_PUBLISHING_RESOLUTIONS,
    ConflictCandidate,
    ConflictCandidateKind,
    ConflictEvidenceRole,
    ConflictIdempotencyKey,
    ConflictKind,
    ConflictResolutionKind,
    ConflictResolutionOutcome,
    ConflictStatus,
    SourceConflict,
    validate_candidate_for_kind,
)
from personal_os.source_conflicts.errors import (
    CONFLICT_INPUT_INVALID_REASONS,
    SourceConflictError,
)
from personal_os.source_conflicts.metrics import (
    SOURCE_CONFLICT_METRIC_CONTRACTS,
    ConflictCaptureOutcome,
    ConflictResolutionMetricOutcome,
    InMemorySourceConflictMetrics,
    SourceConflictMetrics,
    SourceConflictOperation,
    SourceConflictRejectionReason,
)
from personal_os.source_conflicts.ports import (
    ConflictEvidenceReader,
    SourceConflictPolicyGuard,
    SourceConflictStore,
)
from personal_os.source_conflicts.service import SourceConflictService

__all__ = [
    "CONFLICT_INPUT_INVALID_REASONS",
    "SOURCE_CONFLICT_METRIC_CONTRACTS",
    "TERMINAL_CONFLICT_STATUSES",
    "VERSION_PUBLISHING_RESOLUTIONS",
    "CaptureConflictCommand",
    "ConflictCandidate",
    "ConflictCandidateKind",
    "ConflictCaptureOutcome",
    "ConflictEvidenceReader",
    "ConflictEvidenceRole",
    "ConflictIdempotencyKey",
    "ConflictKind",
    "ConflictResolutionKind",
    "ConflictResolutionMetricOutcome",
    "ConflictResolutionOutcome",
    "ConflictResolutionResult",
    "ConflictStatus",
    "InMemorySourceConflictMetrics",
    "ResolveConflictCommand",
    "SourceConflict",
    "SourceConflictError",
    "SourceConflictMetrics",
    "SourceConflictOperation",
    "SourceConflictPolicyGuard",
    "SourceConflictRejectionReason",
    "SourceConflictService",
    "SourceConflictStore",
    "validate_candidate_for_kind",
]
