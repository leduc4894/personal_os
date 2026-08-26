"""Two-device reconciliation journeys over the disposable PostgreSQL stack.

The unit and transaction suites prove each store's durable semantics in
isolation; this module proves the cross-boundary journeys two real devices
drive through the PRODUCTION server stores — ``PostgresqlDeviceEventStore``
for the pull/acknowledge wire and ``PostgresqlDeviceManifestStore`` for the
reconciliation wire — over one seeded workspace owned by device A (the
canonical committer) and observed by device B (the reconciler). Device B's
local view is the deterministic stand-in for the plugin's journal: an
explicit locator/fingerprint/known-id map the tests mutate at exact journey
moments (a remote edit arrives through a REAL pulled page, a local edit
changes the fingerprint, SQLite loss clears the known ids), so every journey
is seeded and ordered, never timing-dependent.

The journeys pinned here: a remote edit applies through the real hydrated
pull and never echoes a second source; the lifecycle events (rename, delete)
reconcile historically with placement at the active locator row and a
tombstone; a canonical commit landing after a run's checkpoint stays outside
the plan and the completion fence (the next pull delivers it); a lost
acknowledgement replays to the same frozen cursor; cursor-regression and
ack-ahead attempts surface their readable reason tokens and the repair
converges the cursor; a policy advance mid-run fails the run closed with the
policy reason on the run row and one fresh run converges under the new
revision; SQLite loss rebinds the manifest entries to the same canonical
source without creating a duplicate; and a local edit made during the
reconciliation settles as the closed divergence conflict while the canonical
source and the cursor still converge.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.device_sync.conftest import (
    DeviceSyncWorkspace,
    seed_device_sync_workspace,
)
from tests.integration.device_sync.test_cursor_and_manifest_transactions import (
    SeededCanonicalSource,
    publish_workspace_policy,
    seed_canonical_source,
    seed_renamed_source,
)
from tests.integration.source_publication.conftest import SourcePublicationStack

from personal_os.device_sync.contracts import (
    AppendManifestPageCommand,
    CompleteManifestCommand,
    DeviceCursorReceipt,
    DeviceSyncContext,
    FinalizeManifestCommand,
    ManifestAction,
    ManifestActionKind,
    ManifestActionsQuery,
    ManifestEntry,
    ManifestRunReceipt,
    ManifestRunState,
    NormalizedLocator,
    SourceFingerprint,
    StartManifestCommand,
)
from personal_os.device_sync.errors import DeviceSyncError, DeviceSyncErrorCode
from personal_os.diagnostics.context import DiagnosticContext, TraceContext
from personal_os.diagnostics.trace_context import SpanId, TraceId
from personal_os.object_storage import ContentDigest
from postgresql_source_store.device_event_store import PostgresqlDeviceEventStore
from postgresql_source_store.device_manifest_store import (
    PostgresqlDeviceManifestStore,
    compute_manifest_final_digest,
)
from postgresql_source_store.engine import (
    create_source_store_engine,
    dispose_source_store_engine,
)
from postgresql_source_store.tables import (
    content_objects,
    device_cursors,
    devices,
    manifest_entry_resolutions,
    manifest_runs,
    source_locators,
    source_versions,
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

_MEDIA_TYPE = "text/markdown"


def _diagnostic() -> DiagnosticContext:
    return DiagnosticContext(request_id=uuid4(), client_request_id=None, trace=_TRACE)


def _digest(label: str) -> ContentDigest:
    return ContentDigest.parse(hashlib.sha256(label.encode("ascii")).hexdigest())


def fingerprint(label: str, *, salt: str | None = None) -> SourceFingerprint:
    """One settled-byte fingerprint; the default salt keeps every canonical
    content object globally unique across test populations."""

    effective_salt = salt if salt is not None else uuid4().hex
    material = f"{label}:{effective_salt}"
    return SourceFingerprint(
        sha256=hashlib.sha256(material.encode("ascii")).hexdigest(),
        size_bytes=len(material),
        media_type=_MEDIA_TYPE,
    )


def final_digest_of(manifest_run_id: UUID, page_digest: ContentDigest) -> ContentDigest:
    """The canonical final digest of the single-page runs these journeys drive."""

    return ContentDigest.parse(compute_manifest_final_digest(((0, 1, page_digest.hexadecimal),)))


# --- the deterministic device-B local view ------------------------------------------


@dataclass(frozen=True, slots=True)
class LocalFileView:
    """One file device B holds locally: its locator, settled fingerprint and
    the known canonical ids its journal recorded before any loss."""

    locator: str
    fingerprint: SourceFingerprint
    known_source_id: UUID | None
    known_version_id: UUID | None


@dataclass
class LocalJournalView:
    """Device B's local journal state: the files it holds and their known
    canonical bindings (the rows a SQLite loss clears)."""

    files: dict[str, LocalFileView] = field(default_factory=dict)

    def observe(
        self,
        locator: str,
        observed: SourceFingerprint,
        *,
        known_source_id: UUID | None = None,
        known_version_id: UUID | None = None,
    ) -> None:
        self.files[locator] = LocalFileView(
            locator=locator,
            fingerprint=observed,
            known_source_id=known_source_id,
            known_version_id=known_version_id,
        )

    def forget_canonical_bindings(self) -> None:
        """SQLite loss: every file's bytes survive, every known id is gone."""

        for locator, view in list(self.files.items()):
            self.files[locator] = LocalFileView(
                locator=locator,
                fingerprint=view.fingerprint,
                known_source_id=None,
                known_version_id=None,
            )


