"""Source-conflict service orchestration tests.

Pins the two shared entrypoints Tasks 4-6 consume: ``capture_conflict``
evaluates the policy guard before any store work, derives the replay label
from the event-identity lookup and hands the command to the store's own
idempotent capture; ``resolve_conflict`` performs the row-locked read, the
policy recheck over that read, and the atomic store transaction — recording
exactly one closed outcome per completed or rejected branch and propagating
every typed error unchanged. The stale-successor transition keeps the
predecessor immutable and opens the successor bound to the newer observed
remote; ``keep_remote`` publishes no source version at all.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from tests.unit.source_conflicts.fakes import (
    MERGED_OBJECT_ID,
    OTHER_WORKSPACE_ID,
    POLICY_AUTHORIZE_CAPTURE,
    POLICY_AUTHORIZE_RESOLUTION,
    PUBLISH_SOURCE_VERSION,
    REMOTE_VERSION_ID,
    SOURCE_ID,
    STORE_CAPTURE,
    STORE_FIND_CAPTURED_CONFLICT,
    STORE_READ_FOR_RESOLUTION,
    STORE_RESOLVE,
    WORKSPACE_ID,
    CallLedger,
    FakeSourceConflictPolicyGuard,
    InMemorySourceConflictStore,
    SequencedUtcClock,
    build_capture_command,
    build_diagnostic_context,
    build_resolve_command,
    fresh_resolution_identities,
)

from personal_os.error_contracts.codes import ErrorCode
from personal_os.exclusion_policy.errors import ExclusionPolicyError
from personal_os.source_conflicts.contracts import (
    ConflictKind,
    ConflictResolutionKind,
    ConflictResolutionOutcome,
    ConflictStatus,
)
from personal_os.source_conflicts.errors import SourceConflictError
from personal_os.source_conflicts.metrics import (
    ConflictCaptureOutcome,
    ConflictResolutionMetricOutcome,
    InMemorySourceConflictMetrics,
    SourceConflictOperation,
    SourceConflictRejectionReason,
)
from personal_os.source_conflicts.service import SourceConflictService

_NEWER_REMOTE_VERSION_ID = UUID("70000000-0000-4000-8000-000000000002")
_SERVICE_CLOCK_BASE = datetime(2026, 9, 2, 1, 2, 3, tzinfo=UTC)


def _sequenced_clock(reads: int = 6) -> SequencedUtcClock:
    return SequencedUtcClock(
        [_SERVICE_CLOCK_BASE + timedelta(seconds=index) for index in range(reads)]
    )


def _no_rejections_recorded(metrics: InMemorySourceConflictMetrics) -> bool:
    return all(
        metrics.rejection_count(operation, reason) == 0
        for operation in SourceConflictOperation
        for reason in SourceConflictRejectionReason
    )


async def _capture_open_conflict(
    service: SourceConflictService,
) -> UUID:
    conflict = await service.capture_conflict(build_capture_command(), build_diagnostic_context())
    return conflict.conflict_id


@pytest.mark.asyncio
async def test_capture_authorizes_policy_then_labels_replay_then_stores() -> None:
    """A fresh capture: policy guard first, replay lookup, then the store write."""

    ledger = CallLedger()
    store = InMemorySourceConflictStore(ledger=ledger)
    guard = FakeSourceConflictPolicyGuard(ledger=ledger)
    metrics = InMemorySourceConflictMetrics()
    command = build_capture_command()
    service = SourceConflictService(
        store=store, policy_guard=guard, metrics=metrics, clock=_sequenced_clock()
    )

    conflict = await service.capture_conflict(command, build_diagnostic_context())

    assert conflict.status is ConflictStatus.OPEN
    assert conflict.workspace_id == WORKSPACE_ID
    assert guard.authorized_captures == [command]
    assert ledger.entries == [
        POLICY_AUTHORIZE_CAPTURE,
        STORE_FIND_CAPTURED_CONFLICT,
        STORE_CAPTURE,
    ]
    assert metrics.capture_count(ConflictKind.STALE_CONTENT, ConflictCaptureOutcome.CAPTURED) == 1
    assert metrics.capture_count(ConflictKind.STALE_CONTENT, ConflictCaptureOutcome.REJECTED) == 0
    assert _no_rejections_recorded(metrics)


@pytest.mark.asyncio
async def test_exact_capture_replay_returns_stored_conflict_and_records_replayed() -> None:
    """A same-identity capture replay returns the stored conflict, labelled replayed."""

    ledger = CallLedger()
    store = InMemorySourceConflictStore(ledger=ledger)
    guard = FakeSourceConflictPolicyGuard(ledger=ledger)
    metrics = InMemorySourceConflictMetrics()
    command = build_capture_command()
    service = SourceConflictService(
        store=store, policy_guard=guard, metrics=metrics, clock=_sequenced_clock()
    )

    first = await service.capture_conflict(command, build_diagnostic_context())
    replay = await service.capture_conflict(command, build_diagnostic_context())

    assert replay.conflict_id == first.conflict_id
    assert replay == first
    assert metrics.capture_count(ConflictKind.STALE_CONTENT, ConflictCaptureOutcome.CAPTURED) == 1
    assert metrics.capture_count(ConflictKind.STALE_CONTENT, ConflictCaptureOutcome.REPLAYED) == 1
    assert store.publication_gateway.commands == []


@pytest.mark.asyncio
async def test_capture_policy_denial_records_rejected_outcome_and_propagates() -> None:
    """A policy denial keeps the rejected capture outcome; no store write happens."""

    ledger = CallLedger()
    store = InMemorySourceConflictStore(ledger=ledger)
    guard = FakeSourceConflictPolicyGuard(
        ledger=ledger,
        capture_error=ExclusionPolicyError(ErrorCode.EXCLUSION_POLICY_DENIED),
    )
    metrics = InMemorySourceConflictMetrics()
    service = SourceConflictService(
        store=store, policy_guard=guard, metrics=metrics, clock=_sequenced_clock()
    )

    with pytest.raises(ExclusionPolicyError) as excinfo:
        await service.capture_conflict(build_capture_command(), build_diagnostic_context())

    assert excinfo.value.error_code is ErrorCode.EXCLUSION_POLICY_DENIED
    assert ledger.entries == [POLICY_AUTHORIZE_CAPTURE]
    assert store.conflicts == {}
    assert metrics.capture_count(ConflictKind.STALE_CONTENT, ConflictCaptureOutcome.REJECTED) == 1
    assert _no_rejections_recorded(metrics)


@pytest.mark.asyncio
async def test_capture_store_rejection_records_closed_reason_and_propagates() -> None:
    """A typed store rejection records its closed reason and outcome, then re-raises."""

    ledger = CallLedger()
    store = InMemorySourceConflictStore(
        ledger=ledger,
        capture_error=SourceConflictError(ErrorCode.SOURCE_CONFLICT_INPUT_INVALID),
    )
    guard = FakeSourceConflictPolicyGuard(ledger=ledger)
    metrics = InMemorySourceConflictMetrics()
    service = SourceConflictService(
        store=store, policy_guard=guard, metrics=metrics, clock=_sequenced_clock()
    )

    with pytest.raises(SourceConflictError) as excinfo:
        await service.capture_conflict(build_capture_command(), build_diagnostic_context())

    assert excinfo.value.error_code is ErrorCode.SOURCE_CONFLICT_INPUT_INVALID
    assert (
        metrics.rejection_count(
            SourceConflictOperation.CAPTURE,
            SourceConflictRejectionReason.SOURCE_CONFLICT_INPUT_INVALID,
        )
        == 1
    )
    assert metrics.capture_count(ConflictKind.STALE_CONTENT, ConflictCaptureOutcome.REJECTED) == 1


@pytest.mark.asyncio
async def test_keep_remote_resolution_records_no_publication_command() -> None:
    """A keep_remote winner publishes nothing: the gateway receives no command."""

    ledger = CallLedger()
    store = InMemorySourceConflictStore(ledger=ledger)
    store.current_version_ids[(WORKSPACE_ID, SOURCE_ID)] = REMOTE_VERSION_ID
    guard = FakeSourceConflictPolicyGuard(ledger=ledger)
    metrics = InMemorySourceConflictMetrics()
    service = SourceConflictService(
        store=store, policy_guard=guard, metrics=metrics, clock=_sequenced_clock()
    )
    conflict_id = await _capture_open_conflict(service)
    command = build_resolve_command(conflict_id=conflict_id)

    result = await service.resolve_conflict(command, WORKSPACE_ID, build_diagnostic_context())

    assert result.kind is ConflictResolutionOutcome.RESOLVED
    assert result.resulting_version_id is None
    assert result.successor is None
    assert store.publication_gateway.commands == []
    assert (
        metrics.resolution_count(
            ConflictResolutionKind.KEEP_REMOTE, ConflictResolutionOutcome.RESOLVED
        )
        == 1
    )


@pytest.mark.asyncio
async def test_stale_resolution_keeps_predecessor_immutable_and_opens_successor() -> None:
    """A stale reviewed remote supersedes the conflict and opens the bound successor."""

    ledger = CallLedger()
    store = InMemorySourceConflictStore(ledger=ledger)
    store.current_version_ids[(WORKSPACE_ID, SOURCE_ID)] = _NEWER_REMOTE_VERSION_ID
    guard = FakeSourceConflictPolicyGuard(ledger=ledger)
    metrics = InMemorySourceConflictMetrics()
    service = SourceConflictService(
        store=store, policy_guard=guard, metrics=metrics, clock=_sequenced_clock()
    )
    conflict_id = await _capture_open_conflict(service)
    command = build_resolve_command(
        conflict_id=conflict_id,
        resolution_kind=ConflictResolutionKind.KEEP_LOCAL,
    )

    result = await service.resolve_conflict(command, WORKSPACE_ID, build_diagnostic_context())

    assert result.kind is ConflictResolutionOutcome.STALE_SUCCESSOR
    assert result.resulting_version_id is None
    assert result.successor is not None
    assert result.successor.observed_remote_version_id == _NEWER_REMOTE_VERSION_ID
    assert result.successor.status is ConflictStatus.OPEN
    predecessor = await store.read(conflict_id, WORKSPACE_ID, build_diagnostic_context())
    assert predecessor.status is ConflictStatus.SUPERSEDED
    assert predecessor.successor_conflict_id == result.successor.conflict_id
    assert predecessor.resulting_version_id is None
    assert predecessor.candidate == result.successor.candidate
    assert store.publication_gateway.commands == []
    assert (
        metrics.resolution_count(
            ConflictResolutionKind.KEEP_LOCAL, ConflictResolutionOutcome.STALE_SUCCESSOR
        )
        == 1
    )


@pytest.mark.asyncio
async def test_keep_local_resolution_publishes_exactly_one_version() -> None:
    """A keep_local winner commits exactly one resulting source version."""

    ledger = CallLedger()
    store = InMemorySourceConflictStore(ledger=ledger)
    store.current_version_ids[(WORKSPACE_ID, SOURCE_ID)] = REMOTE_VERSION_ID
    guard = FakeSourceConflictPolicyGuard(ledger=ledger)
    metrics = InMemorySourceConflictMetrics()
    service = SourceConflictService(
        store=store, policy_guard=guard, metrics=metrics, clock=_sequenced_clock()
    )
    conflict_id = await _capture_open_conflict(service)
    command = build_resolve_command(
        conflict_id=conflict_id,
        resolution_kind=ConflictResolutionKind.KEEP_LOCAL,
    )

    result = await service.resolve_conflict(command, WORKSPACE_ID, build_diagnostic_context())

    assert result.kind is ConflictResolutionOutcome.RESOLVED
    assert result.resulting_version_id is not None
    assert store.publication_gateway.commands == [PUBLISH_SOURCE_VERSION]
    closed = await store.read(conflict_id, WORKSPACE_ID, build_diagnostic_context())
    assert closed.status is ConflictStatus.RESOLVED
    assert closed.resulting_version_id == result.resulting_version_id
    assert (
        metrics.resolution_count(
            ConflictResolutionKind.KEEP_LOCAL, ConflictResolutionOutcome.RESOLVED
        )
        == 1
    )


@pytest.mark.asyncio
async def test_save_merged_resolution_publishes_the_verified_merged_object() -> None:
    """A save_merged winner publishes the command's verified merged object once."""

    ledger = CallLedger()
    store = InMemorySourceConflictStore(ledger=ledger)
    store.current_version_ids[(WORKSPACE_ID, SOURCE_ID)] = REMOTE_VERSION_ID
    guard = FakeSourceConflictPolicyGuard(ledger=ledger)
    metrics = InMemorySourceConflictMetrics()
    service = SourceConflictService(
        store=store, policy_guard=guard, metrics=metrics, clock=_sequenced_clock()
    )
    conflict_id = await _capture_open_conflict(service)
    command = build_resolve_command(
        conflict_id=conflict_id,
        resolution_kind=ConflictResolutionKind.SAVE_MERGED,
        verified_candidate_object_id=MERGED_OBJECT_ID,
    )

    result = await service.resolve_conflict(command, WORKSPACE_ID, build_diagnostic_context())

    assert result.kind is ConflictResolutionOutcome.RESOLVED
    assert result.resulting_version_id is not None
    assert len(store.publication_gateway.commands) == 1
    assert (
        metrics.resolution_count(
            ConflictResolutionKind.SAVE_MERGED, ConflictResolutionOutcome.RESOLVED
        )
        == 1
    )


