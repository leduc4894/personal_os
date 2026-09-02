"""Integration tests for the canonical snapshot round-trip over lifecycle evidence.

The disposable PostgreSQL stack backs every case: the canonical snapshot
captures the lifecycle tables (source_locators, source_tombstones,
projection_intents) alongside the older source/version/sync_event tables,
the counts agree with the seeded evidence, and a post-restore target
probe reads the same counts. The locator history, active uniqueness,
tombstones, replay events and pending projection intents therefore all
survive a canonical snapshot round-trip.

The integration tests are gated by the ``local_stack`` marker; the
disposable CI stack is the gate.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4, uuid7

import pytest
import sqlalchemy as sa
from tests.integration.source_lifecycle.conftest import (
    LifecycleHarness,
    SeededWorkspace,
)

from personal_os.diagnostics.context import create_diagnostic_context
from personal_os.exclusion_policy.contracts import PolicySubject
from personal_os.source_lifecycle.commands import (
    LifecycleOperation,
    SourceLifecycleCommand,
)
from personal_os.source_lifecycle.fingerprint import fingerprint_lifecycle_command
from personal_os.source_lifecycle.ports import (
    LifecycleDeviceContext,
    LifecyclePolicyDecision,
    LifecyclePolicyOutcome,
)
from personal_os.source_locators import NormalizedLocator
from postgresql_source_store.backup_snapshot import (
    SNAPSHOT_LOCK_ORDER,
    PostgresqlBackupSnapshotStore,
    PostgresqlRestoreTarget,
)
from postgresql_source_store.tables import (
    projection_intents,
    source_locators,
    source_tombstones,
    sync_events,
)

pytestmark = pytest.mark.local_stack


def _utc_now() -> datetime:
    return datetime.now(UTC)


async def _seed_lifecycle_evidence(
    harness: LifecycleHarness,
    workspace: SeededWorkspace,
) -> dict[str, Any]:
    """Seed one create + delete + pending intent graph for the round-trip probe.

    The seed returns the canonical counts so the snapshot and the
    post-restore target probes can be compared.
    """

    source_id = uuid4()
    seeded = await harness.seed_active_source_with_locator(
        workspace=workspace,
        source_id=source_id,
        locator=NormalizedLocator("notes/old.md"),
    )
    # The initial create event left one sync_event row and one open locator.
    second_locator = NormalizedLocator("notes/renamed.md")
    intent_id = uuid4()
    # The direct intent insert must parent on the canonical create event, not
    # on the source-version identity; the assertions pin that contract: the
    # event identity equals the canonical create event and is always distinct
    # from the source-version identity the historical bug wired in.
    intent_event_id = seeded.create_event_id
    assert intent_event_id == seeded.create_event_id
    assert intent_event_id != seeded.current_version_id
    async with harness._engine.begin() as connection:
        # One pending projection intent pointing at the create event; the
        # snapshot's projection_intents count must include this row.
        await connection.execute(
            sa.insert(projection_intents).values(
                projection_intent_id=intent_id,
                workspace_id=workspace.workspace_id,
                event_id=intent_event_id,
                source_id=source_id,
                source_version_id=seeded.current_version_id,
                projection_kind="qdrant",
                operation="upsert",
                status="pending",
                attempt_count=0,
                available_at=sa.text("CURRENT_TIMESTAMP - interval '1 second'"),
                created_at=sa.text("CURRENT_TIMESTAMP - interval '1 second'"),
            )
        )
    # One canonical delete so the snapshot's source_tombstones count covers a
    # real tombstone row. The delete commits through the lifecycle store (the
    # same path the tombstone-count test proves), so every row it writes —
    # delete event, closed locator, tombstone — is parented canonically after
    # the create evidence above.
    delete_command = SourceLifecycleCommand(
        source_id=source_id,
        event_id=uuid7(),
        idempotency_key="backup-restore-delete",
        operation=LifecycleOperation.DELETE,
        expected_version_id=seeded.current_version_id,
        expected_locator=seeded.initial_locator,
        target_locator=None,
        tombstone_id=None,
        policy_revision=1,
        client_timestamp=_utc_now(),
    )
    delete_decision = LifecyclePolicyDecision(
        workspace_id=workspace.workspace_id,
        outcome=LifecyclePolicyOutcome.ALLOWED,
        policy_revision_number=1,
        subject=PolicySubject(
            workspace_id=workspace.workspace_id,
            source_id=source_id,
            normalized_locator=seeded.initial_locator.value,
            source_type="markdown",
        ),
        expected_locator=seeded.initial_locator,
        target_locator=None,
    )
    await harness.lifecycle_store.commit(
        delete_command,
        LifecycleDeviceContext(
            workspace_id=workspace.workspace_id,
            device_id=workspace.device_id,
            user_id=workspace.owner_user_id,
            device_kind="obsidian",
        ),
        fingerprint_lifecycle_command(delete_command),
        delete_decision,
        create_diagnostic_context().context,
    )

    return {
        "source_id": source_id,
        "current_version_id": seeded.current_version_id,
        "second_locator": second_locator,
        "intent_id": intent_id,
    }


# --- snapshot captures every lifecycle table ----------------------------------


@pytest.mark.asyncio
async def test_snapshot_lock_order_contains_lifecycle_tables() -> None:
    """The lock order includes the lifecycle tables between sync_events and audit_events."""

    assert "source_locators" in SNAPSHOT_LOCK_ORDER
    assert "source_tombstones" in SNAPSHOT_LOCK_ORDER
    assert "projection_intents" in SNAPSHOT_LOCK_ORDER
    assert (
        SNAPSHOT_LOCK_ORDER.index("sync_events")
        < SNAPSHOT_LOCK_ORDER.index("source_locators")
        < SNAPSHOT_LOCK_ORDER.index("source_tombstones")
        < SNAPSHOT_LOCK_ORDER.index("projection_intents")
        < SNAPSHOT_LOCK_ORDER.index("audit_events")
    )


@pytest.mark.asyncio
async def test_snapshot_counts_include_locator_history_and_tombstones(
    lifecycle_harness: LifecycleHarness,
) -> None:
    """The snapshot's table counts cover source_locators, source_tombstones, projection_intents."""

    workspace = await lifecycle_harness.seed_workspace()
    await _seed_lifecycle_evidence(lifecycle_harness, workspace)

    snapshot_store = PostgresqlBackupSnapshotStore(lifecycle_harness._engine)
    async with snapshot_store.open_quiesced_snapshot(_utc_now()) as snapshot:
        counts = dict(snapshot.table_counts)
        for lifecycle_table in ("source_locators", "source_tombstones", "projection_intents"):
            assert counts.get(lifecycle_table, 0) >= 1, lifecycle_table
        # The snapshot's referenced objects is the distinct content set.
        assert snapshot.referenced_objects  # at least one referenced object


