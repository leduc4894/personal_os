"""In-memory fakes for the source-conflict service orchestration.

Every fake records the exact port call sequence into one shared ledger so a
test can assert the cross-port order the service contract pins (capture:
policy guard, then the replay-label lookup, then the store write; resolve:
the row-locked read, the policy guard over that read, then the atomic store
transaction). The store fake models the capture/resolve state machine the
Task 2 PostgreSQL store owns — idempotency replay by capture key and
resolution event, the open-only transition guard, the reviewed-remote versus
current-pointer recheck, the stale-successor supersession and the winner
commit — including a publication gateway fake whose recorded closed tokens
prove which resolution kinds publish a source version (``keep_remote``
publishes none). The policy-guard fake is a thin scripted wrapper raising
the typed exclusion-policy error. The clock fakes follow the canonical
``Callable[[], datetime]`` aware-UTC seam: the sequenced clock serves the
service's duration reads, the fixed clock serves the store's transition
timestamps. The fakes never retain, echo or log evidence payloads:
locators, keys and object references are compared by identity/equality in
assertions, never recorded into the ledger.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final
from uuid import UUID, uuid7

from personal_os.diagnostics.context import DiagnosticContext, create_diagnostic_context
from personal_os.error_contracts.codes import ErrorCode
from personal_os.exclusion_policy.errors import ExclusionPolicyError
from personal_os.source_conflicts.commands import (
    CaptureConflictCommand,
    ConflictIdempotencyKey,
    ConflictResolutionResult,
    ResolveConflictCommand,
)
from personal_os.source_conflicts.contracts import (
    VERSION_PUBLISHING_RESOLUTIONS,
    ConflictCandidate,
    ConflictCandidateKind,
    ConflictKind,
    ConflictResolutionKind,
    ConflictResolutionOutcome,
    ConflictStatus,
    SourceConflict,
)
from personal_os.source_conflicts.errors import SourceConflictError

#: Shared ledger entry constants: one closed string per observed port call.
POLICY_AUTHORIZE_CAPTURE: Final[str] = "policy.authorize_capture"
POLICY_AUTHORIZE_RESOLUTION: Final[str] = "policy.authorize_resolution"
STORE_FIND_CAPTURED_CONFLICT: Final[str] = "store.find_captured_conflict"
STORE_CAPTURE: Final[str] = "store.capture"
STORE_READ_FOR_RESOLUTION: Final[str] = "store.read_for_resolution"
STORE_RESOLVE: Final[str] = "store.resolve"

#: Closed publication-gateway command token recorded by the store fake.
PUBLISH_SOURCE_VERSION: Final[str] = "publish_source_version"

#: Stable fixture identities: opaque, hex-canonical and non-nil.
WORKSPACE_ID: Final[UUID] = UUID("10000000-0000-4000-8000-000000000001")
OTHER_WORKSPACE_ID: Final[UUID] = UUID("10000000-0000-4000-8000-000000000002")
SOURCE_ID: Final[UUID] = UUID("20000000-0000-4000-8000-000000000001")
DEVICE_ID: Final[UUID] = UUID("30000000-0000-4000-8000-000000000001")
CAPTURE_EVENT_ID: Final[UUID] = UUID("40000000-0000-4000-8000-000000000001")
CAPTURE_KEY_TEXT: Final[str] = "50000000-0000-4000-8000-000000000001"
BASE_VERSION_ID: Final[UUID] = UUID("60000000-0000-4000-8000-000000000001")
REMOTE_VERSION_ID: Final[UUID] = UUID("70000000-0000-4000-8000-000000000001")
CANDIDATE_OBJECT_ID: Final[UUID] = UUID("80000000-0000-4000-8000-000000000001")
RESOLUTION_EVENT_ID: Final[UUID] = UUID("90000000-0000-4000-8000-000000000001")
RESOLUTION_KEY_TEXT: Final[str] = "a0000000-0000-4000-8000-000000000001"
MERGED_OBJECT_ID: Final[UUID] = UUID("b0000000-0000-4000-8000-000000000001")

#: One fixed aware-UTC moment for store transition timestamps.
STORE_CLOCK_MOMENT: Final[datetime] = datetime(2026, 9, 2, 1, 2, 3, tzinfo=UTC)


def build_diagnostic_context() -> DiagnosticContext:
    """One fresh server-owned diagnostic context, like the composition root."""

    return create_diagnostic_context().context


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
    duration; tests queue one moment per expected read. Once a single moment
    remains it is returned forever so a clock seam that repeats or drifts
    backwards cannot turn a recorded duration negative.
    """

    moments: list[datetime]

    def __call__(self) -> datetime:
        if len(self.moments) > 1:
            return self.moments.pop(0)
        return self.moments[0]


