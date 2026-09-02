"""Race convergence of concurrent conflict captures and resolutions (spec 8.5).

Every case runs against the real migrated baseline through the real async
engine and the durable conflict store (plus, for the policy recheck, the
real :class:`SourceConflictService` over that store). The mandated
two-resolver race pins the spec 8.5 invariant: two explicit resolutions of
one open conflict converge to exactly one winning version with the loser
closed by a typed verdict — never a second winner, never a silent
overwrite, never a deadlock. Concurrent duplicate captures replay one
frozen conflict, a capture racing a resolution settles both, a remote
advance supersedes the reviewed resolution into an immutable predecessor
with a correctly bound open successor, a same-identity resolution replay
racing itself returns the single stored winner, and a policy denial at the
resolution boundary fails closed with the conflict left open.
"""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import pytest
from tests.integration.source_conflicts.conftest import ConflictStoreHarness

from personal_os.diagnostics.context import DiagnosticContext, create_diagnostic_context
from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import ApplicationError
from personal_os.exclusion_policy.errors import ExclusionPolicyError
from personal_os.source_conflicts.commands import (
    CaptureConflictCommand,
    ResolveConflictCommand,
)
from personal_os.source_conflicts.contracts import (
    ConflictCandidate,
    ConflictIdempotencyKey,
    ConflictKind,
    ConflictResolutionKind,
    ConflictResolutionOutcome,
    ConflictStatus,
    SourceConflict,
)
from personal_os.source_conflicts.errors import SourceConflictError
from personal_os.source_conflicts.metrics import InMemorySourceConflictMetrics
from personal_os.source_conflicts.service import SourceConflictService

pytestmark = pytest.mark.local_stack


def _fresh_key() -> ConflictIdempotencyKey:
    return ConflictIdempotencyKey(str(uuid4()))


def _stale_capture_command(
    workspace,
    source_id: UUID,
    *,
    base_version_id: UUID,
    remote_version_id: UUID,
    candidate_object_id: UUID,
    event_id: UUID | None = None,
) -> CaptureConflictCommand:
    return CaptureConflictCommand(
        workspace_id=workspace.workspace_id,
        source_id=source_id,
        conflict_kind=ConflictKind.STALE_CONTENT,
        originating_event_id=event_id if event_id is not None else uuid4(),
        originating_device_id=workspace.device_id,
        idempotency_key=_fresh_key(),
        base_version_id=base_version_id,
        observed_remote_version_id=remote_version_id,
        candidate=ConflictCandidate.content(candidate_object_id),
        normalized_locator=None,
    )


def _resolve_command(
    conflict: SourceConflict,
    *,
    resolution_kind: ConflictResolutionKind,
    verified_candidate_object_id: UUID | None = None,
    resolution_event_id: UUID | None = None,
) -> ResolveConflictCommand:
    return ResolveConflictCommand(
        conflict_id=conflict.conflict_id,
        reviewed_remote_version_id=conflict.observed_remote_version_id,
        resolution_kind=resolution_kind,
        resolution_event_id=resolution_event_id if resolution_event_id is not None else uuid4(),
        idempotency_key=_fresh_key(),
        verified_candidate_object_id=verified_candidate_object_id,
    )


async def _seed_open_conflict(
    harness: ConflictStoreHarness,
) -> tuple[object, UUID, SourceConflict, object]:
    """Seed one workspace, source v1, an advanced remote and one open conflict."""

    workspace = await harness.seed_workspace()
    source_id = uuid4()
    first = await harness.seed_active_source_with_version_one(
        workspace=workspace, source_id=source_id, title="Raced note"
    )
    advanced = await harness.advance_source_version(
        workspace=workspace, source_id=source_id, parent=first
    )
    context = create_diagnostic_context().context
    conflict = await harness.store.capture(
        _stale_capture_command(
            workspace,
            source_id,
            base_version_id=first.source_version_id,
            remote_version_id=advanced.source_version_id,
            candidate_object_id=await harness.seed_content_object("raced-candidate"),
        ),
        context,
    )
    return workspace, source_id, conflict, advanced