@dataclass(frozen=True, slots=True)
class ReconcileOutcome:
    """The public facts of one device-B reconciliation journey."""

    run: ManifestRunReceipt
    actions: tuple[ManifestAction, ...]
    resolutions: tuple[Any, ...]
    cursor: DeviceCursorReceipt

    @property
    def source_ids(self) -> set[UUID]:
        """Every canonical source the entries resolved to (the rebind proof)."""

        return {action.source_id for action in self.actions if action.source_id is not None} | {
            resolution.resolved_source_id
            for resolution in self.resolutions
            if resolution.resolved_source_id is not None
        }

    @property
    def action_kinds(self) -> list[ManifestActionKind]:
        return [action.action_kind for action in self.actions]


# --- the two-device stack ----------------------------------------------------------------


class DeviceAPeer:
    """The canonical committer: publishes files and edits into the workspace."""

    def __init__(self, engine: AsyncEngine, workspace: DeviceSyncWorkspace) -> None:
        self._engine = engine
        self.workspace = workspace

    async def publish(
        self, locator: str, observed: SourceFingerprint, *, deleted: bool = False
    ) -> SeededCanonicalSource:
        return await seed_canonical_source(
            self._engine,
            self.workspace,
            locator_text=locator,
            fingerprints=(observed,),
            deleted=deleted,
        )

    async def publish_update(
        self, source: SeededCanonicalSource, observed: SourceFingerprint
    ) -> UUID:
        """Commit one new version of an existing source as an update event."""

        version_id = uuid4()
        content_object_id = uuid4()
        event_id = uuid4()
        committed_at = datetime.now(UTC)
        nonce = uuid4().hex
        idempotency_key = f"two-device-update-{nonce}"
        async with self._engine.begin() as connection:
            current_ordinal = await connection.scalar(
                sa.select(sa.func.max(source_versions.c.content_version)).where(
                    source_versions.c.workspace_id == self.workspace.workspace_id,
                    source_versions.c.source_id == source.source_id,
                )
            )
            next_ordinal = int(current_ordinal or 0) + 1
            await connection.execute(
                sa.insert(content_objects).values(
                    content_object_id=content_object_id,
                    content_hash=observed.sha256,
                    object_key=(
                        f"objects/sha256/{observed.sha256[:2]}/"
                        f"{observed.sha256[2:4]}/{observed.sha256}"
                    ),
                    byte_size=observed.size_bytes,
                    media_type=observed.media_type,
                    verified_at=committed_at,
                )
            )
            await connection.execute(
                sa.insert(source_versions).values(
                    source_version_id=version_id,
                    workspace_id=self.workspace.workspace_id,
                    source_id=source.source_id,
                    content_object_id=content_object_id,
                    content_version=next_ordinal,
                    parent_version_id=source.version_ids[-1],
                    author_kind="device",
                    author_id=self.workspace.device_id,
                    committed_at=committed_at,
                )
            )
            await connection.execute(
                sa.insert(sync_events).values(
                    event_id=event_id,
                    workspace_id=self.workspace.workspace_id,
                    source_id=source.source_id,
                    device_id=self.workspace.device_id,
                    committed_version_id=version_id,
                    base_version_id=source.version_ids[-1],
                    idempotency_key=idempotency_key,
                    request_fingerprint=hashlib.sha256(idempotency_key.encode("ascii")).hexdigest(),
                    event_type="update",
                )
            )
            await connection.execute(
                sa.update(sources)
                .values(
                    current_version_id=version_id,
                    updated_at=sa.text("CURRENT_TIMESTAMP"),
                )
                .where(
                    sources.c.workspace_id == self.workspace.workspace_id,
                    sources.c.source_id == source.source_id,
                )
            )
        return version_id


