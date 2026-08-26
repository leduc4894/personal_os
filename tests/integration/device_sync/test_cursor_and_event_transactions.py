"""Disposable PostgreSQL transaction coverage for device cursors and events.

Live coverage of the durable semantics the unit seam cannot prove: a fresh
device starts at cursor zero, pull pages only through the statement
checkpoint it first read while advancing nothing but the delivered
watermark, acknowledge locks the exact workspace/device cursor row and is
monotonic under concurrency (identical concurrent acks serialize into one
frozen replay, conflicting concurrent acks never regress the cursor),
regression and ack-ahead rejections fail closed, retained history that fell
below a cursor above the workspace compaction floor is the closed cursor
gap while the floor-owning device still pulls, an unhydratable operand
fails the whole pull with the closed integrity error instead of skipping
the row, and the workspace compaction floor follows only active devices.
"""

from __future__ import annotations

import asyncio
import hashlib
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.device_sync.conftest import (
    DeviceEventHistory,
    DeviceRenameUpdateHistory,
    DeviceSyncWorkspace,
    seed_device_event_history,
    seed_device_rename_update_history,
    seed_device_sync_workspace,
)
from tests.integration.source_publication.conftest import SourcePublicationStack

from personal_os.device_sync.contracts import DeviceEventType
from personal_os.device_sync.errors import DeviceSyncError, DeviceSyncErrorCode
from personal_os.diagnostics.context import DiagnosticContext, TraceContext
from personal_os.diagnostics.trace_context import SpanId, TraceId
from postgresql_source_store.device_event_store import PostgresqlDeviceEventStore
from postgresql_source_store.engine import (
    create_source_store_engine,
    dispose_source_store_engine,
)
from postgresql_source_store.tables import (
    device_cursors,
    devices,
    source_locators,
    source_tombstones,
    sync_events,
)

pytestmark = pytest.mark.local_stack

_TRACE = TraceContext(
    trace_id=TraceId("0123456789abcdef0123456789abcdef"),
    remote_parent_span_id=None,
    local_span_id=SpanId("0123456789abcdef"),
    trace_flags=0,
)


def _diagnostic() -> DiagnosticContext:
    return DiagnosticContext(request_id=uuid4(), client_request_id=None, trace=_TRACE)


