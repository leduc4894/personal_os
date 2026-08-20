"""Projection-intent lease lifecycle against the real migrated baseline.

Every case proves one fenced lease invariant from design section 11: claims
run ``FOR UPDATE SKIP LOCKED`` in the pinned order and batch bound, commit
before returning, and never hand one intent to two claimers; expired-lease
reclaim increments the attempt count, records the closed lease-expired error
code and applies the bounded backoff; and the acknowledgement, retry and
terminal transitions affect a row only under the exact intent ID, leased
status and lease token, leaving a stale token's row untouched while the
registered stale-lease diagnostic is emitted.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import event
from tests.integration.projection_dispatch.conftest import (
    ProjectionDispatchHarness,
    SeededIntent,
    SeededWorkspace,
)

from personal_os.diagnostics.events import EventName, SafeToken
from personal_os.error_contracts.codes import ErrorCode
from personal_os.sources.metrics import ProjectionKind
from personal_os.sources.projection_dispatch import (
    LEASE_EXPIRED_ERROR_CODE,
    PROJECTION_CLAIM_BATCH_LIMIT,
    LeasedProjectionIntent,
    projection_retry_backoff_seconds,
)

pytestmark = pytest.mark.local_stack


async def _seed_claimed_intent(
    harness: ProjectionDispatchHarness,
    workspace: SeededWorkspace,
    *,
    attempt_count: int = 0,
) -> tuple[SeededIntent, LeasedProjectionIntent]:
    seeded = await harness.seed_due_intent(workspace, attempt_count=attempt_count)
    now = await harness.database_now()
    claimed = await harness.store.claim_batch(now, PROJECTION_CLAIM_BATCH_LIMIT)
    matching = [
        intent for intent in claimed if intent.projection_intent_id == seeded.projection_intent_id
    ]
    assert len(matching) == 1
    return seeded, matching[0]


@pytest.mark.asyncio
async def test_claim_leases_due_intents_in_pinned_order(
    projection_dispatch_harness: ProjectionDispatchHarness,
) -> None:
    workspace = await projection_dispatch_harness.seed_workspace()
    newest = await projection_dispatch_harness.seed_due_intent(
        workspace, available_at_sql="CURRENT_TIMESTAMP - interval '5 seconds'"
    )
    oldest = await projection_dispatch_harness.seed_due_intent(
        workspace, available_at_sql="CURRENT_TIMESTAMP - interval '30 seconds'"
    )
    middle = await projection_dispatch_harness.seed_due_intent(
        workspace,
        projection_kind="neo4j",
        available_at_sql="CURRENT_TIMESTAMP - interval '15 seconds'",
    )
    now = await projection_dispatch_harness.database_now()

    claimed = await projection_dispatch_harness.store.claim_batch(now, PROJECTION_CLAIM_BATCH_LIMIT)

    assert tuple(intent.projection_intent_id for intent in claimed) == (
        oldest.projection_intent_id,
        middle.projection_intent_id,
        newest.projection_intent_id,
    )
    for intent in claimed:
        row = await projection_dispatch_harness.fetch_intent(intent.projection_intent_id)
        assert row["status"] == "leased"
        assert row["lease_token"] == intent.lease_token
        assert row["leased_until"] == intent.leased_until
        assert row["leased_until"] - row["updated_at"] == timedelta(seconds=60)
        assert row["attempt_count"] == intent.attempt_count
        # The claim itself is not a dispatch outcome: the attempt stays.
        assert row["dispatched_at"] is None


@pytest.mark.asyncio
async def test_claim_breaks_ties_by_identity(
    projection_dispatch_harness: ProjectionDispatchHarness,
) -> None:
    workspace = await projection_dispatch_harness.seed_workspace()
    # One fixed literal timestamp for both rows: transaction clocks would
    # differ per seed transaction and defeat the identity tie-break.
    same_time = "TIMESTAMPTZ '2026-08-14 00:00:00+00'"
    first = await projection_dispatch_harness.seed_due_intent(
        workspace, available_at_sql=same_time, created_at_sql=same_time
    )
    second = await projection_dispatch_harness.seed_due_intent(
        workspace,
        projection_kind="neo4j",
        available_at_sql=same_time,
        created_at_sql=same_time,
    )
    expected_order = sorted((first.projection_intent_id, second.projection_intent_id))
    now = await projection_dispatch_harness.database_now()

    claimed = await projection_dispatch_harness.store.claim_batch(now, PROJECTION_CLAIM_BATCH_LIMIT)

    assert tuple(intent.projection_intent_id for intent in claimed) == tuple(expected_order)


@pytest.mark.asyncio
async def test_claim_skips_rows_not_yet_available(
    projection_dispatch_harness: ProjectionDispatchHarness,
) -> None:
    workspace = await projection_dispatch_harness.seed_workspace()
    due = await projection_dispatch_harness.seed_due_intent(workspace)
    not_yet = await projection_dispatch_harness.seed_due_intent(
        workspace,
        projection_kind="neo4j",
        available_at_sql="CURRENT_TIMESTAMP + interval '1 hour'",
    )
    now = await projection_dispatch_harness.database_now()

    claimed = await projection_dispatch_harness.store.claim_batch(now, PROJECTION_CLAIM_BATCH_LIMIT)

    assert [intent.projection_intent_id for intent in claimed] == [due.projection_intent_id]
    unavailable = await projection_dispatch_harness.fetch_intent(not_yet.projection_intent_id)
    assert unavailable["status"] == "pending"
    assert unavailable["lease_token"] is None


@pytest.mark.asyncio
async def test_claim_respects_the_batch_limit(
    projection_dispatch_harness: ProjectionDispatchHarness,
) -> None:
    workspace = await projection_dispatch_harness.seed_workspace()
    for _ in range(3):
        await projection_dispatch_harness.seed_due_intent(workspace)
    now = await projection_dispatch_harness.database_now()

    claimed = await projection_dispatch_harness.store.claim_batch(now, 2)
    remaining = await projection_dispatch_harness.store.claim_batch(now, 2)

    assert len(claimed) == 2
    assert len(remaining) == 1
    claimed_ids = {intent.projection_intent_id for intent in claimed}
    assert claimed_ids.isdisjoint({intent.projection_intent_id for intent in remaining})


@pytest.mark.asyncio
async def test_claim_commits_before_returning(
    projection_dispatch_harness: ProjectionDispatchHarness,
) -> None:
    workspace = await projection_dispatch_harness.seed_workspace()
    seeded = await projection_dispatch_harness.seed_due_intent(workspace)
    now = await projection_dispatch_harness.database_now()

    claimed = await projection_dispatch_harness.store.claim_batch(now, PROJECTION_CLAIM_BATCH_LIMIT)

    assert len(claimed) == 1
    # A fresh connection observes the committed lease: the claim transaction
    # ended before any caller could perform network I/O.
    row = await projection_dispatch_harness.fetch_intent(seeded.projection_intent_id)
    assert row["status"] == "leased"
    assert row["lease_token"] == claimed[0].lease_token


@pytest.mark.asyncio
async def test_concurrent_claimers_never_own_one_intent(
    projection_dispatch_harness: ProjectionDispatchHarness,
) -> None:
    workspace = await projection_dispatch_harness.seed_workspace()
    seeded_ids = set()
    for _ in range(6):
        seeded = await projection_dispatch_harness.seed_due_intent(workspace)
        seeded_ids.add(seeded.projection_intent_id)
    now = await projection_dispatch_harness.database_now()
    competitor = projection_dispatch_harness.competing_store()

    first, second = await asyncio.gather(
        projection_dispatch_harness.store.claim_batch(now, PROJECTION_CLAIM_BATCH_LIMIT),
        competitor.claim_batch(now, PROJECTION_CLAIM_BATCH_LIMIT),
    )

    first_ids = {intent.projection_intent_id for intent in first}
    second_ids = {intent.projection_intent_id for intent in second}
    assert first_ids.isdisjoint(second_ids), "two claimers must never own one intent"
    assert first_ids | second_ids == seeded_ids


@pytest.mark.asyncio
async def test_acknowledge_with_exact_token_marks_dispatched(
    projection_dispatch_harness: ProjectionDispatchHarness,
) -> None:
    workspace = await projection_dispatch_harness.seed_workspace()
    seeded, intent = await _seed_claimed_intent(projection_dispatch_harness, workspace)
    now = await projection_dispatch_harness.database_now()

    acknowledged = await projection_dispatch_harness.store.acknowledge_dispatched(
        intent.projection_intent_id, intent.lease_token, now
    )

    assert acknowledged is True
    row = await projection_dispatch_harness.fetch_intent(seeded.projection_intent_id)
    assert row["status"] == "dispatched"
    assert row["attempt_count"] == 1
    assert row["lease_token"] is None
    assert row["leased_until"] is None
    assert row["dispatched_at"] is not None
    assert row["last_error_code"] is None


@pytest.mark.asyncio
async def test_stale_token_affects_zero_rows_and_emits_diagnostic(
    projection_dispatch_harness: ProjectionDispatchHarness,
) -> None:
    workspace = await projection_dispatch_harness.seed_workspace()
    seeded, intent = await _seed_claimed_intent(projection_dispatch_harness, workspace)
    await projection_dispatch_harness.expire_lease(seeded.projection_intent_id)
    reclaim_now = (await projection_dispatch_harness.database_now()) + timedelta(seconds=10)
    reclaimed = await projection_dispatch_harness.store.reclaim_expired(reclaim_now)
    assert reclaimed == 1

    acknowledged = await projection_dispatch_harness.store.acknowledge_dispatched(
        intent.projection_intent_id, intent.lease_token, reclaim_now
    )

    assert acknowledged is False
    row = await projection_dispatch_harness.fetch_intent(seeded.projection_intent_id)
    # The stale holder's acknowledgement overwrote nothing: the reclaimed
    # pending state (attempt incremented by the expiry) is intact.
    assert row["status"] == "pending"
    assert row["attempt_count"] == 1
    assert row["lease_token"] is None
    assert row["dispatched_at"] is None
    assert row["last_error_code"] == LEASE_EXPIRED_ERROR_CODE.value
    stale_events = projection_dispatch_harness.diagnostics.of(
        EventName.PROJECTION_INTENT_DISPATCH_FAILED
    )
    assert len(stale_events) == 1
    assert stale_events[0]["intent_id"] == seeded.projection_intent_id
    assert stale_events[0]["error_code"] == LEASE_EXPIRED_ERROR_CODE


@pytest.mark.asyncio
async def test_expired_but_unreclaimed_wrong_token_reports_lease_expired(
    projection_dispatch_harness: ProjectionDispatchHarness,
) -> None:
    workspace = await projection_dispatch_harness.seed_workspace()
    seeded, intent = await _seed_claimed_intent(projection_dispatch_harness, workspace)
    await projection_dispatch_harness.expire_lease(seeded.projection_intent_id)
    now = await projection_dispatch_harness.database_now()

    released = await projection_dispatch_harness.store.release_retry(
        seeded.projection_intent_id,
        uuid4(),
        SafeToken.parse("projection_dispatch_unavailable"),
        now + timedelta(seconds=2),
        now,
    )

    assert released is False
    row = await projection_dispatch_harness.fetch_intent(seeded.projection_intent_id)
    assert row["status"] == "leased"
    assert row["lease_token"] == intent.lease_token
    stale_events = projection_dispatch_harness.diagnostics.of(
        EventName.PROJECTION_INTENT_DISPATCH_FAILED
    )
    assert len(stale_events) == 1
    assert stale_events[0]["intent_id"] == seeded.projection_intent_id
    assert stale_events[0]["error_code"] == LEASE_EXPIRED_ERROR_CODE


@pytest.mark.asyncio
async def test_reclaim_expired_increments_attempt_and_applies_backoff(
    projection_dispatch_harness: ProjectionDispatchHarness,
) -> None:
    workspace = await projection_dispatch_harness.seed_workspace()
    seeded, _ = await _seed_claimed_intent(projection_dispatch_harness, workspace)
    await projection_dispatch_harness.expire_lease(seeded.projection_intent_id)
    now = (await projection_dispatch_harness.database_now()) + timedelta(seconds=10)

    reclaimed = await projection_dispatch_harness.store.reclaim_expired(now)

    assert reclaimed == 1
    row = await projection_dispatch_harness.fetch_intent(seeded.projection_intent_id)
    assert row["status"] == "pending"
    assert row["attempt_count"] == 1
    assert row["lease_token"] is None
    assert row["leased_until"] is None
    assert row["last_error_code"] == LEASE_EXPIRED_ERROR_CODE.value
    expected_delay = timedelta(
        seconds=projection_retry_backoff_seconds(int(row["attempt_count"]) - 1)
    )
    assert row["available_at"] - row["updated_at"] == expected_delay
    assert projection_dispatch_harness.diagnostics.of(
        EventName.PROJECTION_INTENT_LEASE_RECLAIMED
    ) == [{"projection_kind": SafeToken.parse("qdrant"), "count": 1}]
    assert projection_dispatch_harness.metrics.lease_reclaimed_count(ProjectionKind.QDRANT) == 1
    assert projection_dispatch_harness.metrics.lease_reclaimed_count(ProjectionKind.NEO4J) == 0


@pytest.mark.asyncio
async def test_reclaim_backoff_doubles_with_the_prior_attempt_count(
    projection_dispatch_harness: ProjectionDispatchHarness,
) -> None:
    workspace = await projection_dispatch_harness.seed_workspace()
    seeded, _ = await _seed_claimed_intent(projection_dispatch_harness, workspace, attempt_count=3)
    await projection_dispatch_harness.expire_lease(seeded.projection_intent_id)
    now = (await projection_dispatch_harness.database_now()) + timedelta(seconds=10)

    reclaimed = await projection_dispatch_harness.store.reclaim_expired(now)

    assert reclaimed == 1
    row = await projection_dispatch_harness.fetch_intent(seeded.projection_intent_id)
    assert row["attempt_count"] == 4
    assert row["available_at"] - row["updated_at"] == timedelta(
        seconds=projection_retry_backoff_seconds(3)
    )


@pytest.mark.asyncio
async def test_unexpired_leases_are_not_reclaimed(
    projection_dispatch_harness: ProjectionDispatchHarness,
) -> None:
    workspace = await projection_dispatch_harness.seed_workspace()
    seeded, intent = await _seed_claimed_intent(projection_dispatch_harness, workspace)
    now = await projection_dispatch_harness.database_now()

    reclaimed = await projection_dispatch_harness.store.reclaim_expired(now)

    assert reclaimed == 0
    row = await projection_dispatch_harness.fetch_intent(seeded.projection_intent_id)
    assert row["status"] == "leased"
    assert row["lease_token"] == intent.lease_token
    assert (
        projection_dispatch_harness.diagnostics.of(EventName.PROJECTION_INTENT_LEASE_RECLAIMED)
        == []
    )


@pytest.mark.asyncio
async def test_release_retry_returns_to_pending_with_bounded_availability(
    projection_dispatch_harness: ProjectionDispatchHarness,
) -> None:
    workspace = await projection_dispatch_harness.seed_workspace()
    seeded, intent = await _seed_claimed_intent(projection_dispatch_harness, workspace)
    now = await projection_dispatch_harness.database_now()
    available_at = now + timedelta(seconds=projection_retry_backoff_seconds(intent.attempt_count))

    released = await projection_dispatch_harness.store.release_retry(
        seeded.projection_intent_id,
        intent.lease_token,
        SafeToken.parse("projection_dispatch_unavailable"),
        available_at,
        now,
    )

    assert released is True
    row = await projection_dispatch_harness.fetch_intent(seeded.projection_intent_id)
    assert row["status"] == "pending"
    assert row["attempt_count"] == 1
    assert row["lease_token"] is None
    assert row["leased_until"] is None
    assert row["dispatched_at"] is None
    assert row["last_error_code"] == "projection_dispatch_unavailable"
    assert row["available_at"] == available_at
    # The retried intent is claimable again only after its bounded delay.
    immediate = await projection_dispatch_harness.store.claim_batch(
        now, PROJECTION_CLAIM_BATCH_LIMIT
    )
    assert immediate == ()
    later = await projection_dispatch_harness.store.claim_batch(
        available_at + timedelta(seconds=1), PROJECTION_CLAIM_BATCH_LIMIT
    )
    assert [leased_intent.projection_intent_id for leased_intent in later] == [
        seeded.projection_intent_id
    ]


@pytest.mark.asyncio
async def test_mark_terminal_keeps_the_terminal_error_code(
    projection_dispatch_harness: ProjectionDispatchHarness,
) -> None:
    workspace = await projection_dispatch_harness.seed_workspace()
    seeded, intent = await _seed_claimed_intent(projection_dispatch_harness, workspace)
    now = await projection_dispatch_harness.database_now()

    terminated = await projection_dispatch_harness.store.mark_terminal(
        seeded.projection_intent_id,
        intent.lease_token,
        SafeToken.parse("projection_intent_contract_invalid"),
        now,
    )

    assert terminated is True
    row = await projection_dispatch_harness.fetch_intent(seeded.projection_intent_id)
    assert row["status"] == "terminal"
    assert row["attempt_count"] == 1
    assert row["lease_token"] is None
    assert row["leased_until"] is None
    assert row["last_error_code"] == "projection_intent_contract_invalid"
    # A terminal intent is never claimable again.
    claimable = await projection_dispatch_harness.store.claim_batch(
        now, PROJECTION_CLAIM_BATCH_LIMIT
    )
    assert claimable == ()


@pytest.mark.asyncio
async def test_stale_release_and_terminal_tokens_affect_zero_rows(
    projection_dispatch_harness: ProjectionDispatchHarness,
) -> None:
    workspace = await projection_dispatch_harness.seed_workspace()
    seeded, intent = await _seed_claimed_intent(projection_dispatch_harness, workspace)
    stale_token = uuid4()
    now = await projection_dispatch_harness.database_now()

    released = await projection_dispatch_harness.store.release_retry(
        seeded.projection_intent_id,
        stale_token,
        SafeToken.parse("projection_dispatch_unavailable"),
        now + timedelta(seconds=2),
        now,
    )
    terminated = await projection_dispatch_harness.store.mark_terminal(
        seeded.projection_intent_id,
        stale_token,
        SafeToken.parse("projection_intent_contract_invalid"),
        now,
    )

    assert released is False
    assert terminated is False
    row = await projection_dispatch_harness.fetch_intent(seeded.projection_intent_id)
    assert row["status"] == "leased"
    assert row["lease_token"] == intent.lease_token
    assert row["attempt_count"] == 0
    stale_events = projection_dispatch_harness.diagnostics.of(
        EventName.PROJECTION_INTENT_DISPATCH_FAILED
    )
    assert len(stale_events) == 2
    assert {event["intent_id"] for event in stale_events} == {seeded.projection_intent_id}
    assert {event["error_code"] for event in stale_events} == {
        SafeToken.parse(ErrorCode.PROJECTION_INTENT_CONTRACT_INVALID.value)
    }


@pytest.mark.asyncio
async def test_stale_lease_diagnostic_emits_after_guarded_transaction_commits(
    projection_dispatch_harness: ProjectionDispatchHarness,
) -> None:
    workspace = await projection_dispatch_harness.seed_workspace()
    seeded, _intent = await _seed_claimed_intent(projection_dispatch_harness, workspace)
    commit_events: list[None] = []
    emitted_after_commit: list[bool] = []
    original_emit = projection_dispatch_harness.diagnostics.emit

    def record_commit(connection: object) -> None:
        del connection
        commit_events.append(None)

    def record_emit(event_name: EventName, fields: dict[str, object] | None = None) -> None:
        emitted_after_commit.append(bool(commit_events))
        original_emit(event_name, fields)

    event.listen(projection_dispatch_harness._engine.sync_engine, "commit", record_commit)
    projection_dispatch_harness.diagnostics.emit = record_emit
    try:
        released = await projection_dispatch_harness.store.release_retry(
            seeded.projection_intent_id,
            uuid4(),
            SafeToken.parse("projection_dispatch_unavailable"),
            await projection_dispatch_harness.database_now() + timedelta(seconds=2),
            await projection_dispatch_harness.database_now(),
        )
    finally:
        projection_dispatch_harness.diagnostics.emit = original_emit
        event.remove(projection_dispatch_harness._engine.sync_engine, "commit", record_commit)

    assert released is False
    assert emitted_after_commit == [True]


@pytest.mark.asyncio
async def test_second_reclaim_within_the_backoff_window_finds_nothing(
    projection_dispatch_harness: ProjectionDispatchHarness,
) -> None:
    workspace = await projection_dispatch_harness.seed_workspace()
    seeded, _ = await _seed_claimed_intent(projection_dispatch_harness, workspace)
    await projection_dispatch_harness.expire_lease(seeded.projection_intent_id)
    first_now = (await projection_dispatch_harness.database_now()) + timedelta(seconds=10)
    assert await projection_dispatch_harness.store.reclaim_expired(first_now) == 1

    assert await projection_dispatch_harness.store.reclaim_expired(first_now) == 0
    row = await projection_dispatch_harness.fetch_intent(seeded.projection_intent_id)
    assert row["status"] == "pending"
    assert row["attempt_count"] == 1