class DeviceBPeer:
    """The reconciler: pulls the real event pages, acknowledges its cursor and
    drives the manifest reconciliation wire against the production store."""

    def __init__(
        self,
        engine: AsyncEngine,
        events: PostgresqlDeviceEventStore,
        manifests: PostgresqlDeviceManifestStore,
        context: DeviceSyncContext,
    ) -> None:
        self._engine = engine
        self._events = events
        self._manifests = manifests
        self.context = context
        self.journal = LocalJournalView()

    async def pull(self, *, limit: int = 200) -> Any:
        return await self._events.pull_events(
            self.context, limit=limit, diagnostic_context=_diagnostic()
        )

    async def pull_and_apply(self) -> Any:
        """Pull one page and fold every event into the local view exactly the
        way the plugin does: the hydrated wire operands drive the view."""

        page = await self.pull()
        for event in page.events:
            prior = event.prior_locator.value if event.prior_locator is not None else None
            resulting = (
                event.resulting_locator.value if event.resulting_locator is not None else None
            )
            view = self.journal.files.get(prior or "") or self.journal.files.get(resulting or "")
            if event.event_type.value == "deleted":
                if view is not None:
                    del self.journal.files[view.locator]
                continue
            locator = resulting or (view.locator if view else None)
            if locator is None or event.current_fingerprint is None:
                continue
            self.journal.observe(
                locator,
                event.current_fingerprint,
                known_source_id=event.source_id,
                known_version_id=event.current_version_id,
            )
        return page

    async def acknowledge(self, expected_previous: int, applied_through: int) -> Any:
        return await self._events.acknowledge_cursor(
            self.context,
            expected_previous_sequence=expected_previous,
            applied_through_sequence=applied_through,
            diagnostic_context=_diagnostic(),
        )

    async def drop_local_journal(self) -> None:
        """Lose the local journal: the files survive, the known ids vanish."""

        self.journal.forget_canonical_bindings()

    async def start_run(self, *, generation: int = 3) -> ManifestRunReceipt:
        return await self._manifests.start_manifest(
            StartManifestCommand(
                context=self.context,
                client_observation_generation=generation,
                diagnostic_context=_diagnostic(),
            )
        )

    async def append_page(
        self,
        manifest_run_id: UUID,
        *,
        entries: tuple[ManifestEntry, ...],
    ) -> ContentDigest:
        page_digest = _digest(f"two-device-page-{manifest_run_id}")
        await self._manifests.append_manifest_page(
            AppendManifestPageCommand(
                context=self.context,
                manifest_run_id=manifest_run_id,
                page_number=0,
                entries=entries,
                page_digest=page_digest,
                diagnostic_context=_diagnostic(),
            )
        )
        return page_digest

    async def finalize_run(
        self, manifest_run_id: UUID, final_digest: ContentDigest
    ) -> ManifestRunReceipt:
        return await self._manifests.finalize_manifest(
            FinalizeManifestCommand(
                context=self.context,
                manifest_run_id=manifest_run_id,
                total_entry_count=1,
                final_digest=final_digest,
                diagnostic_context=_diagnostic(),
            )
        )

    async def read_actions(self, manifest_run_id: UUID) -> tuple[ManifestAction, ...]:
        actions: list[ManifestAction] = []
        after = 0
        while True:
            page = await self._manifests.read_manifest_actions(
                ManifestActionsQuery(
                    context=self.context,
                    manifest_run_id=manifest_run_id,
                    after_action_index=after,
                    limit=200,
                    diagnostic_context=_diagnostic(),
                )
            )
            actions.extend(page.actions)
            if not page.has_more:
                return tuple(actions)
            after = page.actions[-1].action_index

    async def complete_run(
        self, manifest_run_id: UUID, final_digest: ContentDigest
    ) -> DeviceCursorReceipt:
        return await self._manifests.complete_manifest(
            CompleteManifestCommand(
                context=self.context,
                manifest_run_id=manifest_run_id,
                final_digest=final_digest,
                diagnostic_context=_diagnostic(),
            )
        )

    async def reconcile(self, *, generation: int = 3) -> ReconcileOutcome:
        """Drive the full reconciliation wire from the local view's capture.

        Exactly the plugin's flow: start, upload the ordered capture page,
        finalize with the canonical final digest, read every planned action
        page, complete. The capture is the local view at call time; tests
        mutate the view before this call to model edits and losses.
        """

        run = await self.start_run(generation=generation)
        entries = tuple(
            ManifestEntry(
                local_entry_id=f"local-{index}",
                known_source_id=view.known_source_id,
                known_version_id=view.known_version_id,
                normalized_locator=NormalizedLocator(view.locator),
                fingerprint=view.fingerprint,
                observation_generation=generation,
            )
            for index, view in enumerate(
                sorted(self.journal.files.values(), key=lambda view: view.locator)
            )
        )
        page_digest = _digest(f"two-device-page-{run.manifest_run_id}")
        await self._manifests.append_manifest_page(
            AppendManifestPageCommand(
                context=self.context,
                manifest_run_id=run.manifest_run_id,
                page_number=0,
                entries=entries,
                page_digest=page_digest,
                diagnostic_context=_diagnostic(),
            )
        )
        final_digest = ContentDigest.parse(
            compute_manifest_final_digest(((0, len(entries), page_digest.hexadecimal),))
        )
        planned = await self._manifests.finalize_manifest(
            FinalizeManifestCommand(
                context=self.context,
                manifest_run_id=run.manifest_run_id,
                total_entry_count=len(entries),
                final_digest=final_digest,
                diagnostic_context=_diagnostic(),
            )
        )
        assert planned.state is ManifestRunState.PLANNED
        actions = await self.read_actions(run.manifest_run_id)
        cursor = await self.complete_run(run.manifest_run_id, final_digest)
        resolutions = await self._resolution_rows(run.manifest_run_id)
        return ReconcileOutcome(run=run, actions=actions, resolutions=resolutions, cursor=cursor)

    async def _resolution_rows(self, manifest_run_id: UUID) -> tuple[Any, ...]:
        async with self._engine.connect() as connection:
            rows = (
                await connection.execute(
                    sa.select(
                        manifest_entry_resolutions.c.local_entry_id,
                        manifest_entry_resolutions.c.match_kind,
                        manifest_entry_resolutions.c.resolved_source_id,
                    )
                    .where(manifest_entry_resolutions.c.manifest_run_id == manifest_run_id)
                    .order_by(
                        manifest_entry_resolutions.c.page_number,
                        manifest_entry_resolutions.c.entry_index,
                    )
                )
            ).all()
        return tuple(rows)

    async def cursor_row(self) -> tuple[int, int] | None:
        async with self._engine.connect() as connection:
            row = (
                await connection.execute(
                    sa.select(
                        device_cursors.c.acknowledged_sequence,
                        device_cursors.c.delivered_through_sequence,
                    ).where(
                        device_cursors.c.workspace_id == self.context.workspace_id,
                        device_cursors.c.device_id == self.context.device_id,
                    )
                )
            ).one_or_none()
        if row is None:
            return None
        return int(row.acknowledged_sequence), int(row.delivered_through_sequence)

    async def run_row(self, manifest_run_id: UUID) -> Any:
        async with self._engine.connect() as connection:
            return (
                await connection.execute(
                    sa.select(
                        manifest_runs.c.state,
                        manifest_runs.c.safe_error_code,
                        manifest_runs.c.policy_revision_number,
                    ).where(manifest_runs.c.manifest_run_id == manifest_run_id)
                )
            ).one()