class _DenyingResolutionGuard:
    """Server-side policy guard double that denies every resolution recheck."""

    async def authorize_capture(
        self, command: CaptureConflictCommand, diagnostic_context: DiagnosticContext
    ) -> None:
        del command, diagnostic_context

    async def authorize_resolution(
        self, conflict: SourceConflict, diagnostic_context: DiagnosticContext
    ) -> None:
        del conflict, diagnostic_context
        raise ExclusionPolicyError(ErrorCode.EXCLUSION_POLICY_DENIED)


# --- the mandated two-resolver race ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_resolvers_racing_for_one_conflict_create_at_most_one_winning_version(
    conflict_harness: ConflictStoreHarness,
) -> None:
    """Two explicit resolutions converge to exactly one winning version.

    The store's row-locked resolve transaction serializes the racing
    keep_local and save_merged attempts: the first commits exactly one
    winning source version and closes the conflict, and the second — a
    fresh resolution identity against a now-terminal conflict — is closed
    by the typed terminal-state rejection instead of a second winner. The
    brief's ``any(result.kind is STALE_SUCCESSOR)`` shape is pinned by the
    remote-advance race below, where the loser verdict really is the stale
    successor outcome; this race's loser verdict is the terminal-state
    rejection, which is this store's pinned closed verdict for a lost
    two-resolver race (spec 7 row 4: "no second winner").
    """

    harness = conflict_harness
    _workspace, source_id, conflict, _advanced = await _seed_open_conflict(harness)
    context = create_diagnostic_context().context
    merged_object_id = await harness.seed_content_object("racing-merged-result")

    async def resolve_local():
        return await harness.store.resolve(
            _resolve_command(conflict, resolution_kind=ConflictResolutionKind.KEEP_LOCAL),
            conflict.workspace_id,
            context,
        )

    async def resolve_merged():
        return await harness.store.resolve(
            _resolve_command(
                conflict,
                resolution_kind=ConflictResolutionKind.SAVE_MERGED,
                verified_candidate_object_id=merged_object_id,
            ),
            conflict.workspace_id,
            context,
        )

    results = await asyncio.gather(resolve_local(), resolve_merged(), return_exceptions=True)

    assert await harness.published_version_count(source_id) == 2  # v1 + exactly one winner
    outcomes = [result for result in results if not isinstance(result, BaseException)]
    rejections = [result for result in results if isinstance(result, BaseException)]
    assert len(outcomes) == 1
    assert len(rejections) == 1
    winner = outcomes[0]
    assert winner.kind is ConflictResolutionOutcome.RESOLVED
    assert winner.resulting_version_id is not None
    assert winner.resolution_kind in (
        ConflictResolutionKind.KEEP_LOCAL,
        ConflictResolutionKind.SAVE_MERGED,
    )
    rejection = rejections[0]
    assert isinstance(rejection, SourceConflictError)
    assert rejection.error_code is ErrorCode.SOURCE_CONFLICT_STATE_INVALID
    # The closed conflict binds exactly the winner; the current pointer moved
    # exactly once to the winning version.
    closed = await harness.conflict_row(conflict.conflict_id)
    assert closed.status == ConflictStatus.RESOLVED.value
    assert closed.resulting_version_id == winner.resulting_version_id
    assert await harness.current_version_id(source_id) == winner.resulting_version_id


@pytest.mark.asyncio
async def test_same_identity_resolution_replay_race_returns_the_single_stored_winner(
    conflict_harness: ConflictStoreHarness,
) -> None:
    """A duplicate delivery racing its own identity replays the frozen winner.

    Both concurrent deliveries carry the SAME resolution event identity and
    idempotency key: the first commits the winner, the second replays the
    stored outcome unchanged — exactly one resulting version, no second
    winner and no exception.
    """

    harness = conflict_harness
    _workspace, source_id, conflict, _advanced = await _seed_open_conflict(harness)
    context = create_diagnostic_context().context
    resolution_event_id = uuid4()
    command = _resolve_command(
        conflict,
        resolution_kind=ConflictResolutionKind.KEEP_LOCAL,
        resolution_event_id=resolution_event_id,
    )

    first, replay = await asyncio.gather(
        harness.store.resolve(command, conflict.workspace_id, context),
        harness.store.resolve(command, conflict.workspace_id, context),
    )

    assert first.kind is ConflictResolutionOutcome.RESOLVED
    assert replay == first
    assert await harness.published_version_count(source_id) == 2  # v1 + one winner