@pytest.mark.asyncio
async def test_exact_resolution_replay_returns_frozen_result_without_republishing() -> None:
    """A same-identity resolution replay returns the frozen result, labelled replayed."""

    ledger = CallLedger()
    store = InMemorySourceConflictStore(ledger=ledger)
    store.current_version_ids[(WORKSPACE_ID, SOURCE_ID)] = REMOTE_VERSION_ID
    guard = FakeSourceConflictPolicyGuard(ledger=ledger)
    metrics = InMemorySourceConflictMetrics()
    service = SourceConflictService(
        store=store, policy_guard=guard, metrics=metrics, clock=_sequenced_clock()
    )
    conflict_id = await _capture_open_conflict(service)
    command = build_resolve_command(
        conflict_id=conflict_id,
        resolution_kind=ConflictResolutionKind.KEEP_LOCAL,
    )

    first = await service.resolve_conflict(command, WORKSPACE_ID, build_diagnostic_context())
    replay = await service.resolve_conflict(command, WORKSPACE_ID, build_diagnostic_context())

    assert replay == first
    assert store.publication_gateway.commands == [PUBLISH_SOURCE_VERSION]
    assert (
        metrics.resolution_count(
            ConflictResolutionKind.KEEP_LOCAL, ConflictResolutionMetricOutcome.REPLAYED
        )
        == 1
    )
    assert (
        metrics.resolution_count(
            ConflictResolutionKind.KEEP_LOCAL, ConflictResolutionOutcome.RESOLVED
        )
        == 1
    )