class DeviceEventStoreHarness:
    """One disposable engine, its device event store and seeding helpers."""

    def __init__(self, engine: AsyncEngine, store: PostgresqlDeviceEventStore) -> None:
        self.engine = engine
        self.store = store

    async def seed_history(self) -> DeviceEventHistory:
        return await seed_device_event_history(self.engine)

    async def seed_rename_update_history(self) -> DeviceRenameUpdateHistory:
        return await seed_device_rename_update_history(self.engine)

    async def seed_workspace(self) -> DeviceSyncWorkspace:
        return await seed_device_sync_workspace(self.engine)

    async def seed_device_in_workspace(self, workspace: DeviceSyncWorkspace) -> UUID:
        device_id = uuid4()
        async with self.engine.begin() as connection:
            await connection.execute(
                sa.insert(devices).values(
                    device_id=device_id,
                    workspace_id=workspace.workspace_id,
                    user_id=workspace.owner_user_id,
                    device_name=f"Extra device {device_id.hex[:8]}",
                    device_kind="obsidian",
                )
            )
        return device_id

    async def commit_trailing_update(self, history: DeviceEventHistory) -> int:
        """Commit one further update event beyond the seeded history."""

        event_id = uuid4()
        idempotency_key = f"device-tx-{event_id.hex}"
        async with self.engine.begin() as connection:
            result = await connection.execute(
                sa.insert(sync_events)
                .values(
                    event_id=event_id,
                    workspace_id=history.workspace.workspace_id,
                    source_id=history.source_id,
                    device_id=None,
                    committed_version_id=history.version_ids[1],
                    base_version_id=history.version_ids[1],
                    idempotency_key=idempotency_key,
                    request_fingerprint=hashlib.sha256(idempotency_key.encode("ascii")).hexdigest(),
                    event_type="update",
                )
                .returning(sync_events.c.event_sequence)
            )
            return int(result.scalar_one())

    async def delete_unreferenced_history(self, history: DeviceEventHistory) -> None:
        """Remove exactly the two events no lifecycle row references."""

        referenced_ids = {history.event_ids[index] for index in (0, 2, 3, 4)}
        doomed_ids = [event_id for event_id in history.event_ids if event_id not in referenced_ids]
        async with self.engine.begin() as connection:
            await connection.execute(
                sa.delete(sync_events).where(sync_events.c.event_id.in_(doomed_ids))
            )

    async def read_cursor_row(
        self, workspace: DeviceSyncWorkspace, device_id: UUID
    ) -> tuple[int, int] | None:
        async with self.engine.connect() as connection:
            row = (
                await connection.execute(
                    sa.select(
                        device_cursors.c.acknowledged_sequence,
                        device_cursors.c.delivered_through_sequence,
                    ).where(
                        device_cursors.c.workspace_id == workspace.workspace_id,
                        device_cursors.c.device_id == device_id,
                    )
                )
            ).one_or_none()
        if row is None:
            return None
        return int(row.acknowledged_sequence), int(row.delivered_through_sequence)

    async def revoke_device(self, device_id: UUID, workspace: DeviceSyncWorkspace) -> None:
        async with self.engine.begin() as connection:
            await connection.execute(
                sa.update(devices)
                .values(
                    status="revoked",
                    revoked_at=sa.text("CURRENT_TIMESTAMP"),
                )
                .where(
                    devices.c.workspace_id == workspace.workspace_id,
                    devices.c.device_id == device_id,
                )
            )

    async def delete_entire_history(self, history: DeviceEventHistory) -> None:
        """Remove every canonical row of one workspace's event history.

        Locators and tombstones go first because their restricting foreign
        keys pin the events they reference; the events themselves go last,
        leaving the workspace with no retained history at all.
        """

        async with self.engine.begin() as connection:
            await connection.execute(
                sa.delete(source_locators).where(
                    source_locators.c.workspace_id == history.workspace.workspace_id
                )
            )
            await connection.execute(
                sa.delete(source_tombstones).where(
                    source_tombstones.c.workspace_id == history.workspace.workspace_id
                )
            )
            await connection.execute(
                sa.delete(sync_events).where(
                    sync_events.c.workspace_id == history.workspace.workspace_id
                )
            )


@pytest_asyncio.fixture
async def device_event_store(
    source_publication_stack: SourcePublicationStack,
) -> DeviceEventStoreHarness:
    engine = create_source_store_engine(
        source_publication_stack.settings, source_publication_stack.password
    )
    try:
        yield DeviceEventStoreHarness(engine, PostgresqlDeviceEventStore(engine))
    finally:
        await dispose_source_store_engine(engine)


@pytest.mark.asyncio
async def test_fresh_device_pulls_cursor_zero_before_any_history(
    device_event_store: DeviceEventStoreHarness,
) -> None:
    workspace = await device_event_store.seed_workspace()
    context = workspace.context()
    page = await device_event_store.store.pull_events(
        context, limit=200, diagnostic_context=_diagnostic()
    )
    assert page.acknowledged_sequence == 0
    assert page.delivered_through_sequence == 0
    assert page.page_checkpoint_sequence == 0
    assert page.events == ()
    assert page.has_more is False