@dataclass
class FixedUtcClock:
    """Injectable aware UTC clock returning one constant moment."""

    moment: datetime = STORE_CLOCK_MOMENT

    def __call__(self) -> datetime:
        return self.moment


def build_capture_command(**overrides: object) -> CaptureConflictCommand:
    """A structurally valid stale-content capture; overrides pass through."""

    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "source_id": SOURCE_ID,
        "conflict_kind": ConflictKind.STALE_CONTENT,
        "originating_event_id": CAPTURE_EVENT_ID,
        "originating_device_id": DEVICE_ID,
        "idempotency_key": ConflictIdempotencyKey(CAPTURE_KEY_TEXT),
        "base_version_id": BASE_VERSION_ID,
        "observed_remote_version_id": REMOTE_VERSION_ID,
        "candidate": ConflictCandidate.content(CANDIDATE_OBJECT_ID),
        "normalized_locator": None,
    }
    values.update(overrides)
    return CaptureConflictCommand(**values)  # type: ignore[arg-type]


def build_resolve_command(**overrides: object) -> ResolveConflictCommand:
    """A structurally valid keep_remote resolution; overrides pass through."""

    values: dict[str, object] = {
        "conflict_id": UUID("c0000000-0000-4000-8000-000000000001"),
        "reviewed_remote_version_id": REMOTE_VERSION_ID,
        "resolution_kind": ConflictResolutionKind.KEEP_REMOTE,
        "resolution_event_id": RESOLUTION_EVENT_ID,
        "idempotency_key": ConflictIdempotencyKey(RESOLUTION_KEY_TEXT),
        "verified_candidate_object_id": None,
    }
    values.update(overrides)
    return ResolveConflictCommand(**values)  # type: ignore[arg-type]


def fresh_resolution_identities() -> tuple[UUID, ConflictIdempotencyKey]:
    """A fresh (resolution event id, idempotency key) pair for a new attempt."""

    event_id = uuid7()
    return event_id, ConflictIdempotencyKey(str(event_id))


@dataclass
class FakeConflictPublicationGateway:
    """Publication boundary fake recording closed command tokens only.

    Mirrors the Task 2 store's in-transaction publication of one immutable
    source version under ``keep_local``/``save_merged``: the recorded tokens
    carry no identifiers, locators or digests, so an assertion on
    ``commands`` proves exactly which resolution kinds published.
    """

    commands: list[str] = field(default_factory=list)

    def publish_source_version(self) -> UUID:
        """Record one publication command and mint the resulting version id."""
        self.commands.append(PUBLISH_SOURCE_VERSION)
        return uuid7()


@dataclass
class FakeSourceConflictPolicyGuard:
    """Scripted policy guard: records calls, raises the typed scripted error."""

    ledger: CallLedger
    capture_error: ExclusionPolicyError | None = None
    resolution_error: ExclusionPolicyError | None = None
    authorized_captures: list[CaptureConflictCommand] = field(default_factory=list)
    authorized_resolutions: list[SourceConflict] = field(default_factory=list)

    async def authorize_capture(
        self,
        command: CaptureConflictCommand,
        diagnostic_context: DiagnosticContext,
    ) -> None:
        del diagnostic_context
        self.ledger.record(POLICY_AUTHORIZE_CAPTURE)
        self.authorized_captures.append(command)
        if self.capture_error is not None:
            raise self.capture_error

    async def authorize_resolution(
        self,
        conflict: SourceConflict,
        diagnostic_context: DiagnosticContext,
    ) -> None:
        del diagnostic_context
        self.ledger.record(POLICY_AUTHORIZE_RESOLUTION)
        self.authorized_resolutions.append(conflict)
        if self.resolution_error is not None:
            raise self.resolution_error


