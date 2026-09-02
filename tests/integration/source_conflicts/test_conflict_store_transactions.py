"""Atomic capture, replay and resolution transactions of the conflict store.

Every case runs against the real migrated baseline through the real async
engine. Capture commits exactly one accepted ``conflict_capture`` sync event,
one immutable ``source_conflicts`` evidence row and one audit row without
touching the source current pointer, and an exact replay of the capture
identity returns the stored conflict. Resolution replays by the resolution
event identity, rechecks the reviewed remote against the current pointer, and
either commits the winner (``keep_remote`` closes with no version;
``keep_local``/``save_merged`` publish exactly one version against the
reviewed remote) or supersedes the predecessor and opens a successor bound to
the newer observed remote.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.source_conflicts.conftest import (
    build_conflict_stack_environment,
    expected_row_deltas,
    run_alembic_arguments,
)

from personal_os.diagnostics.context import create_diagnostic_context
from personal_os.error_contracts.codes import ErrorCode
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
)
from personal_os.source_conflicts.errors import SourceConflictError
from personal_os.source_locators import NormalizedLocator

pytestmark = pytest.mark.local_stack


def _fresh_key() -> ConflictIdempotencyKey:
    return ConflictIdempotencyKey(str(uuid4()))


def _capture_command(
    workspace,
    source_id,
    *,
    base_version_id,
    remote_version_id,
    candidate_object_id,
) -> CaptureConflictCommand:
    return CaptureConflictCommand(
        workspace_id=workspace.workspace_id,
        source_id=source_id,
        conflict_kind=ConflictKind.STALE_CONTENT,
        originating_event_id=uuid4(),
        originating_device_id=workspace.device_id,
        idempotency_key=_fresh_key(),
        base_version_id=base_version_id,
        observed_remote_version_id=remote_version_id,
        candidate=ConflictCandidate.content(candidate_object_id),
        normalized_locator=None,
    )


# --- capture -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_capture_inserts_one_event_conflict_and_candidate_reference_without_pointer_change(
    conflict_harness,
) -> None:
    harness = conflict_harness
    workspace = await harness.seed_workspace()
    source_id = uuid4()
    seeded = await harness.seed_active_source_with_version_one(
        workspace=workspace, source_id=source_id, title="Conflicted note"
    )
    before = await harness.current_version_id(source_id)
    counts_before = await _table_counts(harness.engine)
    context = create_diagnostic_context().context

    conflict = await harness.store.capture(
        _capture_command(
            workspace,
            source_id,
            base_version_id=seeded.source_version_id,
            remote_version_id=seeded.source_version_id,
            candidate_object_id=seeded.content_object_id,
        ),
        context,
    )

    assert conflict.status is ConflictStatus.OPEN
    assert conflict.conflict_kind is ConflictKind.STALE_CONTENT
    assert conflict.source_id == source_id
    assert conflict.candidate.verified_candidate_object_id == seeded.content_object_id
    assert await harness.current_version_id(source_id) == before
    assert await harness.count_conflicts(workspace.workspace_id) == 1
    assert await _row_count_deltas(harness.engine, counts_before) == expected_row_deltas(
        source_conflicts=1, sync_events=1, audit_events=1
    )


@pytest.mark.asyncio
async def test_same_capture_idempotency_key_returns_original_conflict(conflict_harness) -> None:
    harness = conflict_harness
    workspace = await harness.seed_workspace()
    source_id = uuid4()
    seeded = await harness.seed_active_source_with_version_one(
        workspace=workspace, source_id=source_id, title="Replayed note"
    )
    command = _capture_command(
        workspace,
        source_id,
        base_version_id=seeded.source_version_id,
        remote_version_id=seeded.source_version_id,
        candidate_object_id=seeded.content_object_id,
    )
    context = create_diagnostic_context().context

    first = await harness.store.capture(command, context)
    replay = await harness.store.capture(command, context)

    assert first == replay
    assert await harness.count_conflicts(workspace.workspace_id) == 1
    assert await harness.sync_conflict_event_count(workspace.workspace_id) == 1


@pytest.mark.asyncio
async def test_capture_replay_lookup_by_originating_event_returns_stored_conflict(
    conflict_harness,
) -> None:
    harness = conflict_harness
    workspace = await harness.seed_workspace()
    source_id = uuid4()
    seeded = await harness.seed_active_source_with_version_one(
        workspace=workspace, source_id=source_id, title="Replay lookup note"
    )
    command = _capture_command(
        workspace,
        source_id,
        base_version_id=seeded.source_version_id,
        remote_version_id=seeded.source_version_id,
        candidate_object_id=seeded.content_object_id,
    )
    context = create_diagnostic_context().context
    stored = await harness.store.capture(command, context)

    found = await harness.store.find_captured_conflict(
        command.originating_event_id, workspace.workspace_id, context
    )
    absent = await harness.store.find_captured_conflict(uuid4(), workspace.workspace_id, context)

    assert found == stored
    assert absent is None


@pytest.mark.asyncio
async def test_capture_key_reuse_with_a_different_event_rejects_idempotency_mismatch(
    conflict_harness,
) -> None:
    harness = conflict_harness
    workspace = await harness.seed_workspace()
    source_id = uuid4()
    seeded = await harness.seed_active_source_with_version_one(
        workspace=workspace, source_id=source_id, title="Mismatch note"
    )
    command = _capture_command(
        workspace,
        source_id,
        base_version_id=seeded.source_version_id,
        remote_version_id=seeded.source_version_id,
        candidate_object_id=seeded.content_object_id,
    )
    context = create_diagnostic_context().context
    await harness.store.capture(command, context)

    reused_key_command = CaptureConflictCommand(
        workspace_id=command.workspace_id,
        source_id=command.source_id,
        conflict_kind=ConflictKind.STALE_CONTENT,
        originating_event_id=uuid4(),
        originating_device_id=command.originating_device_id,
        idempotency_key=command.idempotency_key,
        base_version_id=command.base_version_id,
        observed_remote_version_id=command.observed_remote_version_id,
        candidate=command.candidate,
        normalized_locator=None,
    )
    with pytest.raises(SourceConflictError) as captured:
        await harness.store.capture(reused_key_command, context)
    assert captured.value.error_code is ErrorCode.SOURCE_CONFLICT_IDEMPOTENCY_MISMATCH
    assert await harness.count_conflicts(workspace.workspace_id) == 1


@pytest.mark.asyncio
async def test_capture_rejects_a_candidate_object_that_is_not_canonical(
    conflict_harness,
) -> None:
    harness = conflict_harness
    workspace = await harness.seed_workspace()
    source_id = uuid4()
    seeded = await harness.seed_active_source_with_version_one(
        workspace=workspace, source_id=source_id, title="Ghost object note"
    )
    context = create_diagnostic_context().context
    command = _capture_command(
        workspace,
        source_id,
        base_version_id=seeded.source_version_id,
        remote_version_id=seeded.source_version_id,
        candidate_object_id=uuid4(),
    )

    with pytest.raises(SourceConflictError) as captured:
        await harness.store.capture(command, context)
    assert captured.value.error_code is ErrorCode.SOURCE_CONFLICT_INPUT_INVALID
    assert str(captured.value.safe_details["reason"]) == "candidate_object_invalid"
    assert await harness.count_conflicts(workspace.workspace_id) == 0


@pytest.mark.asyncio
async def test_capture_locator_collision_without_a_source_keeps_locator_evidence(
    conflict_harness,
) -> None:
    harness = conflict_harness
    workspace = await harness.seed_workspace()
    context = create_diagnostic_context().context
    command = CaptureConflictCommand(
        workspace_id=workspace.workspace_id,
        source_id=None,
        conflict_kind=ConflictKind.LOCATOR_COLLISION,
        originating_event_id=uuid4(),
        originating_device_id=workspace.device_id,
        idempotency_key=_fresh_key(),
        base_version_id=None,
        observed_remote_version_id=None,
        candidate=ConflictCandidate.delete(),
        normalized_locator=NormalizedLocator(f"notes/collision-{uuid4().hex[:8]}.md"),
    )

    conflict = await harness.store.capture(command, context)

    assert conflict.status is ConflictStatus.OPEN
    assert conflict.source_id is None
    # A collision without a canonical source cannot bind the NOT NULL
    # sync_events source; the evidence identity lives on the conflict row.
    assert await harness.sync_conflict_event_count(workspace.workspace_id) == 0
    stored = await harness.conflict_row(conflict.conflict_id)
    assert stored.normalized_locator == command.normalized_locator.value
    replay = await harness.store.find_captured_conflict(
        command.originating_event_id, workspace.workspace_id, context
    )
    assert replay == conflict


@pytest.mark.asyncio
async def test_list_open_pages_in_conflict_identity_order_and_hides_closed_conflicts(
    conflict_harness,
) -> None:
    harness = conflict_harness
    workspace = await harness.seed_workspace()
    source_id = uuid4()
    seeded = await harness.seed_active_source_with_version_one(
        workspace=workspace, source_id=source_id, title="Paged note"
    )
    context = create_diagnostic_context().context
    captured_ids = []
    for _ in range(3):
        conflict = await harness.store.capture(
            _capture_command(
                workspace,
                source_id,
                base_version_id=seeded.source_version_id,
                remote_version_id=seeded.source_version_id,
                candidate_object_id=seeded.content_object_id,
            ),
            context,
        )
        captured_ids.append(conflict.conflict_id)

    first_page = await harness.store.list_open(
        workspace.workspace_id,
        limit=2,
        exclusive_start_conflict_id=None,
        diagnostic_context=context,
    )
    second_page = await harness.store.list_open(
        workspace.workspace_id,
        limit=2,
        exclusive_start_conflict_id=first_page[-1].conflict_id,
        diagnostic_context=context,
    )

    assert [conflict.conflict_id for conflict in first_page] == sorted(captured_ids)[:2]
    assert [conflict.conflict_id for conflict in second_page] == sorted(captured_ids)[2:]


@pytest.mark.asyncio
async def test_read_scopes_one_conflict_to_its_workspace(conflict_harness) -> None:
    harness = conflict_harness
    workspace = await harness.seed_workspace()
    other_workspace = await harness.seed_workspace()
    source_id = uuid4()
    seeded = await harness.seed_active_source_with_version_one(
        workspace=workspace, source_id=source_id, title="Scoped note"
    )
    context = create_diagnostic_context().context
    conflict = await harness.store.capture(
        _capture_command(
            workspace,
            source_id,
            base_version_id=seeded.source_version_id,
            remote_version_id=seeded.source_version_id,
            candidate_object_id=seeded.content_object_id,
        ),
        context,
    )

    assert (
        await harness.store.read(conflict.conflict_id, workspace.workspace_id, context) == conflict
    )
    with pytest.raises(SourceConflictError) as captured:
        await harness.store.read(conflict.conflict_id, other_workspace.workspace_id, context)
    assert captured.value.error_code is ErrorCode.SOURCE_CONFLICT_NOT_FOUND


# --- resolution ----------------------------------------------------------------


def _resolve_command(
    conflict_id,
    *,
    reviewed_remote_version_id,
    resolution_kind: ConflictResolutionKind,
    verified_candidate_object_id=None,
) -> ResolveConflictCommand:
    return ResolveConflictCommand(
        conflict_id=conflict_id,
        reviewed_remote_version_id=reviewed_remote_version_id,
        resolution_kind=resolution_kind,
        resolution_event_id=uuid4(),
        idempotency_key=_fresh_key(),
        verified_candidate_object_id=verified_candidate_object_id,
    )


async def _seed_open_conflict(harness, workspace, *, source_id=None, reviewed_remote=None):
    source_id = source_id or uuid4()
    seeded = await harness.seed_active_source_with_version_one(
        workspace=workspace, source_id=source_id, title="Resolution note"
    )
    conflict = await harness.store.capture(
        _capture_command(
            workspace,
            source_id,
            base_version_id=seeded.source_version_id,
            remote_version_id=seeded.source_version_id,
            candidate_object_id=seeded.content_object_id,
        ),
        create_diagnostic_context().context,
    )
    return conflict, seeded


@pytest.mark.asyncio
async def test_resolve_keep_remote_closes_without_publishing_a_version(
    conflict_harness,
) -> None:
    harness = conflict_harness
    workspace = await harness.seed_workspace()
    source_id = uuid4()
    conflict, seeded = await _seed_open_conflict(harness, workspace, source_id=source_id)
    counts_before = await _table_counts(harness.engine)
    pointer_before = await harness.current_version_id(source_id)
    context = create_diagnostic_context().context
    command = _resolve_command(
        conflict.conflict_id,
        reviewed_remote_version_id=seeded.source_version_id,
        resolution_kind=ConflictResolutionKind.KEEP_REMOTE,
    )

    result = await harness.store.resolve(command, workspace.workspace_id, context)
    replay = await harness.store.resolve(command, workspace.workspace_id, context)

    assert result.kind is ConflictResolutionOutcome.RESOLVED
    assert result.resolution_kind is ConflictResolutionKind.KEEP_REMOTE
    assert result.resulting_version_id is None
    assert result.successor is None
    assert replay == result
    assert await harness.current_version_id(source_id) == pointer_before
    assert await _row_count_deltas(harness.engine, counts_before) == expected_row_deltas(
        sync_events=1, audit_events=1
    )
    stored = await harness.conflict_row(conflict.conflict_id)
    assert stored.status == "resolved"
    assert stored.resulting_version_id is None
    assert stored.successor_conflict_id is None


@pytest.mark.asyncio
async def test_resolve_keep_local_publishes_exactly_one_version_against_the_reviewed_remote(
    conflict_harness,
) -> None:
    harness = conflict_harness
    workspace = await harness.seed_workspace()
    source_id = uuid4()
    conflict, seeded = await _seed_open_conflict(harness, workspace, source_id=source_id)
    counts_before = await _table_counts(harness.engine)
    context = create_diagnostic_context().context
    command = _resolve_command(
        conflict.conflict_id,
        reviewed_remote_version_id=seeded.source_version_id,
        resolution_kind=ConflictResolutionKind.KEEP_LOCAL,
    )

    result = await harness.store.resolve(command, workspace.workspace_id, context)
    replay = await harness.store.resolve(command, workspace.workspace_id, context)

    assert result.kind is ConflictResolutionOutcome.RESOLVED
    assert result.resulting_version_id is not None
    assert replay == result
    assert await harness.current_version_id(source_id) == result.resulting_version_id
    assert await harness.published_version_count(source_id) == 2
    assert await _row_count_deltas(harness.engine, counts_before) == expected_row_deltas(
        source_versions=1,
        projection_intents=2,
        sync_events=1,
        audit_events=1,
    )
    stored = await harness.conflict_row(conflict.conflict_id)
    assert stored.status == "resolved"
    assert stored.resulting_version_id == result.resulting_version_id
    assert await harness.resulting_version_object(result.resulting_version_id) == (
        seeded.content_object_id
    )


@pytest.mark.asyncio
async def test_resolve_save_merged_publishes_the_verified_merged_object(
    conflict_harness,
) -> None:
    harness = conflict_harness
    workspace = await harness.seed_workspace()
    source_id = uuid4()
    conflict, seeded = await _seed_open_conflict(harness, workspace, source_id=source_id)
    merged_object_id = await harness.seed_content_object(f"merged-{uuid4()}")
    context = create_diagnostic_context().context
    command = _resolve_command(
        conflict.conflict_id,
        reviewed_remote_version_id=seeded.source_version_id,
        resolution_kind=ConflictResolutionKind.SAVE_MERGED,
        verified_candidate_object_id=merged_object_id,
    )

    result = await harness.store.resolve(command, workspace.workspace_id, context)

    assert result.kind is ConflictResolutionOutcome.RESOLVED
    assert result.resulting_version_id is not None
    assert await harness.resulting_version_object(result.resulting_version_id) == merged_object_id
    assert await harness.current_version_id(source_id) == result.resulting_version_id


@pytest.mark.asyncio
async def test_stale_resolution_supersedes_predecessor_and_opens_successor(
    conflict_harness,
) -> None:
    harness = conflict_harness
    workspace = await harness.seed_workspace()
    source_id = uuid4()
    conflict, seeded = await _seed_open_conflict(harness, workspace, source_id=source_id)
    newer_remote = await harness.advance_source_version(
        workspace=workspace, source_id=source_id, parent=seeded
    )
    counts_before = await _table_counts(harness.engine)
    context = create_diagnostic_context().context
    command = _resolve_command(
        conflict.conflict_id,
        reviewed_remote_version_id=seeded.source_version_id,
        resolution_kind=ConflictResolutionKind.KEEP_LOCAL,
    )

    result = await harness.store.resolve(command, workspace.workspace_id, context)
    replay = await harness.store.resolve(command, workspace.workspace_id, context)

    assert result.kind is ConflictResolutionOutcome.STALE_SUCCESSOR
    assert result.resulting_version_id is None
    assert replay == result
    successor = result.successor
    assert successor is not None
    assert successor.status is ConflictStatus.OPEN
    assert successor.observed_remote_version_id == newer_remote.source_version_id
    assert successor.base_version_id == conflict.base_version_id
    assert successor.candidate == conflict.candidate
    assert successor.source_id == source_id
    # No version was published and the pointer stays on the newer remote.
    assert await harness.current_version_id(source_id) == newer_remote.source_version_id
    assert await _row_count_deltas(harness.engine, counts_before) == expected_row_deltas(
        source_conflicts=1, sync_events=1, audit_events=1
    )
    predecessor = await harness.conflict_row(conflict.conflict_id)
    assert predecessor.status == "superseded"
    assert predecessor.successor_conflict_id == successor.conflict_id
    # The predecessor evidence stays immutable after the stale attempt.
    assert predecessor.base_version_id == conflict.base_version_id
    assert predecessor.observed_remote_version_id == conflict.observed_remote_version_id
    assert predecessor.verified_candidate_object_id == (
        conflict.candidate.verified_candidate_object_id
    )


@pytest.mark.asyncio
async def test_resolve_on_a_terminal_conflict_with_a_new_event_rejects_state_invalid(
    conflict_harness,
) -> None:
    harness = conflict_harness
    workspace = await harness.seed_workspace()
    source_id = uuid4()
    conflict, seeded = await _seed_open_conflict(harness, workspace, source_id=source_id)
    context = create_diagnostic_context().context
    first_command = _resolve_command(
        conflict.conflict_id,
        reviewed_remote_version_id=seeded.source_version_id,
        resolution_kind=ConflictResolutionKind.KEEP_REMOTE,
    )
    await harness.store.resolve(first_command, workspace.workspace_id, context)

    second_command = _resolve_command(
        conflict.conflict_id,
        reviewed_remote_version_id=seeded.source_version_id,
        resolution_kind=ConflictResolutionKind.KEEP_LOCAL,
    )
    with pytest.raises(SourceConflictError) as captured:
        await harness.store.resolve(second_command, workspace.workspace_id, context)
    assert captured.value.error_code is ErrorCode.SOURCE_CONFLICT_STATE_INVALID


@pytest.mark.asyncio
async def test_resolve_rejects_key_reuse_for_a_different_resolution_event(
    conflict_harness,
) -> None:
    harness = conflict_harness
    workspace = await harness.seed_workspace()
    source_id = uuid4()
    first_conflict, first_seeded = await _seed_open_conflict(
        harness, workspace, source_id=source_id
    )
    context = create_diagnostic_context().context
    first_command = _resolve_command(
        first_conflict.conflict_id,
        reviewed_remote_version_id=first_seeded.source_version_id,
        resolution_kind=ConflictResolutionKind.KEEP_REMOTE,
    )
    first_result = await harness.store.resolve(first_command, workspace.workspace_id, context)
    assert first_result.kind is ConflictResolutionOutcome.RESOLVED

    second_conflict, second_seeded = await _seed_open_conflict(
        harness, workspace, source_id=uuid4()
    )
    reused_key_command = ResolveConflictCommand(
        conflict_id=second_conflict.conflict_id,
        reviewed_remote_version_id=second_seeded.source_version_id,
        resolution_kind=ConflictResolutionKind.KEEP_REMOTE,
        resolution_event_id=uuid4(),
        idempotency_key=first_command.idempotency_key,
        verified_candidate_object_id=None,
    )
    with pytest.raises(SourceConflictError) as captured:
        await harness.store.resolve(reused_key_command, workspace.workspace_id, context)
    assert captured.value.error_code is ErrorCode.SOURCE_CONFLICT_IDEMPOTENCY_MISMATCH


# --- gated downgrade walk -------------------------------------------------------


@pytest.mark.asyncio
async def test_gated_downgrade_walks_cleanly_after_a_published_resolution(
    conflict_harness,
    source_conflict_stack,
) -> None:
    """The destructive gate covers the intents a published resolution leaves.

    A ``keep_local`` resolution commits two upsert projection intents whose
    ``fk_projection_intents__event_source`` RESTRICTs the conflict-event
    delete, so an ungated downgrade must refuse while that evidence exists,
    and the gated walk must remove the rebuildable intents before the
    conflict events — completing the downgrade instead of aborting
    mid-flight on the raw foreign-key violation.
    """
    harness = conflict_harness
    workspace = await harness.seed_workspace()
    source_id = uuid4()
    conflict, seeded = await _seed_open_conflict(harness, workspace, source_id=source_id)
    context = create_diagnostic_context().context
    command = _resolve_command(
        conflict.conflict_id,
        reviewed_remote_version_id=seeded.source_version_id,
        resolution_kind=ConflictResolutionKind.KEEP_LOCAL,
    )
    result = await harness.store.resolve(command, workspace.workspace_id, context)
    assert result.resulting_version_id is not None
    assert await harness.published_version_count(source_id) == 2

    environment = build_conflict_stack_environment(source_conflict_stack.port)

    refused = run_alembic_arguments(environment, ("downgrade", "20260901_03"))
    assert refused.returncode != 0
    # The ungated walk refuses at a closed token — the repository's Alembic
    # environment guard fires first ("database_destructive_downgrade_refused")
    # and this revision's own evidence gate backs it — never a raw driver
    # error, and the database stays at head.
    refusal_output = refused.stderr + refused.stdout
    assert "database_destructive_downgrade_refused" in refusal_output or (
        "source_conflict_downgrade_requires_explicit_gate" in refusal_output
    )

    downgrade = run_alembic_arguments(
        environment, ("-x", "allow_destructive=true", "downgrade", "20260901_03")
    )
    assert downgrade.returncode == 0

    from postgresql_source_store.tables import projection_intents, source_versions

    async with harness.engine.connect() as connection:
        conflict_table = await connection.execute(
            sa.text("SELECT to_regclass('knowledge.source_conflicts')")
        )
        assert conflict_table.scalar_one() is None
        conflict_events = await connection.execute(
            sa.text(
                "SELECT count(*) FROM knowledge.sync_events "
                "WHERE event_type IN ('conflict_capture', 'conflict_resolve')"
            )
        )
        assert int(conflict_events.scalar_one()) == 0
        workspace_intents = await connection.execute(
            sa.select(sa.func.count())
            .select_from(projection_intents)
            .where(projection_intents.c.workspace_id == workspace.workspace_id)
        )
        assert int(workspace_intents.scalar_one()) == 0
        surviving_versions = await connection.execute(
            sa.select(sa.func.count())
            .select_from(source_versions)
            .where(source_versions.c.source_id == source_id)
        )
        assert int(surviving_versions.scalar_one()) == 2

    upgrade = run_alembic_arguments(environment, ("upgrade", "head"))
    assert upgrade.returncode == 0
    back_at_head = run_alembic_arguments(environment, ("current", "--check-heads"))
    assert back_at_head.returncode == 0


async def _table_counts(engine: AsyncEngine) -> dict[str, int]:
    from postgresql_source_store.tables import SOURCE_STORE_TABLES

    counts: dict[str, int] = {}
    async with engine.connect() as connection:
        for table_name, table in SOURCE_STORE_TABLES.items():
            result = await connection.execute(sa.select(sa.func.count()).select_from(table))
            counts[table_name] = int(result.scalar_one())
    return counts


async def _row_count_deltas(engine: AsyncEngine, counts_before: dict[str, int]) -> dict[str, int]:
    counts_after = await _table_counts(engine)
    return {
        table_name: counts_after[table_name] - counts_before[table_name]
        for table_name in counts_after
    }