@pytest.mark.asyncio
async def test_pull_hydrates_every_operation_shape_in_sequence_order(
    device_event_store: DeviceEventStoreHarness,
) -> None:
    history = await device_event_store.seed_history()
    page = await device_event_store.store.pull_events(
        history.workspace.context(), limit=200, diagnostic_context=_diagnostic()
    )
    assert [event.event_type for event in page.events] == [
        DeviceEventType.CREATED,
        DeviceEventType.UPDATED,
        DeviceEventType.RENAMED,
        DeviceEventType.DELETED,
        DeviceEventType.RESTORED,
        DeviceEventType.UPDATED,
    ]
    assert [event.event_id for event in page.events] == list(history.event_ids)
    assert page.acknowledged_sequence == 0
    trailing = history.event_sequences["trailing_update"]
    assert page.delivered_through_sequence == trailing
    assert page.page_checkpoint_sequence == trailing
    assert page.has_more is False

    created, updated, renamed, deleted, restored, trailing_update = page.events
    assert created.base_version_id is None
    assert created.current_version_id == history.version_ids[0]
    assert created.current_fingerprint is not None
    assert created.resulting_locator is not None
    assert created.resulting_locator.value.endswith("alpha.md")
    assert created.prior_locator is None
    assert created.tombstone_id is None

    assert updated.base_version_id == history.version_ids[0]
    assert updated.current_version_id == history.version_ids[1]
    assert updated.base_fingerprint is not None
    assert updated.current_fingerprint is not None
    # The update's content target is the locator the source held open at
    # the event's own sequence: alpha.md (post-create, pre-rename) — not
    # the current gamma.md path and never a null operand.
    assert updated.resulting_locator is not None
    assert updated.resulting_locator.value.endswith("alpha.md")
    assert updated.prior_locator is None
    assert updated.tombstone_id is None

    assert renamed.prior_locator is not None
    assert renamed.prior_locator.value.endswith("alpha.md")
    assert renamed.resulting_locator is not None
    assert renamed.resulting_locator.value.endswith("beta.md")
    assert renamed.tombstone_id is None

    assert deleted.prior_locator is not None
    assert deleted.prior_locator.value.endswith("beta.md")
    assert deleted.resulting_locator is None
    assert deleted.tombstone_id == history.tombstone_id

    assert restored.resulting_locator is not None
    assert restored.resulting_locator.value.endswith("gamma.md")
    assert restored.prior_locator is None
    assert restored.tombstone_id == history.tombstone_id

    assert trailing_update.base_version_id == history.version_ids[1]
    # The trailing update commits after the restore, so its active locator
    # at its own sequence is the post-restore gamma.md path.
    assert trailing_update.resulting_locator is not None
    assert trailing_update.resulting_locator.value.endswith("gamma.md")
    assert trailing_update.tombstone_id is None


@pytest.mark.asyncio
async def test_update_after_rename_hydrates_the_post_rename_active_locator(
    device_event_store: DeviceEventStoreHarness,
) -> None:
    """create -> rename -> update: the at-sequence resolution proof.

    The update commits strictly after the rename, so its resulting locator
    must be the post-rename path — the locator the source held open at the
    update's own sequence, never the pre-rename path and never a null
    operand the applier would reject.
    """

    history = await device_event_store.seed_rename_update_history()
    page = await device_event_store.store.pull_events(
        history.workspace.context(), limit=200, diagnostic_context=_diagnostic()
    )
    assert [event.event_type for event in page.events] == [
        DeviceEventType.CREATED,
        DeviceEventType.RENAMED,
        DeviceEventType.UPDATED,
    ]
    created, renamed, updated = page.events
    assert created.resulting_locator is not None
    assert created.resulting_locator.value == history.locator_paths["alpha"]
    assert renamed.prior_locator is not None
    assert renamed.prior_locator.value == history.locator_paths["alpha"]
    assert renamed.resulting_locator is not None
    assert renamed.resulting_locator.value == history.locator_paths["beta"]
    assert updated.resulting_locator is not None
    assert updated.resulting_locator.value == history.locator_paths["beta"]
    assert updated.prior_locator is None