@dataclass
class InMemorySourceConflictStore:
    """In-memory model of the Task 2 store's conflict state machine.

    Owns the same closed transitions the durable store owns: capture replay
    by key with evidence equality (event-under-another-key and foreign-key
    reuse reject with the typed idempotency mismatch), resolution replay by
    event identity, the open-only guard, the workspace scope of every read,
    and the reviewed-remote versus current-pointer recheck that selects the
    winner or the stale-successor supersession. ``current_version_ids`` is
    the settable projection of the canonical source pointer the durable
    store locks in its own transaction.
    """

    ledger: CallLedger
    publication_gateway: FakeConflictPublicationGateway = field(
        default_factory=FakeConflictPublicationGateway
    )
    clock: Callable[[], datetime] = field(default_factory=FixedUtcClock)
    capture_error: SourceConflictError | None = None
    resolve_error: SourceConflictError | None = None
    read_error: SourceConflictError | None = None
    conflicts: dict[UUID, SourceConflict] = field(default_factory=dict)
    current_version_ids: dict[tuple[UUID, UUID], UUID | None] = field(default_factory=dict)
    observed_workspace_scopes: list[tuple[str, UUID]] = field(default_factory=list)
    _conflicts_by_originating_event: dict[tuple[UUID, UUID], UUID] = field(default_factory=dict)
    _capture_key_owners: dict[tuple[UUID, str], UUID] = field(default_factory=dict)
    _resolution_results: dict[tuple[UUID, UUID], ConflictResolutionResult] = field(
        default_factory=dict
    )
    _resolution_key_owners: dict[tuple[UUID, str], UUID] = field(default_factory=dict)

    # --- capture ----------------------------------------------------------------

    async def capture(
        self,
        command: CaptureConflictCommand,
        diagnostic_context: DiagnosticContext,
    ) -> SourceConflict:
        del diagnostic_context
        self.ledger.record(STORE_CAPTURE)
        if self.capture_error is not None:
            raise self.capture_error
        key_owner = self._capture_key_owners.get(
            (command.workspace_id, command.idempotency_key.value)
        )
        if key_owner is not None:
            stored = self.conflicts[key_owner]
            if stored.originating_event_id != command.originating_event_id or (
                not self._capture_evidence_matches(stored, command)
            ):
                raise SourceConflictError(ErrorCode.SOURCE_CONFLICT_IDEMPOTENCY_MISMATCH)
            return stored
        if (
            command.workspace_id,
            command.originating_event_id,
        ) in self._conflicts_by_originating_event:
            # The key lookup missed, so this event identity is held under
            # another idempotency key.
            raise SourceConflictError(ErrorCode.SOURCE_CONFLICT_IDEMPOTENCY_MISMATCH)
        conflict_id = uuid7()
        conflict = SourceConflict(
            conflict_id=conflict_id,
            workspace_id=command.workspace_id,
            source_id=command.source_id,
            conflict_kind=command.conflict_kind,
            status=ConflictStatus.OPEN,
            originating_event_id=command.originating_event_id,
            originating_device_id=command.originating_device_id,
            base_version_id=command.base_version_id,
            observed_remote_version_id=command.observed_remote_version_id,
            candidate=command.candidate,
            captured_at=self.clock(),
            resolution_kind=None,
            resolution_event_id=None,
            resulting_version_id=None,
            successor_conflict_id=None,
            closed_at=None,
        )
        self.conflicts[conflict_id] = conflict
        self._conflicts_by_originating_event[
            (command.workspace_id, command.originating_event_id)
        ] = conflict_id
        self._capture_key_owners[(command.workspace_id, command.idempotency_key.value)] = (
            conflict_id
        )
        return conflict

    @staticmethod
    def _capture_evidence_matches(stored: SourceConflict, command: CaptureConflictCommand) -> bool:
        """Compare the read-model evidence of a replay candidate exactly."""
        return bool(
            stored.workspace_id == command.workspace_id
            and stored.source_id == command.source_id
            and stored.conflict_kind == command.conflict_kind
            and stored.originating_device_id == command.originating_device_id
            and stored.base_version_id == command.base_version_id
            and stored.observed_remote_version_id == command.observed_remote_version_id
            and stored.candidate == command.candidate
        )

    # --- reads ------------------------------------------------------------------

    async def find_captured_conflict(
        self,
        originating_event_id: UUID,
        workspace_id: UUID,
        diagnostic_context: DiagnosticContext,
    ) -> SourceConflict | None:
        del diagnostic_context
        self.ledger.record(STORE_FIND_CAPTURED_CONFLICT)
        self.observed_workspace_scopes.append(("find_captured_conflict", workspace_id))
        conflict_id = self._conflicts_by_originating_event.get((workspace_id, originating_event_id))
        if conflict_id is None:
            return None
        return self.conflicts[conflict_id]

    async def list_open(
        self,
        workspace_id: UUID,
        *,
        limit: int,
        exclusive_start_conflict_id: UUID | None,
        diagnostic_context: DiagnosticContext,
    ) -> tuple[SourceConflict, ...]:
        del diagnostic_context, limit, exclusive_start_conflict_id
        open_conflicts = sorted(
            (
                conflict
                for conflict in self.conflicts.values()
                if conflict.workspace_id == workspace_id and conflict.status is ConflictStatus.OPEN
            ),
            key=lambda conflict: conflict.conflict_id,
        )
        return tuple(open_conflicts)

    async def read(
        self,
        conflict_id: UUID,
        workspace_id: UUID,
        diagnostic_context: DiagnosticContext,
    ) -> SourceConflict:
        del diagnostic_context
        return self._scoped_conflict(conflict_id, workspace_id)

    async def read_for_resolution(
        self,
        conflict_id: UUID,
        workspace_id: UUID,
        diagnostic_context: DiagnosticContext,
    ) -> SourceConflict:
        del diagnostic_context
        self.ledger.record(STORE_READ_FOR_RESOLUTION)
        self.observed_workspace_scopes.append(("read_for_resolution", workspace_id))
        if self.read_error is not None:
            raise self.read_error
        return self._scoped_conflict(conflict_id, workspace_id)

    def _scoped_conflict(self, conflict_id: UUID, workspace_id: UUID) -> SourceConflict:
        conflict = self.conflicts.get(conflict_id)
        if conflict is None or conflict.workspace_id != workspace_id:
            raise SourceConflictError(ErrorCode.SOURCE_CONFLICT_NOT_FOUND)
        return conflict

    # --- resolution ---------------------------------------------------------------

    async def resolve(
        self,
        command: ResolveConflictCommand,
        workspace_id: UUID,
        diagnostic_context: DiagnosticContext,
    ) -> ConflictResolutionResult:
        del diagnostic_context
        self.ledger.record(STORE_RESOLVE)
        self.observed_workspace_scopes.append(("resolve", workspace_id))
        if self.resolve_error is not None:
            raise self.resolve_error
        replayed = self._resolution_results.get((workspace_id, command.resolution_event_id))
        if replayed is not None:
            if (
                self._resolution_key_owners.get((workspace_id, command.idempotency_key.value))
                != command.resolution_event_id
            ):
                raise SourceConflictError(ErrorCode.SOURCE_CONFLICT_IDEMPOTENCY_MISMATCH)
            return replayed
        idempotency_key = command.idempotency_key.value
        if (workspace_id, idempotency_key) in self._resolution_key_owners or (
            workspace_id,
            idempotency_key,
        ) in self._capture_key_owners:
            raise SourceConflictError(ErrorCode.SOURCE_CONFLICT_IDEMPOTENCY_MISMATCH)
        conflict = self._scoped_conflict(command.conflict_id, workspace_id)
        if conflict.status is not ConflictStatus.OPEN:
            raise SourceConflictError(ErrorCode.SOURCE_CONFLICT_STATE_INVALID)
        current_version_id = (
            self.current_version_ids.get((workspace_id, conflict.source_id))
            if conflict.source_id is not None
            else None
        )
        closed_at = self.clock()
        if command.reviewed_remote_version_id != current_version_id:
            return self._supersede_with_successor(
                command=command,
                conflict=conflict,
                workspace_id=workspace_id,
                current_version_id=current_version_id,
                closed_at=closed_at,
            )
        return self._commit_winner(
            command=command,
            conflict=conflict,
            workspace_id=workspace_id,
            closed_at=closed_at,
        )

    def _supersede_with_successor(
        self,
        *,
        command: ResolveConflictCommand,
        conflict: SourceConflict,
        workspace_id: UUID,
        current_version_id: UUID | None,
        closed_at: datetime,
    ) -> ConflictResolutionResult:
        """Record the stale attempt, supersede and open the bound successor."""
        successor_id = uuid7()
        successor = SourceConflict(
            conflict_id=successor_id,
            workspace_id=workspace_id,
            source_id=conflict.source_id,
            conflict_kind=conflict.conflict_kind,
            status=ConflictStatus.OPEN,
            originating_event_id=command.resolution_event_id,
            originating_device_id=conflict.originating_device_id,
            base_version_id=conflict.base_version_id,
            observed_remote_version_id=current_version_id,
            candidate=conflict.candidate,
            captured_at=closed_at,
            resolution_kind=None,
            resolution_event_id=None,
            resulting_version_id=None,
            successor_conflict_id=None,
            closed_at=None,
        )
        superseded = dataclasses.replace(
            conflict,
            status=ConflictStatus.SUPERSEDED,
            resolution_kind=command.resolution_kind,
            resolution_event_id=command.resolution_event_id,
            resulting_version_id=None,
            successor_conflict_id=successor_id,
            closed_at=closed_at,
        )
        self.conflicts[successor_id] = successor
        self.conflicts[conflict.conflict_id] = superseded
        self._conflicts_by_originating_event[(workspace_id, command.resolution_event_id)] = (
            successor_id
        )
        self._capture_key_owners[(workspace_id, command.idempotency_key.value)] = successor_id
        return self._freeze_result(
            command=command,
            conflict_id=conflict.conflict_id,
            workspace_id=workspace_id,
            kind=ConflictResolutionOutcome.STALE_SUCCESSOR,
            resulting_version_id=None,
            successor=successor,
            completed_at=closed_at,
        )

    def _commit_winner(
        self,
        *,
        command: ResolveConflictCommand,
        conflict: SourceConflict,
        workspace_id: UUID,
        closed_at: datetime,
    ) -> ConflictResolutionResult:
        """Accept the winner and close the conflict resolved."""
        resulting_version_id: UUID | None = None
        if command.resolution_kind in VERSION_PUBLISHING_RESOLUTIONS:
            winning_object_id = (
                conflict.candidate.verified_candidate_object_id
                if command.resolution_kind is ConflictResolutionKind.KEEP_LOCAL
                else command.verified_candidate_object_id
            )
            if (
                conflict.candidate.candidate_kind is not ConflictCandidateKind.CONTENT
                or winning_object_id is None
            ):
                raise SourceConflictError(
                    ErrorCode.SOURCE_CONFLICT_INPUT_INVALID,
                )
            resulting_version_id = self.publication_gateway.publish_source_version()
        resolved = dataclasses.replace(
            conflict,
            status=ConflictStatus.RESOLVED,
            resolution_kind=command.resolution_kind,
            resolution_event_id=command.resolution_event_id,
            resulting_version_id=resulting_version_id,
            successor_conflict_id=None,
            closed_at=closed_at,
        )
        self.conflicts[conflict.conflict_id] = resolved
        return self._freeze_result(
            command=command,
            conflict_id=conflict.conflict_id,
            workspace_id=workspace_id,
            kind=ConflictResolutionOutcome.RESOLVED,
            resulting_version_id=resulting_version_id,
            successor=None,
            completed_at=closed_at,
        )

    def _freeze_result(
        self,
        *,
        command: ResolveConflictCommand,
        conflict_id: UUID,
        workspace_id: UUID,
        kind: ConflictResolutionOutcome,
        resulting_version_id: UUID | None,
        successor: SourceConflict | None,
        completed_at: datetime,
    ) -> ConflictResolutionResult:
        result = ConflictResolutionResult(
            kind=kind,
            conflict_id=conflict_id,
            resolution_event_id=command.resolution_event_id,
            resolution_kind=command.resolution_kind,
            resulting_version_id=resulting_version_id,
            successor=successor,
            completed_at=completed_at,
        )
        self._resolution_results[(workspace_id, command.resolution_event_id)] = result
        self._resolution_key_owners[(workspace_id, command.idempotency_key.value)] = (
            command.resolution_event_id
        )
        return result


__all__ = [
    "CANDIDATE_OBJECT_ID",
    "CAPTURE_EVENT_ID",
    "DEVICE_ID",
    "MERGED_OBJECT_ID",
    "OTHER_WORKSPACE_ID",
    "POLICY_AUTHORIZE_CAPTURE",
    "POLICY_AUTHORIZE_RESOLUTION",
    "PUBLISH_SOURCE_VERSION",
    "REMOTE_VERSION_ID",
    "RESOLUTION_EVENT_ID",
    "SOURCE_ID",
    "STORE_CAPTURE",
    "STORE_CLOCK_MOMENT",
    "STORE_FIND_CAPTURED_CONFLICT",
    "STORE_READ_FOR_RESOLUTION",
    "STORE_RESOLVE",
    "WORKSPACE_ID",
    "CallLedger",
    "FakeConflictPublicationGateway",
    "FakeSourceConflictPolicyGuard",
    "FixedUtcClock",
    "InMemorySourceConflictStore",
    "SequencedUtcClock",
    "build_capture_command",
    "build_diagnostic_context",
    "build_resolve_command",
    "fresh_resolution_identities",
]
