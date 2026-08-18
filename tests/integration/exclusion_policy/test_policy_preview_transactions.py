"""PostgreSQL transaction contracts of the exact-snapshot preview (spec 10).

The disposable stack backs every case: the request captures the exact binding
(draft identity/version/digest, base revision, checkpoint, actor) with the
preview-requested audit row; the single repeatable-read activity streams
500-row keyset pages, heartbeats between them, evaluates old/new policy over
every current valid source and lands the complete evidence, counters, digest,
``ready_at`` and the 15-minute expiry in one atomic commit; a midstream
injected failure (and any crash like it) rolls back every result and leaves
the row pending; a source mutation during execution is invisible to the open
snapshot (no cross-snapshot batch merging); stale bindings — draft edit,
checkpoint advance — reject before any scan; result reads refuse stale
checkpoints and cap pages at the 200-row bound over the stable
``(impact_class, source_id)`` cursor; a ready preview expires after its
``ready_at`` plus fifteen minutes; the leased outbox claims, releases and
sweeps through the fenced transitions; and no-active-policy semantics preview
existing valid sources as ``newly_allowed``.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from tests.integration.exclusion_policy.conftest import PolicyMigrationHarness

from personal_os.diagnostics.context import DiagnosticContext, TraceContext
from personal_os.diagnostics.events import SafeToken
from personal_os.diagnostics.trace_context import SpanId, TraceId
from personal_os.error_contracts.codes import ErrorCode
from personal_os.exclusion_policy.contracts import RuleKind
from personal_os.exclusion_policy.errors import ExclusionPolicyError
from personal_os.exclusion_policy.metrics import (
    InMemoryExclusionPolicyMetrics,
    PreviewMetricOutcome,
)
from personal_os.exclusion_policy.normalization import normalize_rule
from personal_os.exclusion_policy.ports import PolicyActor, PolicyActorKind
from personal_os.exclusion_policy.previews import (
    PREVIEW_READY_EXPIRY_SECONDS,
    PREVIEW_RESULT_PAGE_MAXIMUM,
    PREVIEW_SCAN_PAGE_SIZE,
    PreviewImpactClass,
    PreviewProgress,
    PreviewStatus,
)
from postgresql_source_store.policy_drafts import PostgresqlPolicyDraftStore
from postgresql_source_store.policy_previews import (
    PREVIEW_EXECUTION_DEADLINE_ERROR_CODE,
    PREVIEW_LEASE_EXPIRED_ERROR_CODE,
    InjectedPreviewFailure,
    LeasedPolicyPreview,
    PostgresqlPolicyPreviewStore,
)
from postgresql_source_store.tables import (
    audit_events,
    policy_drafts,
    policy_preview_results,
    policy_previews,
    sources,
    sync_events,
)

pytestmark = pytest.mark.local_stack

_TRACE = TraceContext(
    trace_id=TraceId("0123456789abcdef0123456789abcdef"),
    remote_parent_span_id=None,
    local_span_id=SpanId("0123456789abcdef"),
    trace_flags=0,
)


def _context() -> DiagnosticContext:
    return DiagnosticContext(request_id=uuid4(), client_request_id=None, trace=_TRACE)


def _actor(user_id: UUID) -> PolicyActor:
    return PolicyActor(actor_kind=PolicyActorKind.USER, user_id=user_id)


class PreviewHarness:
    """Seeding and inspection helpers over one engine and preview store."""

    def __init__(self, base: PolicyMigrationHarness) -> None:
        self.base = base
        self.engine = base.engine
        self.metrics = InMemoryExclusionPolicyMetrics()
        self.store = PostgresqlPolicyPreviewStore(
            base.engine, lease_token_generator=uuid4, metrics=self.metrics
        )
        self.draft_store = PostgresqlPolicyDraftStore(base.engine)

    async def reset_previews(self) -> None:
        """Reset preview state and test-seeded sources for isolation.

        The stack's own seeded source (referenced by its sync event) stays;
        every source this module's tests seeded goes, so count assertions
        see only the durable baseline plus the current test's rows.
        """

        async with self.engine.begin() as connection:
            # Audit rows are append-only; preview-requested history stays.
            await connection.execute(policy_preview_results.delete())
            await connection.execute(policy_previews.delete())
            await connection.execute(
                sa.delete(sources).where(
                    sources.c.workspace_id == self.base.stack.workspace_id,
                    sources.c.source_id.not_in(
                        sa.select(sync_events.c.source_id).where(
                            sync_events.c.workspace_id == self.base.stack.workspace_id
                        )
                    ),
                )
            )

    async def seed_sources(self, count: int, *, source_type: str = "markdown") -> list[UUID]:
        source_ids = [uuid4() for _ in range(count)]
        async with self.engine.begin() as connection:
            await connection.execute(
                sa.insert(sources).values(
                    [
                        {
                            "source_id": source_id,
                            "workspace_id": self.base.stack.workspace_id,
                            "source_type": source_type,
                            "title": f"Preview Source {uuid4().hex[:8]}",
                        }
                        for source_id in source_ids
                    ]
                )
            )
        return source_ids

    async def seed_sync_event(self) -> int:
        event_id = uuid4()
        nonce = uuid4().hex
        async with self.engine.begin() as connection:
            referenced = await connection.execute(
                sa.select(sync_events.c.source_id)
                .where(sync_events.c.workspace_id == self.base.stack.workspace_id)
                .limit(1)
            )
            await connection.execute(
                sa.insert(sync_events).values(
                    event_id=event_id,
                    workspace_id=self.base.stack.workspace_id,
                    source_id=referenced.scalar_one(),
                    idempotency_key=f"preview-{nonce}",
                    request_fingerprint=hashlib.sha256(nonce.encode()).hexdigest(),
                    event_type="update",
                )
            )
        return await self.current_checkpoint()

    async def current_checkpoint(self) -> int:
        async with self.engine.connect() as connection:
            value = await connection.execute(
                sa.select(sa.func.max(sync_events.c.event_sequence)).where(
                    sync_events.c.workspace_id == self.base.stack.workspace_id
                )
            )
            return int(value.scalar_one() or 0)

    async def source_type_counts(self) -> dict[str, int]:
        async with self.engine.connect() as connection:
            rows = await connection.execute(
                sa.select(sources.c.source_type, sa.func.count())
                .where(
                    sources.c.workspace_id == self.base.stack.workspace_id,
                    sources.c.deleted_at.is_(None),
                )
                .group_by(sources.c.source_type)
            )
            return {str(kind): int(count) for kind, count in rows.all()}

    async def request(self) -> Any:
        return await self.store.request_preview(
            self.base.stack.workspace_id,
            _actor(self.base.stack.owner_user_id),
            _context(),
        )

    async def fetch_preview_row(self, preview_id: UUID) -> dict[str, Any]:
        async with self.engine.connect() as connection:
            row = await connection.execute(
                sa.select(*policy_previews.c).where(
                    policy_previews.c.policy_preview_id == preview_id
                )
            )
            return dict(row.one()._mapping)

    async def replace_draft(self, *rules: tuple[RuleKind, dict[str, Any]]) -> int:
        async with self.engine.connect() as connection:
            draft_row = await connection.execute(
                sa.select(policy_drafts.c.policy_draft_id, policy_drafts.c.draft_version).where(
                    policy_drafts.c.workspace_id == self.base.stack.workspace_id
                )
            )
            draft = draft_row.one()
        normalized = tuple(
            normalize_rule(uuid4(), kind, rule_index=index, **operand)
            for index, (kind, operand) in enumerate(rules)
        )
        updated = await self.draft_store.replace_rules(
            draft.policy_draft_id,
            int(draft.draft_version),
            normalized,
            _actor(self.base.stack.owner_user_id),
            _context(),
        )
        return updated.draft_version

    async def backdate_ready_expiry(self, preview_id: UUID) -> None:
        async with self.engine.begin() as connection:
            await connection.execute(
                sa.update(policy_previews)
                .values(
                    created_at=sa.text("CURRENT_TIMESTAMP - interval '20 minutes'"),
                    available_at=sa.text("CURRENT_TIMESTAMP - interval '20 minutes'"),
                    ready_at=sa.text("CURRENT_TIMESTAMP - interval '16 minutes'"),
                    expires_at=sa.text("CURRENT_TIMESTAMP - interval '1 minute'"),
                )
                .where(policy_previews.c.policy_preview_id == preview_id)
            )

    async def backdate_creation(self, preview_id: UUID) -> None:
        async with self.engine.begin() as connection:
            await connection.execute(
                sa.update(policy_previews)
                .values(
                    created_at=sa.text("CURRENT_TIMESTAMP - interval '16 minutes'"),
                    available_at=sa.text("CURRENT_TIMESTAMP - interval '16 minutes'"),
                )
                .where(policy_previews.c.policy_preview_id == preview_id)
            )

    async def expire_lease(self, preview_id: UUID) -> None:
        async with self.engine.begin() as connection:
            await connection.execute(
                sa.update(policy_previews)
                .values(
                    created_at=sa.text("CURRENT_TIMESTAMP - interval '80 seconds'"),
                    available_at=sa.text("CURRENT_TIMESTAMP - interval '80 seconds'"),
                    leased_until=sa.text("CURRENT_TIMESTAMP - interval '5 seconds'"),
                )
                .where(policy_previews.c.policy_preview_id == preview_id)
            )

    async def claim_exclusive(self, preview_id: UUID) -> LeasedPolicyPreview | None:
        """Claim until the exact preview appears in one claim batch."""

        import asyncio as _asyncio

        for _ in range(40):
            claimed = await self.store.claim_pending_previews(datetime.now(UTC), 20)
            for lease in claimed:
                if lease.policy_preview_id == preview_id:
                    return lease
            await _asyncio.sleep(0.5)
        return None


@pytest_asyncio.fixture
async def preview_harness(
    policy_migration_harness: PolicyMigrationHarness,
) -> PreviewHarness:
    harness = PreviewHarness(policy_migration_harness)
    await harness.reset_previews()
    return harness


@pytest.mark.asyncio
async def test_request_captures_binding_audit_and_pending_row(
    preview_harness: PreviewHarness,
) -> None:
    record = await preview_harness.request()

    assert record.status is PreviewStatus.PENDING
    assert record.source_checkpoint_event_sequence == (await preview_harness.current_checkpoint())
    assert record.base_policy_revision_id is None
    assert record.draft_version >= 1
    assert len(record.draft_sha256) == 64

    row = await preview_harness.fetch_preview_row(record.policy_preview_id)
    assert row["state"] == "pending"
    assert row["created_by_user_id"] == preview_harness.base.stack.owner_user_id
    assert row["impact_digest"] is None

    async with preview_harness.engine.connect() as connection:
        audit_count = await connection.execute(
            sa.select(sa.func.count()).where(
                audit_events.c.action == "exclusion_policy.preview_requested",
                audit_events.c.target_id == record.policy_preview_id,
            )
        )
        assert int(audit_count.scalar_one()) == 1
    assert preview_harness.metrics.preview_count(PreviewMetricOutcome.READY) == 0


@pytest.mark.asyncio
async def test_activity_streams_pages_heartbeats_and_writes_atomic_evidence(
    preview_harness: PreviewHarness,
) -> None:
    pdf_count = 200
    other_count = PREVIEW_SCAN_PAGE_SIZE // 2 + 50
    pdf_sources = set(await preview_harness.seed_sources(pdf_count, source_type="pdf"))
    await preview_harness.seed_sources(other_count)
    await preview_harness.replace_draft(
        (RuleKind.SOURCE_TYPE, {"text_operand": "pdf"}),
        (RuleKind.EXTENSION, {"text_operand": ".tmp"}),
    )
    counts = await preview_harness.source_type_counts()
    total_sources = sum(counts.values())
    markdown_total = total_sources - counts.get("pdf", 0)
    record = await preview_harness.request()
    heartbeats: list[PreviewProgress] = []

    async def heartbeat(progress: PreviewProgress) -> None:
        heartbeats.append(progress)

    ready = await preview_harness.store.run_preview_activity(
        record.policy_preview_id, _context(), heartbeat
    )

    assert ready.status is PreviewStatus.READY
    assert ready.ready_at is not None and ready.expires_at is not None
    assert (ready.expires_at - ready.ready_at).total_seconds() == (PREVIEW_READY_EXPIRY_SECONDS)
    # Every pdf source definitely matches the source-type rule; every other
    # source lacks locator evidence for the extension rule and stays
    # indeterminate — both over a previous enforced deny (no active policy).
    assert ready.still_excluded_count == counts.get("pdf", 0)
    assert ready.indeterminate_count == markdown_total
    assert len(ready.impact_digest or "") == 64
    assert await preview_harness.store.count_results(record.policy_preview_id) == (total_sources)
    assert [progress.batch_count for progress in heartbeats] == [1, 2]
    assert heartbeats[-1].evaluated_subjects == total_sources
    assert preview_harness.metrics.preview_count(PreviewMetricOutcome.READY) == 1

    page = await preview_harness.store.list_preview_results(
        record.policy_preview_id, _context(), limit=PREVIEW_RESULT_PAGE_MAXIMUM
    )
    pdf_row = next(
        (row for row in page.rows if row.source_id in pdf_sources),
        None,
    )
    while pdf_row is None and page.next_cursor is not None:
        page = await preview_harness.store.list_preview_results(
            record.policy_preview_id, _context(), cursor=page.next_cursor
        )
        pdf_row = next((row for row in page.rows if row.source_id in pdf_sources), None)
    assert pdf_row is not None
    assert pdf_row.impact_class is PreviewImpactClass.STILL_EXCLUDED
    assert pdf_row.previous_raw_decision.value == "indeterminate"
    assert pdf_row.previous_enforced_decision.value == "excluded"
    assert pdf_row.proposed_match_state.value == "matched"
    assert len(pdf_row.matched_rule_ids) == 1


@pytest.mark.asyncio
async def test_preview_activity_rolls_back_every_result_after_midstream_failure(
    preview_harness: PreviewHarness,
) -> None:
    await preview_harness.seed_sources(PREVIEW_SCAN_PAGE_SIZE + 100)
    expected_total = sum((await preview_harness.source_type_counts()).values())
    record = await preview_harness.request()

    with pytest.raises(InjectedPreviewFailure):
        await preview_harness.store.run_preview_activity(
            record.policy_preview_id,
            _context(),
            fail_after_subjects=PREVIEW_SCAN_PAGE_SIZE + 1,
        )
    assert await preview_harness.store.count_results(record.policy_preview_id) == 0
    assert (
        await preview_harness.store.get_preview(record.policy_preview_id, _context())
    ).status is PreviewStatus.PENDING

    # The retry restarts from the same captured inputs and completes over the
    # draft rules an earlier test installed (pdf match plus a locator rule):
    # markdown sources end indeterminate, none newly allowed.
    ready = await preview_harness.store.run_preview_activity(record.policy_preview_id, _context())
    assert ready.status is PreviewStatus.READY
    assert await preview_harness.store.count_results(record.policy_preview_id) == (expected_total)
    counts = await preview_harness.source_type_counts()
    assert ready.indeterminate_count == expected_total - counts.get("pdf", 0)
    assert ready.still_excluded_count == counts.get("pdf", 0)


@pytest.mark.asyncio
async def test_source_mutation_during_execution_never_merges_snapshots(
    preview_harness: PreviewHarness,
) -> None:
    # The draft still carries the rules an earlier test installed (pdf match
    # plus a locator rule), so expected classes follow the same grammar:
    # pdf -> still_excluded, everything else -> indeterminate.
    await preview_harness.seed_sources(PREVIEW_SCAN_PAGE_SIZE + 50)
    counts = await preview_harness.source_type_counts()
    expected_total = sum(counts.values())
    record = await preview_harness.request()
    late_source_ids: list[UUID] = []

    async def heartbeat(progress: PreviewProgress) -> None:
        if progress.batch_count == 1 and not late_source_ids:
            # A concurrent writer commits a new pdf source between two pages.
            late_source_ids.extend(await preview_harness.seed_sources(1, source_type="pdf"))

    ready = await preview_harness.store.run_preview_activity(
        record.policy_preview_id, _context(), heartbeat
    )

    # The open repeatable-read snapshot never admitted the late source.
    assert ready.still_excluded_count == counts.get("pdf", 0)
    assert ready.indeterminate_count == expected_total - counts.get("pdf", 0)
    assert await preview_harness.store.count_results(record.policy_preview_id) == (expected_total)
    page = await preview_harness.store.list_preview_results(
        record.policy_preview_id, _context(), limit=PREVIEW_RESULT_PAGE_MAXIMUM
    )
    while page.rows:
        for row in page.rows:
            assert row.source_id not in late_source_ids
        if page.next_cursor is None:
            break
        page = await preview_harness.store.list_preview_results(
            record.policy_preview_id, _context(), cursor=page.next_cursor
        )


@pytest.mark.asyncio
async def test_stale_checkpoint_rejects_execution_and_result_reads(
    preview_harness: PreviewHarness,
) -> None:
    await preview_harness.seed_sources(5)
    stale_record = await preview_harness.request()
    await preview_harness.seed_sync_event()

    with pytest.raises(ExclusionPolicyError) as rejected:
        await preview_harness.store.run_preview_activity(stale_record.policy_preview_id, _context())
    assert rejected.value.error_code is ErrorCode.EXCLUSION_POLICY_PREVIEW_STALE
    assert rejected.value.safe_details["reason"].value == ("preview_source_checkpoint_stale")
    assert (
        await preview_harness.store.get_preview(stale_record.policy_preview_id, _context())
    ).status is PreviewStatus.PENDING

    # A preview bound after the advance completes and serves its pages; the
    # next source event makes every later result read stale.
    fresh = await preview_harness.request()
    ready = await preview_harness.store.run_preview_activity(fresh.policy_preview_id, _context())
    assert ready.status is PreviewStatus.READY
    await preview_harness.store.list_preview_results(fresh.policy_preview_id, _context())
    await preview_harness.seed_sync_event()
    with pytest.raises(ExclusionPolicyError) as stale_read:
        await preview_harness.store.list_preview_results(fresh.policy_preview_id, _context())
    assert stale_read.value.error_code is ErrorCode.EXCLUSION_POLICY_PREVIEW_STALE


@pytest.mark.asyncio
async def test_draft_mutation_after_request_rejects_execution(
    preview_harness: PreviewHarness,
) -> None:
    await preview_harness.seed_sources(3)
    record = await preview_harness.request()
    await preview_harness.replace_draft((RuleKind.SOURCE_TYPE, {"text_operand": "pdf"}))

    with pytest.raises(ExclusionPolicyError) as rejected:
        await preview_harness.store.run_preview_activity(record.policy_preview_id, _context())
    assert rejected.value.error_code is ErrorCode.EXCLUSION_POLICY_PREVIEW_STALE
    assert rejected.value.safe_details["reason"].value == "preview_draft_stale"
    assert await preview_harness.store.count_results(record.policy_preview_id) == 0


@pytest.mark.asyncio
async def test_no_active_policy_previews_valid_sources_as_newly_allowed(
    preview_harness: PreviewHarness,
) -> None:
    await preview_harness.seed_sources(2)
    expected_total = sum((await preview_harness.source_type_counts()).values())
    record = await preview_harness.request()

    ready = await preview_harness.store.run_preview_activity(record.policy_preview_id, _context())

    assert ready.status is PreviewStatus.READY
    assert ready.newly_allowed_count == expected_total
    page = await preview_harness.store.list_preview_results(record.policy_preview_id, _context())
    assert len(page.rows) == expected_total
    for row in page.rows:
        assert row.previous_raw_decision.value == "indeterminate"
        assert row.previous_enforced_decision.value == "excluded"
        assert row.proposed_raw_decision.value == "allowed"
        assert row.impact_class is PreviewImpactClass.NEWLY_ALLOWED


@pytest.mark.asyncio
async def test_result_pages_are_capped_and_cursor_stable(
    preview_harness: PreviewHarness,
) -> None:
    await preview_harness.seed_sources(PREVIEW_RESULT_PAGE_MAXIMUM + 50)
    expected_total = sum((await preview_harness.source_type_counts()).values())
    record = await preview_harness.request()
    await preview_harness.store.run_preview_activity(record.policy_preview_id, _context())

    first = await preview_harness.store.list_preview_results(
        record.policy_preview_id, _context(), limit=PREVIEW_RESULT_PAGE_MAXIMUM
    )
    assert len(first.rows) == PREVIEW_RESULT_PAGE_MAXIMUM
    assert first.next_cursor is not None
    second = await preview_harness.store.list_preview_results(
        record.policy_preview_id, _context(), cursor=first.next_cursor
    )
    assert len(second.rows) == expected_total - PREVIEW_RESULT_PAGE_MAXIMUM
    assert second.next_cursor is None
    ordered = [str(row.source_id) for row in first.rows + second.rows]
    assert ordered == sorted(ordered)


@pytest.mark.asyncio
async def test_ready_preview_expires_after_fifteen_minutes(
    preview_harness: PreviewHarness,
) -> None:
    await preview_harness.seed_sources(1)
    record = await preview_harness.request()
    await preview_harness.store.run_preview_activity(record.policy_preview_id, _context())

    await preview_harness.backdate_ready_expiry(record.policy_preview_id)

    expired = await preview_harness.store.get_preview(record.policy_preview_id, _context())
    assert expired.status is PreviewStatus.EXPIRED
    with pytest.raises(ExclusionPolicyError) as rejected:
        await preview_harness.store.list_preview_results(record.policy_preview_id, _context())
    assert rejected.value.error_code is ErrorCode.EXCLUSION_POLICY_PREVIEW_EXPIRED
    sweep = await preview_harness.store.expire_overdue_previews(datetime.now(UTC))
    assert sweep.ready_expired == 0  # The lazy read already expired the row.


@pytest.mark.asyncio
async def test_execution_deadline_sweep_fails_overdue_rows(
    preview_harness: PreviewHarness,
) -> None:
    await preview_harness.seed_sources(1)
    record = await preview_harness.request()
    await preview_harness.backdate_creation(record.policy_preview_id)

    sweep = await preview_harness.store.expire_overdue_previews(datetime.now(UTC))

    assert sweep.execution_deadline_failed == 1
    row = await preview_harness.fetch_preview_row(record.policy_preview_id)
    assert row["state"] == "failed"
    assert row["safe_error_code"] == PREVIEW_EXECUTION_DEADLINE_ERROR_CODE.value


@pytest.mark.asyncio
async def test_leased_outbox_claims_releases_and_reclaims(
    preview_harness: PreviewHarness,
) -> None:
    await preview_harness.seed_sources(1)
    record = await preview_harness.request()

    lease = await preview_harness.claim_exclusive(record.policy_preview_id)
    assert lease is not None
    assert lease.source_event_checkpoint == await preview_harness.current_checkpoint()
    row = await preview_harness.fetch_preview_row(record.policy_preview_id)
    assert row["state"] == "leased"
    assert row["lease_token"] == lease.lease_token
    assert int(row["attempt_count"]) == 1

    released = await preview_harness.store.release_retry(
        record.policy_preview_id,
        lease.lease_token,
        SafeToken.parse("exclusion_policy_commit_outcome_unknown"),
        datetime.now(UTC),
    )
    assert released
    row = await preview_harness.fetch_preview_row(record.policy_preview_id)
    assert row["state"] == "pending"
    assert row["safe_error_code"] == "exclusion_policy_commit_outcome_unknown"

    assert await preview_harness.claim_exclusive(record.policy_preview_id) is not None
    await preview_harness.expire_lease(record.policy_preview_id)
    reclaimed = await preview_harness.store.reclaim_expired_leases(datetime.now(UTC))
    assert reclaimed >= 1
    row = await preview_harness.fetch_preview_row(record.policy_preview_id)
    assert row["state"] == "pending"
    assert row["safe_error_code"] == PREVIEW_LEASE_EXPIRED_ERROR_CODE.value

    ready = await preview_harness.store.run_preview_activity(record.policy_preview_id, _context())
    assert ready.status is PreviewStatus.READY