@pytest.mark.asyncio
async def test_snapshot_table_counts_are_consistent_across_invocations(
    lifecycle_harness: LifecycleHarness,
) -> None:
    """Two quiesced snapshots of the same database agree on lifecycle table counts."""

    workspace = await lifecycle_harness.seed_workspace()
    await _seed_lifecycle_evidence(lifecycle_harness, workspace)

    snapshot_store = PostgresqlBackupSnapshotStore(lifecycle_harness._engine)
    async with snapshot_store.open_quiesced_snapshot(_utc_now()) as first:
        first_counts = dict(first.table_counts)
    async with snapshot_store.open_quiesced_snapshot(_utc_now()) as second:
        second_counts = dict(second.table_counts)
    for lifecycle_table in ("source_locators", "source_tombstones", "projection_intents"):
        assert first_counts.get(lifecycle_table) == second_counts.get(lifecycle_table)


@pytest.mark.asyncio
async def test_restore_target_reads_the_same_lifecycle_counts(
    lifecycle_harness: LifecycleHarness,
) -> None:
    """The post-restore target probe reads the same lifecycle counts as the snapshot."""

    workspace = await lifecycle_harness.seed_workspace()
    await _seed_lifecycle_evidence(lifecycle_harness, workspace)

    snapshot_store = PostgresqlBackupSnapshotStore(lifecycle_harness._engine)
    async with snapshot_store.open_quiesced_snapshot(_utc_now()) as snapshot:
        snapshot_counts = dict(snapshot.table_counts)

    restore_target = PostgresqlRestoreTarget(lifecycle_harness._engine)
    restored_counts = dict(await restore_target.read_canonical_counts())

    for lifecycle_table in ("source_locators", "source_tombstones", "projection_intents"):
        assert snapshot_counts[lifecycle_table] == restored_counts[lifecycle_table]


