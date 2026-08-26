"""Durable device event pull hydration and monotonic cursor acknowledgement.

:class:`PostgresqlDeviceEventStore` implements the
:class:`~personal_os.device_sync.ports.DeviceEventStore` port over the
``20260813_01`` baseline and the ``20260826_01`` cursor schema. ``pull_events``
runs one ``READ COMMITTED`` transaction that reads the workspace/device
cursor row (a fresh device implicitly starts at sequence zero), freezes one
statement checkpoint — the single greatest retained ``event_sequence`` of
the credential-scoped workspace — pages at most ``MAX_PULL_EVENTS`` events
strictly above the delivered watermark and only through that checkpoint,
hydrates every event's operation-shaped operands from the joined canonical
event, version, object, locator and tombstone rows, and then advances
nothing but the delivered watermark through a guarded monotonic update. An
event is never skipped: a missing retained predecessor operand raises the
closed cursor gap, an impossible hydrated shape raises the closed integrity
failure, and retained history that falls below a cursor above the workspace
compaction floor raises the closed cursor gap instead of fabricating events.

``acknowledge_cursor`` locks the exact workspace/device cursor row, rejects
a regression and an acknowledgement above the delivered watermark, requires
the expected prior sequence on every advance, and returns the frozen
receipt unchanged on exact acknowledgement replay. Concurrent identical
acknowledgements therefore serialize into one idempotent replay while
concurrent conflicting acknowledgements never regress the cursor.
``minimum_acknowledged_sequence`` exposes the workspace compaction floor —
the minimum acknowledgement across the workspace's active devices; actual
event-compaction execution stays the deferred retention owner of the
device sync design.

Driver failures cross the boundary only through the closed device sync
registry: lock contention retries at most three times with the shared
cancellable 50-250 ms jitter and maps to the retryable
``device_sync_dependency_unavailable`` when exhausted, connection-class
unavailability maps to the same retryable code, and integrity violations,
unclassified database failures and non-database exceptions are internal
bugs. SQLSTATE, SQL text, parameters and driver messages remain chained
only; locators, digests and fingerprints never enter a typed error,
statement or log line.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final
from uuid import UUID, uuid7

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from personal_os.device_sync.contracts import (
    MAX_PULL_EVENTS,
    DeviceCursorReceipt,
    DeviceEventPage,
    DeviceEventType,
    DeviceSyncContext,
    DeviceSyncEvent,
    SourceFingerprint,
)
from personal_os.device_sync.errors import DeviceSyncError, DeviceSyncErrorCode
from personal_os.diagnostics.context import DiagnosticContext
from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import ApplicationError, InternalApplicationError
from personal_os.source_locators import NormalizedLocator
from postgresql_source_store.engine import apply_transaction_bounds
from postgresql_source_store.error_mapping import (
    RETRY_JITTER_MAXIMUM_SECONDS,
    RETRY_JITTER_MINIMUM_SECONDS,
    DatabaseFailureKind,
    classify_database_failure,
)
from postgresql_source_store.tables import (
    content_objects,
    device_cursors,
    devices,
    manifest_actions,
    source_locators,
    source_tombstones,
    source_versions,
    sync_events,
)

#: Canonical ``devices.status`` of a device whose acknowledgement still
#: fences workspace event compaction.
DEVICE_STATUS_ACTIVE: Final[str] = "active"

#: The closed mapping from the canonical ``sync_events.event_type`` tokens
#: (``create``, ``update``, ``rename``, ``move``, ``delete``, ``restore``)
#: onto the domain vocabulary of
#: :class:`~personal_os.device_sync.contracts.DeviceEventType`. A token
#: outside this mapping is an integrity failure, never a skip.
EVENT_TYPE_BY_DATABASE_TOKEN: Final[Mapping[str, DeviceEventType]] = {
    "create": DeviceEventType.CREATED,
    "update": DeviceEventType.UPDATED,
    "rename": DeviceEventType.RENAMED,
    "move": DeviceEventType.MOVED,
    "delete": DeviceEventType.DELETED,
    "restore": DeviceEventType.RESTORED,
}

#: One hydration row: a SQLAlchemy row mapping from the adapter's
#: ``.mappings()`` results or an equivalent mapping in tests.
type _MappedRow = RowMapping | Mapping[str, Any]


# --- hydration ----------------------------------------------------------------


def _integrity_failed() -> DeviceSyncError:
    return DeviceSyncError(DeviceSyncErrorCode.EVENT_INTEGRITY_FAILED)


def _cursor_gap() -> DeviceSyncError:
    return DeviceSyncError(DeviceSyncErrorCode.CURSOR_GAP)


def _hydrate_fingerprint(
    row: _MappedRow,
    *,
    sha256_key: str,
    size_bytes_key: str,
    media_type_key: str,
    missing_error: DeviceSyncError,
) -> SourceFingerprint:
    """Hydrate one version fingerprint operand from its content object row.

    A referenced version whose content evidence row is absent is never
    silently downgraded to ``None``: the caller decides whether the missing
    operand is a missing retained predecessor (the cursor gap) or an
    unhydratable operand of the event itself (the integrity failure).
    """

    sha256 = row[sha256_key]
    size_bytes = row[size_bytes_key]
    media_type = row[media_type_key]
    if sha256 is None or size_bytes is None or media_type is None:
        raise missing_error
    try:
        return SourceFingerprint(
            sha256=str(sha256),
            size_bytes=int(size_bytes),
            media_type=str(media_type),
        )
    except ValueError:
        raise _integrity_failed() from None


def _hydrate_locator(value: Any) -> NormalizedLocator | None:
    """Hydrate one locator operand, failing closed on invalid grammar."""

    if value is None:
        return None
    try:
        return NormalizedLocator(str(value))
    except ValueError:
        raise _integrity_failed() from None


def hydrate_device_event(row: _MappedRow) -> DeviceSyncEvent:
    """Hydrate one canonical operation-shaped event from its joined row.

    The row is the pull page projection of ``sync_events`` left-joined with
    the locator it opened and closed, the tombstone it opened and closed,
    and the base/current versions' content objects. An unknown event-type
    token, a naive committed time, an invalid operand grammar or any shape
    the domain contract forbids is the closed integrity failure; a base
    version operand whose content evidence row is gone is the closed cursor
    gap (the retained predecessor is missing), and no row is ever skipped.
    """

    event_type = EVENT_TYPE_BY_DATABASE_TOKEN.get(str(row["event_type"]))
    if event_type is None:
        raise _integrity_failed()
    committed_at = row["committed_at"]
    if not isinstance(committed_at, datetime) or committed_at.tzinfo is None:
        raise _integrity_failed()
    base_version_id = row["base_version_id"]
    current_version_id = row["current_version_id"]
    base_fingerprint = (
        None
        if base_version_id is None
        else _hydrate_fingerprint(
            row,
            sha256_key="base_sha256",
            size_bytes_key="base_size_bytes",
            media_type_key="base_media_type",
            missing_error=_cursor_gap(),
        )
    )
    current_fingerprint = (
        None
        if current_version_id is None
        else _hydrate_fingerprint(
            row,
            sha256_key="current_sha256",
            size_bytes_key="current_size_bytes",
            media_type_key="current_media_type",
            missing_error=_integrity_failed(),
        )
    )
    delete_tombstone_id = row["delete_tombstone_id"]
    restore_tombstone_id = row["restore_tombstone_id"]
    tombstone_id = (
        delete_tombstone_id if delete_tombstone_id is not None else restore_tombstone_id
    )
    try:
        hydrated = DeviceSyncEvent(
            event_id=row["event_id"],
            event_sequence=int(row["event_sequence"]),
            event_type=event_type,
            source_id=row["source_id"],
            origin_device_id=row["origin_device_id"],
            base_version_id=base_version_id,
            current_version_id=current_version_id,
            base_fingerprint=base_fingerprint,
            current_fingerprint=current_fingerprint,
            prior_locator=_hydrate_locator(row["prior_locator"]),
            resulting_locator=_hydrate_locator(row["resulting_locator"]),
            tombstone_id=tombstone_id,
            committed_at=committed_at,
        )
    except ValueError:
        raise _integrity_failed() from None
    return hydrated


# --- cursor gap classification ------------------------------------------------


def classify_cursor_gap(
    *,
    delivered_through_sequence: int,
    checkpoint_sequence: int | None,
    floor_sequence: int,
) -> bool:
    """Return whether retained history can still satisfy the device cursor.

    The workspace compaction floor is the minimum acknowledged sequence
    across the workspace's active devices: every event above the floor must
    stay retained. A cursor at or above the floor whose statement checkpoint
    falls below the delivered watermark therefore proves retained history
    was removed that some device still needs — the closed cursor gap —
    while the floor-owning device's own compacted history stays pullable
    (its watermark never rises above the floor).
    """

    if delivered_through_sequence <= floor_sequence:
        return False
    if checkpoint_sequence is None:
        return True
    return checkpoint_sequence < delivered_through_sequence


def validate_pull_limit(limit: int) -> None:
    """Reject any pull window outside ``1 .. MAX_PULL_EVENTS``."""

    if not 1 <= limit <= MAX_PULL_EVENTS:
        raise ValueError(
            f"limit must be between 1 and {MAX_PULL_EVENTS} events per pull page"
        )


# --- statement builders --------------------------------------------------------


def device_event_checkpoint_statement(workspace_id: UUID) -> sa.Select[tuple[Any, ...]]:
    """Build the parameter-bound statement checkpoint read for one workspace.

    The descending ordered head read is exactly ``max(event_sequence)`` over
    the credential-scoped workspace and stops on the first matching index
    entry; the pull pages only through the value it froze.
    """

    return (
        sa.select(sync_events.c.event_sequence)
        .where(sync_events.c.workspace_id == workspace_id)
        .order_by(sync_events.c.event_sequence.desc())
        .limit(1)
    )


def device_pull_page_statement(
    workspace_id: UUID,
    *,
    after_sequence: int,
    through_sequence: int,
    limit: int,
) -> sa.Select[tuple[Any, ...]]:
    """Build the bounded credential-scoped pull page with hydration operands.

    The statement pages strictly above ``after_sequence`` and only through
    ``through_sequence`` in canonical sequence order with a parameter-bound
    ``LIMIT``; every operand is hydrated from the canonical lifecycle rows
    (the locators the event opened and closed, the tombstones it opened and
    closed, and the base/current versions' content objects) instead of a
    second stored event body.
    """

    opened_locator = source_locators.alias("opened_locator")
    closed_locator = source_locators.alias("closed_locator")
    delete_tombstone = source_tombstones.alias("delete_tombstone")
    restore_tombstone = source_tombstones.alias("restore_tombstone")
    current_version = source_versions.alias("current_version")
    base_version = source_versions.alias("base_version")
    current_object = content_objects.alias("current_object")
    base_object = content_objects.alias("base_object")
    return (
        sa.select(
            sync_events.c.event_id,
            sync_events.c.event_sequence,
            sync_events.c.event_type,
            sync_events.c.source_id,
            sync_events.c.device_id.label("origin_device_id"),
            sync_events.c.base_version_id,
            sync_events.c.committed_version_id.label("current_version_id"),
            sync_events.c.committed_at,
            opened_locator.c.normalized_locator.label("resulting_locator"),
            closed_locator.c.normalized_locator.label("prior_locator"),
            delete_tombstone.c.source_tombstone_id.label("delete_tombstone_id"),
            restore_tombstone.c.source_tombstone_id.label("restore_tombstone_id"),
            current_object.c.content_hash.label("current_sha256"),
            current_object.c.byte_size.label("current_size_bytes"),
            current_object.c.media_type.label("current_media_type"),
            base_object.c.content_hash.label("base_sha256"),
            base_object.c.byte_size.label("base_size_bytes"),
            base_object.c.media_type.label("base_media_type"),
        )
        .select_from(sync_events)
        .outerjoin(
            opened_locator,
            sa.and_(
                opened_locator.c.workspace_id == sync_events.c.workspace_id,
                opened_locator.c.source_id == sync_events.c.source_id,
                opened_locator.c.opened_event_id == sync_events.c.event_id,
            ),
        )
        .outerjoin(
            closed_locator,
            sa.and_(
                closed_locator.c.workspace_id == sync_events.c.workspace_id,
                closed_locator.c.source_id == sync_events.c.source_id,
                closed_locator.c.closed_event_id == sync_events.c.event_id,
            ),
        )
        .outerjoin(
            delete_tombstone,
            sa.and_(
                delete_tombstone.c.workspace_id == sync_events.c.workspace_id,
                delete_tombstone.c.source_id == sync_events.c.source_id,
                delete_tombstone.c.delete_event_id == sync_events.c.event_id,
            ),
        )
        .outerjoin(
            restore_tombstone,
            sa.and_(
                restore_tombstone.c.workspace_id == sync_events.c.workspace_id,
                restore_tombstone.c.source_id == sync_events.c.source_id,
                restore_tombstone.c.restore_event_id == sync_events.c.event_id,
            ),
        )
        .outerjoin(
            current_version,
            sa.and_(
                current_version.c.workspace_id == sync_events.c.workspace_id,
                current_version.c.source_id == sync_events.c.source_id,
                current_version.c.source_version_id == sync_events.c.committed_version_id,
            ),
        )
        .outerjoin(
            base_version,
            sa.and_(
                base_version.c.workspace_id == sync_events.c.workspace_id,
                base_version.c.source_id == sync_events.c.source_id,
                base_version.c.source_version_id == sync_events.c.base_version_id,
            ),
        )
        .outerjoin(
            current_object,
            current_object.c.content_object_id == current_version.c.content_object_id,
        )
        .outerjoin(
            base_object,
            base_object.c.content_object_id == base_version.c.content_object_id,
        )
        .where(
            sync_events.c.workspace_id == workspace_id,
            sync_events.c.event_sequence > sa.bindparam("after_sequence", after_sequence),
            sync_events.c.event_sequence
            <= sa.bindparam("through_sequence", through_sequence),
        )
        .order_by(sync_events.c.event_sequence.asc())
        .limit(sa.bindparam("pull_limit", limit))
    )


def device_cursor_select_statement(
    workspace_id: UUID,
    device_id: UUID,
    *,
    for_update: bool = False,
) -> sa.Select[tuple[Any, ...]]:
    """Build the credential-scoped cursor row read, optionally row-locked."""

    statement = sa.select(
        device_cursors.c.acknowledged_sequence,
        device_cursors.c.delivered_through_sequence,
    ).where(
        device_cursors.c.workspace_id == workspace_id,
        device_cursors.c.device_id == device_id,
    )
    if for_update:
        statement = statement.with_for_update()
    return statement


def device_cursor_bootstrap_insert_statement(
    *,
    device_cursor_id: UUID,
    workspace_id: UUID,
    device_id: UUID,
    delivered_through_sequence: int,
) -> sa.Insert:
    """Build the conflict-tolerant first cursor row for one device.

    A fresh device starts at acknowledged sequence zero; the insert races
    no concurrent writer because the workspace/device conflict target makes
    a lost race a no-op for the guarded watermark update that follows.
    """

    return (
        postgresql_insert(device_cursors)
        .values(
            device_cursor_id=device_cursor_id,
            workspace_id=workspace_id,
            device_id=device_id,
            acknowledged_sequence=0,
            delivered_through_sequence=delivered_through_sequence,
        )
        .on_conflict_do_nothing(index_elements=["workspace_id", "device_id"])
    )


def device_delivered_watermark_advance_statement(
    workspace_id: UUID,
    device_id: UUID,
    *,
    delivered_through_sequence: int,
) -> sa.Update:
    """Build the guarded monotonic delivered-watermark advance.

    Pull updates only the delivered column and only forward, so concurrent
    pulls and acknowledgements of the same device converge without ever
    violating the delivery-order database check.
    """

    new_delivered: sa.BindParameter[int] = sa.bindparam(
        "delivered_through_sequence", delivered_through_sequence
    )
    return (
        sa.update(device_cursors)
        .values(
            delivered_through_sequence=new_delivered,
            updated_at=sa.text("CURRENT_TIMESTAMP"),
        )
        .where(
            device_cursors.c.workspace_id == workspace_id,
            device_cursors.c.device_id == device_id,
            device_cursors.c.delivered_through_sequence < new_delivered,
        )
    )


def device_acknowledged_advance_statement(
    workspace_id: UUID,
    device_id: UUID,
    *,
    applied_through_sequence: int,
) -> sa.Update:
    """Build the locked acknowledged-cursor advance (delivered stays put)."""

    return (
        sa.update(device_cursors)
        .values(
            acknowledged_sequence=sa.bindparam(
                "applied_through_sequence", applied_through_sequence
            ),
            updated_at=sa.text("CURRENT_TIMESTAMP"),
        )
        .where(
            device_cursors.c.workspace_id == workspace_id,
            device_cursors.c.device_id == device_id,
        )
    )


def workspace_minimum_acknowledged_statement(
    workspace_id: UUID,
) -> sa.Select[tuple[Any, ...]]:
    """Build the workspace compaction floor over active devices."""

    return (
        sa.select(sa.func.min(device_cursors.c.acknowledged_sequence))
        .select_from(device_cursors)
        .join(
            devices,
            sa.and_(
                devices.c.workspace_id == device_cursors.c.workspace_id,
                devices.c.device_id == device_cursors.c.device_id,
            ),
        )
        .where(
            device_cursors.c.workspace_id == workspace_id,
            devices.c.status == DEVICE_STATUS_ACTIVE,
        )
    )


def manifest_action_page_statement(
    manifest_run_id: UUID,
    *,
    workspace_id: UUID,
    after_action_index: int,
    limit: int,
) -> sa.Select[tuple[Any, ...]]:
    """Build the stable ordered manifest action page after one index.

    The manifest store reads its frozen actions through this page; the
    statement walks the primary-key index in ``action_index`` order with a
    parameter-bound limit and never rewrites a planned action. The statement
    is run-scoped: Task 4's store composes it with the credential-derived
    workspace/device ownership of the run (resolving the run through the
    ``DeviceSyncContext`` first) so no foreign run's actions ever cross the
    credential boundary. A ``download`` action's checkpoint placement
    locator text hydrates at read time through a workspace-scoped outer
    join onto the canonical locator row its ``source_locator_id`` names —
    never through persisted locator text — so a foreign or dangling
    locator reference stays unhydrated and fails closed at the store
    boundary.
    """

    return (
        sa.select(
            manifest_actions.c.action_index,
            manifest_actions.c.action_kind,
            manifest_actions.c.local_entry_id,
            manifest_actions.c.source_id,
            manifest_actions.c.source_version_id,
            manifest_actions.c.source_locator_id,
            manifest_actions.c.source_tombstone_id,
            manifest_actions.c.safe_reason_code,
            sa.case(
                (
                    manifest_actions.c.action_kind == sa.literal_column("'download'"),
                    source_locators.c.normalized_locator,
                ),
                else_=None,
            ).label("checkpoint_locator"),
        )
        .select_from(manifest_actions)
        .outerjoin(
            source_locators,
            sa.and_(
                source_locators.c.workspace_id == workspace_id,
                source_locators.c.source_locator_id == manifest_actions.c.source_locator_id,
            ),
        )
        .where(
            manifest_actions.c.manifest_run_id == manifest_run_id,
            manifest_actions.c.action_index
            > sa.bindparam("after_action_index", after_action_index),
        )
        .order_by(manifest_actions.c.action_index.asc())
        .limit(sa.bindparam("pull_limit", limit))
    )


# --- domain database retry policy ----------------------------------------------


def map_device_sync_database_failure(cause: BaseException) -> ApplicationError:
    """Map a database or driver failure onto the closed device sync boundary.

    Connection-class unavailability and contention exhausted after the
    bounded retries are the retryable ``device_sync_dependency_unavailable``:
    the plugin's cancellable backoff owns the re-attempt. An integrity-
    constraint violation (a deterministic rejection on a healthy
    connection), any unclassified database failure and a non-database
    exception are internal bugs of the safe ``internal_error`` class. The
    cause remains chained only; its SQLSTATE, constraint name, statement,
    parameters and text never enter the mapped error.
    """

    failure_kind = classify_database_failure(cause)
    if failure_kind in {
        DatabaseFailureKind.UNAVAILABLE,
        DatabaseFailureKind.CONTENTION,
    }:
        return DeviceSyncError(DeviceSyncErrorCode.DEPENDENCY_UNAVAILABLE)
    return InternalApplicationError(ErrorCode.INTERNAL_ERROR)


@dataclass(frozen=True, slots=True)
class DeviceSyncDatabaseRetryPolicy:
    """Bounded retry for the device event store over the shared classifier.

    At most ``maximum_attempts`` attempts run with the shared cancellable
    50-250 ms jitter. Typed application errors pass through untouched; lock
    contention retries inside the bound and maps to the retryable
    dependency code when exhausted, while every other failure — including
    connection-class unavailability, whose re-attempt belongs to the
    plugin's foreground backoff, an integrity violation or an unclassified
    database failure — maps immediately without leaking driver evidence.
    Both store transactions are idempotent (the watermark and
    acknowledgement advances are guarded), so a lost acknowledgement
    resolves through the same replay on the next pull/acknowledge.
    """

    maximum_attempts: int = 3

    async def run[T](
        self,
        operation: Callable[[int], Awaitable[T]],
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[float, float], float] = random.uniform,
    ) -> T:
        for attempt in range(1, self.maximum_attempts + 1):
            try:
                return await operation(attempt)
            except ApplicationError:
                raise
            except Exception as cause:
                failure_kind = classify_database_failure(cause)
                if (
                    failure_kind is DatabaseFailureKind.CONTENTION
                    and attempt < self.maximum_attempts
                ):
                    await sleep(jitter(RETRY_JITTER_MINIMUM_SECONDS, RETRY_JITTER_MAXIMUM_SECONDS))
                    continue
                raise map_device_sync_database_failure(cause) from cause
        raise AssertionError("retry loop exhausted without a result")


# --- store ----------------------------------------------------------------------


class PostgresqlDeviceEventStore:
    """Event pull hydration and monotonic cursor fencing over the baseline.

    The store takes the composition-owned :class:`AsyncEngine`, the
    optional domain retry policy and the UUIDv7 allocator seam, and opens
    no connection at construction. Every method runs one ``READ COMMITTED``
    transaction behind the pinned ``SET LOCAL`` bounds and is scoped
    entirely by the credential-derived
    :class:`~personal_os.device_sync.contracts.DeviceSyncContext`.
    """

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        retry: DeviceSyncDatabaseRetryPolicy | None = None,
        identity_generator: Callable[[], UUID] | None = None,
    ) -> None:
        self._engine = engine
        self._retry = retry if retry is not None else DeviceSyncDatabaseRetryPolicy()
        self._identity_generator = identity_generator if identity_generator is not None else uuid7

    # -- pull ---------------------------------------------------------------

    async def pull_events(
        self,
        context: DeviceSyncContext,
        *,
        limit: int,
        diagnostic_context: DiagnosticContext,
    ) -> DeviceEventPage:
        """Deliver one bounded page of immutable events past the cursor.

        The first statement freezes one checkpoint; events committed after
        it wait for a later pull. Hydration never skips a row: a missing
        retained predecessor or retained history below a cursor above the
        workspace compaction floor is the closed cursor gap, and an
        impossible hydrated shape is the closed integrity failure.
        """

        del diagnostic_context
        validate_pull_limit(limit)
        return await self._retry.run(lambda _attempt: self._pull_once(context, limit))

    async def _pull_once(
        self, context: DeviceSyncContext, limit: int
    ) -> DeviceEventPage:
        async with (
            self._engine.connect() as connection,
            connection.begin(),
        ):
            await apply_transaction_bounds(connection)
            acknowledged, delivered = await self._read_cursor(connection, context)
            checkpoint_result = await connection.execute(
                device_event_checkpoint_statement(context.workspace_id)
            )
            checkpoint_row = checkpoint_result.one_or_none()
            checkpoint = None if checkpoint_row is None else int(checkpoint_row[0])
            # The gap witness runs whenever retained history no longer
            # reaches a non-zero delivered watermark — partial loss (the
            # checkpoint falls below it) and total loss (no checkpoint at
            # all) alike. A zero watermark skips the floor round trip: a
            # fresh device on an empty workspace can never be gapped.
            if delivered > 0 and (checkpoint is None or checkpoint < delivered):
                floor = await self._read_floor(connection, context.workspace_id)
                if classify_cursor_gap(
                    delivered_through_sequence=delivered,
                    checkpoint_sequence=checkpoint,
                    floor_sequence=floor,
                ):
                    raise _cursor_gap()
            if checkpoint is None or checkpoint <= delivered:
                # No events remain inside this pull's frozen checkpoint. The
                # reported page checkpoint is the delivered watermark: the
                # page contract forbids a checkpoint beneath it, and a None
                # checkpoint means no retained history exists at all.
                return DeviceEventPage(
                    acknowledged_sequence=acknowledged,
                    page_checkpoint_sequence=delivered,
                    delivered_through_sequence=delivered,
                    events=(),
                    has_more=False,
                )
            page_result = await connection.execute(
                device_pull_page_statement(
                    context.workspace_id,
                    after_sequence=delivered,
                    through_sequence=checkpoint,
                    limit=limit + 1,
                )
            )
            rows = page_result.mappings().all()
            has_more = len(rows) > limit
            events = tuple(hydrate_device_event(row) for row in rows[:limit])
            new_delivered = events[-1].event_sequence if events else delivered
            await self._advance_delivered_watermark(
                connection,
                context,
                previous_delivered=delivered,
                new_delivered=new_delivered,
            )
            return DeviceEventPage(
                acknowledged_sequence=acknowledged,
                page_checkpoint_sequence=checkpoint,
                delivered_through_sequence=new_delivered,
                events=events,
                has_more=has_more,
            )

    async def _read_cursor(
        self, connection: AsyncConnection, context: DeviceSyncContext
    ) -> tuple[int, int]:
        result = await connection.execute(
            device_cursor_select_statement(context.workspace_id, context.device_id)
        )
        row = result.one_or_none()
        if row is None:
            return 0, 0
        return int(row.acknowledged_sequence), int(row.delivered_through_sequence)

    async def _read_floor(self, connection: AsyncConnection, workspace_id: UUID) -> int:
        result = await connection.execute(
            workspace_minimum_acknowledged_statement(workspace_id)
        )
        floor = result.scalar_one_or_none()
        return 0 if floor is None else int(floor)

    async def _advance_delivered_watermark(
        self,
        connection: AsyncConnection,
        context: DeviceSyncContext,
        *,
        previous_delivered: int,
        new_delivered: int,
    ) -> None:
        if new_delivered <= previous_delivered:
            return
        await connection.execute(
            device_cursor_bootstrap_insert_statement(
                device_cursor_id=self._identity_generator(),
                workspace_id=context.workspace_id,
                device_id=context.device_id,
                delivered_through_sequence=new_delivered,
            )
        )
        await connection.execute(
            device_delivered_watermark_advance_statement(
                context.workspace_id,
                context.device_id,
                delivered_through_sequence=new_delivered,
            )
        )

    # -- acknowledge ----------------------------------------------------------

    async def acknowledge_cursor(
        self,
        context: DeviceSyncContext,
        *,
        expected_previous_sequence: int,
        applied_through_sequence: int,
        diagnostic_context: DiagnosticContext,
    ) -> DeviceCursorReceipt:
        """Advance the acknowledged cursor behind the locked row.

        Regression and acknowledgement above the delivered watermark fail
        closed, every advance requires the expected prior sequence, and the
        exact acknowledgement replay returns the frozen cursor without a
        second mutation.
        """

        del diagnostic_context
        if expected_previous_sequence < 0 or applied_through_sequence < 0:
            raise ValueError("cursor sequences must be non-negative")
        return await self._retry.run(
            lambda _attempt: self._acknowledge_once(
                context,
                expected_previous_sequence=expected_previous_sequence,
                applied_through_sequence=applied_through_sequence,
            )
        )

    async def _acknowledge_once(
        self,
        context: DeviceSyncContext,
        *,
        expected_previous_sequence: int,
        applied_through_sequence: int,
    ) -> DeviceCursorReceipt:
        async with (
            self._engine.connect() as connection,
            connection.begin(),
        ):
            await apply_transaction_bounds(connection)
            result = await connection.execute(
                device_cursor_select_statement(
                    context.workspace_id, context.device_id, for_update=True
                )
            )
            row = result.one_or_none()
            if row is None:
                if applied_through_sequence > 0:
                    raise DeviceSyncError(DeviceSyncErrorCode.CURSOR_ACK_AHEAD)
                if expected_previous_sequence != 0:
                    raise DeviceSyncError(DeviceSyncErrorCode.CURSOR_REGRESSION)
                await connection.execute(
                    device_cursor_bootstrap_insert_statement(
                        device_cursor_id=self._identity_generator(),
                        workspace_id=context.workspace_id,
                        device_id=context.device_id,
                        delivered_through_sequence=0,
                    )
                )
                return DeviceCursorReceipt(
                    acknowledged_sequence=0, delivered_through_sequence=0
                )
            acknowledged = int(row.acknowledged_sequence)
            delivered = int(row.delivered_through_sequence)
            if applied_through_sequence < acknowledged:
                raise DeviceSyncError(DeviceSyncErrorCode.CURSOR_REGRESSION)
            if applied_through_sequence > delivered:
                raise DeviceSyncError(DeviceSyncErrorCode.CURSOR_ACK_AHEAD)
            if applied_through_sequence == acknowledged:
                if expected_previous_sequence > acknowledged:
                    raise DeviceSyncError(DeviceSyncErrorCode.CURSOR_REGRESSION)
                return DeviceCursorReceipt(
                    acknowledged_sequence=acknowledged,
                    delivered_through_sequence=delivered,
                )
            if expected_previous_sequence != acknowledged:
                raise DeviceSyncError(DeviceSyncErrorCode.CURSOR_REGRESSION)
            await connection.execute(
                device_acknowledged_advance_statement(
                    context.workspace_id,
                    context.device_id,
                    applied_through_sequence=applied_through_sequence,
                )
            )
            return DeviceCursorReceipt(
                acknowledged_sequence=applied_through_sequence,
                delivered_through_sequence=delivered,
            )

    # -- compaction floor -------------------------------------------------------

    async def minimum_acknowledged_sequence(self, workspace_id: UUID) -> int:
        """Return the workspace compaction floor over active devices.

        Read-only: the deferred event-compaction owner consumes the floor;
        this store never executes compaction itself.
        """

        async with (
            self._engine.connect() as connection,
            connection.begin(),
        ):
            await apply_transaction_bounds(connection)
            return await self._read_floor(connection, workspace_id)


__all__ = [
    "DEVICE_STATUS_ACTIVE",
    "DeviceSyncDatabaseRetryPolicy",
    "PostgresqlDeviceEventStore",
    "classify_cursor_gap",
    "device_acknowledged_advance_statement",
    "device_cursor_bootstrap_insert_statement",
    "device_cursor_select_statement",
    "device_delivered_watermark_advance_statement",
    "device_event_checkpoint_statement",
    "device_pull_page_statement",
    "hydrate_device_event",
    "manifest_action_page_statement",
    "map_device_sync_database_failure",
    "validate_pull_limit",
    "workspace_minimum_acknowledged_statement",
]
