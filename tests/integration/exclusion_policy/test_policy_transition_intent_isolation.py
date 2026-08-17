"""Origin-isolation contract: policy-transition intents never reach ingestion.

The disposable stack backs the proof at every layer of the source-event
dispatch path (spec 8.5/15): the PostgreSQL claim select claims only
``source_event`` origins, so a due pending ``policy_transition`` row stays
pending — never leased, never marked terminal merely because its later
consumer is absent; the real dispatch loop over the real store starts only
the source-event intent's workflow; and even a hydrated policy-transition
lease is rejected by the closed ``source_ingestion_reference/v1`` input
contract before any Temporal call. The durable rows stay visible to
operations until the projection child phase installs
``policy-projection-transition/{workspace_id}/{policy_revision_id}/{source_id}``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from tests.integration.exclusion_policy.conftest import PolicyMigrationHarness
from workflow_worker.projection_dispatch_runtime import ProjectionDispatchRuntime
from workflow_worker.projection_workflow_starter import (
    SourceIngestionReference,
    source_ingestion_reference_for_intent,
)

from personal_os.diagnostics.events import SafeToken
from personal_os.error_contracts.codes import ErrorCode
from personal_os.sources.errors import ProjectionDispatchError
from personal_os.sources.projection_dispatch import (
    LeasedProjectionIntent,
    ProjectionIntentOriginKind,
)
from postgresql_source_store.projection_intents import PostgresqlProjectionIntentStore
from postgresql_source_store.tables import projection_intents

pytestmark = pytest.mark.local_stack

_FIXED_LEASED_UNTIL = datetime(2026, 8, 17, 12, 0, 0, tzinfo=UTC) + timedelta(seconds=60)


@dataclass
class RecordingStarter:
    """Start recorder proving which intents reached the workflow start."""

    calls: list[Any] = field(default_factory=list)

    async def start_source_ingestion(self, reference: Any) -> object:
        self.calls.append(reference)
        return object()


@pytest_asyncio.fixture
async def isolation_harness(
    policy_migration_harness: PolicyMigrationHarness,
) -> dict[str, Any]:
    engine = policy_migration_harness.engine
    store = PostgresqlProjectionIntentStore(engine, lease_token_generator=uuid4)
    # The migration conftest seeded one due pending source-event intent for
    # the workspace; add one due pending policy-transition intent bound to a
    # real published revision (the origin FK requires it).
    published = await policy_migration_harness.seed_published_policy()
    policy_transition_intent_id = uuid4()
    async with engine.begin() as connection:
        await connection.execute(
            projection_intents.insert().values(
                projection_intent_id=policy_transition_intent_id,
                workspace_id=policy_migration_harness.stack.workspace_id,
                origin_kind="policy_transition",
                event_id=None,
                policy_revision_id=published.policy_revision_id,
                source_id=policy_migration_harness.stack.seeded_source_id,
                source_version_id=None,
                projection_kind="qdrant",
                operation="delete",
                status="pending",
                attempt_count=0,
                available_at=sa.text("CURRENT_TIMESTAMP - interval '1 second'"),
                created_at=sa.text("CURRENT_TIMESTAMP - interval '1 second'"),
            )
        )
    return {
        "base": policy_migration_harness,
        "store": store,
        "policy_transition_intent_id": policy_transition_intent_id,
    }


def _intent_row_state_sql(intent_id: UUID) -> str:
    return (
        "SELECT status, attempt_count, lease_token, last_error_code"
        " FROM knowledge.projection_intents WHERE projection_intent_id = :intent_id"
    )


@pytest.mark.asyncio
async def test_claim_selects_only_source_event_origins(isolation_harness: dict[str, Any]) -> None:
    base: PolicyMigrationHarness = isolation_harness["base"]
    store: PostgresqlProjectionIntentStore = isolation_harness["store"]

    async with base.engine.connect() as connection:
        now_result = await connection.execute(sa.text("SELECT CURRENT_TIMESTAMP"))
    now = now_result.scalar_one()
    assert isinstance(now, datetime)

    claimed = await store.claim_batch(now, 50)

    claimed_origins = {intent.origin_kind for intent in claimed}
    assert claimed_origins == {ProjectionIntentOriginKind.SOURCE_EVENT}
    claimed_ids = {intent.projection_intent_id for intent in claimed}
    assert isolation_harness["policy_transition_intent_id"] not in claimed_ids

    # The policy-transition row stays exactly as durable: pending, unleased,
    # zero attempts — not terminal merely because its consumer is absent.
    row = await base.fetch_all(
        _intent_row_state_sql(isolation_harness["policy_transition_intent_id"]),
        {"intent_id": isolation_harness["policy_transition_intent_id"]},
    )
    assert row[0].status == "pending"
    assert int(row[0].attempt_count) == 0
    assert row[0].lease_token is None
    assert row[0].last_error_code is None


@pytest.mark.asyncio
async def test_dispatch_loop_never_starts_ingestion_for_policy_origins(
    isolation_harness: dict[str, Any],
) -> None:
    base: PolicyMigrationHarness = isolation_harness["base"]
    store: PostgresqlProjectionIntentStore = isolation_harness["store"]

    # Return the source-event intent of the previous test (acknowledged or
    # leased) back to pending so the loop sees both rows again.
    async with base.engine.begin() as connection:
        await connection.execute(
            sa.text(
                "UPDATE knowledge.projection_intents SET status = 'pending',"
                " lease_token = NULL, leased_until = NULL"
                " WHERE origin_kind = 'source_event'"
                " AND status IN ('leased', 'dispatched')"
            )
        )

    starter = RecordingStarter()
    runtime = ProjectionDispatchRuntime(
        store=store,  # type: ignore[arg-type]
        starter=starter,  # type: ignore[arg-type]
        clock=lambda: datetime.now(UTC),
    )
    await runtime.dispatch_pending_intents_once()

    # The claim handed the loop only source-event rows: the seeded intent
    # was claimed and left its leased state (the version-less row takes the
    # terminal input-contract path), while no start call can name a policy
    # transition — the closed input carries only canonical event references.
    for call in starter.calls:
        assert isinstance(call, SourceIngestionReference)
        assert call.event_id == base.stack.seeded_event_id

    source_event_row = await base.fetch_all(
        "SELECT status FROM knowledge.projection_intents"
        " WHERE origin_kind = 'source_event'"
        " AND event_id = :event_id",
        {"event_id": base.stack.seeded_event_id},
    )
    assert source_event_row[0].status != "pending"

    row = await base.fetch_all(
        _intent_row_state_sql(isolation_harness["policy_transition_intent_id"]),
        {"intent_id": isolation_harness["policy_transition_intent_id"]},
    )
    assert row[0].status == "pending"
    assert int(row[0].attempt_count) == 0


def test_policy_transition_lease_cannot_build_the_ingestion_input() -> None:
    intent = LeasedProjectionIntent(
        projection_intent_id=uuid4(),
        workspace_id=uuid4(),
        origin_kind=ProjectionIntentOriginKind.POLICY_TRANSITION,
        event_id=None,
        policy_revision_id=uuid4(),
        source_id=uuid4(),
        source_version_id=uuid4(),
        projection_kind=SafeToken.parse("qdrant"),
        operation=SafeToken.parse("delete"),
        attempt_count=0,
        lease_token=uuid4(),
        leased_until=_FIXED_LEASED_UNTIL,
    )

    with pytest.raises(ProjectionDispatchError) as raised:
        source_ingestion_reference_for_intent(intent)

    assert raised.value.error_code is ErrorCode.PROJECTION_INTENT_CONTRACT_INVALID
    assert raised.value.is_retryable is False
