"""In-memory fakes for the source lifecycle service orchestration.

Every fake records the exact port call sequence into one shared ledger so a
test can assert the full cross-port order (policy preflight, store
preflight, store commit) with string entries only. The store fake models
both the exact-replay path (returning ``committed_result`` when set) and
the bounded-retry commit path (replaying ``commit_error`` on each attempt).
The policy fake is a thin wrapper around a chosen
:class:`LifecyclePolicyDecision`; a scripted policy raises the typed error
to model denied access before any store work. The clock is the canonical
``Callable[[], datetime]`` injectable aware UTC seam used by other domain
services; tests queue one moment per expected read. The fakes never retain,
echo or log command payloads: locators, titles, idempotency keys and
fingerprints are compared by identity/equality in the assertions, not
recorded.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final
from uuid import UUID, uuid4

from personal_os.diagnostics.context import DiagnosticContext, create_diagnostic_context
from personal_os.exclusion_policy.contracts import PolicySubject
from personal_os.source_lifecycle.commands import (
    LifecycleOperation,
    LifecycleState,
    SourceLifecycleCommand,
    SourceLifecycleCommitResult,
)
from personal_os.source_lifecycle.errors import SourceLifecycleError, SourceLifecycleErrorCode
from personal_os.source_lifecycle.fingerprint import LifecycleRequestFingerprint
from personal_os.source_lifecycle.ports import (
    LifecycleDeviceContext,
    LifecyclePolicyDecision,
    LifecyclePolicyOutcome,
)
from personal_os.source_locators import NormalizedLocator

#: Shared ledger entry constants: one string per observed port call.
POLICY_EVALUATE_LIFECYCLE: Final[str] = "policy.evaluate_lifecycle"
STORE_RESOLVE_COMMITTED: Final[str] = "store.resolve_committed"
STORE_COMMIT: Final[str] = "store.commit"


@dataclass
class CallLedger:
    """Append-only shared record of observed port calls across all fakes."""

    entries: list[str] = field(default_factory=list)

    def record(self, entry: str) -> None:
        self.entries.append(entry)


@dataclass
class SequencedUtcClock:
    """Injectable aware UTC clock returning queued moments, then the last one.

    The service reads the clock at start and again at outcome to measure
    duration; tests queue one moment per expected read. Once a single
    moment remains it is returned forever so a clock seam that repeats or
    drifts backwards cannot turn a recorded duration negative.
    """

    moments: list[datetime]

    def __call__(self) -> datetime:
        if len(self.moments) > 1:
            return self.moments.pop(0)
        return self.moments[0]


def build_rename_command(**overrides: object) -> SourceLifecycleCommand:
    """A structurally valid RENAME command; overrides pass through to the constructor."""

    values: dict[str, object] = {
        "source_id": UUID("018f47a0-7b00-7000-8000-000000000002"),
        "event_id": UUID("018f47a0-7b00-7000-8000-000000000003"),
        "idempotency_key": "lifecycle-rename-001",
        "operation": LifecycleOperation.RENAME,
        "expected_version_id": UUID("018f47a0-7b00-7000-8000-000000000005"),
        "expected_locator": NormalizedLocator("notes/old.md"),
        "target_locator": NormalizedLocator("notes/new.md"),
        "tombstone_id": None,
        "policy_revision": 1,
        "client_timestamp": datetime(2026, 8, 20, 1, 2, 3, tzinfo=UTC),
    }
    values.update(overrides)
    return SourceLifecycleCommand(**values)  # type: ignore[arg-type]


def build_move_command(**overrides: object) -> SourceLifecycleCommand:
    """A structurally valid MOVE command; overrides pass through to the constructor."""

    values: dict[str, object] = {
        "source_id": UUID("018f47a0-7b00-7000-8000-000000000010"),
        "event_id": UUID("018f47a0-7b00-7000-8000-000000000011"),
        "idempotency_key": "lifecycle-move-001",
        "operation": LifecycleOperation.MOVE,
        "expected_version_id": UUID("018f47a0-7b00-7000-8000-000000000013"),
        "expected_locator": NormalizedLocator("notes/sub/old.md"),
        "target_locator": NormalizedLocator("archive/old.md"),
        "tombstone_id": None,
        "policy_revision": 1,
        "client_timestamp": datetime(2026, 8, 20, 1, 2, 4, tzinfo=UTC),
    }
    values.update(overrides)
    return SourceLifecycleCommand(**values)  # type: ignore[arg-type]


def build_delete_command(**overrides: object) -> SourceLifecycleCommand:
    """A structurally valid DELETE command; overrides pass through to the constructor."""

    values: dict[str, object] = {
        "source_id": UUID("018f47a0-7b00-7000-8000-000000000020"),
        "event_id": UUID("018f47a0-7b00-7000-8000-000000000021"),
        "idempotency_key": "lifecycle-delete-001",
        "operation": LifecycleOperation.DELETE,
        "expected_version_id": UUID("018f47a0-7b00-7000-8000-000000000023"),
        "expected_locator": NormalizedLocator("notes/drop.md"),
        "target_locator": None,
        "tombstone_id": None,
        "policy_revision": 1,
        "client_timestamp": datetime(2026, 8, 20, 1, 2, 5, tzinfo=UTC),
    }
    values.update(overrides)
    return SourceLifecycleCommand(**values)  # type: ignore[arg-type]


def build_restore_command(**overrides: object) -> SourceLifecycleCommand:
    """A structurally valid RESTORE command; overrides pass through to the constructor."""

    values: dict[str, object] = {
        "source_id": UUID("018f47a0-7b00-7000-8000-000000000030"),
        "event_id": UUID("018f47a0-7b00-7000-8000-000000000031"),
        "idempotency_key": "lifecycle-restore-001",
        "operation": LifecycleOperation.RESTORE,
        "expected_version_id": UUID("018f47a0-7b00-7000-8000-000000000033"),
        "expected_locator": None,
        "target_locator": NormalizedLocator("notes/restored.md"),
        "tombstone_id": UUID("018f47a0-7b00-7000-8000-000000000034"),
        "policy_revision": 1,
        "client_timestamp": datetime(2026, 8, 20, 1, 2, 6, tzinfo=UTC),
    }
    values.update(overrides)
    return SourceLifecycleCommand(**values)  # type: ignore[arg-type]


def build_device_context(**overrides: object) -> LifecycleDeviceContext:
    """A structurally valid device context; overrides pass through to the constructor."""

    values: dict[str, object] = {
        "workspace_id": UUID("018f47a0-7b00-7000-8000-000000000040"),
        "device_id": UUID("018f47a0-7b00-7000-8000-000000000041"),
        "user_id": UUID("018f47a0-7b00-7000-8000-000000000042"),
        "device_kind": "obsidian",
    }
    values.update(overrides)
    return LifecycleDeviceContext(**values)  # type: ignore[arg-type]


def build_decision(
    *,
    device_context: LifecycleDeviceContext,
    command: SourceLifecycleCommand,
    outcome: LifecyclePolicyOutcome = LifecyclePolicyOutcome.ALLOWED,
    policy_revision_number: int = 1,
) -> LifecyclePolicyDecision:
    """A closed policy verdict matching the command's locator operands."""

    if command.target_locator is not None:
        subject_locator = command.target_locator.value
    elif command.expected_locator is not None:
        subject_locator = command.expected_locator.value
    else:
        subject_locator = None

    return LifecyclePolicyDecision(
        workspace_id=device_context.workspace_id,
        outcome=outcome,
        policy_revision_number=policy_revision_number,
        subject=PolicySubject(
            workspace_id=device_context.workspace_id,
            source_id=command.source_id,
            normalized_locator=subject_locator,
        ),
        expected_locator=command.expected_locator,
        target_locator=command.target_locator,
    )