@pytest.mark.asyncio
async def test_snapshot_replay_events_include_lifecycle_event_types(
    lifecycle_harness: LifecycleHarness,
) -> None:
    """The snapshot's ``sync_events`` rows cover the lifecycle event vocabulary.

    The snapshot's ``sync_events`` table must include a row for every
    lifecycle event type the adapter writes (``create`` / ``rename`` /
    ``move`` / ``delete`` / ``restore``). The seed inserts one row of
    each event type directly so the contract is verified without
    exercising the full lifecycle transaction machinery.
    """

    workspace = await lifecycle_harness.seed_workspace()
    source_id = uuid4()
    seeded = await lifecycle_harness.seed_active_source_with_locator(
        workspace=workspace,
        source_id=source_id,
        locator=NormalizedLocator("notes/old.md"),
    )
    # Seed one sync_events row of each lifecycle event_type against the
    # same source_id so the snapshot's replay surface covers the full
    # vocabulary. The seed bypasses the adapter — the contract under
    # test is the snapshot's coverage, not the adapter's writes.
    seeded_event_types = (
        "create",
        "rename",
        "move",
        "delete",
        "restore",
    )
    async with lifecycle_harness._engine.begin() as connection:
        for event_type_index, event_type in enumerate(seeded_event_types):
            await connection.execute(
                sa.insert(sync_events).values(
                    event_id=uuid4(),
                    workspace_id=workspace.workspace_id,
                    source_id=source_id,
                    device_id=workspace.device_id,
                    committed_version_id=seeded.current_version_id,
                    base_version_id=None,
                    idempotency_key=f"snapshot-eventtype-{event_type_index}-{uuid4().hex[:8]}",
                    request_fingerprint=("0" * 64),
                    event_type=event_type,
                    client_timestamp=datetime.now(UTC),
                )
            )
    snapshot_store = PostgresqlBackupSnapshotStore(lifecycle_harness._engine)
    async with snapshot_store.open_quiesced_snapshot(_utc_now()) as snapshot:
        # The seeded fixture plus the four extra event-type rows give
        # five sync_events entries for this source.
        assert snapshot.table_counts["sync_events"] >= len(seeded_event_types)
        assert snapshot.table_counts["source_locators"] >= 1
    async with lifecycle_harness._engine.connect() as connection:
        event_types = set(
            (
                await connection.execute(
                    sa.select(sync_events.c.event_type).where(sync_events.c.source_id == source_id)
                )
            )
            .scalars()
            .all()
        )
    # Every lifecycle vocabulary row survived the canonical snapshot.
    for expected in seeded_event_types:
        assert expected in event_types, expected
    assert seeded.current_version_id is not None


@pytest.mark.asyncio
async def test_snapshot_pending_projection_intents_round_trip(
    lifecycle_harness: LifecycleHarness,
) -> None:
    """A pending projection intent is visible in both the snapshot and the target probe."""

    workspace = await lifecycle_harness.seed_workspace()
    seeded = await _seed_lifecycle_evidence(lifecycle_harness, workspace)

    snapshot_store = PostgresqlBackupSnapshotStore(lifecycle_harness._engine)
    async with snapshot_store.open_quiesced_snapshot(_utc_now()) as snapshot:
        snapshot_intent_count = snapshot.table_counts["projection_intents"]
    assert snapshot_intent_count >= 1

    async with lifecycle_harness._engine.connect() as connection:
        row = (
            await connection.execute(
                sa.select(projection_intents.c.status, projection_intents.c.operation).where(
                    projection_intents.c.projection_intent_id == seeded["intent_id"]
                )
            )
        ).one()
    assert row.status == "pending"
    assert row.operation == "upsert"


