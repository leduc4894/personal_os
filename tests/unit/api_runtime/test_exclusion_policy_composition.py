"""Exclusion-policy runtime composition: offline determinism and serve wiring.

The offline composition is the deterministic double the OpenAPI export and
the route tests consume: fixed identities, one seeded self-signed keyset
revision and no database, key file or environment read. The serve
composition builds the real service graph over the shared engine — drafts,
previews, publication and the plugin/query reads — and constructs without
opening a connection. The query service owns the keyset page bound: it
fetches one row beyond the page maximum so ``has_more`` is exact, and the
page maximum is the spec value 16.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Final, cast
from uuid import UUID

import pytest
import sqlalchemy as sa
from api_runtime.exclusion_policy_composition import (
    KEYSET_PAGE_MAXIMUM,
    STALE_RUNNING_PAGE_MAXIMUM,
    STALE_RUNNING_THRESHOLD_SECONDS,
    OfflineExclusionPolicyState,
    PolicyKeysetPage,
    PolicyQueryService,
    PostgresqlPolicyPluginReadStore,
    compose_exclusion_policy,
    compose_offline_exclusion_policy,
)
from api_runtime.exclusion_policy_models import PolicyReconciliationSummary
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncEngine

from personal_os.diagnostics.context import create_diagnostic_context
from personal_os.exclusion_policy.drafts import PolicyDraftService
from personal_os.exclusion_policy.ports import PolicyKeysetRecord
from personal_os.exclusion_policy.previews import (
    PREVIEW_EXECUTION_DEADLINE_SECONDS,
    PolicyPreviewRecord,
    PolicyPreviewService,
    PreviewStatus,
)
from personal_os.exclusion_policy.publication import ExclusionPolicyPublicationService
from personal_os.exclusion_policy.signatures import (
    build_keyset_payload,
    compute_payload_sha256_hex,
)

_FIXED_NOW: Final[datetime] = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
_WORKSPACE_ID: Final[UUID] = UUID("00000000-0000-7000-8000-000000000002")


def _context():
    return create_diagnostic_context().context


def _keyset_record(revision: int) -> PolicyKeysetRecord:
    payload = build_keyset_payload(
        workspace_id=_WORKSPACE_ID,
        keyset_revision=revision,
        parent_keyset_revision=None if revision == 1 else revision - 1,
        created_at=_FIXED_NOW,
        keys=(),
    )
    return PolicyKeysetRecord(
        policy_keyset_id=UUID(int=revision),
        workspace_id=_WORKSPACE_ID,
        keyset_revision=revision,
        parent_keyset_revision=None if revision == 1 else revision - 1,
        canonical_payload_bytes=payload,
        payload_sha256=compute_payload_sha256_hex(payload),
        keys=(),
        signatures=(),
        created_by_user_id=None,
        created_at=_FIXED_NOW,
    )


def test_offline_composition_builds_the_four_services() -> None:
    runtime = compose_offline_exclusion_policy()
    assert isinstance(runtime.drafts, PolicyDraftService)
    assert isinstance(runtime.previews, PolicyPreviewService)
    assert isinstance(runtime.publication, ExclusionPolicyPublicationService)
    assert isinstance(runtime.queries, PolicyQueryService)


@pytest.mark.asyncio
async def test_offline_composition_is_deterministic_across_invocations() -> None:
    left = compose_offline_exclusion_policy()
    right = compose_offline_exclusion_policy()
    left_page = await left.queries.list_keyset_page(_WORKSPACE_ID, 0, _context())
    right_page = await right.queries.list_keyset_page(_WORKSPACE_ID, 0, _context())
    assert left_page.has_more is False
    assert [row.keyset_revision for row in left_page.keysets] == [1]
    assert left_page.keysets[0].canonical_payload_bytes == (
        right_page.keysets[0].canonical_payload_bytes
    )
    assert left_page.keysets[0].payload_sha256 == right_page.keysets[0].payload_sha256


@pytest.mark.asyncio
async def test_query_service_slices_the_bounded_ordered_page() -> None:
    state = OfflineExclusionPolicyState()
    state.keyset_rows.extend(_keyset_record(revision) for revision in range(2, 22))
    runtime = compose_offline_exclusion_policy(state=state)
    context = _context()

    first = await runtime.queries.list_keyset_page(_WORKSPACE_ID, 0, context)
    assert isinstance(first, PolicyKeysetPage)
    assert len(first.keysets) == KEYSET_PAGE_MAXIMUM
    assert [row.keyset_revision for row in first.keysets] == list(range(1, 17))
    assert first.has_more is True

    tail = await runtime.queries.list_keyset_page(_WORKSPACE_ID, 16, context)
    assert [row.keyset_revision for row in tail.keysets] == [17, 18, 19, 20, 21]
    assert tail.has_more is False

    empty = await runtime.queries.list_keyset_page(_WORKSPACE_ID, 21, context)
    assert empty.keysets == ()
    assert empty.has_more is False


@pytest.mark.asyncio
async def test_query_service_status_combines_draft_and_reconciliation() -> None:
    state = OfflineExclusionPolicyState()
    runtime = compose_offline_exclusion_policy(state=state)
    context = _context()
    status = await runtime.queries.get_policy_status(_WORKSPACE_ID, context)
    assert status.active_policy_revision_id is None
    assert status.active_revision_number == 0
    assert status.draft.draft_version == 1
    assert await runtime.queries.get_reconciliation_summary(_WORKSPACE_ID, context) is None
    assert await runtime.queries.load_active_snapshot(_WORKSPACE_ID, context) is None


@pytest.mark.asyncio
async def test_offline_reconciliation_summary_carries_the_safe_error_code() -> None:
    """A failed reconciliation summary keeps its durable closed reason token.

    A summary built without a failure renders the null-safe absent reason,
    never a fake success token.
    """

    state = OfflineExclusionPolicyState()
    state.reconciliation_summary = PolicyReconciliationSummary(
        policy_revision_id=UUID(int=1), state="pending", updated_at=_FIXED_NOW
    )
    runtime = compose_offline_exclusion_policy(state=state)
    pending = await runtime.queries.get_reconciliation_summary(_WORKSPACE_ID, _context())
    assert pending is not None
    assert pending.safe_error_code is None

    state.reconciliation_summary = PolicyReconciliationSummary(
        policy_revision_id=UUID(int=1),
        state="terminal",
        updated_at=_FIXED_NOW,
        safe_error_code="reconciliation_dispatch_terminal",
    )
    failed = await runtime.queries.get_reconciliation_summary(_WORKSPACE_ID, _context())
    assert failed is not None
    assert failed.safe_error_code == "reconciliation_dispatch_terminal"


def _preview_record(
    *,
    preview_id: UUID,
    status: PreviewStatus,
    created_at: datetime,
    impact_digest: str | None = None,
    ready_at: datetime | None = None,
) -> PolicyPreviewRecord:
    """Build one offline preview row for staleness seeding."""

    return PolicyPreviewRecord(
        policy_preview_id=preview_id,
        workspace_id=_WORKSPACE_ID,
        policy_draft_id=UUID(int=1),
        draft_version=1,
        draft_sha256="a" * 64,
        base_policy_revision_id=None,
        source_checkpoint_event_sequence=0,
        status=status,
        impact_digest=impact_digest,
        safe_error_code=None,
        created_by_user_id=UUID(int=2),
        created_at=created_at,
        ready_at=ready_at,
        expires_at=None,
        consumed_at=None,
    )


@pytest.mark.asyncio
async def test_stale_running_previews_report_the_age_of_overdue_rows() -> None:
    """Rows the worker owes a transition to are reported with their age.

    The staleness read covers exactly the executable states the worker's
    overdue sweep fails (pending, leased, running — all rendered "Preview
    running" by the Admin UI) and reports each row beyond the bound with its
    age in seconds, oldest first. Terminal rows are never stale.
    """

    state = OfflineExclusionPolicyState()
    stale_running_id = UUID(int=101)
    stale_pending_id = UUID(int=102)
    state.preview_rows[stale_running_id] = _preview_record(
        preview_id=stale_running_id,
        status=PreviewStatus.RUNNING,
        created_at=_FIXED_NOW - timedelta(seconds=2 * PREVIEW_EXECUTION_DEADLINE_SECONDS),
    )
    state.preview_rows[stale_pending_id] = _preview_record(
        preview_id=stale_pending_id,
        status=PreviewStatus.PENDING,
        created_at=_FIXED_NOW - timedelta(seconds=PREVIEW_EXECUTION_DEADLINE_SECONDS),
    )
    state.preview_rows[UUID(int=103)] = _preview_record(
        preview_id=UUID(int=103),
        status=PreviewStatus.RUNNING,
        created_at=_FIXED_NOW,
    )
    state.preview_rows[UUID(int=104)] = _preview_record(
        preview_id=UUID(int=104),
        status=PreviewStatus.READY,
        created_at=_FIXED_NOW - timedelta(seconds=10 * PREVIEW_EXECUTION_DEADLINE_SECONDS),
        impact_digest="b" * 64,
        ready_at=_FIXED_NOW,
    )
    runtime = compose_offline_exclusion_policy(state=state)

    stale = await runtime.queries.get_stale_running_previews(_WORKSPACE_ID, _context())

    assert [(row.policy_preview_id, row.age_seconds) for row in stale] == [
        (stale_running_id, 2 * PREVIEW_EXECUTION_DEADLINE_SECONDS),
        (stale_pending_id, PREVIEW_EXECUTION_DEADLINE_SECONDS),
    ]


@pytest.mark.asyncio
async def test_stale_running_previews_stay_empty_when_nothing_is_stale() -> None:
    """No executable row beyond the bound renders no stale rows at all."""

    state = OfflineExclusionPolicyState()
    state.preview_rows[UUID(int=201)] = _preview_record(
        preview_id=UUID(int=201),
        status=PreviewStatus.RUNNING,
        created_at=_FIXED_NOW,
    )
    runtime = compose_offline_exclusion_policy(state=state)

    stale = await runtime.queries.get_stale_running_previews(_WORKSPACE_ID, _context())

    assert stale == ()


@dataclass
class _FakeRowResult:
    """Result double answering one row for the summary select."""

    row: Mapping[str, object] | None

    def mappings(self) -> _FakeRowResult:
        return self

    def first(self) -> Mapping[str, object] | None:
        return self.row


@dataclass
class _FakeConnection:
    """Connection double capturing the summary select and answering one row."""

    row: Mapping[str, object] | None = None
    selects: list[object] = field(default_factory=list)

    async def __aenter__(self) -> _FakeConnection:
        return self

    async def __aexit__(self, *exception_details: object) -> bool:
        return False

    def begin(self) -> _FakeConnection:
        return self

    async def execute(self, statement: object) -> _FakeRowResult:
        if isinstance(statement, sa.Select):
            self.selects.append(statement)
            return _FakeRowResult(self.row)
        return _FakeRowResult(None)


@dataclass
class _FakeEngine:
    """Engine double handing out its single connection."""

    connection: _FakeConnection

    def connect(self) -> _FakeConnection:
        return self.connection


@dataclass
class _FakeListRowResult:
    """Result double answering the full row list of the staleness select."""

    rows: list[Mapping[str, object]]

    def mappings(self) -> _FakeListRowResult:
        return self

    def all(self) -> list[Mapping[str, object]]:
        return self.rows


@dataclass
class _FakeListConnection:
    """Connection double capturing the staleness select and answering rows."""

    rows: list[Mapping[str, object]] = field(default_factory=list)
    selects: list[object] = field(default_factory=list)

    async def __aenter__(self) -> _FakeListConnection:
        return self

    async def __aexit__(self, *exception_details: object) -> bool:
        return False

    def begin(self) -> _FakeListConnection:
        return self

    async def execute(self, statement: object) -> _FakeListRowResult:
        if isinstance(statement, sa.Select):
            self.selects.append(statement)
            return _FakeListRowResult(self.rows)
        return _FakeListRowResult([])


@dataclass
class _FakeListEngine:
    """Engine double handing out its single list-answering connection."""

    connection: _FakeListConnection

    def connect(self) -> _FakeListConnection:
        return self.connection


@pytest.mark.asyncio
async def test_serve_staleness_read_selects_executable_rows_beyond_the_bound() -> None:
    """The serve staleness read is one bounded read-only select (W3/C5).

    The compiled PostgreSQL statement is pinned end to end: the workspace
    filter, the exact executable state set (the set the worker's overdue
    sweep fails), the staleness bound derived from the database clock
    against ``created_at``, the oldest-first order and the bounded page — so
    a serve-only regression (wrong state set, dropped workspace filter,
    off-by-one bound) cannot pass. The hydration carries only the opaque id
    plus the age in seconds.
    """

    stale_id = UUID(int=301)
    connection = _FakeListConnection(rows=[{"policy_preview_id": stale_id, "age_seconds": 2400}])
    store = PostgresqlPolicyPluginReadStore(cast("AsyncEngine", _FakeListEngine(connection)))

    stale = await store.list_stale_running_previews(_WORKSPACE_ID, _context())

    (select_statement,) = connection.selects
    selected_columns = {
        column.name for column in cast("sa.Select", select_statement).selected_columns
    }
    assert "policy_preview_id" in selected_columns
    # The bound is the domain's own execution deadline — the moment a live
    # worker's sweep would have failed the row — pinned so an off-by-one
    # drift of the constant itself cannot pass either.
    assert STALE_RUNNING_THRESHOLD_SECONDS == PREVIEW_EXECUTION_DEADLINE_SECONDS
    compiled_sql = " ".join(
        str(
            cast("sa.Select", select_statement).compile(
                dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
            )
        ).split()
    )
    assert f"knowledge.policy_previews.workspace_id = '{_WORKSPACE_ID}'" in compiled_sql, (
        compiled_sql
    )
    assert "knowledge.policy_previews.state IN ('pending', 'leased', 'running')" in compiled_sql, (
        compiled_sql
    )
    assert (
        "knowledge.policy_previews.created_at <= CURRENT_TIMESTAMP "
        f"- make_interval(0, 0, 0, 0, 0, 0, {STALE_RUNNING_THRESHOLD_SECONDS})" in compiled_sql
    ), compiled_sql
    assert "ORDER BY knowledge.policy_previews.created_at ASC" in compiled_sql, compiled_sql
    assert f"LIMIT {STALE_RUNNING_PAGE_MAXIMUM}" in compiled_sql, compiled_sql
    assert [(row.policy_preview_id, row.age_seconds) for row in stale] == [(stale_id, 2400)]


@pytest.mark.asyncio
async def test_serve_reconciliation_summary_selects_and_returns_the_safe_error_code() -> None:
    """The serve summary read selects the durable reason column (W2 parity)."""

    connection = _FakeConnection(
        row={
            "policy_revision_id": UUID(int=1),
            "state": "terminal",
            "updated_at": _FIXED_NOW,
            "safe_error_code": "reconciliation_dispatch_terminal",
        }
    )
    store = PostgresqlPolicyPluginReadStore(cast("AsyncEngine", _FakeEngine(connection)))

    summary = await store.get_reconciliation_summary(_WORKSPACE_ID, _context())

    (select_statement,) = connection.selects
    selected_columns = {
        column.name for column in cast("sa.Select", select_statement).selected_columns
    }
    assert {"policy_revision_id", "state", "updated_at", "safe_error_code"} <= selected_columns
    assert summary is not None
    assert summary.state == "terminal"
    assert summary.safe_error_code == "reconciliation_dispatch_terminal"


def test_serve_composition_constructs_over_the_engine_without_io() -> None:
    from api_runtime.exclusion_policy_crypto import Ed25519PolicySigner

    signer = Ed25519PolicySigner.from_seed_bytes(bytes(range(32)))
    runtime = compose_exclusion_policy(engine=cast("AsyncEngine", object()), signer=signer)
    assert isinstance(runtime.drafts, PolicyDraftService)
    assert isinstance(runtime.previews, PolicyPreviewService)
    assert isinstance(runtime.publication, ExclusionPolicyPublicationService)
    assert isinstance(runtime.queries, PolicyQueryService)