def build_commit_result(
    command: SourceLifecycleCommand,
    *,
    source_version_id: UUID | None = None,
    event_sequence: int = 1,
    tombstone_id: UUID | None = None,
    resulting_locator: NormalizedLocator | None = None,
    committed_at: datetime | None = None,
) -> SourceLifecycleCommitResult:
    """A canonical committed result matching ``command``'s operation."""

    if command.operation is LifecycleOperation.DELETE:
        return SourceLifecycleCommitResult(
            source_id=command.source_id,
            source_version_id=source_version_id or uuid4(),
            event_id=command.event_id,
            event_sequence=event_sequence,
            state=LifecycleState.DELETED,
            tombstone_id=tombstone_id or uuid4(),
            resulting_locator=None,
            committed_at=committed_at or datetime(2026, 8, 20, 1, 2, 3, tzinfo=UTC),
        )
    resulting = resulting_locator or command.target_locator
    assert resulting is not None
    return SourceLifecycleCommitResult(
        source_id=command.source_id,
        source_version_id=source_version_id or uuid4(),
        event_id=command.event_id,
        event_sequence=event_sequence,
        state=LifecycleState.ACTIVE,
        tombstone_id=None,
        resulting_locator=resulting,
        committed_at=committed_at or datetime(2026, 8, 20, 1, 2, 3, tzinfo=UTC),
    )


def build_diagnostic_context() -> DiagnosticContext:
    """A fresh server-owned diagnostic context for one request-bound unit of work."""

    return create_diagnostic_context().context


class CancellationError(Exception):
    """Sentinel cancellation raised by the policy/store fakes to model cancellation."""


@dataclass
class FakeLifecyclePolicy:
    """Policy port fake returning one decision or raising a scripted typed error.

    ``evaluate_lifecycle`` returns ``decision`` when set, otherwise raises
    ``error`` (the test scripts the exact error to model denied or invalid
    access). ``calls`` carries the locator-bearing command and device
    context so tests can assert the exact boundary evidence without
    retaining raw locator values.
    """

    ledger: CallLedger
    decision: LifecyclePolicyDecision | None = None
    error: SourceLifecycleError | None = None
    decision_delay_seconds: float = 0.0
    calls: list[
        tuple[SourceLifecycleCommand, LifecycleDeviceContext]
    ] = field(default_factory=list)

    async def evaluate_lifecycle(
        self,
        command: SourceLifecycleCommand,
        device_context: LifecycleDeviceContext,
    ) -> LifecyclePolicyDecision:
        self.ledger.record(POLICY_EVALUATE_LIFECYCLE)
        self.calls.append((command, device_context))
        if self.decision_delay_seconds > 0:
            await asyncio.sleep(self.decision_delay_seconds)
        if self.error is not None:
            raise self.error
        if self.decision is None:
            raise AssertionError(
                "FakeLifecyclePolicy has neither decision nor error configured"
            )
        return self.decision