@pytest.mark.asyncio
async def test_stale_resolution_replay_returns_frozen_stale_outcome() -> None:
    """A replay of a stale resolution returns the same successor, no new writes."""

    ledger = CallLedger()
    store = InMemorySourceConflictStore(ledger=ledger)
    store.current_version_ids[(WORKSPACE_ID, SOURCE_ID)] = _NEWER_REMOTE_VERSION_ID
    guard = FakeSourceConflictPolicyGuard(ledger=ledger)
    metrics = InMemorySourceConflictMetrics()
    service = SourceConflictService(
        store=store, policy_guard=guard, metrics=metrics, clock=_sequenced_clock()
    )
    conflict_id = await _capture_open_conflict(service)
    command = build_resolve_command(
        conflict_id=conflict_id,
        resolution_kind=ConflictResolutionKind.KEEP_LOCAL,
    )

    first = await service.resolve_conflict(command, WORKSPACE_ID, build_diagnostic_context())
    replay = await service.resolve_conflict(command, WORKSPACE_ID, build_diagnostic_context())

    assert replay.kind is ConflictResolutionOutcome.STALE_SUCCESSOR
    assert replay.successor is not None
    assert replay.successor.conflict_id == first.successor.conflict_id
    assert (
        metrics.resolution_count(
            ConflictResolutionKind.KEEP_LOCAL, ConflictResolutionMetricOutcome.REPLAYED
        )
        == 1
    )
    assert (
        metrics.resolution_count(
            ConflictResolutionKind.KEEP_LOCAL, ConflictResolutionOutcome.STALE_SUCCESSOR
        )
        == 1
    )