@pytest.mark.asyncio
async def test_update_without_a_resolvable_active_locator_fails_integrity(
    device_event_store: DeviceEventStoreHarness,
) -> None:
    """An update whose active locator cannot be resolved never null-passes.

    The first bounded pull delivers only through the rename, so the second
    page hydrates exactly the update; removing the workspace's locator
    rows then leaves that update with no resolvable active locator at its
    sequence — the closed integrity failure, never a skipped row and
    never a locator-less update the plugin would reject downstream.
    """

    history = await device_event_store.seed_rename_update_history()
    store = device_event_store.store
    context = history.workspace.context()
    first = await store.pull_events(context, limit=2, diagnostic_context=_diagnostic())
    assert first.delivered_through_sequence == history.event_sequences["rename"]

    async with device_event_store.engine.begin() as connection:
        await connection.execute(
            sa.delete(source_locators).where(
                source_locators.c.workspace_id == history.workspace.workspace_id
            )
        )

    with pytest.raises(DeviceSyncError) as raised:
        await store.pull_events(context, limit=200, diagnostic_context=_diagnostic())
    assert raised.value.code is DeviceSyncErrorCode.EVENT_INTEGRITY_FAILED


@pytest.mark.asyncio
async def test_pull_pages_through_the_frozen_checkpoint_with_bounded_limits(
    device_event_store: DeviceEventStoreHarness,
) -> None:
    history = await device_event_store.seed_history()
    store = device_event_store.store
    context = history.workspace.context()

    first = await store.pull_events(context, limit=2, diagnostic_context=_diagnostic())
    assert len(first.events) == 2
    assert first.delivered_through_sequence == history.event_sequences["update"]
    assert first.page_checkpoint_sequence == history.event_sequences["trailing_update"]
    assert first.has_more is True

    second = await store.pull_events(context, limit=3, diagnostic_context=_diagnostic())
    assert [event.event_id for event in second.events] == list(history.event_ids[2:5])
    assert second.delivered_through_sequence == history.event_sequences["restore"]
    assert second.acknowledged_sequence == 0
    assert second.has_more is True

    third = await store.pull_events(context, limit=200, diagnostic_context=_diagnostic())
    assert [event.event_id for event in third.events] == [history.event_ids[5]]
    assert third.delivered_through_sequence == history.event_sequences["trailing_update"]
    assert third.has_more is False


@pytest.mark.asyncio
async def test_events_committed_after_a_pull_wait_for_the_next_checkpoint(
    device_event_store: DeviceEventStoreHarness,
) -> None:
    history = await device_event_store.seed_history()
    store = device_event_store.store
    context = history.workspace.context()
    first = await store.pull_events(context, limit=5, diagnostic_context=_diagnostic())
    assert first.delivered_through_sequence == history.event_sequences["restore"]

    later_sequence = await device_event_store.commit_trailing_update(history)
    assert later_sequence > first.page_checkpoint_sequence

    second = await store.pull_events(context, limit=200, diagnostic_context=_diagnostic())
    assert second.page_checkpoint_sequence == later_sequence
    assert second.delivered_through_sequence == later_sequence
    assert [event.event_sequence for event in second.events] == [
        history.event_sequences["trailing_update"],
        later_sequence,
    ]


@pytest.mark.asyncio
async def test_pull_updates_only_the_delivered_watermark(
    device_event_store: DeviceEventStoreHarness,
) -> None:
    history = await device_event_store.seed_history()
    context = history.workspace.context()
    await device_event_store.store.pull_events(context, limit=200, diagnostic_context=_diagnostic())
    trailing = history.event_sequences["trailing_update"]
    assert await device_event_store.read_cursor_row(history.workspace, context.device_id) == (
        0,
        trailing,
    )
    await device_event_store.store.pull_events(context, limit=200, diagnostic_context=_diagnostic())
    assert await device_event_store.read_cursor_row(history.workspace, context.device_id) == (
        0,
        trailing,
    )