class TwoDeviceStack:
    """One seeded workspace, its two devices and the production stores."""

    engine: AsyncEngine
    workspace: DeviceSyncWorkspace
    device_a: DeviceAPeer
    device_b: DeviceBPeer

    def __init__(
        self,
        *,
        engine: AsyncEngine,
        workspace: DeviceSyncWorkspace,
        device_a: DeviceAPeer,
        device_b: DeviceBPeer,
    ) -> None:
        self.engine = engine
        self.workspace = workspace
        self.device_a = device_a
        self.device_b = device_b

    async def count_sources_at(self, locator: str) -> int:
        """Distinct sources with the locator open (their active placement)."""

        async with self.engine.connect() as connection:
            value = await connection.scalar(
                sa.select(sa.func.count(sa.distinct(source_locators.c.source_id))).where(
                    source_locators.c.workspace_id == self.workspace.workspace_id,
                    source_locators.c.normalized_locator == locator,
                    source_locators.c.closed_sequence.is_(None),
                )
            )
        return int(value or 0)

    async def workspace_checkpoint(self) -> int:
        async with self.engine.connect() as connection:
            value = await connection.scalar(
                sa.select(sa.func.max(sync_events.c.event_sequence)).where(
                    sync_events.c.workspace_id == self.workspace.workspace_id
                )
            )
        return int(value or 0)