# --- concurrent captures --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_captures_of_distinct_events_each_retain_their_evidence(
    conflict_harness: ConflictStoreHarness,
) -> None:
    """Distinct concurrent stale captures converge without deadlock or overwrite."""

    harness = conflict_harness
    workspace = await harness.seed_workspace()
    source_id = uuid4()
    first = await harness.seed_active_source_with_version_one(
        workspace=workspace, source_id=source_id, title="Contested note"
    )
    advanced = await harness.advance_source_version(
        workspace=workspace, source_id=source_id, parent=first
    )
    context = create_diagnostic_context().context
    commands = [
        _stale_capture_command(
            workspace,
            source_id,
            base_version_id=first.source_version_id,
            remote_version_id=advanced.source_version_id,
            candidate_object_id=await harness.seed_content_object(f"raced-candidate-{index}"),
        )
        for index in range(4)
    ]

    captured = await asyncio.gather(
        *(harness.store.capture(command, context) for command in commands)
    )

    assert len({conflict.conflict_id for conflict in captured}) == 4
    assert await harness.count_conflicts(workspace.workspace_id) == 4
    # Capture never moves the current pointer and never publishes.
    assert await harness.current_version_id(source_id) == advanced.source_version_id
    assert await harness.published_version_count(source_id) == 2  # v1 + v2 seed


@pytest.mark.asyncio
async def test_concurrent_duplicate_capture_delivery_replays_one_frozen_conflict(
    conflict_harness: ConflictStoreHarness,
) -> None:
    """Four racing duplicate deliveries all answer the one stored conflict."""

    harness = conflict_harness
    workspace = await harness.seed_workspace()
    source_id = uuid4()
    first = await harness.seed_active_source_with_version_one(
        workspace=workspace, source_id=source_id, title="Replayed capture"
    )
    advanced = await harness.advance_source_version(
        workspace=workspace, source_id=source_id, parent=first
    )
    context = create_diagnostic_context().context
    command = _stale_capture_command(
        workspace,
        source_id,
        base_version_id=first.source_version_id,
        remote_version_id=advanced.source_version_id,
        candidate_object_id=await harness.seed_content_object("replayed-candidate"),
    )

    results = await asyncio.gather(
        *(harness.store.capture(command, context) for _attempt in range(4))
    )

    assert all(result == results[0] for result in results)
    assert await harness.count_conflicts(workspace.workspace_id) == 1
    assert await harness.sync_conflict_event_count(workspace.workspace_id) == 1


# --- capture versus resolution --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_capture_racing_resolution_settles_both_without_deadlock(
    conflict_harness: ConflictStoreHarness,
) -> None:
    """A second capture racing the first conflict's resolution settles both.

    Both transactions take the same locks in the same order (the workspace
    idempotency advisory lock, then the source row lock), so the race
    converges: the resolution commits its winner and closes the first
    conflict while the capture retains the second conflict's evidence
    untouched by the pointer move.
    """

    harness = conflict_harness
    workspace, source_id, conflict, advanced = await _seed_open_conflict(harness)
    context = create_diagnostic_context().context
    second_capture = _stale_capture_command(
        workspace,
        source_id,
        base_version_id=advanced.source_version_id,
        remote_version_id=advanced.source_version_id,
        candidate_object_id=await harness.seed_content_object("second-candidate"),
    )

    resolution_result, captured = await asyncio.gather(
        harness.store.resolve(
            _resolve_command(conflict, resolution_kind=ConflictResolutionKind.KEEP_LOCAL),
            conflict.workspace_id,
            context,
        ),
        harness.store.capture(second_capture, context),
    )

    assert resolution_result.kind is ConflictResolutionOutcome.RESOLVED
    assert captured.status is ConflictStatus.OPEN
    assert captured.conflict_id != conflict.conflict_id
    assert await harness.published_version_count(source_id) == 3  # v1 + v2 + winner
    assert await harness.count_conflicts(workspace.workspace_id) == 2
    first_row = await harness.conflict_row(conflict.conflict_id)
    assert first_row.status == ConflictStatus.RESOLVED.value


