"""Provider-neutral lifecycle persistence and policy ports."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from personal_os.diagnostics.context import DiagnosticContext
from personal_os.exclusion_policy.contracts import PolicySubject
from personal_os.source_lifecycle.commands import (
    SourceLifecycleCommand,
    SourceLifecycleCommitResult,
)
from personal_os.source_lifecycle.fingerprint import LifecycleRequestFingerprint
from personal_os.source_locators import NormalizedLocator


@dataclass(frozen=True, slots=True)
class LifecycleDeviceContext:
    """Credential-derived identity of the submitting Obsidian device.

    Workspace, device and owner user IDs all derive exclusively from the
    authenticated bearer credential; a request body never chooses any of
    them. ``device_kind`` is the closed backend vocabulary mirroring the
    devices table and is recorded on tombstone and audit rows.
    """

    workspace_id: UUID
    device_id: UUID
    user_id: UUID
    device_kind: str

    def __post_init__(self) -> None:
        if not isinstance(self.device_kind, str) or not self.device_kind:
            raise ValueError("device_kind must be a non-empty string")


class LifecyclePolicyOutcome(StrEnum):
    """Closed outcome of one lifecycle policy evaluation.

    The three members map onto the published revision's enforced decision:
    ``ALLOWED`` keeps the canonical mutation truthful and selects
    projection upserts; ``DENIED`` and ``INDETERMINATE`` keep the truthful
    canonical mutation but select projection deletes, never falsifying
    lifecycle state.
    """

    ALLOWED = "allowed"
    DENIED = "denied"
    INDETERMINATE = "indeterminate"


@dataclass(frozen=True, slots=True)
class LifecyclePolicyDecision:
    """The closed policy verdict passed to the atomic lifecycle commit.

    The decision is the result of the service-level evaluation outside the
    transaction; the adapter re-verifies the locked active policy revision
    before mutating canonical state, never trusting this evidence alone.
    The locator evidence (``expected_locator`` and ``target_locator``) is
    carried by the command itself; this value only carries the verdict and
    the closed safe-detail references. ``policy_revision_number`` is the
    revision under which the verdict was reached; a mismatch with the
    locked active revision invalidates the verdict and the adapter
    re-evaluates under the lock.
    """

    workspace_id: UUID
    outcome: LifecyclePolicyOutcome
    policy_revision_number: int
    subject: PolicySubject
    expected_locator: NormalizedLocator | None
    target_locator: NormalizedLocator | None

    def __post_init__(self) -> None:
        if self.policy_revision_number < 1:
            raise ValueError("policy_revision_number must be positive")


#: Adapter-facing alias of the canonical command result. The service and
#: adapter share the same value object so the wire-level envelope and the
## adapter result are interchangeable in tests and composition.
LifecycleCommitResult = SourceLifecycleCommitResult


class SourceLifecycleStore(Protocol):
    """Durable replay lookup and atomic lifecycle transition boundary."""

    async def resolve_committed(
        self,
        command: SourceLifecycleCommand,
        device_context: LifecycleDeviceContext,
        request_fingerprint: LifecycleRequestFingerprint,
        diagnostic_context: DiagnosticContext,
    ) -> SourceLifecycleCommitResult | None: ...

    async def commit(
        self,
        command: SourceLifecycleCommand,
        device_context: LifecycleDeviceContext,
        request_fingerprint: LifecycleRequestFingerprint,
        policy_decision: LifecyclePolicyDecision,
        diagnostic_context: DiagnosticContext,
    ) -> SourceLifecycleCommitResult: ...


class SourceLifecyclePolicy(Protocol):
    """Service-level lifecycle policy seam (spec 11).

    The service consults the policy before the atomic store commit and
    hands the resulting :class:`LifecyclePolicyDecision` through unchanged
    so the store can re-verify the locked active revision under the
    policy-state row lock; the verdict and the safe-detail references are
    the only evidence crossing the boundary. The decision is never
    inspected by the service to choose a retry policy or to mutate the
    canonical state — the store is the sole owner of the projection-intent
    selection between ``upsert`` and ``delete``.
    """

    async def evaluate_lifecycle(
        self,
        command: SourceLifecycleCommand,
        device_context: LifecycleDeviceContext,
    ) -> LifecyclePolicyDecision: ...


__all__ = [
    "LifecycleCommitResult",
    "LifecycleDeviceContext",
    "LifecyclePolicyDecision",
    "LifecyclePolicyOutcome",
    "SourceLifecyclePolicy",
    "SourceLifecycleStore",
]