@pytest.mark.asyncio
async def test_acknowledge_advances_and_exact_replay_returns_frozen_receipt(
    device_event_store: DeviceEventStoreHarness,
) -> None:
    history = await device_event_store.seed_history()
    context = history.workspace.context()
    await device_event_store.store.pull_events(context, limit=200, diagnostic_context=_diagnostic())
    trailing = history.event_sequences["trailing_update"]
    advanced = await device_event_store.store.acknowledge_cursor(
        context,
        expected_previous_sequence=0,
        applied_through_sequence=trailing,
        diagnostic_context=_diagnostic(),
    )
    assert advanced.acknowledged_sequence == trailing
    assert advanced.delivered_through_sequence == trailing

    replayed = await device_event_store.store.acknowledge_cursor(
        context,
        expected_previous_sequence=0,
        applied_through_sequence=trailing,
        diagnostic_context=_diagnostic(),
    )
    assert replayed == advanced
    assert await device_event_store.read_cursor_row(history.workspace, context.device_id) == (
        trailing,
        trailing,
    )


@pytest.mark.asyncio
async def test_acknowledge_rejects_regression_and_stale_expected_prior(
    device_event_store: DeviceEventStoreHarness,
) -> None:
    history = await device_event_store.seed_history()
    store = device_event_store.store
    context = history.workspace.context()
    await store.pull_events(context, limit=200, diagnostic_context=_diagnostic())
    rename_sequence = history.event_sequences["rename"]
    trailing = history.event_sequences["trailing_update"]

    partial = await store.acknowledge_cursor(
        context,
        expected_previous_sequence=0,
        applied_through_sequence=rename_sequence,
        diagnostic_context=_diagnostic(),
    )
    assert partial.acknowledged_sequence == rename_sequence

    with pytest.raises(DeviceSyncError) as raised:
        await store.acknowledge_cursor(
            context,
            expected_previous_sequence=rename_sequence,
            applied_through_sequence=history.event_sequences["create"],
            diagnostic_context=_diagnostic(),
        )
    assert raised.value.code is DeviceSyncErrorCode.CURSOR_REGRESSION

    with pytest.raises(DeviceSyncError) as stale_raised:
        await store.acknowledge_cursor(
            context,
            expected_previous_sequence=0,
            applied_through_sequence=trailing,
            diagnostic_context=_diagnostic(),
        )
    assert stale_raised.value.code is DeviceSyncErrorCode.CURSOR_REGRESSION
    assert await device_event_store.read_cursor_row(history.workspace, context.device_id) == (
        rename_sequence,
        trailing,
    )


@pytest.mark.asyncio
async def test_ack_above_delivered_watermark_fails_closed(
    device_event_store: DeviceEventStoreHarness,
) -> None:
    history = await device_event_store.seed_history()
    store = device_event_store.store
    context = history.workspace.context()
    await store.pull_events(context, limit=1, diagnostic_context=_diagnostic())
    with pytest.raises(DeviceSyncError) as raised:
        await store.acknowledge_cursor(
            context,
            expected_previous_sequence=0,
            applied_through_sequence=history.event_sequences["update"] + 1,
            diagnostic_context=_diagnostic(),
        )
    assert raised.value.code is DeviceSyncErrorCode.CURSOR_ACK_AHEAD


@pytest.mark.asyncio
async def test_concurrent_identical_acks_serialize_idempotently(
    device_event_store: DeviceEventStoreHarness,
) -> None:
    history = await device_event_store.seed_history()
    store = device_event_store.store
    context = history.workspace.context()
    await store.pull_events(context, limit=200, diagnostic_context=_diagnostic())
    trailing = history.event_sequences["trailing_update"]

    first, second = await asyncio.gather(
        store.acknowledge_cursor(
            context,
            expected_previous_sequence=0,
            applied_through_sequence=trailing,
            diagnostic_context=_diagnostic(),
        ),
        store.acknowledge_cursor(
            context,
            expected_previous_sequence=0,
            applied_through_sequence=trailing,
            diagnostic_context=_diagnostic(),
        ),
    )
    assert first == second
    assert first.acknowledged_sequence == trailing
    assert first.delivered_through_sequence == trailing
    assert await device_event_store.read_cursor_row(history.workspace, context.device_id) == (
        trailing,
        trailing,
    )