@pytest_asyncio.fixture
async def two_device_stack(
    source_publication_stack: SourcePublicationStack,
) -> AsyncIterator[TwoDeviceStack]:
    engine = create_source_store_engine(
        source_publication_stack.settings, source_publication_stack.password
    )
    try:
        workspace = await seed_device_sync_workspace(engine)
        await publish_workspace_policy(engine, workspace)
        device_b_id = uuid4()
        async with engine.begin() as connection:
            await connection.execute(
                sa.insert(devices).values(
                    device_id=device_b_id,
                    workspace_id=workspace.workspace_id,
                    user_id=workspace.owner_user_id,
                    device_name="Two-Device Device B",
                    device_kind="obsidian",
                )
            )
        device_b_context = DeviceSyncContext(
            workspace_id=workspace.workspace_id,
            device_id=device_b_id,
            user_id=workspace.owner_user_id,
        )
        yield TwoDeviceStack(
            engine=engine,
            workspace=workspace,
            device_a=DeviceAPeer(engine, workspace),
            device_b=DeviceBPeer(
                engine,
                PostgresqlDeviceEventStore(engine),
                PostgresqlDeviceManifestStore(engine),
                device_b_context,
            ),
        )
    finally:
        await dispose_source_store_engine(engine)


# --- remote edit through the real pull, and no echo --------------------------------------


@pytest.mark.asyncio
async def test_remote_edit_applies_through_pull_and_never_echoes_a_second_source(
    two_device_stack: TwoDeviceStack,
) -> None:
    device_a, device_b = two_device_stack.device_a, two_device_stack.device_b
    locator = "notes/two-device-remote-edit.md"
    base_fingerprint = fingerprint("two-device-remote-edit-base")
    edited_fingerprint = fingerprint("two-device-remote-edit-new")

    source = await device_a.publish(locator, base_fingerprint)
    first_page = await device_b.pull_and_apply()
    assert [event.event_type.value for event in first_page.events] == ["created"]
    applied_through = first_page.delivered_through_sequence
    await device_b.acknowledge(0, applied_through)

    # The remote edit: a new canonical version on the SAME source.
    edited_version_id = await device_a.publish_update(source, edited_fingerprint)
    second_page = await device_b.pull_and_apply()
    assert [event.event_type.value for event in second_page.events] == ["updated"]
    update_event = second_page.events[0]
    # Task 12b: the update carries its active locator (the content target).
    assert update_event.resulting_locator == NormalizedLocator(locator)
    assert update_event.prior_locator is None
    assert update_event.current_version_id == edited_version_id
    await device_b.acknowledge(applied_through, second_page.delivered_through_sequence)

    # Device B's capture matches the canonical current bytes: the plan is
    # exactly no_change — the received edit never echoes back as an upload,
    # and no second source ever appears at the locator.
    outcome = await device_b.reconcile()
    assert outcome.action_kinds == [ManifestActionKind.NO_CHANGE]
    assert outcome.source_ids == {source.source_id}
    assert await two_device_stack.count_sources_at(locator) == 1
    checkpoint = await two_device_stack.workspace_checkpoint()
    assert outcome.cursor.acknowledged_sequence == checkpoint
    assert await device_b.cursor_row() == (
        outcome.cursor.acknowledged_sequence,
        outcome.cursor.delivered_through_sequence,
    )


# --- lifecycle events across devices -----------------------------------------------------


