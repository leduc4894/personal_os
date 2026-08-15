"""Public identity-bootstrap domain contracts.

Immutable bootstrap command/result types, the provider-neutral validation
grammar, the typed identity error with its closed reason tokens, the metrics
sink seam and the provider-neutral bootstrap store port. The modules import
no infrastructure SDK, composition root or provider package.
"""

from personal_os.identity.bootstrap import (
    ExistingIdentityDevice,
    ExistingIdentityState,
    ExistingIdentityUser,
    ExistingIdentityWorkspace,
    IdentityBootstrapService,
    bootstrap_completion_event,
    classify_existing_identity,
    resolve_trusted_workspace_id,
)
from personal_os.identity.contracts import (
    BOOTSTRAP_INPUT_REASONS,
    IDENTITY_METRIC_CONTRACTS,
    BootstrapDeviceKind,
    BootstrapIdentityCommand,
    BootstrapIdentityOutcome,
    BootstrapIdentityResult,
    BootstrapInputReason,
    IdentityBootstrapError,
    IdentityBootstrapMetrics,
    InMemoryIdentityBootstrapMetrics,
    validate_bootstrap_identity_command,
)
from personal_os.identity.ports import IdentityBootstrapStore

__all__ = [
    "BOOTSTRAP_INPUT_REASONS",
    "IDENTITY_METRIC_CONTRACTS",
    "BootstrapDeviceKind",
    "BootstrapIdentityCommand",
    "BootstrapIdentityOutcome",
    "BootstrapIdentityResult",
    "BootstrapInputReason",
    "ExistingIdentityDevice",
    "ExistingIdentityState",
    "ExistingIdentityUser",
    "ExistingIdentityWorkspace",
    "IdentityBootstrapError",
    "IdentityBootstrapMetrics",
    "IdentityBootstrapService",
    "IdentityBootstrapStore",
    "InMemoryIdentityBootstrapMetrics",
    "bootstrap_completion_event",
    "classify_existing_identity",
    "resolve_trusted_workspace_id",
    "validate_bootstrap_identity_command",
]
