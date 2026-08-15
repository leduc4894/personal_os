"""Identity bootstrap service and provider-neutral replay/drift classification.

The classifier consumes a provider-neutral snapshot of the canonical identity
rows (spec 5.4): an exact replay returns the originally committed ids with the
stored workspace creation timestamp, while any drift — cardinality, display
data, status, ownership, or the bootstrap device itself — fails closed as a
terminal ``identity_bootstrap_state_conflict`` without repair. The service
delegates the atomic create-or-replay decision to the store port, records the
closed outcome metric, and builds the registered completion event from ids and
the outcome enum only, mirroring the pure diagnostic-field builder pattern of
:mod:`personal_os.sources.projection_dispatch`; no name, key or free-text
value ever enters an event field, and the validated payload is handed to the
diagnostic sink by the composition root that owns the configured logger.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final, NoReturn
from uuid import UUID

from personal_os.diagnostics.context import DiagnosticContext
from personal_os.diagnostics.events import (
    EventName,
    RejectedDiagnosticPayload,
    build_registered_event,
)
from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import InternalApplicationError
from personal_os.identity.contracts import (
    BootstrapIdentityCommand,
    BootstrapIdentityOutcome,
    BootstrapIdentityResult,
    IdentityBootstrapError,
    IdentityBootstrapMetrics,
)
from personal_os.identity.ports import IdentityBootstrapStore

__all__ = [
    "ExistingIdentityDevice",
    "ExistingIdentityState",
    "ExistingIdentityUser",
    "ExistingIdentityWorkspace",
    "IdentityBootstrapService",
    "bootstrap_completion_event",
    "classify_existing_identity",
    "resolve_trusted_workspace_id",
]

_WORKSPACE_STATUS_ACTIVE: Final[str] = "active"
_USER_STATUS_ACTIVE: Final[str] = "active"
_DEVICE_STATUS_ACTIVE: Final[str] = "active"


@dataclass(frozen=True, slots=True)
class ExistingIdentityUser:
    """One canonical user row view; carries no secret or credential data."""

    user_id: UUID
    username: str
    display_name: str
    status: str


@dataclass(frozen=True, slots=True)
class ExistingIdentityWorkspace:
    """One canonical workspace row view with its original creation timestamp."""

    workspace_id: UUID
    owner_user_id: UUID
    workspace_key: str
    display_name: str
    status: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ExistingIdentityDevice:
    """One canonical device row view, including its revocation marker."""

    device_id: UUID
    workspace_id: UUID
    user_id: UUID
    device_name: str
    device_kind: str
    status: str
    revoked_at: datetime | None


@dataclass(frozen=True, slots=True)
class ExistingIdentityState:
    """Provider-neutral snapshot of canonical identity rows (spec 5.4)."""

    users: tuple[ExistingIdentityUser, ...]
    workspaces: tuple[ExistingIdentityWorkspace, ...]
    devices: tuple[ExistingIdentityDevice, ...]


def _state_conflict() -> NoReturn:
    raise IdentityBootstrapError(ErrorCode.IDENTITY_BOOTSTRAP_STATE_CONFLICT)


def resolve_trusted_workspace_id(
    state: ExistingIdentityState, command: BootstrapIdentityCommand
) -> UUID | None:
    """The single active workspace whose key matches, else ``None``."""
    trusted = [
        workspace
        for workspace in state.workspaces
        if workspace.status == _WORKSPACE_STATUS_ACTIVE
        and workspace.workspace_key == command.workspace_key
    ]
    return trusted[0].workspace_id if len(trusted) == 1 else None


def classify_existing_identity(
    state: ExistingIdentityState, command: BootstrapIdentityCommand
) -> BootstrapIdentityResult:
    """Classify existing identity state as exact replay or conflict (spec 5.4).

    Never mutates, never repairs: any drift from the originally bootstrapped
    values is a terminal ``identity_bootstrap_state_conflict``.
    """
    if len(state.users) != 1 or len(state.workspaces) != 1:
        _state_conflict()
    user, workspace = state.users[0], state.workspaces[0]
    if (
        user.username != command.username
        or user.display_name != command.user_display_name
        or user.status != _USER_STATUS_ACTIVE
        or workspace.workspace_key != command.workspace_key
        or workspace.display_name != command.workspace_display_name
        or workspace.status != _WORKSPACE_STATUS_ACTIVE
        or workspace.owner_user_id != user.user_id
    ):
        _state_conflict()
    matching = [
        device
        for device in state.devices
        if device.workspace_id == workspace.workspace_id
        and device.device_name == command.device_name
        and device.device_kind == command.device_kind.value
    ]
    if len(matching) != 1:
        _state_conflict()
    device = matching[0]
    if (
        device.status != _DEVICE_STATUS_ACTIVE
        or device.revoked_at is not None
        or device.user_id != user.user_id
    ):
        _state_conflict()
    return BootstrapIdentityResult(
        user_id=user.user_id,
        workspace_id=workspace.workspace_id,
        device_id=device.device_id,
        outcome=BootstrapIdentityOutcome.EXISTING,
        committed_at=workspace.created_at,
    )


def bootstrap_completion_event(
    result: BootstrapIdentityResult,
) -> tuple[EventName, dict[str, object]]:
    """Select the registered completion event and its safe field payload.

    The payload carries only registry-safe values — the closed outcome enum and
    server-assigned ids — so no username, key, display or device name can ever
    reach a diagnostic line; the field set satisfies the Task 1 registry
    definitions for both completion events exactly.
    """
    if result.outcome is BootstrapIdentityOutcome.CREATED:
        return EventName.IDENTITY_BOOTSTRAP_SUCCEEDED, {
            "outcome": result.outcome,
            "user_id": result.user_id,
            "workspace_id": result.workspace_id,
            "device_id": result.device_id,
        }
    return EventName.IDENTITY_BOOTSTRAP_REPLAYED, {
        "user_id": result.user_id,
        "workspace_id": result.workspace_id,
        "device_id": result.device_id,
    }


@dataclass(frozen=True, slots=True)
class IdentityBootstrapService:
    """Validates, delegates to the atomic store and emits safe diagnostics."""

    store: IdentityBootstrapStore
    metrics: IdentityBootstrapMetrics

    async def bootstrap(
        self, command: BootstrapIdentityCommand, diagnostic_context: DiagnosticContext
    ) -> BootstrapIdentityResult:
        """Delegate the atomic create-or-replay, then record the closed outcome.

        Emits the registered completion event validated through
        :func:`build_registered_event`; the composition root that owns the
        configured diagnostic sink consumes the validated payload.
        """
        result = await self.store.bootstrap(command, diagnostic_context)
        self.metrics.record_bootstrap(result.outcome)
        event_name, fields = bootstrap_completion_event(result)
        built = build_registered_event(event_name, fields)
        if isinstance(built, RejectedDiagnosticPayload):
            # A rejected payload here means registry drift, a programming
            # error rather than untrusted input; raise so it also surfaces
            # in optimized (python -O) runs instead of vanishing with assert.
            raise InternalApplicationError(ErrorCode.INTERNAL_ERROR)
        return result