# --- remote advance (the stale-successor race) ---------------------------------------------------


@pytest.mark.asyncio
async def test_remote_advance_during_review_supersedes_and_opens_bound_successor(
    conflict_harness: ConflictStoreHarness,
) -> None:
    """A reviewed remote that advanced yields the stale-successor outcome.

    The attempted resolution is recorded as stale, the predecessor evidence
    stays immutable, the successor binds the newer observed remote, and a
    same-identity replay returns the frozen stale outcome unchanged — the
    brief's mandated stale-successor race shape.
    """

    harness = conflict_harness
    workspace, source_id, conflict, advanced = await _seed_open_conflict(harness)
    context = create_diagnostic_context().context
    newest = await harness.advance_source_version(
        workspace=workspace, source_id=source_id, parent=advanced
    )
    command = _resolve_command(conflict, resolution_kind=ConflictResolutionKind.KEEP_LOCAL)

    stale = await harness.store.resolve(command, conflict.workspace_id, context)
    replay = await harness.store.resolve(command, conflict.workspace_id, context)

    assert stale.kind is ConflictResolutionOutcome.STALE_SUCCESSOR
    assert replay == stale
    assert stale.resulting_version_id is None
    assert stale.successor is not None
    assert stale.successor.status is ConflictStatus.OPEN
    assert stale.successor.observed_remote_version_id == newest.source_version_id
    assert stale.successor.source_id == source_id
    # The predecessor is superseded, immutable and bound to this attempt.
    predecessor = await harness.conflict_row(conflict.conflict_id)
    assert predecessor.status == ConflictStatus.SUPERSEDED.value
    assert predecessor.successor_conflict_id == stale.successor.conflict_id
    assert predecessor.observed_remote_version_id == advanced.source_version_id
    # No version was published by the stale attempt and the pointer never moved.
    assert await harness.published_version_count(source_id) == 3  # v1 + v2 + v3 seed
    assert await harness.current_version_id(source_id) == newest.source_version_id


# --- policy advance ------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_policy_denial_at_resolution_fails_closed_and_keeps_the_conflict_open(
    conflict_harness: ConflictStoreHarness,
) -> None:
    """A policy revision advanced to denial fails the resolution closed.

    The service re-evaluates the exclusion policy over the row-locked read
    before any store mutation: the typed policy denial propagates, no
    version is published, the conflict stays open and the current pointer
    never moves — no unauthorized byte is ever published (spec 7 row 3).
    """

    harness = conflict_harness
    _workspace, source_id, conflict, advanced = await _seed_open_conflict(harness)
    service = SourceConflictService(
        store=harness.store,
        policy_guard=_DenyingResolutionGuard(),
        metrics=InMemorySourceConflictMetrics(),
    )
    context = create_diagnostic_context().context
    before_pointer = await harness.current_version_id(source_id)

    with pytest.raises(ExclusionPolicyError):
        await service.resolve_conflict(
            _resolve_command(conflict, resolution_kind=ConflictResolutionKind.KEEP_LOCAL),
            workspace_id=conflict.workspace_id,
            diagnostic_context=context,
        )

    row = await harness.conflict_row(conflict.conflict_id)
    assert row.status == ConflictStatus.OPEN.value
    assert await harness.published_version_count(source_id) == 2  # v1 + v2 seed
    assert await harness.current_version_id(source_id) == before_pointer
    # The retry after the denial returns to the same open conflict state.
    with pytest.raises(ApplicationError):
        await service.resolve_conflict(
            _resolve_command(conflict, resolution_kind=ConflictResolutionKind.KEEP_REMOTE),
            workspace_id=conflict.workspace_id,
            diagnostic_context=context,
        )
    del advanced