@pytest.mark.asyncio
async def test_concurrent_conflicting_acks_never_regress(
    device_event_store: DeviceEventStoreHarness,
) -> None:
    history = await device_event_store.seed_history()
    store = device_event_store.store
    context = history.workspace.context()
    await store.pull_events(context, limit=200, diagnostic_context=_diagnostic())
    trailing = history.event_sequences["trailing_update"]
    rename_sequence = history.event_sequences["rename"]

    outcomes = await asyncio.gather(
        store.acknowledge_cursor(
            context,
            expected_previous_sequence=0,
            applied_through_sequence=trailing,
            diagnostic_context=_diagnostic(),
        ),
        store.acknowledge_cursor(
            context,
            expected_previous_sequence=0,
            applied_through_sequence=rename_sequence,
            diagnostic_context=_diagnostic(),
        ),
        return_exceptions=True,
    )
    receipts = [outcome for outcome in outcomes if not isinstance(outcome, BaseException)]
    rejections = [outcome for outcome in outcomes if isinstance(outcome, BaseException)]
    assert len(receipts) == 1
    assert len(rejections) == 1
    assert isinstance(rejections[0], DeviceSyncError)
    assert rejections[0].code is DeviceSyncErrorCode.CURSOR_REGRESSION
    acknowledged, delivered = await device_event_store.read_cursor_row(
        history.workspace, context.device_id
    )
    assert acknowledged in {trailing, rename_sequence}
    assert delivered == trailing
    assert acknowledged == receipts[0].acknowledged_sequence


@pytest.mark.asyncio
async def test_missing_retained_history_above_floor_raises_cursor_gap(
    device_event_store: DeviceEventStoreHarness,
) -> None:
    history = await device_event_store.seed_history()
    store = device_event_store.store
    context = history.workspace.context()
    first = await store.pull_events(context, limit=200, diagnostic_context=_diagnostic())
    assert first.delivered_through_sequence == history.event_sequences["trailing_update"]

    await device_event_store.delete_unreferenced_history(history)

    with pytest.raises(DeviceSyncError) as raised:
        await store.pull_events(context, limit=200, diagnostic_context=_diagnostic())
    assert raised.value.code is DeviceSyncErrorCode.CURSOR_GAP


@pytest.mark.asyncio
async def test_total_history_loss_above_floor_raises_cursor_gap(
    device_event_store: DeviceEventStoreHarness,
) -> None:
    """A laggard above the floor never sees a silent caught-up page.

    The device has delivered (but not acknowledged) the full history, so the
    workspace floor stays at zero; removing every retained event must be the
    closed cursor gap — never an empty page that reports the laggard as
    caught up. The floor-owning control afterwards (acknowledged through
    the delivered watermark, so the watermark never rises above the floor)
    still pulls an empty page from the same emptied workspace.
    """

    history = await device_event_store.seed_history()
    store = device_event_store.store
    context = history.workspace.context()
    await store.pull_events(context, limit=200, diagnostic_context=_diagnostic())
    trailing = history.event_sequences["trailing_update"]
    assert await store.minimum_acknowledged_sequence(history.workspace.workspace_id) == 0

    await device_event_store.delete_entire_history(history)

    with pytest.raises(DeviceSyncError) as raised:
        await store.pull_events(context, limit=200, diagnostic_context=_diagnostic())
    assert raised.value.code is DeviceSyncErrorCode.CURSOR_GAP

    await store.acknowledge_cursor(
        context,
        expected_previous_sequence=0,
        applied_through_sequence=trailing,
        diagnostic_context=_diagnostic(),
    )
    assert await store.minimum_acknowledged_sequence(history.workspace.workspace_id) == (trailing)
    caught_up = await store.pull_events(context, limit=200, diagnostic_context=_diagnostic())
    assert caught_up.events == ()
    assert caught_up.acknowledged_sequence == trailing
    assert caught_up.delivered_through_sequence == trailing
    assert caught_up.page_checkpoint_sequence == trailing
    assert caught_up.has_more is False