@pytest.mark.asyncio
async def test_lifecycle_events_reconcile_historically_with_tombstones(
    two_device_stack: TwoDeviceStack,
) -> None:
    device_a, device_b = two_device_stack.device_a, two_device_stack.device_b
    nonce = uuid4().hex[:8]
    old_locator = f"notes/{nonce}/lifecycle-old.md"
    new_locator = f"notes/{nonce}/lifecycle-new.md"
    deleted_locator = f"notes/{nonce}/lifecycle-gone.md"
    renamed_fingerprint = fingerprint("two-device-lifecycle-renamed")
    deleted_fingerprint = fingerprint("two-device-lifecycle-deleted")

    renamed_source = await seed_renamed_source(
        two_device_stack.engine,
        two_device_stack.workspace,
        old_locator_text=old_locator,
        new_locator_text=new_locator,
        fingerprint=renamed_fingerprint,
    )
    deleted_source = await device_a.publish(deleted_locator, deleted_fingerprint, deleted=True)

    page = await device_b.pull()
    operations = [event.event_type.value for event in page.events]
    assert operations == ["created", "renamed", "created", "deleted"]
    rename_event = page.events[1]
    assert rename_event.prior_locator == NormalizedLocator(old_locator)
    assert rename_event.resulting_locator == NormalizedLocator(new_locator)
    delete_event = page.events[3]
    assert delete_event.prior_locator == NormalizedLocator(deleted_locator)
    await device_b.acknowledge(0, page.delivered_through_sequence)

    # Device B's stale capture still holds the pre-rename locator with the
    # exact current fingerprint, and the deleted file's last-known bytes.
    device_b.journal.observe(
        old_locator,
        renamed_fingerprint,
        known_source_id=renamed_source.source_id,
        known_version_id=renamed_source.version_ids[0],
    )
    device_b.journal.observe(
        deleted_locator,
        deleted_fingerprint,
        known_source_id=deleted_source.source_id,
        known_version_id=deleted_source.version_ids[0],
    )

    outcome = await device_b.reconcile()
    assert outcome.action_kinds.count(ManifestActionKind.NO_CHANGE) == 1
    assert outcome.action_kinds.count(ManifestActionKind.APPLY_TOMBSTONE) == 1
    no_change = next(
        action for action in outcome.actions if action.action_kind is ManifestActionKind.NO_CHANGE
    )
    tombstone = next(
        action
        for action in outcome.actions
        if action.action_kind is ManifestActionKind.APPLY_TOMBSTONE
    )
    assert no_change.source_id == renamed_source.source_id
    # The plan names the locator row ACTIVE at the checkpoint — where the
    # file lives now — never the closed locator the entry matched through.
    assert no_change.source_locator_id == renamed_source.new_locator_id
    assert no_change.source_locator_id != renamed_source.old_locator_id
    assert tombstone.source_tombstone_id == deleted_source.tombstone_id
    assert outcome.source_ids == {renamed_source.source_id, deleted_source.source_id}
    assert await two_device_stack.count_sources_at(old_locator) == 0
    assert await two_device_stack.count_sources_at(new_locator) == 1
    assert await device_b.cursor_row() == (
        outcome.cursor.acknowledged_sequence,
        outcome.cursor.delivered_through_sequence,
    )


# --- the concurrent canonical commit after the checkpoint -------------------------------


@pytest.mark.asyncio
async def test_concurrent_commit_after_checkpoint_stays_outside_plan_and_fence(
    two_device_stack: TwoDeviceStack,
) -> None:
    device_a, device_b = two_device_stack.device_a, two_device_stack.device_b
    locator = "notes/two-device-race-checkpoint.md"
    base_fingerprint = fingerprint("two-device-race-base")
    checkpoint_fingerprint = fingerprint("two-device-race-checkpoint-current")
    post_checkpoint_fingerprint = fingerprint("two-device-race-after-checkpoint")

    source = await device_a.publish(locator, base_fingerprint)
    checkpoint_version_id = await device_a.publish_update(source, checkpoint_fingerprint)
    checkpoint = await two_device_stack.workspace_checkpoint()

    # Device B's run starts NOW: its checkpoint binds. The concurrent
    # canonical commit lands strictly after it.
    run = await device_b.start_run()
    assert run.checkpoint_sequence == checkpoint
    post_checkpoint_version_id = await device_a.publish_update(source, post_checkpoint_fingerprint)
    assert post_checkpoint_version_id != checkpoint_version_id

    entry = ManifestEntry(
        local_entry_id="local-race",
        known_source_id=source.source_id,
        known_version_id=source.version_ids[0],
        normalized_locator=NormalizedLocator(locator),
        fingerprint=base_fingerprint,
        observation_generation=3,
    )
    page_digest = await device_b.append_page(run.manifest_run_id, entries=(entry,))
    final_digest = final_digest_of(run.manifest_run_id, page_digest)
    planned = await device_b.finalize_run(run.manifest_run_id, final_digest)
    assert planned.state is ManifestRunState.PLANNED
    actions = await device_b.read_actions(run.manifest_run_id)
    assert [action.action_kind for action in actions] == [ManifestActionKind.DOWNLOAD]
    # The catch-up download pins the version active AT the checkpoint, never
    # the concurrent commit that landed after it.
    download = actions[0]
    assert download.source_version_id == checkpoint_version_id
    assert download.checkpoint_locator == NormalizedLocator(locator)

    cursor = await device_b.complete_run(run.manifest_run_id, final_digest)
    # The completion fence acknowledges exactly the checkpoint; the
    # concurrent commit stays ahead of device B's cursor.
    assert cursor.acknowledged_sequence == checkpoint
    assert await device_b.cursor_row() == (
        cursor.acknowledged_sequence,
        cursor.delivered_through_sequence,
    )

    # The next pull delivers the concurrent commit's update event.
    next_page = await device_b.pull()
    assert [event.event_type.value for event in next_page.events] == ["updated"]
    assert next_page.events[0].current_version_id == post_checkpoint_version_id
    assert await two_device_stack.count_sources_at(locator) == 1