@pytest.mark.asyncio
async def test_resolution_reads_then_authorizes_then_transacts() -> None:
    """The resolve order: locked read, policy recheck over that read, transaction."""

    ledger = CallLedger()
    store = InMemorySourceConflictStore(ledger=ledger)
    store.current_version_ids[(WORKSPACE_ID, SOURCE_ID)] = REMOTE_VERSION_ID
    guard = FakeSourceConflictPolicyGuard(ledger=ledger)
    metrics = InMemorySourceConflictMetrics()
    service = SourceConflictService(
        store=store, policy_guard=guard, metrics=metrics, clock=_sequenced_clock()
    )
    conflict_id = await _capture_open_conflict(service)
    command = build_resolve_command(conflict_id=conflict_id)

    await service.resolve_conflict(command, WORKSPACE_ID, build_diagnostic_context())

    assert ledger.entries == [
        POLICY_AUTHORIZE_CAPTURE,
        STORE_FIND_CAPTURED_CONFLICT,
        STORE_CAPTURE,
        STORE_READ_FOR_RESOLUTION,
        POLICY_AUTHORIZE_RESOLUTION,
        STORE_RESOLVE,
    ]
    assert len(guard.authorized_resolutions) == 1
    assert guard.authorized_resolutions[0].conflict_id == conflict_id


