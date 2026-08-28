"""Disposable PostgreSQL transaction coverage for multipart session state.

Live coverage of the durable semantics the unit seam cannot prove: one
frozen operation resolves exactly one session for its whole lifetime — a
sequential exact replay and a concurrent reservation storm converge on the
same row with no second provider workload; the finite completion lease
serializes completion (one claimant wins, the concurrent callers observe
the closed in-progress token) and fences every terminal write (a lease
that expired and was reclaimed can never land the old claimant's result,
while the replacement claimant and the committed replay can); part facts
land only when they match the exact session geometry, replaying the same
provider observation is idempotent and a conflicting observation fails
closed; ownership and the 24-hour deadline fail closed at load; the expiry
sweep strikes overdue forward sessions into the cleanup obligation, the
cleanup lease fences stale workers, a failed cleanup persists its closed
reason with an exact bounded next retry, and a succeeded cleanup freezes
the terminal cleaned shape; and the four hot statements address the
shipped indexes (unique session/operation lookups, the partial expiry
sweep and the partial cleanup claim) without sequentially scanning a
populated session table.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import timedelta
from typing import Any, Final
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncConnection
from tests.integration.multipart_upload.conftest import (
    SEEDED_MULTIPART_FINAL_PART_BYTES,
    MultipartStoreHarness,
    MutableUtcClock,
    SeededMultipartOperation,
    diagnostic_context,
)

from personal_os.error_contracts.codes import ErrorCode
from personal_os.multipart_upload.contracts import (
    MULTIPART_PART_SIZE_BYTES,
    MultipartSessionState,
    MultipartUploadSessionId,
)
from personal_os.multipart_upload.errors import MultipartUploadError
from personal_os.multipart_upload.ports import (
    MultipartProviderPartETag,
    MultipartSessionClaim,
    MultipartSessionRecord,
)
from personal_os.small_file_sync.contracts import (
    SmallFileDeviceContext,
    SmallFileTerminalResult,
    SmallFileTerminalResultKind,
)
from postgresql_source_store.multipart_upload_store import (
    MULTIPART_CLEANUP_RETRY_BASE_SECONDS,
    MULTIPART_COMPLETION_LEASE_SECONDS,
    cleanup_claim_select_statement,
    expiry_sweep_select_statement,
    multipart_operation_select_statement,
    multipart_session_select_statement,
)
from postgresql_source_store.tables import (
    multipart_uploads,
    small_file_upload_operations,
)

pytestmark = pytest.mark.local_stack

#: Population bound of the query-plan fixture and the rare slice of rows
#: each partial index predicate matches.
_POPULATED_SESSION_MINIMUM: Final[int] = 1_500
_EXPIRED_FORWARD_ROWS: Final[int] = 24
_CLEANUP_DUE_ROWS: Final[int] = 30

#: Every index the shipped ``20260828_01`` migration created, including its
#: unique constraints' backing indexes.
_APPROVED_INDEX_NAMES: Final[frozenset[str]] = frozenset(
    {
        "pk_multipart_uploads",
        "uq_multipart_uploads__session_id",
        "uq_multipart_uploads__operation",
        "ix_multipart_uploads__workspace_state",
        "ix_multipart_uploads__expiry_sweep",
        "ix_multipart_uploads__cleanup_claim",
    }
)


def _clock_of(harness: MultipartStoreHarness) -> MutableUtcClock:
    return harness.clock


def _now(harness: MultipartStoreHarness) -> Any:
    return harness.clock()


def _terminal_result(clock: MutableUtcClock) -> SmallFileTerminalResult:
    return SmallFileTerminalResult(
        result_kind=SmallFileTerminalResultKind.COMMITTED,
        source_id=uuid4(),
        source_version_id=uuid4(),
        content_version=1,
        committed_at=clock(),
    )


async def _seed_and_reserve(
    harness: MultipartStoreHarness,
) -> tuple[MultipartSessionRecord, SeededMultipartOperation, SmallFileDeviceContext]:
    device_context = await harness.seed_device()
    seeded = await harness.seed_operation(device_context, now=_now(harness))
    record = await harness.reserve(seeded, device_context)
    return record, seeded, device_context


class TestSessionReservationReplay:
    @pytest.mark.asyncio
    async def test_same_operation_replays_one_session_without_new_provider_work(
        self, multipart_store_harness: MultipartStoreHarness
    ) -> None:
        harness = multipart_store_harness
        device_context = await harness.seed_device()
        seeded = await harness.seed_operation(device_context, now=_now(harness))

        first = await harness.reserve(seeded, device_context)
        replay = await harness.reserve(seeded, device_context)

        assert replay.session_id == first.session_id
        assert first.state is MultipartSessionState.CREATED
        assert await harness.session_count(device_context.workspace_id) == 1

    @pytest.mark.asyncio
    async def test_concurrent_reservations_converge_on_one_session_row(
        self, multipart_store_harness: MultipartStoreHarness
    ) -> None:
        harness = multipart_store_harness
        device_context = await harness.seed_device()
        seeded = await harness.seed_operation(device_context, now=_now(harness))

        records = await asyncio.gather(
            *(harness.reserve(seeded, device_context) for _ in range(6))
        )

        assert len({record.session_id.value for record in records}) == 1
        assert await harness.session_count(device_context.workspace_id) == 1

    @pytest.mark.asyncio
    async def test_reserve_replay_of_committed_session_returns_frozen_result(
        self, multipart_store_harness: MultipartStoreHarness
    ) -> None:
        harness = multipart_store_harness
        record, seeded, device_context = await _seed_and_reserve(harness)
        claim = await harness.store.claim_completion(
            session_id=record.session_id,
            device_context=device_context,
            diagnostic_context=diagnostic_context(),
        )
        terminal_result = _terminal_result(_clock_of(harness))
        await harness.store.record_terminal_result(
            claim=claim,
            result=terminal_result,
            diagnostic_context=diagnostic_context(),
        )

        replay = await harness.reserve(seeded, device_context)

        assert replay.session_id == record.session_id
        assert replay.state is MultipartSessionState.COMMITTED
        assert replay.terminal_result == terminal_result
        assert await harness.session_count(device_context.workspace_id) == 1

    @pytest.mark.asyncio
    async def test_reserve_rejects_foreign_device_identity(
        self, multipart_store_harness: MultipartStoreHarness
    ) -> None:
        harness = multipart_store_harness
        device_context = await harness.seed_device()
        seeded = await harness.seed_operation(device_context, now=_now(harness))
        foreign_device = await harness.seed_foreign_device(device_context)

        with pytest.raises(MultipartUploadError, match="multipart_session_state_invalid"):
            await harness.store.reserve_session(
                operation=seeded.operation,
                staging_key=seeded.staging_key,
                provider_upload_id=seeded.provider_upload_id,
                device_context=foreign_device,
                diagnostic_context=diagnostic_context(),
            )
        assert await harness.session_count(device_context.workspace_id) == 0


class TestCompletionFencing:
    @pytest.mark.asyncio
    async def test_concurrent_completion_claims_admit_exactly_one_claimant(
        self, multipart_store_harness: MultipartStoreHarness
    ) -> None:
        harness = multipart_store_harness
        record, _seeded, device_context = await _seed_and_reserve(harness)

        outcomes = await asyncio.gather(
            *(
                harness.store.claim_completion(
                    session_id=record.session_id,
                    device_context=device_context,
                    diagnostic_context=diagnostic_context(),
                )
                for _ in range(5)
            ),
            return_exceptions=True,
        )

        claims = [o for o in outcomes if isinstance(o, MultipartSessionClaim)]
        rejections = [o for o in outcomes if isinstance(o, MultipartUploadError)]
        assert len(claims) == 1
        assert len(rejections) == 4
        for rejection in rejections:
            assert rejection.error_code is ErrorCode.MULTIPART_COMPLETION_IN_PROGRESS
        row = await harness.session_row(record.session_id)
        assert row["state"] == MultipartSessionState.COMPLETING.value
        assert row["claim_token"] == claims[0].claim_token

    @pytest.mark.asyncio
    async def test_old_completion_lease_cannot_record_terminal_result(
        self, multipart_store_harness: MultipartStoreHarness
    ) -> None:
        harness = multipart_store_harness
        clock = _clock_of(harness)
        record, _seeded, device_context = await _seed_and_reserve(harness)

        old_claim = await harness.store.claim_completion(
            session_id=record.session_id,
            device_context=device_context,
            diagnostic_context=diagnostic_context(),
        )
        clock.advance(timedelta(seconds=MULTIPART_COMPLETION_LEASE_SECONDS + 1))
        replacement = await harness.store.claim_completion(
            session_id=record.session_id,
            device_context=device_context,
            diagnostic_context=diagnostic_context(),
        )
        assert replacement.claim_token != old_claim.claim_token

        terminal_result = _terminal_result(clock)
        with pytest.raises(MultipartUploadError, match="multipart_completion_in_progress"):
            await harness.store.record_terminal_result(
                claim=old_claim,
                result=terminal_result,
                diagnostic_context=diagnostic_context(),
            )

        # The replacement claimant still writes the frozen result, and the
        # committed replay afterwards returns it unchanged.
        await harness.store.record_terminal_result(
            claim=replacement,
            result=terminal_result,
            diagnostic_context=diagnostic_context(),
        )
        replay_claim = await harness.store.claim_completion(
            session_id=record.session_id,
            device_context=device_context,
            diagnostic_context=diagnostic_context(),
        )
        assert replay_claim.is_committed_replay
        assert replay_claim.session.terminal_result == terminal_result

    @pytest.mark.asyncio
    async def test_expired_completion_lease_without_reclaim_still_fences(
        self, multipart_store_harness: MultipartStoreHarness
    ) -> None:
        harness = multipart_store_harness
        clock = _clock_of(harness)
        record, _seeded, device_context = await _seed_and_reserve(harness)

        claim = await harness.store.claim_completion(
            session_id=record.session_id,
            device_context=device_context,
            diagnostic_context=diagnostic_context(),
        )
        clock.advance(timedelta(seconds=MULTIPART_COMPLETION_LEASE_SECONDS + 1))

        with pytest.raises(MultipartUploadError, match="multipart_completion_in_progress"):
            await harness.store.record_terminal_result(
                claim=claim,
                result=_terminal_result(clock),
                diagnostic_context=diagnostic_context(),
            )

    @pytest.mark.asyncio
    async def test_identical_committed_result_replay_is_idempotent(
        self, multipart_store_harness: MultipartStoreHarness
    ) -> None:
        harness = multipart_store_harness
        clock = _clock_of(harness)
        record, _seeded, device_context = await _seed_and_reserve(harness)
        claim = await harness.store.claim_completion(
            session_id=record.session_id,
            device_context=device_context,
            diagnostic_context=diagnostic_context(),
        )
        terminal_result = _terminal_result(clock)
        await harness.store.record_terminal_result(
            claim=claim, result=terminal_result, diagnostic_context=diagnostic_context()
        )

        # A claimant that lost the response replays the identical frozen
        # write: it converges instead of failing.
        await harness.store.record_terminal_result(
            claim=claim, result=terminal_result, diagnostic_context=diagnostic_context()
        )
        conflicting_result = SmallFileTerminalResult(
            result_kind=SmallFileTerminalResultKind.NO_CHANGE,
            source_id=uuid4(),
            source_version_id=uuid4(),
            content_version=2,
            committed_at=clock(),
        )
        with pytest.raises(MultipartUploadError, match="multipart_session_state_invalid"):
            await harness.store.record_terminal_result(
                claim=claim,
                result=conflicting_result,
                diagnostic_context=diagnostic_context(),
            )

    @pytest.mark.asyncio
    async def test_terminal_failure_obligation_is_claimable_cleanup(
        self, multipart_store_harness: MultipartStoreHarness
    ) -> None:
        harness = multipart_store_harness
        record, seeded, device_context = await _seed_and_reserve(harness)
        claim = await harness.store.claim_completion(
            session_id=record.session_id,
            device_context=device_context,
            diagnostic_context=diagnostic_context(),
        )

        await harness.store.record_terminal_result(
            claim=claim,
            failure_state=MultipartSessionState.INTEGRITY_FAILED,
            diagnostic_context=diagnostic_context(),
        )
        row = await harness.session_row(record.session_id)
        assert row["state"] == MultipartSessionState.INTEGRITY_FAILED.value
        assert row["cleanup_state"] == "pending"
        assert row["claim_token"] is None

        claims = [
            claim
            for claim in await harness.store.claim_cleanup_batch(
                batch_limit=25, diagnostic_context=diagnostic_context()
            )
            if claim.session.session_id == record.session_id
        ]
        assert len(claims) == 1
        assert claims[0].session.state is MultipartSessionState.CLEANUP_PENDING
        assert claims[0].session.staging_key == seeded.staging_key
        await harness.store.record_cleanup_result(
            claim=claims[0],
            is_succeeded=True,
            diagnostic_context=diagnostic_context(),
        )
        row = await harness.session_row(record.session_id)
        assert row["state"] == MultipartSessionState.CLEANED.value
        assert row["cleanup_state"] == "succeeded"
        assert row["cleanup_next_retry_at"] is None
        assert row["claim_token"] is None


class TestProviderPartFacts:
    @pytest.mark.asyncio
    async def test_part_facts_land_only_on_exact_geometry_matches(
        self, multipart_store_harness: MultipartStoreHarness
    ) -> None:
        harness = multipart_store_harness
        record, _seeded, device_context = await _seed_and_reserve(harness)
        etag = MultipartProviderPartETag("provider-observed-etag-part-1")

        await harness.store.record_provider_part(
            session_id=record.session_id,
            part_number=1,
            etag=etag,
            verified_size_bytes=MULTIPART_PART_SIZE_BYTES,
            device_context=device_context,
            diagnostic_context=diagnostic_context(),
        )
        # Replaying the same provider observation is an idempotent no-op.
        await harness.store.record_provider_part(
            session_id=record.session_id,
            part_number=1,
            etag=etag,
            verified_size_bytes=MULTIPART_PART_SIZE_BYTES,
            device_context=device_context,
            diagnostic_context=diagnostic_context(),
        )
        # The final part carries exactly the remaining bytes.
        await harness.store.record_provider_part(
            session_id=record.session_id,
            part_number=3,
            etag=MultipartProviderPartETag("provider-observed-etag-part-3"),
            verified_size_bytes=SEEDED_MULTIPART_FINAL_PART_BYTES,
            device_context=device_context,
            diagnostic_context=diagnostic_context(),
        )

        loaded = await harness.store.load_owned_session(
            session_id=record.session_id,
            device_context=device_context,
            diagnostic_context=diagnostic_context(),
        )
        assert loaded.completed_part_numbers == frozenset({1, 3})
        assert loaded.state is MultipartSessionState.UPLOADING

    @pytest.mark.asyncio
    async def test_inconsistent_provider_observations_fail_closed(
        self, multipart_store_harness: MultipartStoreHarness
    ) -> None:
        harness = multipart_store_harness
        record, _seeded, device_context = await _seed_and_reserve(harness)

        with pytest.raises(MultipartUploadError, match="multipart_part_invalid"):
            await harness.store.record_provider_part(
                session_id=record.session_id,
                part_number=0,
                etag=MultipartProviderPartETag("below-one"),
                verified_size_bytes=MULTIPART_PART_SIZE_BYTES,
                device_context=device_context,
                diagnostic_context=diagnostic_context(),
            )
        with pytest.raises(MultipartUploadError, match="multipart_provider_state_invalid"):
            await harness.store.record_provider_part(
                session_id=record.session_id,
                part_number=2,
                etag=MultipartProviderPartETag("wrong-window-size"),
                verified_size_bytes=SEEDED_MULTIPART_FINAL_PART_BYTES,
                device_context=device_context,
                diagnostic_context=diagnostic_context(),
            )

        await harness.store.record_provider_part(
            session_id=record.session_id,
            part_number=2,
            etag=MultipartProviderPartETag("first-observation"),
            verified_size_bytes=MULTIPART_PART_SIZE_BYTES,
            device_context=device_context,
            diagnostic_context=diagnostic_context(),
        )
        with pytest.raises(MultipartUploadError, match="multipart_provider_state_invalid"):
            await harness.store.record_provider_part(
                session_id=record.session_id,
                part_number=2,
                etag=MultipartProviderPartETag("conflicting-observation"),
                verified_size_bytes=MULTIPART_PART_SIZE_BYTES,
                device_context=device_context,
                diagnostic_context=diagnostic_context(),
            )

    @pytest.mark.asyncio
    async def test_part_number_outside_geometry_is_part_invalid(
        self, multipart_store_harness: MultipartStoreHarness
    ) -> None:
        harness = multipart_store_harness
        record, _seeded, device_context = await _seed_and_reserve(harness)

        with pytest.raises(MultipartUploadError, match="multipart_part_invalid"):
            await harness.store.record_provider_part(
                session_id=record.session_id,
                part_number=4,
                etag=MultipartProviderPartETag("beyond-part-count"),
                verified_size_bytes=MULTIPART_PART_SIZE_BYTES,
                device_context=device_context,
                diagnostic_context=diagnostic_context(),
            )


class TestOwnershipAndExpiry:
    @pytest.mark.asyncio
    async def test_load_owned_session_rejects_foreign_owner_without_leaking(
        self, multipart_store_harness: MultipartStoreHarness
    ) -> None:
        harness = multipart_store_harness
        record, _seeded, device_context = await _seed_and_reserve(harness)
        foreign_device = await harness.seed_foreign_device(device_context)

        with pytest.raises(MultipartUploadError, match="multipart_session_not_found"):
            await harness.store.load_owned_session(
                session_id=record.session_id,
                device_context=foreign_device,
                diagnostic_context=diagnostic_context(),
            )

    @pytest.mark.asyncio
    async def test_expired_forward_session_fails_closed_at_load(
        self, multipart_store_harness: MultipartStoreHarness
    ) -> None:
        harness = multipart_store_harness
        clock = _clock_of(harness)
        record, _seeded, device_context = await _seed_and_reserve(harness)

        clock.advance(timedelta(hours=24, seconds=1))
        with pytest.raises(MultipartUploadError, match="multipart_session_expired"):
            await harness.store.load_owned_session(
                session_id=record.session_id,
                device_context=device_context,
                diagnostic_context=diagnostic_context(),
            )


class TestExpirySweepAndCleanup:
    @pytest.mark.asyncio
    async def test_expiry_strike_leases_exact_cleanup_identity(
        self, multipart_store_harness: MultipartStoreHarness
    ) -> None:
        harness = multipart_store_harness
        clock = _clock_of(harness)
        record, seeded, _device_context = await _seed_and_reserve(harness)

        clock.advance(timedelta(hours=24, seconds=1))
        claims = await harness.store.claim_cleanup_batch(
            batch_limit=25, diagnostic_context=diagnostic_context()
        )

        # The sweep may lawfully strike other tests' overdue sessions; only
        # this session's claim is this test's concern.
        mine = [claim for claim in claims if claim.session.session_id == record.session_id]
        assert len(mine) == 1
        assert mine[0].session.state is MultipartSessionState.CLEANUP_PENDING
        assert mine[0].session.staging_key == seeded.staging_key
        assert mine[0].session.provider_upload_id == seeded.provider_upload_id
        row = await harness.session_row(record.session_id)
        assert row["claim_token"] == mine[0].claim_token
        assert row["claim_expires_at"] == mine[0].claim_expires_at

    @pytest.mark.asyncio
    async def test_cleanup_failure_persists_reason_and_bounded_next_retry(
        self, multipart_store_harness: MultipartStoreHarness
    ) -> None:
        harness = multipart_store_harness
        clock = _clock_of(harness)
        record, _seeded, _device_context = await _seed_and_reserve(harness)
        clock.advance(timedelta(hours=24, seconds=1))
        first_claims = [
            claim
            for claim in await harness.store.claim_cleanup_batch(
                batch_limit=25, diagnostic_context=diagnostic_context()
            )
            if claim.session.session_id == record.session_id
        ]
        assert len(first_claims) == 1

        await harness.store.record_cleanup_result(
            claim=first_claims[0],
            is_succeeded=False,
            failure_reason=ErrorCode.MULTIPART_DEPENDENCY_UNAVAILABLE,
            diagnostic_context=diagnostic_context(),
        )
        row = await harness.session_row(record.session_id)
        assert row["cleanup_state"] == "failed"
        # The strike scheduled attempt one; its failure schedules attempt
        # two with the base backoff of the failed attempt's ordinal.
        assert row["cleanup_attempt_count"] == 2
        assert row["cleanup_reason_code"] == "multipart_dependency_unavailable"
        assert row["cleanup_next_retry_at"] == clock() + timedelta(
            seconds=MULTIPART_CLEANUP_RETRY_BASE_SECONDS
        )

        # The bounded backoff hides this row from the next claim until due.
        interim_claims = await harness.store.claim_cleanup_batch(
            batch_limit=25, diagnostic_context=diagnostic_context()
        )
        assert record.session_id not in {
            claim.session.session_id for claim in interim_claims
        }
        clock.advance(timedelta(seconds=MULTIPART_CLEANUP_RETRY_BASE_SECONDS + 1))
        second_claims = [
            claim
            for claim in await harness.store.claim_cleanup_batch(
                batch_limit=25, diagnostic_context=diagnostic_context()
            )
            if claim.session.session_id == record.session_id
        ]
        assert len(second_claims) == 1

        # A stale worker holding the reclaimed row's previous lease can no
        # longer write the cleanup outcome.
        with pytest.raises(MultipartUploadError, match="multipart_session_state_invalid"):
            await harness.store.record_cleanup_result(
                claim=first_claims[0],
                is_succeeded=True,
                diagnostic_context=diagnostic_context(),
            )

        await harness.store.record_cleanup_result(
            claim=second_claims[0],
            is_succeeded=True,
            diagnostic_context=diagnostic_context(),
        )
        row = await harness.session_row(record.session_id)
        assert row["state"] == MultipartSessionState.CLEANED.value
        assert row["cleanup_state"] == "succeeded"
        assert row["cleanup_reason_code"] is None
        assert row["cleanup_next_retry_at"] is None
        assert row["claim_token"] is None

    @pytest.mark.asyncio
    async def test_cleanup_batch_is_bounded_by_the_requested_limit(
        self, multipart_store_harness: MultipartStoreHarness
    ) -> None:
        harness = multipart_store_harness
        device_context = await harness.seed_device()
        seeded_records = []
        for _ in range(3):
            seeded = await harness.seed_operation(device_context, now=_now(harness))
            seeded_records.append(await harness.reserve(seeded, device_context))
        seeded_session_ids = {record.session_id for record in seeded_records}
        _clock_of(harness).advance(timedelta(hours=24, seconds=1))

        # Other tests' overdue sessions may share the sweep; this test's
        # proof is that every batch respects the bound, no session is ever
        # claimed twice, and all three seeded sessions eventually lease.
        claimed_session_ids: list[MultipartUploadSessionId] = []
        for _ in range(50):
            if seeded_session_ids.issubset(set(claimed_session_ids)):
                break
            batch = await harness.store.claim_cleanup_batch(
                batch_limit=2, diagnostic_context=diagnostic_context()
            )
            assert len(batch) <= 2
            batch_ids = [claim.session.session_id for claim in batch]
            assert not set(batch_ids).intersection(claimed_session_ids)
            claimed_session_ids.extend(batch_ids)
            if not batch:
                break
        assert seeded_session_ids.issubset(set(claimed_session_ids))


class TestQueryPlans:
    @pytest.mark.asyncio
    async def test_session_store_hot_queries_use_shipped_indexes(
        self, multipart_store_harness: MultipartStoreHarness
    ) -> None:
        harness = multipart_store_harness
        clock = _clock_of(harness)
        populated = await _populate_sessions_for_plans(harness, clock)
        sweep_now = clock() + timedelta(hours=48)
        probe_session_id = MultipartUploadSessionId(populated[0]["session_id"])
        probe_operation_id = populated[0]["operation_id"]
        assert isinstance(probe_operation_id, UUID)
        async with harness.engine.connect() as connection:
            plans = {
                "session_lookup": await _explain(
                    connection,
                    multipart_session_select_statement(
                        probe_session_id, for_update=False
                    ),
                ),
                "operation_lookup": await _explain(
                    connection,
                    multipart_operation_select_statement(
                        probe_operation_id, for_update=False
                    ),
                ),
                "expiry_sweep": await _explain(
                    connection, expiry_sweep_select_statement(now=sweep_now, batch_limit=25)
                ),
                "cleanup_claim": await _explain(
                    connection, cleanup_claim_select_statement(now=sweep_now, batch_limit=25)
                ),
            }

        for query_name, payload in plans.items():
            node_types, index_names = _plan_summary(payload)
            assert "Seq Scan" not in node_types, (
                f"{query_name} sequentially scanned the populated session table"
            )
            assert index_names, f"{query_name} touched no shipped index"
            assert index_names <= _APPROVED_INDEX_NAMES, (
                f"{query_name} touched unapproved indexes: "
                f"{index_names - _APPROVED_INDEX_NAMES}"
            )
        assert "ix_multipart_uploads__expiry_sweep" in _plan_summary(plans["expiry_sweep"])[1]
        assert "ix_multipart_uploads__cleanup_claim" in _plan_summary(plans["cleanup_claim"])[1]
        assert "uq_multipart_uploads__session_id" in _plan_summary(plans["session_lookup"])[1]
        assert "uq_multipart_uploads__operation" in _plan_summary(plans["operation_lookup"])[1]


async def _populate_sessions_for_plans(
    harness: MultipartStoreHarness, clock: MutableUtcClock
) -> list[dict[str, Any]]:
    """Bulk-seed a planner-realistic session population and analyze it.

    Most rows rest in the committed terminal shape (outside both partial
    indexes); a rare slice stays in a forward state whose deadline already
    passed (the expiry sweep population) and another rare slice carries a
    due cleanup obligation (the cleanup claim population), so the planner
    prefers each partial index over a sequential scan.
    """

    device_context = await harness.seed_device()
    now = clock()
    operation_rows: list[dict[str, Any]] = []
    session_rows: list[dict[str, Any]] = []
    for index in range(_POPULATED_SESSION_MINIMUM):
        operation_id = uuid4()
        nonce = uuid4().hex
        digest = hashlib.sha256(nonce.encode("utf-8")).hexdigest()
        operation_rows.append(
            {
                "operation_id": operation_id,
                "operation_token_hash": hashlib.sha256(nonce.encode("ascii")).hexdigest(),
                "workspace_id": device_context.workspace_id,
                "device_id": device_context.device_id,
                "event_id": uuid4(),
                "idempotency_key": str(uuid4()),
                "operation_kind": "create",
                "declared_sha256": digest,
                "declared_size_bytes": 20 * 1024 * 1024,
                "declared_media_type": "text/markdown",
                "policy_revision_number": 1,
                "reserved_source_id": uuid4(),
                "normalized_locator": None,
                "locator_fingerprint": None,
                "state": "pending",
                "expires_at": now + timedelta(hours=1),
            }
        )
        is_overdue_forward = index < _EXPIRED_FORWARD_ROWS
        is_cleanup_due = (
            _EXPIRED_FORWARD_ROWS <= index < _EXPIRED_FORWARD_ROWS + _CLEANUP_DUE_ROWS
        )
        is_committed = not is_overdue_forward and not is_cleanup_due
        session_rows.append(
            {
                "multipart_upload_id": uuid4(),
                "session_id": f"{'p' * 32}{index:08d}",
                "workspace_id": device_context.workspace_id,
                "device_id": device_context.device_id,
                "operation_id": operation_id,
                "declared_sha256": digest,
                "declared_size_bytes": 20 * 1024 * 1024,
                "declared_media_type": "text/markdown",
                "base_version_id": None,
                "policy_revision_number": 1,
                "part_size_bytes": MULTIPART_PART_SIZE_BYTES,
                "part_count": 3,
                "staging_key": f"staging/multipart/plan/{nonce}",
                "provider_upload_id": f"provider-upload-plan-{nonce}",
                "state": (
                    "committed"
                    if is_committed
                    else "uploading"
                    if is_overdue_forward
                    else "cleanup_pending"
                ),
                "claim_token": None,
                "claim_expires_at": None,
                "result_kind": "committed" if is_committed else None,
                "result_source_id": uuid4() if is_committed else None,
                "result_source_version_id": uuid4() if is_committed else None,
                "result_content_version": 1 if is_committed else None,
                "result_committed_at": now - timedelta(hours=1) if is_committed else None,
                "cleanup_state": "pending" if is_cleanup_due else "none",
                # The cleanup-shape CHECK pins an open obligation to at
                # least one scheduled attempt.
                "cleanup_attempt_count": 1 if is_cleanup_due else 0,
                "cleanup_next_retry_at": (
                    now - timedelta(minutes=1) if is_cleanup_due else None
                ),
                "cleanup_reason_code": None,
                "expires_at": (
                    now - timedelta(hours=1) if is_overdue_forward else now + timedelta(hours=1)
                ),
                "created_at": now - timedelta(hours=2),
                "updated_at": now - timedelta(hours=2),
            }
        )
    async with harness.engine.begin() as connection:
        await connection.execute(sa.insert(small_file_upload_operations).values(operation_rows))
        await connection.execute(sa.insert(multipart_uploads).values(session_rows))
        await connection.execute(sa.text("ANALYZE knowledge.multipart_uploads"))
        await connection.execute(sa.text("ANALYZE knowledge.small_file_upload_operations"))
    return session_rows


async def _explain(
    connection: AsyncConnection, statement: sa.Select[tuple[Any, ...]]
) -> list[dict[str, Any]]:
    compiled = statement.compile(
        dialect=postgresql.dialect(),
        compile_kwargs={"literal_binds": True},
    )
    assert "%" not in str(compiled), "the compiled plan probe must stay parameter-free"
    result = await connection.execute(sa.text("EXPLAIN (FORMAT JSON) " + str(compiled)))
    payload = result.scalar_one()
    if isinstance(payload, str):
        payload = json.loads(payload)
    return list(payload)


def _plan_summary(payload: list[dict[str, Any]]) -> tuple[set[str], set[str]]:
    node_types: set[str] = set()
    index_names: set[str] = set()

    def walk(node: dict[str, Any]) -> None:
        node_types.add(str(node.get("Node Type", "")))
        if node.get("Index Name"):
            index_names.add(str(node["Index Name"]))
        for child in node.get("Plans", []):
            walk(child)

    for plan in payload:
        walk(plan["Plan"])
    return node_types, index_names