# --- the lost acknowledgement ------------------------------------------------------------


@pytest.mark.asyncio
async def test_lost_acknowledgement_replays_the_same_frozen_cursor(
    two_device_stack: TwoDeviceStack,
) -> None:
    device_a, device_b = two_device_stack.device_a, two_device_stack.device_b
    locator = "notes/two-device-lost-ack.md"
    observed = fingerprint("two-device-lost-ack")

    await device_a.publish(locator, observed)
    page = await device_b.pull_and_apply()
    applied_through = page.delivered_through_sequence

    first = await device_b.acknowledge(0, applied_through)
    # The response is lost; the device retries the exact acknowledgement.
    replayed = await device_b.acknowledge(0, applied_through)
    assert replayed == first
    assert await device_b.cursor_row() == (
        first.acknowledged_sequence,
        first.delivered_through_sequence,
    )

    # The journey still converges: the reconciliation completes over the
    # frozen cursor with exactly one canonical source.
    outcome = await device_b.reconcile()
    assert outcome.action_kinds == [ManifestActionKind.NO_CHANGE]
    assert await two_device_stack.count_sources_at(locator) == 1
    assert await device_b.cursor_row() == (
        outcome.cursor.acknowledged_sequence,
        outcome.cursor.delivered_through_sequence,
    )


# --- cursor regression / ack ahead: readable tokens, then repair -------------------------


@pytest.mark.asyncio
async def test_cursor_violations_surface_readable_tokens_and_repair_converges(
    two_device_stack: TwoDeviceStack,
) -> None:
    device_a, device_b = two_device_stack.device_a, two_device_stack.device_b
    locator = "notes/two-device-cursor-violations.md"
    observed = fingerprint("two-device-cursor-violations")

    await device_a.publish(locator, observed)
    page = await device_b.pull_and_apply()
    applied_through = page.delivered_through_sequence
    acknowledged = await device_b.acknowledge(0, applied_through)
    assert acknowledged.acknowledged_sequence == applied_through

    # A regression attempt names its closed reason and never moves the row.
    with pytest.raises(DeviceSyncError) as regression:
        await device_b.acknowledge(applied_through, 0)
    assert regression.value.code is DeviceSyncErrorCode.CURSOR_REGRESSION
    # An ack ahead of everything the server delivered fails closed too.
    with pytest.raises(DeviceSyncError) as ahead:
        await device_b.acknowledge(applied_through, applied_through + 1)
    assert ahead.value.code is DeviceSyncErrorCode.CURSOR_ACK_AHEAD
    assert await device_b.cursor_row() == (applied_through, applied_through)

    outcome = await device_b.reconcile()
    assert outcome.action_kinds == [ManifestActionKind.NO_CHANGE]
    assert await device_b.cursor_row() == (
        outcome.cursor.acknowledged_sequence,
        outcome.cursor.delivered_through_sequence,
    )
    assert await two_device_stack.count_sources_at(locator) == 1


# --- the mid-run policy advance ----------------------------------------------------------


@pytest.mark.asyncio
async def test_policy_advance_fails_the_run_closed_and_a_fresh_run_converges(
    two_device_stack: TwoDeviceStack,
) -> None:
    device_a, device_b = two_device_stack.device_a, two_device_stack.device_b
    locator = "notes/two-device-policy-advance.md"
    observed = fingerprint("two-device-policy-advance")

    source = await device_a.publish(locator, observed)
    device_b.journal.observe(
        locator,
        observed,
        known_source_id=source.source_id,
        known_version_id=source.version_ids[0],
    )
    entry = ManifestEntry(
        local_entry_id="local-policy",
        known_source_id=source.source_id,
        known_version_id=source.version_ids[0],
        normalized_locator=NormalizedLocator(locator),
        fingerprint=observed,
        observation_generation=3,
    )
    run = await device_b.start_run()
    page_digest = await device_b.append_page(run.manifest_run_id, entries=(entry,))
    final_digest = final_digest_of(run.manifest_run_id, page_digest)
    await device_b.finalize_run(run.manifest_run_id, final_digest)

    # The workspace publishes revision 2 while device B's run is planned.
    await publish_workspace_policy(
        two_device_stack.engine, two_device_stack.workspace, revision_number=2
    )

    with pytest.raises(DeviceSyncError) as raised:
        await device_b.read_actions(run.manifest_run_id)
    assert raised.value.code is DeviceSyncErrorCode.MANIFEST_POLICY_ADVANCED
    run_row = await device_b.run_row(run.manifest_run_id)
    assert run_row.state == "failed"
    assert run_row.safe_error_code == DeviceSyncErrorCode.MANIFEST_POLICY_ADVANCED.value
    assert await device_b.cursor_row() is None

    # One fresh run binds the new revision and converges.
    outcome = await device_b.reconcile()
    assert outcome.run.policy_revision_number == 2
    assert outcome.action_kinds == [ManifestActionKind.NO_CHANGE]
    assert outcome.source_ids == {source.source_id}
    assert await two_device_stack.count_sources_at(locator) == 1
    assert await device_b.cursor_row() == (
        outcome.cursor.acknowledged_sequence,
        outcome.cursor.delivered_through_sequence,
    )