@pytest.mark.asyncio
async def test_resolution_policy_denial_records_rejected_outcome_and_propagates() -> None:
    """A policy denial on resolution leaves the conflict open and records rejected."""

    ledger = CallLedger()
    store = InMemorySourceConflictStore(ledger=ledger)
    store.current_version_ids[(WORKSPACE_ID, SOURCE_ID)] = REMOTE_VERSION_ID
    guard = FakeSourceConflictPolicyGuard(
        ledger=ledger,
        resolution_error=ExclusionPolicyError(ErrorCode.EXCLUSION_POLICY_DENIED),
    )
    metrics = InMemorySourceConflictMetrics()
    service = SourceConflictService(
        store=store, policy_guard=guard, metrics=metrics, clock=_sequenced_clock()
    )
    conflict_id = await _capture_open_conflict(service)
    command = build_resolve_command(conflict_id=conflict_id)

    with pytest.raises(ExclusionPolicyError) as excinfo:
        await service.resolve_conflict(command, WORKSPACE_ID, build_diagnostic_context())

    assert excinfo.value.error_code is ErrorCode.EXCLUSION_POLICY_DENIED
    assert STORE_RESOLVE not in ledger.entries
    still_open = await store.read(conflict_id, WORKSPACE_ID, build_diagnostic_context())
    assert still_open.status is ConflictStatus.OPEN
    assert (
        metrics.resolution_count(
            ConflictResolutionKind.KEEP_REMOTE, ConflictResolutionMetricOutcome.REJECTED
        )
        == 1
    )
    assert _no_rejections_recorded(metrics)