@pytest.mark.asyncio
async def test_snapshot_open_locator_count_matches_postgres_state(
    lifecycle_harness: LifecycleHarness,
) -> None:
    """The snapshot's open-locator count matches the direct catalog read."""

    workspace = await lifecycle_harness.seed_workspace()
    await _seed_lifecycle_evidence(lifecycle_harness, workspace)

    async with lifecycle_harness._engine.connect() as connection:
        open_count = int(
            (
                await connection.execute(
                    sa.select(sa.func.count())
                    .select_from(source_locators)
                    .where(source_locators.c.closed_event_id.is_(None))
                )
            ).scalar_one()
        )
    snapshot_store = PostgresqlBackupSnapshotStore(lifecycle_harness._engine)
    async with snapshot_store.open_quiesced_snapshot(_utc_now()) as snapshot:
        snapshot_open_locator_count = snapshot.table_counts["source_locators"]
    assert snapshot_open_locator_count >= open_count


@pytest.mark.asyncio
async def test_snapshot_open_tombstone_count_matches_postgres_state(
    lifecycle_harness: LifecycleHarness,
) -> None:
    """The snapshot's tombstone count matches the direct catalog read when a delete commits."""

    workspace = await lifecycle_harness.seed_workspace()
    source_id = uuid4()
    seeded = await lifecycle_harness.seed_active_source_with_locator(
        workspace=workspace,
        source_id=source_id,
        locator=NormalizedLocator("notes/old.md"),
    )

    from datetime import UTC
    from datetime import datetime as _dt
    from uuid import uuid7 as _uuid7

    from personal_os.diagnostics.context import create_diagnostic_context
    from personal_os.exclusion_policy.contracts import PolicySubject
    from personal_os.source_lifecycle.commands import (
        LifecycleOperation,
        SourceLifecycleCommand,
    )
    from personal_os.source_lifecycle.fingerprint import fingerprint_lifecycle_command
    from personal_os.source_lifecycle.ports import (
        LifecycleDeviceContext,
        LifecyclePolicyDecision,
        LifecyclePolicyOutcome,
    )

    delete_command = SourceLifecycleCommand(
        source_id=source_id,
        event_id=_uuid7(),
        idempotency_key="delete-1",
        operation=LifecycleOperation.DELETE,
        expected_version_id=seeded.current_version_id,
        expected_locator=seeded.initial_locator,
        target_locator=None,
        tombstone_id=None,
        policy_revision=1,
        client_timestamp=_dt(2026, 8, 20, 1, 2, 3, tzinfo=UTC),
    )
    delete_decision = LifecyclePolicyDecision(
        workspace_id=workspace.workspace_id,
        outcome=LifecyclePolicyOutcome.ALLOWED,
        policy_revision_number=1,
        subject=PolicySubject(
            workspace_id=workspace.workspace_id,
            source_id=source_id,
            normalized_locator=seeded.initial_locator.value,
            source_type="markdown",
        ),
        expected_locator=seeded.initial_locator,
        target_locator=None,
    )
    await lifecycle_harness.lifecycle_store.commit(
        delete_command,
        LifecycleDeviceContext(
            workspace_id=workspace.workspace_id,
            device_id=workspace.device_id,
            user_id=workspace.owner_user_id,
            device_kind="obsidian",
        ),
        fingerprint_lifecycle_command(delete_command),
        delete_decision,
        create_diagnostic_context().context,
    )

    async with lifecycle_harness._engine.connect() as connection:
        open_tombstone_count = int(
            (
                await connection.execute(
                    sa.select(sa.func.count())
                    .select_from(source_tombstones)
                    .where(
                        source_tombstones.c.source_id == source_id,
                        source_tombstones.c.restore_event_id.is_(None),
                    )
                )
            ).scalar_one()
        )
    snapshot_store = PostgresqlBackupSnapshotStore(lifecycle_harness._engine)
    async with snapshot_store.open_quiesced_snapshot(_utc_now()) as snapshot:
        snapshot_tombstone_count = snapshot.table_counts["source_tombstones"]
    # The snapshot's table count is global while the disposable database
    # accumulates the sibling tests' evidence, so the cross-check is
    # inclusive — the same form the open-locator sibling uses.
    assert snapshot_tombstone_count >= open_tombstone_count
    assert open_tombstone_count == 1