# --- SQLite loss (the brief's verbatim journey) -------------------------------------------


@dataclass(frozen=True, slots=True)
class VaultFile:
    """One vault file a journey publishes: its locator and settled bytes."""

    locator: str
    fingerprint: SourceFingerprint


def vault_file(label: str, *, locator: str) -> VaultFile:
    return VaultFile(locator=locator, fingerprint=fingerprint(label))


FILE = vault_file("two-device-sqlite-loss", locator="notes/two-device-sqlite-loss.md")


@pytest.mark.asyncio
async def test_sqlite_loss_rebinds_without_duplicate_source(
    two_device_stack: TwoDeviceStack,
) -> None:
    device_a, device_b = two_device_stack.device_a, two_device_stack.device_b
    source = await device_a.publish(FILE.locator, FILE.fingerprint)
    device_b.journal.observe(
        FILE.locator,
        FILE.fingerprint,
        known_source_id=source.source_id,
        known_version_id=source.version_ids[0],
    )
    await device_b.drop_local_journal()

    result = await device_b.reconcile()

    assert result.source_ids == {source.source_id}
    assert await two_device_stack.count_sources_at(FILE.locator) == 1
    assert result.action_kinds == [ManifestActionKind.NO_CHANGE]


# --- the local edit during the reconciliation ---------------------------------------------


@pytest.mark.asyncio
async def test_edit_during_reconcile_settles_the_divergence_conflict(
    two_device_stack: TwoDeviceStack,
) -> None:
    device_a, device_b = two_device_stack.device_a, two_device_stack.device_b
    locator = "notes/two-device-edit-during-reconcile.md"
    canonical_fingerprint = fingerprint("two-device-edit-during-canonical")
    local_edit_fingerprint = fingerprint("two-device-edit-during-local")

    source = await device_a.publish(locator, canonical_fingerprint)
    # The canonical source advances while device B holds the v1 base: only
    # a divergence from BOTH the trusted base and the canonical current is
    # the local-diverged conflict (a plain edit on the still-current base
    # would plan an upload instead).
    advanced_fingerprint = fingerprint("two-device-edit-during-advanced")
    await device_a.publish_update(source, advanced_fingerprint)
    device_b.journal.observe(
        locator,
        canonical_fingerprint,
        known_source_id=source.source_id,
        known_version_id=source.version_ids[0],
    )

    # Device B starts the run (the capture moment), then edits the file
    # locally BEFORE the page lands.
    run = await device_b.start_run()
    device_b.journal.observe(
        locator,
        local_edit_fingerprint,
        known_source_id=source.source_id,
        known_version_id=source.version_ids[0],
    )
    entry = ManifestEntry(
        local_entry_id="local-edited",
        known_source_id=source.source_id,
        known_version_id=source.version_ids[0],
        normalized_locator=NormalizedLocator(locator),
        fingerprint=local_edit_fingerprint,
        observation_generation=3,
    )
    page_digest = await device_b.append_page(run.manifest_run_id, entries=(entry,))
    final_digest = final_digest_of(run.manifest_run_id, page_digest)
    await device_b.finalize_run(run.manifest_run_id, final_digest)
    actions = await device_b.read_actions(run.manifest_run_id)

    # The diverged entry settles as the closed conflict with its readable
    # reason; the local bytes survive (no download clobbers them) and the
    # one canonical source is untouched.
    assert [action.action_kind for action in actions] == [ManifestActionKind.CONFLICT]
    conflict = actions[0]
    assert conflict.reason is not None
    assert conflict.reason.value == "device_manifest_local_diverged"
    cursor = await device_b.complete_run(run.manifest_run_id, final_digest)
    assert await two_device_stack.count_sources_at(locator) == 1
    assert await device_b.cursor_row() == (
        cursor.acknowledged_sequence,
        cursor.delivered_through_sequence,
    )