@pytest.mark.asyncio
async def test_resolve_unknown_conflict_records_not_found_rejection() -> None:
    """An unknown conflict id rejects closed with the typed not-found reason."""

    ledger = CallLedger()
    store = InMemorySourceConflictStore(ledger=ledger)
    guard = FakeSourceConflictPolicyGuard(ledger=ledger)
    metrics = InMemorySourceConflictMetrics()
    service = SourceConflictService(
        store=store, policy_guard=guard, metrics=metrics, clock=_sequenced_clock()
    )
    command = build_resolve_command()

    with pytest.raises(SourceConflictError) as excinfo:
        await service.resolve_conflict(command, WORKSPACE_ID, build_diagnostic_context())

    assert excinfo.value.error_code is ErrorCode.SOURCE_CONFLICT_NOT_FOUND
    assert (
        metrics.rejection_count(
            SourceConflictOperation.RESOLVE,
            SourceConflictRejectionReason.SOURCE_CONFLICT_NOT_FOUND,
        )
        == 1
    )
    assert (
        metrics.resolution_count(
            ConflictResolutionKind.KEEP_REMOTE, ConflictResolutionMetricOutcome.REJECTED
        )
        == 1
    )


@pytest.mark.asyncio
async def test_resolve_against_terminal_conflict_records_state_invalid() -> None:
    """A fresh resolution event against a terminal conflict rejects state-invalid."""

    ledger = CallLedger()
    store = InMemorySourceConflictStore(ledger=ledger)
    store.current_version_ids[(WORKSPACE_ID, SOURCE_ID)] = REMOTE_VERSION_ID
    guard = FakeSourceConflictPolicyGuard(ledger=ledger)
    metrics = InMemorySourceConflictMetrics()
    service = SourceConflictService(
        store=store, policy_guard=guard, metrics=metrics, clock=_sequenced_clock(8)
    )
    conflict_id = await _capture_open_conflict(service)
    closed = await service.resolve_conflict(
        build_resolve_command(conflict_id=conflict_id),
        WORKSPACE_ID,
        build_diagnostic_context(),
    )
    assert closed.kind is ConflictResolutionOutcome.RESOLVED

    second_event_id, second_key = fresh_resolution_identities()
    with pytest.raises(SourceConflictError) as excinfo:
        await service.resolve_conflict(
            build_resolve_command(
                conflict_id=conflict_id,
                resolution_event_id=second_event_id,
                idempotency_key=second_key,
            ),
            WORKSPACE_ID,
            build_diagnostic_context(),
        )

    assert excinfo.value.error_code is ErrorCode.SOURCE_CONFLICT_STATE_INVALID
    assert (
        metrics.rejection_count(
            SourceConflictOperation.RESOLVE,
            SourceConflictRejectionReason.SOURCE_CONFLICT_STATE_INVALID,
        )
        == 1
    )


@pytest.mark.asyncio
async def test_resolution_workspace_scope_is_threaded_to_every_store_call() -> None:
    """Every resolution store call receives the credential-derived workspace."""

    ledger = CallLedger()
    store = InMemorySourceConflictStore(ledger=ledger)
    guard = FakeSourceConflictPolicyGuard(ledger=ledger)
    metrics = InMemorySourceConflictMetrics()
    service = SourceConflictService(
        store=store, policy_guard=guard, metrics=metrics, clock=_sequenced_clock()
    )
    conflict_id = await _capture_open_conflict(service)
    command = build_resolve_command(conflict_id=conflict_id)

    with pytest.raises(SourceConflictError) as excinfo:
        await service.resolve_conflict(command, OTHER_WORKSPACE_ID, build_diagnostic_context())

    assert excinfo.value.error_code is ErrorCode.SOURCE_CONFLICT_NOT_FOUND
    resolution_scopes = [
        workspace_id
        for operation, workspace_id in store.observed_workspace_scopes
        if operation in {"read_for_resolution", "resolve"}
    ]
    # The scoped read rejects before any transaction opens; the resolve
    # transaction itself received the same credential-derived workspace on
    # every successful path (pinned by the order test's ledger).
    assert resolution_scopes == [OTHER_WORKSPACE_ID]
    still_open = await store.read(conflict_id, WORKSPACE_ID, build_diagnostic_context())
    assert still_open.status is ConflictStatus.OPEN