@pytest.mark.asyncio
async def test_compacted_history_below_own_floor_still_pulls(
    device_event_store: DeviceEventStoreHarness,
) -> None:
    history = await device_event_store.seed_history()
    store = device_event_store.store
    context = history.workspace.context()
    await store.pull_events(context, limit=200, diagnostic_context=_diagnostic())
    trailing = history.event_sequences["trailing_update"]
    await store.acknowledge_cursor(
        context,
        expected_previous_sequence=0,
        applied_through_sequence=trailing,
        diagnostic_context=_diagnostic(),
    )
    assert await store.minimum_acknowledged_sequence(history.workspace.workspace_id) == trailing

    await device_event_store.delete_unreferenced_history(history)

    page = await store.pull_events(context, limit=200, diagnostic_context=_diagnostic())
    assert page.events == ()
    assert page.acknowledged_sequence == trailing
    assert page.delivered_through_sequence == trailing
    assert page.page_checkpoint_sequence == trailing
    assert page.has_more is False


@pytest.mark.asyncio
async def test_unhydratable_operand_raises_integrity_and_never_skips(
    device_event_store: DeviceEventStoreHarness,
) -> None:
    history = await device_event_store.seed_history()
    async with device_event_store.engine.begin() as connection:
        await connection.execute(
            sa.delete(source_locators).where(
                source_locators.c.source_locator_id == history.locator_ids["beta"]
            )
        )
    with pytest.raises(DeviceSyncError) as raised:
        await device_event_store.store.pull_events(
            history.workspace.context(), limit=200, diagnostic_context=_diagnostic()
        )
    assert raised.value.code is DeviceSyncErrorCode.EVENT_INTEGRITY_FAILED


@pytest.mark.asyncio
async def test_minimum_acknowledged_sequence_tracks_active_devices_only(
    device_event_store: DeviceEventStoreHarness,
) -> None:
    history = await device_event_store.seed_history()
    store = device_event_store.store
    first_context = history.workspace.context()
    rename_sequence = history.event_sequences["rename"]
    trailing = history.event_sequences["trailing_update"]

    assert await store.minimum_acknowledged_sequence(history.workspace.workspace_id) == 0
    await store.pull_events(first_context, limit=200, diagnostic_context=_diagnostic())
    await store.acknowledge_cursor(
        first_context,
        expected_previous_sequence=0,
        applied_through_sequence=rename_sequence,
        diagnostic_context=_diagnostic(),
    )
    assert await store.minimum_acknowledged_sequence(history.workspace.workspace_id) == (
        rename_sequence
    )

    second_device_id = await device_event_store.seed_device_in_workspace(history.workspace)
    from personal_os.device_sync.contracts import DeviceSyncContext

    second_context = DeviceSyncContext(
        workspace_id=history.workspace.workspace_id,
        device_id=second_device_id,
        user_id=history.workspace.owner_user_id,
    )
    await store.pull_events(second_context, limit=200, diagnostic_context=_diagnostic())
    assert await store.minimum_acknowledged_sequence(history.workspace.workspace_id) == 0
    await store.acknowledge_cursor(
        second_context,
        expected_previous_sequence=0,
        applied_through_sequence=trailing,
        diagnostic_context=_diagnostic(),
    )
    assert await store.minimum_acknowledged_sequence(history.workspace.workspace_id) == (
        rename_sequence
    )

    await device_event_store.revoke_device(first_context.device_id, history.workspace)
    assert await store.minimum_acknowledged_sequence(history.workspace.workspace_id) == trailing
