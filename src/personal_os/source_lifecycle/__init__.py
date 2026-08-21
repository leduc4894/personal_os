"""Framework-neutral source rename, move, delete and restore contracts."""

from personal_os.source_lifecycle.commands import (
    LifecycleOperation,
    LifecycleState,
    SourceLifecycleCommand,
    SourceLifecycleCommitResult,
)
from personal_os.source_lifecycle.errors import SourceLifecycleError, SourceLifecycleErrorCode
from personal_os.source_lifecycle.fingerprint import (
    LifecycleRequestFingerprint,
    fingerprint_lifecycle_command,
)
from personal_os.source_lifecycle.metrics import (
    SOURCE_LIFECYCLE_METRIC_CONTRACTS,
    LifecycleMetricOutcome,
    SourceLifecycleMetrics,
)
from personal_os.source_lifecycle.ports import (
    LifecycleCommitResult,
    LifecycleDeviceContext,
    LifecyclePolicyDecision,
    LifecyclePolicyOutcome,
    SourceLifecyclePolicy,
    SourceLifecycleStore,
)
from personal_os.source_lifecycle.service import SourceLifecycleService
from personal_os.source_lifecycle.title import derive_title_v1

__all__ = [
    "SOURCE_LIFECYCLE_METRIC_CONTRACTS",
    "LifecycleCommitResult",
    "LifecycleDeviceContext",
    "LifecycleMetricOutcome",
    "LifecycleOperation",
    "LifecyclePolicyDecision",
    "LifecyclePolicyOutcome",
    "LifecycleRequestFingerprint",
    "LifecycleState",
    "SourceLifecycleCommand",
    "SourceLifecycleCommitResult",
    "SourceLifecycleError",
    "SourceLifecycleErrorCode",
    "SourceLifecycleMetrics",
    "SourceLifecyclePolicy",
    "SourceLifecycleService",
    "SourceLifecycleStore",
    "derive_title_v1",
    "fingerprint_lifecycle_command",
]