@dataclass
class FakeLifecycleStore:
    """Store port fake modelling exact-replay preflight and atomic commit.

    ``resolve_committed`` returns ``committed_result`` when set, ``None``
    on a miss; ``resolve_error`` is raised when set. ``commit`` records
    the call, returns ``commit_result`` or raises ``commit_error`` (an
    exact-replay commit, modelled by the adapter, returns ``committed_result``
    without invoking ``commit``). Fingerprints and policy decisions are
    retained for assertion, not logged.
    """

    ledger: CallLedger
    commit_result: SourceLifecycleCommitResult
    committed_result: SourceLifecycleCommitResult | None = None
    resolve_error: SourceLifecycleError | None = None
    commit_error: SourceLifecycleError | None = None
    resolve_fingerprints: list[LifecycleRequestFingerprint] = field(default_factory=list)
    commit_fingerprints: list[LifecycleRequestFingerprint] = field(default_factory=list)
    commit_decisions: list[LifecyclePolicyDecision] = field(default_factory=list)
    commit_commands: list[SourceLifecycleCommand] = field(default_factory=list)
    commit_device_contexts: list[LifecycleDeviceContext] = field(default_factory=list)
    commit_delay_seconds: float = 0.0

    async def resolve_committed(
        self,
        command: SourceLifecycleCommand,
        device_context: LifecycleDeviceContext,
        request_fingerprint: LifecycleRequestFingerprint,
        diagnostic_context: DiagnosticContext,
    ) -> SourceLifecycleCommitResult | None:
        del diagnostic_context
        self.ledger.record(STORE_RESOLVE_COMMITTED)
        self.resolve_fingerprints.append(request_fingerprint)
        if self.resolve_error is not None:
            raise self.resolve_error
        return self.committed_result

    async def commit(
        self,
        command: SourceLifecycleCommand,
        device_context: LifecycleDeviceContext,
        request_fingerprint: LifecycleRequestFingerprint,
        policy_decision: LifecyclePolicyDecision,
        diagnostic_context: DiagnosticContext,
    ) -> SourceLifecycleCommitResult:
        del diagnostic_context
        self.ledger.record(STORE_COMMIT)
        self.commit_commands.append(command)
        self.commit_device_contexts.append(device_context)
        self.commit_fingerprints.append(request_fingerprint)
        self.commit_decisions.append(policy_decision)
        if self.commit_delay_seconds > 0:
            await asyncio.sleep(self.commit_delay_seconds)
        if self.commit_error is not None:
            raise self.commit_error
        return self.commit_result


def build_locator_conflict_error() -> SourceLifecycleError:
    """The typed locator-conflict error a rename/move may raise."""

    return SourceLifecycleError(SourceLifecycleErrorCode.LOCATOR_CONFLICT)


def build_locator_missing_error() -> SourceLifecycleError:
    """The typed locator-missing error a delete may raise."""

    return SourceLifecycleError(SourceLifecycleErrorCode.LOCATOR_MISSING)


def build_tombstone_not_found_error() -> SourceLifecycleError:
    """The typed tombstone-not-found error a restore may raise."""

    return SourceLifecycleError(SourceLifecycleErrorCode.TOMBSTONE_NOT_FOUND)


def build_tombstone_closed_error() -> SourceLifecycleError:
    """The typed tombstone-closed error a restore may raise."""

    return SourceLifecycleError(SourceLifecycleErrorCode.TOMBSTONE_CLOSED)


def build_version_conflict_error() -> SourceLifecycleError:
    """The typed version-conflict error any transition may raise."""

    return SourceLifecycleError(SourceLifecycleErrorCode.VERSION_CONFLICT)


def build_input_invalid_error() -> SourceLifecycleError:
    """The typed input-invalid error any boundary may raise."""

    return SourceLifecycleError(SourceLifecycleErrorCode.INPUT_INVALID)


def build_commit_outcome_unknown_error() -> SourceLifecycleError:
    """The typed commit-outcome-unknown error an ambiguous commit may raise."""

    return SourceLifecycleError(SourceLifecycleErrorCode.COMMIT_OUTCOME_UNKNOWN)


def allowing_clock(monotonic_now: float = 0.0) -> Callable[[], datetime]:
    """Return a clock callable returning a fixed moment for monotonic duration tests."""

    del monotonic_now
    return _fixed_clock(datetime(2026, 8, 20, 1, 2, 3, tzinfo=UTC))


def _fixed_clock(moment: datetime) -> Callable[[], datetime]:
    def _clock() -> datetime:
        return moment

    return _clock
