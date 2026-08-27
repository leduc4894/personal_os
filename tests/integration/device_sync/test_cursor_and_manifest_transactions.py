"""Disposable PostgreSQL transaction coverage for manifest reconciliation.

Live coverage of the durable semantics the unit seam cannot prove: a run
start binds the cursor base, the statement checkpoint and the workspace's
active policy revision while exactly resuming the one unfinished run;
pages are ordered from zero with exact digest/count replay and a different
digest for the same page fails the run; the cumulative 100,000-entry cap
holds; finalization verifies the canonical-JSON final digest over the
run's pages and materializes the deterministic action plan (all six action
kinds, canonical-only downloads, checkpoint-bounded identity recovery);
the first successful action-page read moves ``planned`` to ``applying``
and later reads are state-preserving replays; a policy advance or the
one-hour database expiry invalidates the unfinished run with the closed
reason persisted on the run row; and only the same transaction changing
the exact ``applying`` run to ``completed`` may advance the device cursor
to the checkpoint without a delivered watermark — foreign, expired,
failed and already-completed runs grant no new advance, and a lost
completion response replays to the same cursor receipt.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.device_sync.conftest import (
    DeviceSyncWorkspace,
    seed_device_sync_workspace,
)
from tests.integration.source_publication.conftest import SourcePublicationStack

from personal_os.device_sync.contracts import (
    MAX_MANIFEST_PAGE_ENTRIES,
    MAX_MANIFEST_RUN_ENTRIES,
    AppendManifestPageCommand,
    CompleteManifestCommand,
    DeviceCursorReceipt,
    DeviceSyncContext,
    FinalizeManifestCommand,
    ManifestActionKind,
    ManifestActionPage,
    ManifestActionsQuery,
    ManifestEntry,
    ManifestPageReceipt,
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
    manifest_actions,
    manifest_entry_resolutions,
    manifest_runs,
    policy_drafts,
    policy_previews,
    policy_rules,
    policy_signing_keys,
    source_locators,
    source_policies,
    source_tombstones,
    source_versions,
    sources,
    sync_events,
    workspace_policy_state,
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


def _digest(label: str) -> ContentDigest:
    return ContentDigest.parse(hashlib.sha256(label.encode("ascii")).hexdigest())


def fingerprint(
    label: str, *, media_type: str = "text/markdown", salt: str | None = None
) -> SourceFingerprint:
    """One settled-byte fingerprint; the default salt keeps every canonical
    content object globally unique across test populations."""

    effective_salt = salt if salt is not None else uuid4().hex
    material = f"{label}:{effective_salt}"
    return SourceFingerprint(
        sha256=hashlib.sha256(material.encode("ascii")).hexdigest(),
        size_bytes=len(material),
        media_type=media_type,
    )


# --- workspace policy publication ------------------------------------------------


@dataclass(frozen=True, slots=True)
class SeededPolicyRule:
    """One directly seeded published policy rule row."""

    rule_kind: str
    text_operand: str

    def row(self, policy_revision_id: UUID) -> dict[str, Any]:
        semantic = hashlib.sha256(
            f"{self.rule_kind}:{self.text_operand}".encode("ascii")
        ).hexdigest()
        return {
            "policy_revision_id": policy_revision_id,
            "rule_id": uuid4(),
            "rule_kind": self.rule_kind,
            "source_id_operand": None,
            "text_operand": self.text_operand,
            "size_bytes_operand": None,
            "semantic_fingerprint": semantic,
        }


async def publish_workspace_policy(
    engine: AsyncEngine,
    workspace: DeviceSyncWorkspace,
    *,
    rules: tuple[SeededPolicyRule, ...] = (),
    revision_number: int = 1,
) -> UUID:
    """Seed one minimal published policy revision and activate it.

    The manifest store reads only the revision identity, its rules and the
    workspace's active pointer; every NOT NULL publication column receives
    a grammatically valid placeholder because no signature verification
    runs on this path.
    """

    now = datetime.now(UTC)
    nonce = uuid4().hex
    policy_revision_id = uuid4()
    preview_id = uuid4()
    draft_id = uuid4()
    signing_key_id = uuid4()
    payload = hashlib.sha256(nonce.encode("ascii")).digest()
    async with engine.begin() as connection:
        # The publication grammar keeps exactly ONE draft per workspace
        # (``uq_policy_drafts__workspace``): a later revision evolves that
        # single draft instead of inserting a second row, exactly the way
        # the real publish flow advances a workspace's draft. The lineage
        # check (``ck_source_policies__parent_lineage``) likewise forces
        # every revision after the first to name its parent: chain the new
        # revision onto the workspace's currently active one.
        active_revision_id = (
            await connection.execute(
                sa.select(workspace_policy_state.c.active_policy_revision_id).where(
                    workspace_policy_state.c.workspace_id == workspace.workspace_id
                )
            )
        ).scalar_one_or_none()
        existing_draft_id = (
            await connection.execute(
                sa.select(policy_drafts.c.policy_draft_id).where(
                    policy_drafts.c.workspace_id == workspace.workspace_id
                )
            )
        ).scalar_one_or_none()
        if existing_draft_id is None:
            await connection.execute(
                sa.insert(policy_drafts).values(
                    policy_draft_id=draft_id,
                    workspace_id=workspace.workspace_id,
                    draft_version=revision_number,
                    base_policy_revision_id=None,
                    created_by_user_id=workspace.owner_user_id,
                    updated_by_user_id=workspace.owner_user_id,
                    created_at=now,
                    updated_at=now,
                )
            )
        else:
            draft_id = existing_draft_id
            await connection.execute(
                sa.update(policy_drafts)
                .values(draft_version=revision_number, updated_at=now)
                .where(policy_drafts.c.policy_draft_id == draft_id)
            )
        await connection.execute(
            sa.insert(policy_previews).values(
                policy_preview_id=preview_id,
                workspace_id=workspace.workspace_id,
                policy_draft_id=draft_id,
                draft_version=revision_number,
                draft_sha256=hashlib.sha256(b"manifest-fixture-draft").hexdigest(),
                base_policy_revision_id=None,
                source_checkpoint_event_sequence=0,
                state="ready",
                newly_excluded_count=0,
                still_excluded_count=0,
                newly_allowed_count=0,
                still_allowed_count=0,
                indeterminate_count=0,
                impact_digest=hashlib.sha256(b"manifest-fixture-impact").hexdigest(),
                attempt_count=1,
                available_at=now,
                created_by_user_id=workspace.owner_user_id,
                created_at=now,
                ready_at=now,
                expires_at=now,
            )
        )
        await connection.execute(
            sa.insert(policy_signing_keys).values(
                signing_key_id=signing_key_id,
                workspace_id=workspace.workspace_id,
                algorithm="Ed25519",
                public_key_bytes=hashlib.sha256(nonce.encode("ascii")).digest(),
                introduced_keyset_revision=1,
                created_at=now,
            )
        )
        await connection.execute(
            sa.insert(source_policies).values(
                policy_revision_id=policy_revision_id,
                workspace_id=workspace.workspace_id,
                revision_number=revision_number,
                parent_policy_revision_id=(active_revision_id if revision_number > 1 else None),
                default_decision="allowed",
                source_checkpoint_event_sequence=0,
                policy_preview_id=preview_id,
                publication_idempotency_key=f"manifest-fixture-{nonce[:16]}",
                request_fingerprint=hashlib.sha256(nonce.encode("ascii")).hexdigest(),
                snapshot_contract="exclusion_policy_snapshot/v1",
                snapshot_payload_bytes=payload,
                snapshot_payload_sha256=hashlib.sha256(payload).hexdigest(),
                signing_key_id=signing_key_id,
                signature_bytes=b"s" * 64,
                published_by_user_id=workspace.owner_user_id,
                published_at=now,
            )
        )
        for rule in rules:
            await connection.execute(sa.insert(policy_rules).values(**rule.row(policy_revision_id)))
        await connection.execute(
            postgresql_insert(workspace_policy_state)
            .values(
                workspace_id=workspace.workspace_id,
                active_policy_revision_id=policy_revision_id,
                active_revision_number=revision_number,
            )
            .on_conflict_do_update(
                index_elements=["workspace_id"],
                set_={
                    "active_policy_revision_id": policy_revision_id,
                    "active_revision_number": revision_number,
                    "updated_at": sa.text("CURRENT_TIMESTAMP"),
                },
            )
        )
    return policy_revision_id


# --- canonical reconciliation population ------------------------------------------


@dataclass(frozen=True, slots=True)
class SeededCanonicalSource:
    """One seeded canonical source with its checkpoint lifecycle evidence."""

    source_id: UUID
    version_ids: tuple[UUID, ...]
    locator_id: UUID
    locator_text: str
    tombstone_id: UUID | None


async def seed_canonical_source(
    engine: AsyncEngine,
    workspace: DeviceSyncWorkspace,
    *,
    locator_text: str,
    fingerprints: tuple[SourceFingerprint, ...],
    deleted: bool = False,
) -> SeededCanonicalSource:
    """Seed one source whose create/update/delete history is canonical.

    The source commits one create event per fingerprint (each opening the
    same locator is invalid, so a single locator row is opened by the
    create and closed by the delete), and a delete closes the locator and
    opens the tombstone retaining the final version and the locator.
    """

    nonce = uuid4().hex
    source_id = uuid4()
    content_object_ids = [uuid4() for _ in fingerprints]
    version_ids = [uuid4() for _ in fingerprints]
    event_ids = [uuid4() for _ in fingerprints] + ([uuid4()] if deleted else [])
    now = datetime.now(UTC)
    sequences: list[int] = []

    async with engine.begin() as connection:
        await connection.execute(
            sa.insert(sources).values(
                source_id=source_id,
                workspace_id=workspace.workspace_id,
                source_type="markdown",
                title=f"Reconciliation source {nonce[:12]}",
                sync_state="pending",
                current_version_id=None,
            )
        )
        for index, observed in enumerate(fingerprints):
            await connection.execute(
                sa.insert(content_objects).values(
                    content_object_id=content_object_ids[index],
                    content_hash=observed.sha256,
                    object_key=(
                        f"objects/sha256/{observed.sha256[:2]}/"
                        f"{observed.sha256[2:4]}/{observed.sha256}"
                    ),
                    byte_size=observed.size_bytes,
                    media_type=observed.media_type,
                    verified_at=now,
                )
            )
            await connection.execute(
                sa.insert(source_versions).values(
                    source_version_id=version_ids[index],
                    workspace_id=workspace.workspace_id,
                    source_id=source_id,
                    content_object_id=content_object_ids[index],
                    content_version=index + 1,
                    parent_version_id=version_ids[index - 1] if index else None,
                    author_kind="device",
                    author_id=workspace.device_id,
                    committed_at=now,
                )
            )
        for index in range(len(fingerprints)):
            base = version_ids[index - 1] if index else None
            result = await connection.execute(
                sa.insert(sync_events)
                .values(
                    event_id=event_ids[index],
                    workspace_id=workspace.workspace_id,
                    source_id=source_id,
                    device_id=workspace.device_id,
                    committed_version_id=version_ids[index],
                    base_version_id=base,
                    idempotency_key=f"manifest-fixture-{nonce}-{index}",
                    request_fingerprint=hashlib.sha256(
                        f"manifest-fixture-{nonce}-{index}".encode("ascii")
                    ).hexdigest(),
                    event_type="create" if index == 0 else "update",
                )
                .returning(sync_events.c.event_sequence)
            )
            sequences.append(int(result.scalar_one()))
        await connection.execute(
            sa.update(sources)
            .values(
                sync_state="active",
                current_version_id=version_ids[-1],
                updated_at=sa.text("CURRENT_TIMESTAMP"),
            )
            .where(
                sources.c.workspace_id == workspace.workspace_id,
                sources.c.source_id == source_id,
            )
        )
        locator_id = uuid4()
        delete_sequence: int | None = None
        tombstone_id: UUID | None = None
        if deleted:
            delete_event_id = event_ids[-1]
            result = await connection.execute(
                sa.insert(sync_events)
                .values(
                    event_id=delete_event_id,
                    workspace_id=workspace.workspace_id,
                    source_id=source_id,
                    device_id=workspace.device_id,
                    committed_version_id=version_ids[-1],
                    base_version_id=version_ids[-1],
                    idempotency_key=f"manifest-fixture-{nonce}-delete",
                    request_fingerprint=hashlib.sha256(
                        f"manifest-fixture-{nonce}-delete".encode("ascii")
                    ).hexdigest(),
                    event_type="delete",
                )
                .returning(sync_events.c.event_sequence)
            )
            delete_sequence = int(result.scalar_one())
            tombstone_id = uuid4()
            await connection.execute(
                sa.insert(source_tombstones).values(
                    source_tombstone_id=tombstone_id,
                    workspace_id=workspace.workspace_id,
                    source_id=source_id,
                    delete_event_id=delete_event_id,
                    retained_version_id=version_ids[-1],
                    retained_locator=locator_text,
                    actor_kind="device",
                    actor_id=workspace.device_id,
                    deleted_at=sa.text("CURRENT_TIMESTAMP"),
                )
            )
        await connection.execute(
            sa.insert(source_locators).values(
                source_locator_id=locator_id,
                workspace_id=workspace.workspace_id,
                source_id=source_id,
                normalized_locator=locator_text,
                display_locator=locator_text,
                opened_event_id=event_ids[0],
                opened_sequence=sequences[0],
                closed_event_id=event_ids[-1] if deleted else None,
                closed_sequence=delete_sequence if deleted else None,
                closed_at=sa.text("CURRENT_TIMESTAMP") if deleted else None,
            )
        )
    return SeededCanonicalSource(
        source_id=source_id,
        version_ids=tuple(version_ids),
        locator_id=locator_id,
        locator_text=locator_text,
        tombstone_id=tombstone_id,
    )


@dataclass(frozen=True, slots=True)
class SeededRenamedSource:
    """One seeded source whose create locator a later rename closed."""

    source_id: UUID
    version_ids: tuple[UUID, ...]
    old_locator_id: UUID
    old_locator_text: str
    new_locator_id: UUID
    new_locator_text: str


async def seed_renamed_source(
    engine: AsyncEngine,
    workspace: DeviceSyncWorkspace,
    *,
    old_locator_text: str,
    new_locator_text: str,
    fingerprint: SourceFingerprint,
) -> SeededRenamedSource:
    """Seed one source renamed remotely before any run checkpoint.

    The create event opens the old locator and the rename event closes it
    while opening the new one, keeping the version identity: at any later
    checkpoint the old locator is historical evidence and the new locator
    is the source's active placement.
    """

    nonce = uuid4().hex
    source_id = uuid4()
    content_object_id = uuid4()
    version_id = uuid4()
    create_event_id = uuid4()
    rename_event_id = uuid4()
    now = datetime.now(UTC)

    async with engine.begin() as connection:
        await connection.execute(
            sa.insert(sources).values(
                source_id=source_id,
                workspace_id=workspace.workspace_id,
                source_type="markdown",
                title=f"Rename journey source {nonce[:12]}",
                sync_state="pending",
                current_version_id=None,
            )
        )
        await connection.execute(
            sa.insert(content_objects).values(
                content_object_id=content_object_id,
                content_hash=fingerprint.sha256,
                object_key=(
                    f"objects/sha256/{fingerprint.sha256[:2]}/"
                    f"{fingerprint.sha256[2:4]}/{fingerprint.sha256}"
                ),
                byte_size=fingerprint.size_bytes,
                media_type=fingerprint.media_type,
                verified_at=now,
            )
        )
        await connection.execute(
            sa.insert(source_versions).values(
                source_version_id=version_id,
                workspace_id=workspace.workspace_id,
                source_id=source_id,
                content_object_id=content_object_id,
                content_version=1,
                parent_version_id=None,
                author_kind="device",
                author_id=workspace.device_id,
                committed_at=now,
            )
        )
        create_result = await connection.execute(
            sa.insert(sync_events)
            .values(
                event_id=create_event_id,
                workspace_id=workspace.workspace_id,
                source_id=source_id,
                device_id=workspace.device_id,
                committed_version_id=version_id,
                base_version_id=None,
                idempotency_key=f"rename-journey-{nonce}-create",
                request_fingerprint=hashlib.sha256(
                    f"rename-journey-{nonce}-create".encode("ascii")
                ).hexdigest(),
                event_type="create",
            )
            .returning(sync_events.c.event_sequence)
        )
        create_sequence = int(create_result.scalar_one())
        rename_result = await connection.execute(
            sa.insert(sync_events)
            .values(
                event_id=rename_event_id,
                workspace_id=workspace.workspace_id,
                source_id=source_id,
                device_id=workspace.device_id,
                committed_version_id=version_id,
                base_version_id=version_id,
                idempotency_key=f"rename-journey-{nonce}-rename",
                request_fingerprint=hashlib.sha256(
                    f"rename-journey-{nonce}-rename".encode("ascii")
                ).hexdigest(),
                event_type="rename",
            )
            .returning(sync_events.c.event_sequence)
        )
        rename_sequence = int(rename_result.scalar_one())
        await connection.execute(
            sa.update(sources)
            .values(
                sync_state="active",
                current_version_id=version_id,
                updated_at=sa.text("CURRENT_TIMESTAMP"),
            )
            .where(
                sources.c.workspace_id == workspace.workspace_id,
                sources.c.source_id == source_id,
            )
        )
        old_locator_id = uuid4()
        new_locator_id = uuid4()
        await connection.execute(
            sa.insert(source_locators).values(
                source_locator_id=old_locator_id,
                workspace_id=workspace.workspace_id,
                source_id=source_id,
                normalized_locator=old_locator_text,
                display_locator=old_locator_text,
                opened_event_id=create_event_id,
                opened_sequence=create_sequence,
                closed_event_id=rename_event_id,
                closed_sequence=rename_sequence,
                closed_at=sa.text("CURRENT_TIMESTAMP"),
            )
        )
        await connection.execute(
            sa.insert(source_locators).values(
                source_locator_id=new_locator_id,
                workspace_id=workspace.workspace_id,
                source_id=source_id,
                normalized_locator=new_locator_text,
                display_locator=new_locator_text,
                opened_event_id=rename_event_id,
                opened_sequence=rename_sequence,
                closed_event_id=None,
                closed_sequence=None,
                closed_at=None,
            )
        )
    return SeededRenamedSource(
        source_id=source_id,
        version_ids=(version_id,),
        old_locator_id=old_locator_id,
        old_locator_text=old_locator_text,
        new_locator_id=new_locator_id,
        new_locator_text=new_locator_text,
    )


@dataclass(frozen=True, slots=True)
class ReconciliationPopulation:
    """The canonical sources and foreign evidence of the planner scenario."""

    workspace: DeviceSyncWorkspace
    policy_revision_number: int
    match_source: SeededCanonicalSource
    stale_source: SeededCanonicalSource
    diverged_source: SeededCanonicalSource
    deleted_source: SeededCanonicalSource
    absent_source: SeededCanonicalSource
    forbidden_source: SeededCanonicalSource
    foreign_source_id: UUID
    match_fingerprint: SourceFingerprint
    stale_base_fingerprint: SourceFingerprint
    stale_current_fingerprint: SourceFingerprint
    diverged_fingerprint: SourceFingerprint
    deleted_fingerprint: SourceFingerprint


MATCH_LOCATOR = "notes/match.md"
STALE_LOCATOR = "notes/advanced.md"
DIVERGED_LOCATOR = "notes/diverged.md"
DELETED_LOCATOR = "notes/gone.md"
ABSENT_LOCATOR = "notes/absent.md"
FORBIDDEN_LOCATOR = "secret/plan.md"
UPLOAD_LOCATOR = "notes/new-file.md"
EXCLUDED_LOCATOR = "notes/vault-artifact.md"
RENAMED_OLD_LOCATOR = "notes/renamed-old.md"
RENAMED_NEW_LOCATOR = "notes/renamed-new.md"
RENAMED_JOURNEY_DELETED_LOCATOR = "notes/renamed-journey-gone.md"


def unique_fingerprint(label: str, *, media_type: str = "text/markdown") -> SourceFingerprint:
    return fingerprint(label, media_type=media_type)


async def seed_reconciliation_population(engine: AsyncEngine) -> ReconciliationPopulation:
    """Seed the workspace, published policy and canonical planner scenario."""

    workspace = await seed_device_sync_workspace(engine)
    await publish_workspace_policy(
        engine,
        workspace,
        rules=(SeededPolicyRule(rule_kind="media_type", text_operand="text/x-forbidden"),),
    )
    match_fingerprint = unique_fingerprint("match")
    stale_base_fingerprint = unique_fingerprint("stale-base")
    stale_current_fingerprint = unique_fingerprint("stale-current")
    diverged_fingerprint = unique_fingerprint("diverged")
    deleted_fingerprint = unique_fingerprint("deleted")
    absent_fingerprint = unique_fingerprint("absent")
    forbidden_fingerprint = unique_fingerprint("forbidden", media_type="text/x-forbidden")
    match_source = await seed_canonical_source(
        engine, workspace, locator_text=MATCH_LOCATOR, fingerprints=(match_fingerprint,)
    )
    stale_source = await seed_canonical_source(
        engine,
        workspace,
        locator_text=STALE_LOCATOR,
        fingerprints=(stale_base_fingerprint, stale_current_fingerprint),
    )
    diverged_source = await seed_canonical_source(
        engine, workspace, locator_text=DIVERGED_LOCATOR, fingerprints=(diverged_fingerprint,)
    )
    deleted_source = await seed_canonical_source(
        engine,
        workspace,
        locator_text=DELETED_LOCATOR,
        fingerprints=(deleted_fingerprint,),
        deleted=True,
    )
    absent_source = await seed_canonical_source(
        engine, workspace, locator_text=ABSENT_LOCATOR, fingerprints=(absent_fingerprint,)
    )
    forbidden_source = await seed_canonical_source(
        engine, workspace, locator_text=FORBIDDEN_LOCATOR, fingerprints=(forbidden_fingerprint,)
    )
    foreign_workspace = await seed_device_sync_workspace(engine)
    await publish_workspace_policy(engine, foreign_workspace)
    foreign_source = await seed_canonical_source(
        engine,
        foreign_workspace,
        locator_text="notes/foreign.md",
        fingerprints=(fingerprint("manifest-fixture-foreign"),),
    )
    return ReconciliationPopulation(
        workspace=workspace,
        policy_revision_number=1,
        match_source=match_source,
        stale_source=stale_source,
        diverged_source=diverged_source,
        deleted_source=deleted_source,
        absent_source=absent_source,
        forbidden_source=forbidden_source,
        foreign_source_id=foreign_source.source_id,
        match_fingerprint=match_fingerprint,
        stale_base_fingerprint=stale_base_fingerprint,
        stale_current_fingerprint=stale_current_fingerprint,
        diverged_fingerprint=diverged_fingerprint,
        deleted_fingerprint=deleted_fingerprint,
    )


def manifest_entry(
    entry_id: str,
    *,
    locator: str,
    observed: SourceFingerprint,
    known_source_id: UUID | None = None,
    known_version_id: UUID | None = None,
) -> ManifestEntry:
    return ManifestEntry(
        local_entry_id=entry_id,
        known_source_id=known_source_id,
        known_version_id=known_version_id,
        normalized_locator=NormalizedLocator(locator),
        fingerprint=observed,
        observation_generation=3,
    )


async def workspace_checkpoint(engine: AsyncEngine, workspace_id: UUID) -> int:
    async with engine.connect() as connection:
        value = await connection.scalar(
            sa.select(sa.func.max(sync_events.c.event_sequence)).where(
                sync_events.c.workspace_id == workspace_id
            )
        )
    return int(value or 0)


# --- harness -----------------------------------------------------------------------


@dataclass
class ManifestStoreHarness:
    """One disposable engine, the manifest store and its event-store peer."""

    engine: AsyncEngine
    store: PostgresqlDeviceManifestStore
    events: PostgresqlDeviceEventStore

    async def start(
        self,
        context: DeviceSyncContext,
        *,
        generation: int = 3,
    ) -> ManifestRunReceipt:
        return await self.store.start_manifest(
            StartManifestCommand(
                context=context,
                client_observation_generation=generation,
                diagnostic_context=_diagnostic(),
            )
        )

    async def append_page(
        self,
        context: DeviceSyncContext,
        manifest_run_id: UUID,
        *,
        page_number: int,
        entries: tuple[ManifestEntry, ...],
        page_digest: ContentDigest,
    ) -> ManifestPageReceipt:
        return await self.store.append_manifest_page(
            AppendManifestPageCommand(
                context=context,
                manifest_run_id=manifest_run_id,
                page_number=page_number,
                entries=entries,
                page_digest=page_digest,
                diagnostic_context=_diagnostic(),
            )
        )

    async def finalize(
        self,
        context: DeviceSyncContext,
        manifest_run_id: UUID,
        *,
        total_entry_count: int,
        final_digest: ContentDigest,
    ) -> ManifestRunReceipt:
        return await self.store.finalize_manifest(
            FinalizeManifestCommand(
                context=context,
                manifest_run_id=manifest_run_id,
                total_entry_count=total_entry_count,
                final_digest=final_digest,
                diagnostic_context=_diagnostic(),
            )
        )

    async def read_actions(
        self,
        context: DeviceSyncContext,
        manifest_run_id: UUID,
        *,
        after: int = 0,
        limit: int = 200,
    ) -> ManifestActionPage:
        return await self.store.read_manifest_actions(
            ManifestActionsQuery(
                context=context,
                manifest_run_id=manifest_run_id,
                after_action_index=after,
                limit=limit,
                diagnostic_context=_diagnostic(),
            )
        )

    async def complete(
        self,
        context: DeviceSyncContext,
        manifest_run_id: UUID,
        *,
        final_digest: ContentDigest,
    ) -> DeviceCursorReceipt:
        receipt = await self.store.complete_manifest(
            CompleteManifestCommand(
                context=context,
                manifest_run_id=manifest_run_id,
                final_digest=final_digest,
                diagnostic_context=_diagnostic(),
            )
        )
        assert isinstance(receipt, DeviceCursorReceipt)
        return receipt

    async def run_row(self, manifest_run_id: UUID) -> Any:
        async with self.engine.connect() as connection:
            return (
                await connection.execute(
                    sa.select(
                        manifest_runs.c.state,
                        manifest_runs.c.next_page_number,
                        manifest_runs.c.entry_count,
                        manifest_runs.c.safe_error_code,
                        manifest_runs.c.final_digest,
                    ).where(manifest_runs.c.manifest_run_id == manifest_run_id)
                )
            ).one()

    async def cursor_row(self, workspace: DeviceSyncWorkspace) -> tuple[int, int] | None:
        async with self.engine.connect() as connection:
            row = (
                await connection.execute(
                    sa.select(
                        device_cursors.c.acknowledged_sequence,
                        device_cursors.c.delivered_through_sequence,
                    ).where(
                        device_cursors.c.workspace_id == workspace.workspace_id,
                        device_cursors.c.device_id == workspace.device_id,
                    )
                )
            ).one_or_none()
        if row is None:
            return None
        return int(row.acknowledged_sequence), int(row.delivered_through_sequence)

    async def resolution_rows(self, manifest_run_id: UUID) -> list[Any]:
        async with self.engine.connect() as connection:
            return list(
                await connection.execute(
                    sa.select(
                        manifest_entry_resolutions.c.local_entry_id,
                        manifest_entry_resolutions.c.match_kind,
                        manifest_entry_resolutions.c.locator_evidence_digest,
                        manifest_entry_resolutions.c.resolved_source_id,
                        manifest_entry_resolutions.c.resolved_source_locator_id,
                    )
                    .where(manifest_entry_resolutions.c.manifest_run_id == manifest_run_id)
                    .order_by(
                        manifest_entry_resolutions.c.page_number,
                        manifest_entry_resolutions.c.entry_index,
                    )
                )
            )

    async def set_entry_count(self, manifest_run_id: UUID, entry_count: int) -> None:
        async with self.engine.begin() as connection:
            await connection.execute(
                sa.update(manifest_runs)
                .values(entry_count=entry_count)
                .where(manifest_runs.c.manifest_run_id == manifest_run_id)
            )

    async def expire_run(self, manifest_run_id: UUID) -> None:
        async with self.engine.begin() as connection:
            await connection.execute(
                sa.update(manifest_runs)
                .values(expires_at=sa.text("CURRENT_TIMESTAMP"))
                .where(manifest_runs.c.manifest_run_id == manifest_run_id)
            )

    async def advance_policy(self, workspace: DeviceSyncWorkspace) -> None:
        async with self.engine.begin() as connection:
            await connection.execute(
                sa.update(workspace_policy_state)
                .values(active_revision_number=sa.text("active_revision_number + 1"))
                .where(workspace_policy_state.c.workspace_id == workspace.workspace_id)
            )

    async def drive_planned_run(
        self, population: ReconciliationPopulation
    ) -> tuple[ManifestRunReceipt, ContentDigest]:
        """Start, fill and finalize the seven-entry planner scenario run."""

        context = population.workspace.context()
        run = await self.start(context)
        entries = (
            manifest_entry(
                "entry-match", locator=MATCH_LOCATOR, observed=population.match_fingerprint
            ),
            manifest_entry(
                "entry-stale",
                locator=STALE_LOCATOR,
                observed=population.stale_base_fingerprint,
                known_source_id=population.stale_source.source_id,
                known_version_id=population.stale_source.version_ids[0],
            ),
            manifest_entry(
                "entry-diverged",
                locator=DIVERGED_LOCATOR,
                observed=unique_fingerprint("diverged-local"),
            ),
            manifest_entry(
                "entry-gone", locator=DELETED_LOCATOR, observed=population.deleted_fingerprint
            ),
            manifest_entry(
                "entry-upload",
                locator=UPLOAD_LOCATOR,
                observed=fingerprint("manifest-fixture-upload"),
            ),
            manifest_entry(
                "entry-excluded",
                locator=EXCLUDED_LOCATOR,
                observed=fingerprint("manifest-fixture-excluded", media_type="text/x-forbidden"),
            ),
            manifest_entry(
                "entry-foreign",
                locator="notes/foreign-evidence.md",
                observed=fingerprint("manifest-fixture-foreign-entry"),
                known_source_id=population.foreign_source_id,
            ),
        )
        page_digest = _digest("planner-scenario-page-0")
        await self.append_page(
            context, run.manifest_run_id, page_number=0, entries=entries, page_digest=page_digest
        )
        final_digest_hex = compute_manifest_final_digest(
            ((0, len(entries), page_digest.hexadecimal),)
        )
        final_digest = ContentDigest.parse(final_digest_hex)
        planned = await self.finalize(
            context,
            run.manifest_run_id,
            total_entry_count=len(entries),
            final_digest=final_digest,
        )
        assert planned.state is ManifestRunState.PLANNED
        return run, final_digest


@pytest_asyncio.fixture
async def manifest_store(
    source_publication_stack: SourcePublicationStack,
) -> ManifestStoreHarness:
    engine = create_source_store_engine(
        source_publication_stack.settings, source_publication_stack.password
    )
    try:
        yield ManifestStoreHarness(
            engine=engine,
            store=PostgresqlDeviceManifestStore(engine),
            events=PostgresqlDeviceEventStore(engine),
        )
    finally:
        await dispose_source_store_engine(engine)


# --- run start, resume and the single unfinished run -------------------------------


@pytest.mark.asyncio
async def test_start_binds_cursor_base_checkpoint_and_policy_revision(
    manifest_store: ManifestStoreHarness,
) -> None:
    population = await seed_reconciliation_population(manifest_store.engine)
    context = population.workspace.context()
    checkpoint = await workspace_checkpoint(manifest_store.engine, context.workspace_id)

    run = await manifest_store.start(context, generation=7)

    assert run.state is ManifestRunState.COLLECTING
    assert run.base_acknowledged_sequence == 0
    assert run.checkpoint_sequence == checkpoint
    assert run.policy_revision_number == population.policy_revision_number
    assert run.client_observation_generation == 7
    assert run.next_page_number == 0
    assert run.entry_count == 0


@pytest.mark.asyncio
async def test_start_requires_an_active_published_policy(
    manifest_store: ManifestStoreHarness,
) -> None:
    workspace = await seed_device_sync_workspace(manifest_store.engine)
    with pytest.raises(DeviceSyncError) as raised:
        await manifest_store.start(workspace.context())
    assert raised.value.code is DeviceSyncErrorCode.MANIFEST_POLICY_ADVANCED

    await publish_workspace_policy(manifest_store.engine, workspace)
    run = await manifest_store.start(workspace.context())
    assert run.state is ManifestRunState.COLLECTING


@pytest.mark.asyncio
async def test_start_exactly_resumes_the_one_unfinished_run(
    manifest_store: ManifestStoreHarness,
) -> None:
    population = await seed_reconciliation_population(manifest_store.engine)
    context = population.workspace.context()
    first = await manifest_store.start(context, generation=5)
    entry = manifest_entry("entry-1", locator=UPLOAD_LOCATOR, observed=unique_fingerprint("filler"))
    await manifest_store.append_page(
        context,
        first.manifest_run_id,
        page_number=0,
        entries=(entry,),
        page_digest=_digest("resume-page"),
    )

    resumed = await manifest_store.start(context, generation=5)
    assert resumed.manifest_run_id == first.manifest_run_id
    assert resumed.next_page_number == 1
    assert resumed.entry_count == 1

    # A different observation generation means the client minted a newer
    # barrier and abandoned this run's local progress (explicit repair after
    # an interrupted run, or a rebuilt journal): the stale unfinished run is
    # superseded — expired with its evidence retained — and a fresh run
    # starts under the new generation instead of dead-locking the device
    # until the one-hour database deadline.
    superseded = await manifest_store.start(context, generation=6)
    assert superseded.manifest_run_id != first.manifest_run_id
    assert superseded.state is ManifestRunState.COLLECTING
    assert superseded.client_observation_generation == 6
    again = await manifest_store.start(context, generation=6)
    assert again.manifest_run_id == superseded.manifest_run_id


@pytest.mark.asyncio
async def test_concurrent_starts_share_the_single_unfinished_run(
    manifest_store: ManifestStoreHarness,
) -> None:
    population = await seed_reconciliation_population(manifest_store.engine)
    context = population.workspace.context()
    first, second = await asyncio.gather(
        manifest_store.start(context),
        manifest_store.start(context),
    )
    assert first.manifest_run_id == second.manifest_run_id
    assert first.state is ManifestRunState.COLLECTING


# --- ordered pages, exact replay and the run caps ------------------------------------


@pytest.mark.asyncio
async def test_pages_are_ordered_from_zero_with_exact_replay(
    manifest_store: ManifestStoreHarness,
) -> None:
    population = await seed_reconciliation_population(manifest_store.engine)
    context = population.workspace.context()
    run = await manifest_store.start(context)
    entries = tuple(
        manifest_entry(
            f"entry-{index}",
            locator=f"notes/ordered-{index}.md",
            observed=unique_fingerprint("filler"),
        )
        for index in range(3)
    )

    with pytest.raises(DeviceSyncError) as raised:
        await manifest_store.append_page(
            context,
            run.manifest_run_id,
            page_number=1,
            entries=entries[:1],
            page_digest=_digest("ordered-page-1"),
        )
    assert raised.value.code is DeviceSyncErrorCode.MANIFEST_PAGE_INVALID

    first = await manifest_store.append_page(
        context,
        run.manifest_run_id,
        page_number=0,
        entries=entries,
        page_digest=_digest("ordered-page-0"),
    )
    assert first.page_number == 0
    assert first.accepted_entry_count == 3
    assert first.next_page_number == 1

    replayed = await manifest_store.append_page(
        context,
        run.manifest_run_id,
        page_number=0,
        entries=entries,
        page_digest=_digest("ordered-page-0"),
    )
    assert replayed == first
    row = await manifest_store.run_row(run.manifest_run_id)
    assert row.entry_count == 3

    second = await manifest_store.append_page(
        context,
        run.manifest_run_id,
        page_number=1,
        entries=(),
        page_digest=_digest("ordered-page-1"),
    )
    assert second.next_page_number == 2


@pytest.mark.asyncio
async def test_same_page_number_with_different_digest_fails_run(
    manifest_store: ManifestStoreHarness,
) -> None:
    population = await seed_reconciliation_population(manifest_store.engine)
    context = population.workspace.context()
    run = await manifest_store.start(context)
    filler = unique_fingerprint("filler")
    entries = (manifest_entry("entry-0", locator=UPLOAD_LOCATOR, observed=filler),)
    page_zero = AppendManifestPageCommand(
        context=context,
        manifest_run_id=run.manifest_run_id,
        page_number=0,
        entries=entries,
        page_digest=_digest("planner-scenario-page-0"),
        diagnostic_context=_diagnostic(),
    )
    await manifest_store.store.append_manifest_page(page_zero)
    with pytest.raises(DeviceSyncError) as raised:
        await manifest_store.store.append_manifest_page(
            AppendManifestPageCommand(
                context=context,
                manifest_run_id=run.manifest_run_id,
                page_number=0,
                entries=entries,
                page_digest=_digest("other-page-digest"),
                diagnostic_context=_diagnostic(),
            )
        )
    assert raised.value.code is DeviceSyncErrorCode.MANIFEST_PAGE_REPLAY_MISMATCH
    row = await manifest_store.run_row(run.manifest_run_id)
    assert row.state == "failed"
    assert row.safe_error_code == DeviceSyncErrorCode.MANIFEST_PAGE_REPLAY_MISMATCH.value


@pytest.mark.asyncio
async def test_same_page_number_with_different_count_fails_run(
    manifest_store: ManifestStoreHarness,
) -> None:
    population = await seed_reconciliation_population(manifest_store.engine)
    context = population.workspace.context()
    run = await manifest_store.start(context)
    filler = unique_fingerprint("filler")
    filler_two = unique_fingerprint("filler-two")
    await manifest_store.append_page(
        context,
        run.manifest_run_id,
        page_number=0,
        entries=(manifest_entry("entry-0", locator=UPLOAD_LOCATOR, observed=filler),),
        page_digest=_digest("count-page"),
    )
    with pytest.raises(DeviceSyncError) as raised:
        await manifest_store.append_page(
            context,
            run.manifest_run_id,
            page_number=0,
            entries=(
                manifest_entry("entry-0", locator=UPLOAD_LOCATOR, observed=filler),
                manifest_entry("entry-1", locator=ABSENT_LOCATOR, observed=filler_two),
            ),
            page_digest=_digest("count-page"),
        )
    assert raised.value.code is DeviceSyncErrorCode.MANIFEST_PAGE_REPLAY_MISMATCH


@pytest.mark.asyncio
async def test_cumulative_run_entry_cap_is_enforced(
    manifest_store: ManifestStoreHarness,
) -> None:
    population = await seed_reconciliation_population(manifest_store.engine)
    context = population.workspace.context()
    run = await manifest_store.start(context)
    full_page = tuple(
        manifest_entry(
            f"entry-{index}",
            locator=f"notes/cap-{index}.md",
            observed=unique_fingerprint("filler"),
        )
        for index in range(MAX_MANIFEST_PAGE_ENTRIES)
    )
    await manifest_store.append_page(
        context, run.manifest_run_id, page_number=0, entries=full_page, page_digest=_digest("cap-0")
    )
    await manifest_store.set_entry_count(run.manifest_run_id, MAX_MANIFEST_RUN_ENTRIES)
    with pytest.raises(DeviceSyncError) as raised:
        await manifest_store.append_page(
            context,
            run.manifest_run_id,
            page_number=1,
            entries=(
                manifest_entry(
                    "entry-over", locator=UPLOAD_LOCATOR, observed=unique_fingerprint("filler")
                ),
            ),
            page_digest=_digest("cap-1"),
        )
    assert raised.value.code is DeviceSyncErrorCode.MANIFEST_PAGE_INVALID
    row = await manifest_store.run_row(run.manifest_run_id)
    assert row.state == "collecting"


@pytest.mark.asyncio
async def test_pages_after_finalize_are_state_invalid(
    manifest_store: ManifestStoreHarness,
) -> None:
    population = await seed_reconciliation_population(manifest_store.engine)
    context = population.workspace.context()
    run = await manifest_store.start(context)
    filler = unique_fingerprint("filler")
    entries = (manifest_entry("entry-0", locator=UPLOAD_LOCATOR, observed=filler),)
    page_digest = _digest("state-page")
    await manifest_store.append_page(
        context, run.manifest_run_id, page_number=0, entries=entries, page_digest=page_digest
    )
    await manifest_store.finalize(
        context,
        run.manifest_run_id,
        total_entry_count=1,
        final_digest=ContentDigest.parse(
            compute_manifest_final_digest(((0, 1, page_digest.hexadecimal),))
        ),
    )
    with pytest.raises(DeviceSyncError) as raised:
        await manifest_store.append_page(
            context, run.manifest_run_id, page_number=1, entries=(), page_digest=_digest("late")
        )
    assert raised.value.code is DeviceSyncErrorCode.MANIFEST_STATE_INVALID


@pytest.mark.asyncio
async def test_concurrent_identical_page_appends_count_once(
    manifest_store: ManifestStoreHarness,
) -> None:
    population = await seed_reconciliation_population(manifest_store.engine)
    context = population.workspace.context()
    run = await manifest_store.start(context)
    filler = unique_fingerprint("filler")
    entries = (manifest_entry("entry-0", locator=UPLOAD_LOCATOR, observed=filler),)
    race_page = _digest("race")
    first, second = await asyncio.gather(
        manifest_store.append_page(
            context, run.manifest_run_id, page_number=0, entries=entries, page_digest=race_page
        ),
        manifest_store.append_page(
            context, run.manifest_run_id, page_number=0, entries=entries, page_digest=race_page
        ),
    )
    assert first == second
    row = await manifest_store.run_row(run.manifest_run_id)
    assert row.entry_count == 1
    assert row.next_page_number == 1


# --- finalization, the deterministic plan and immutable pagination ---------------------


@pytest.mark.asyncio
async def test_finalize_verifies_count_and_canonical_final_digest(
    manifest_store: ManifestStoreHarness,
) -> None:
    population = await seed_reconciliation_population(manifest_store.engine)
    context = population.workspace.context()
    run = await manifest_store.start(context)
    filler = unique_fingerprint("filler")
    entries = (manifest_entry("entry-0", locator=UPLOAD_LOCATOR, observed=filler),)
    page_digest = _digest("finalize-page")
    await manifest_store.append_page(
        context, run.manifest_run_id, page_number=0, entries=entries, page_digest=page_digest
    )
    final_digest = ContentDigest.parse(
        compute_manifest_final_digest(((0, 1, page_digest.hexadecimal),))
    )

    with pytest.raises(DeviceSyncError) as raised:
        await manifest_store.finalize(
            context, run.manifest_run_id, total_entry_count=2, final_digest=final_digest
        )
    assert raised.value.code is DeviceSyncErrorCode.MANIFEST_DIGEST_MISMATCH
    assert (await manifest_store.run_row(run.manifest_run_id)).state == "failed"

    run = await manifest_store.start(context, generation=4)
    await manifest_store.append_page(
        context, run.manifest_run_id, page_number=0, entries=entries, page_digest=page_digest
    )
    with pytest.raises(DeviceSyncError) as raised:
        await manifest_store.finalize(
            context,
            run.manifest_run_id,
            total_entry_count=1,
            final_digest=_digest("wrong-final-digest"),
        )
    assert raised.value.code is DeviceSyncErrorCode.MANIFEST_DIGEST_MISMATCH

    run = await manifest_store.start(context, generation=5)
    await manifest_store.append_page(
        context, run.manifest_run_id, page_number=0, entries=entries, page_digest=page_digest
    )
    planned = await manifest_store.finalize(
        context, run.manifest_run_id, total_entry_count=1, final_digest=final_digest
    )
    assert planned.state is ManifestRunState.PLANNED
    replayed = await manifest_store.finalize(
        context, run.manifest_run_id, total_entry_count=1, final_digest=final_digest
    )
    assert replayed.state is ManifestRunState.PLANNED


@pytest.mark.asyncio
async def test_finalize_materializes_the_deterministic_action_plan(
    manifest_store: ManifestStoreHarness,
) -> None:
    population = await seed_reconciliation_population(manifest_store.engine)
    context = population.workspace.context()
    run, _final_digest = await manifest_store.drive_planned_run(population)

    resolutions = await manifest_store.resolution_rows(run.manifest_run_id)
    assert [row.local_entry_id for row in resolutions] == [
        "entry-match",
        "entry-stale",
        "entry-diverged",
        "entry-gone",
        "entry-upload",
        "entry-excluded",
        "entry-foreign",
    ]
    assert resolutions[0].match_kind == "current_locator"
    assert resolutions[0].resolved_source_id == population.match_source.source_id
    assert resolutions[3].match_kind == "open_tombstone_fingerprint"
    assert resolutions[4].match_kind == "unproven"
    assert resolutions[6].match_kind == "unproven"
    for row in resolutions:
        assert len(row.locator_evidence_digest) == 64

    page = await manifest_store.read_actions(context, run.manifest_run_id, limit=200)
    kinds = [action.action_kind for action in page.actions]
    assert kinds == [
        ManifestActionKind.NO_CHANGE,
        ManifestActionKind.DOWNLOAD,
        ManifestActionKind.CONFLICT,
        ManifestActionKind.APPLY_TOMBSTONE,
        ManifestActionKind.UPLOAD,
        ManifestActionKind.EXCLUDED,
        ManifestActionKind.CONFLICT,
        ManifestActionKind.DOWNLOAD,
    ]
    assert page.has_more is False

    no_change, stale_download, diverged, tombstone, upload, excluded, foreign, absent_download = (
        page.actions
    )
    assert no_change.local_entry_id == "entry-match"
    assert no_change.source_id == population.match_source.source_id
    assert no_change.source_version_id == population.match_source.version_ids[0]
    assert no_change.reason is None
    # Only download actions hydrate the checkpoint-active locator text at
    # read time (task 11b): the catch-up download echoes its manifest entry,
    # the canonical-only download carries no entry, and both name the locator
    # row open at the run checkpoint.
    assert no_change.checkpoint_locator is None
    assert stale_download.local_entry_id == "entry-stale"
    assert stale_download.source_id == population.stale_source.source_id
    assert stale_download.source_version_id == population.stale_source.version_ids[1]
    assert stale_download.checkpoint_locator == NormalizedLocator(STALE_LOCATOR)
    assert diverged.reason is not None
    assert diverged.reason.value == "device_manifest_local_diverged"
    assert tombstone.source_tombstone_id == population.deleted_source.tombstone_id
    assert upload.source_id is None
    assert excluded.local_entry_id == "entry-excluded"
    assert excluded.reason is not None
    assert excluded.reason.value == "device_manifest_policy_excluded"
    assert foreign.local_entry_id == "entry-foreign"
    assert foreign.reason is not None
    assert foreign.reason.value == "device_manifest_identity_ambiguous"
    assert absent_download.source_id == population.absent_source.source_id
    assert absent_download.local_entry_id is None
    assert absent_download.checkpoint_locator == NormalizedLocator(ABSENT_LOCATOR)
    # The policy-forbidden canonical source absent locally plans no action.
    assert all(action.source_id != population.forbidden_source.source_id for action in page.actions)


@pytest.mark.asyncio
async def test_remote_rename_journey_binds_historically_and_places_at_the_active_locator(
    manifest_store: ManifestStoreHarness,
) -> None:
    """One live rule-2 journey: the entry carries the source's closed old
    locator plus the exact current fingerprint, so it proves through the
    historical bucket (the closed locator row resolves), the deleted
    neighbor's closed locator never binds historically (rule-3 tombstone
    only), and the planned no_change names the locator row open at the
    checkpoint — the operand the plugin places the file with — never the
    closed locator the entry matched."""

    workspace = await seed_device_sync_workspace(manifest_store.engine)
    await publish_workspace_policy(manifest_store.engine, workspace)
    context = workspace.context()
    renamed_fingerprint = unique_fingerprint("rename-journey-current")
    renamed_source = await seed_renamed_source(
        manifest_store.engine,
        workspace,
        old_locator_text=RENAMED_OLD_LOCATOR,
        new_locator_text=RENAMED_NEW_LOCATOR,
        fingerprint=renamed_fingerprint,
    )
    deleted_fingerprint = unique_fingerprint("rename-journey-deleted")
    deleted_source = await seed_canonical_source(
        manifest_store.engine,
        workspace,
        locator_text=RENAMED_JOURNEY_DELETED_LOCATOR,
        fingerprints=(deleted_fingerprint,),
        deleted=True,
    )

    run = await manifest_store.start(context)
    entries = (
        manifest_entry("entry-renamed", locator=RENAMED_OLD_LOCATOR, observed=renamed_fingerprint),
        manifest_entry(
            "entry-gone",
            locator=RENAMED_JOURNEY_DELETED_LOCATOR,
            observed=deleted_fingerprint,
        ),
    )
    page_digest = _digest("rename-journey-page-0")
    await manifest_store.append_page(
        context, run.manifest_run_id, page_number=0, entries=entries, page_digest=page_digest
    )
    final_digest = ContentDigest.parse(
        compute_manifest_final_digest(((0, len(entries), page_digest.hexadecimal),))
    )
    planned = await manifest_store.finalize(
        context,
        run.manifest_run_id,
        total_entry_count=len(entries),
        final_digest=final_digest,
    )
    assert planned.state is ManifestRunState.PLANNED

    resolutions = await manifest_store.resolution_rows(run.manifest_run_id)
    assert [row.local_entry_id for row in resolutions] == ["entry-renamed", "entry-gone"]
    # The closed old locator bucketed historically and resolved to its own
    # (historical) locator row id.
    assert resolutions[0].match_kind == "historical_locator_fingerprint"
    assert resolutions[0].resolved_source_id == renamed_source.source_id
    assert resolutions[0].resolved_source_locator_id == renamed_source.old_locator_id
    # The deleted source's closed locator is not a live authority: it never
    # becomes a historical candidate, so the tombstone proves instead.
    assert resolutions[1].match_kind == "open_tombstone_fingerprint"
    assert resolutions[1].resolved_source_id == deleted_source.source_id

    page = await manifest_store.read_actions(context, run.manifest_run_id, limit=200)
    assert [action.action_kind for action in page.actions] == [
        ManifestActionKind.NO_CHANGE,
        ManifestActionKind.APPLY_TOMBSTONE,
    ]
    no_change, tombstone = page.actions
    assert no_change.local_entry_id == "entry-renamed"
    assert no_change.source_id == renamed_source.source_id
    assert no_change.source_version_id == renamed_source.version_ids[0]
    assert no_change.source_locator_id == renamed_source.new_locator_id
    assert no_change.source_locator_id != renamed_source.old_locator_id
    assert tombstone.source_tombstone_id == deleted_source.tombstone_id
    # The renamed source is resolved, so no canonical-only duplicate
    # download joins the plan.
    assert page.has_more is False


@pytest.mark.asyncio
async def test_commit_between_start_and_finalize_stays_outside_the_plan_and_fence(
    manifest_store: ManifestStoreHarness,
) -> None:
    """One durable race pin: a canonical source committed strictly between a
    run's start and its finalize is invisible to the plan — the planner's
    canonical universe is the run checkpoint, so the fresh source earns no
    canonical-only download and the completion fence acknowledges exactly
    the checkpoint, leaving the new event sequences for the next pull."""

    population = await seed_reconciliation_population(manifest_store.engine)
    context = population.workspace.context()
    checkpoint = await workspace_checkpoint(manifest_store.engine, context.workspace_id)

    run = await manifest_store.start(context)
    assert run.checkpoint_sequence == checkpoint
    post_checkpoint_source = await seed_canonical_source(
        manifest_store.engine,
        population.workspace,
        locator_text="notes/committed-mid-run.md",
        fingerprints=(unique_fingerprint("mid-run"),),
    )

    entry = manifest_entry(
        "entry-match", locator=MATCH_LOCATOR, observed=population.match_fingerprint
    )
    page_digest = _digest("mid-run-page-0")
    await manifest_store.append_page(
        context, run.manifest_run_id, page_number=0, entries=(entry,), page_digest=page_digest
    )
    final_digest = ContentDigest.parse(
        compute_manifest_final_digest(((0, 1, page_digest.hexadecimal),))
    )
    planned = await manifest_store.finalize(
        context, run.manifest_run_id, total_entry_count=1, final_digest=final_digest
    )
    assert planned.state is ManifestRunState.PLANNED

    page = await manifest_store.read_actions(context, run.manifest_run_id, limit=200)
    kinds = [action.action_kind for action in page.actions]
    # The one manifest entry resolves to exactly one no_change action. Every
    # other planned action is a canonical-only catch-up download (task 12's
    # action-wire convergence) for a live PRE-checkpoint source the single
    # entry did not represent: stale, diverged and absent. The tombstoned
    # delete and the policy-forbidden source earn nothing.
    assert kinds.count(ManifestActionKind.NO_CHANGE) == 1
    assert set(kinds) <= {ManifestActionKind.NO_CHANGE, ManifestActionKind.DOWNLOAD}
    no_change = next(
        action for action in page.actions if action.action_kind is ManifestActionKind.NO_CHANGE
    )
    assert no_change.source_id == population.match_source.source_id
    downloads = [
        action for action in page.actions if action.action_kind is ManifestActionKind.DOWNLOAD
    ]
    assert {action.source_id for action in downloads} == {
        population.stale_source.source_id,
        population.diverged_source.source_id,
        population.absent_source.source_id,
    }
    assert all(action.local_entry_id is None for action in downloads)
    # The mid-run commit never joins the checkpoint-bounded plan: its source
    # is outside the run's canonical universe, so no canonical-only download
    # and no upload references it.
    assert all(action.source_id != post_checkpoint_source.source_id for action in page.actions)
    assert all(
        action.checkpoint_locator != NormalizedLocator("notes/committed-mid-run.md")
        for action in downloads
    )
    assert page.has_more is False

    receipt = await manifest_store.complete(context, run.manifest_run_id, final_digest=final_digest)
    assert receipt.acknowledged_sequence == checkpoint
    assert receipt.delivered_through_sequence < await workspace_checkpoint(
        manifest_store.engine, context.workspace_id
    )


@pytest.mark.asyncio
async def test_first_action_read_transitions_planned_to_applying_and_replays_stably(
    manifest_store: ManifestStoreHarness,
) -> None:
    population = await seed_reconciliation_population(manifest_store.engine)
    context = population.workspace.context()
    run, _ = await manifest_store.drive_planned_run(population)

    first = await manifest_store.read_actions(context, run.manifest_run_id, limit=3)
    assert len(first.actions) == 3
    assert first.has_more is True
    assert (await manifest_store.run_row(run.manifest_run_id)).state == "applying"

    rest = await manifest_store.read_actions(context, run.manifest_run_id, after=2, limit=200)
    assert [action.action_index for action in rest.actions] == [2, 3, 4, 5, 6, 7]
    assert rest.has_more is False
    replayed = await manifest_store.read_actions(context, run.manifest_run_id, limit=200)
    assert (
        replayed.actions
        == (await manifest_store.read_actions(context, run.manifest_run_id, limit=200)).actions
    )
    assert [action.action_index for action in replayed.actions] == list(range(8))
    assert (await manifest_store.run_row(run.manifest_run_id)).state == "applying"


@pytest.mark.asyncio
async def test_download_action_with_unhydratable_locator_fails_closed(
    manifest_store: ManifestStoreHarness,
) -> None:
    """A persisted download action whose ``source_locator_id`` resolves to no
    in-workspace locator row (a dangling or foreign reference) fails closed at
    the read boundary with the invalid-state code: a download action can never
    reach the wire without its placement operand (task 11b)."""

    population = await seed_reconciliation_population(manifest_store.engine)
    context = population.workspace.context()
    run, _ = await manifest_store.drive_planned_run(population)
    # Point the canonical-only download's locator reference at a row no
    # workspace owns — nothing rewrites planned actions through the store, so
    # only the read-time hydration join observes the unresolvable operand.
    async with manifest_store.engine.begin() as connection:
        updated = await connection.execute(
            sa.update(manifest_actions)
            .values(source_locator_id=uuid4())
            .where(
                manifest_actions.c.manifest_run_id == run.manifest_run_id,
                manifest_actions.c.local_entry_id.is_(None),
            )
        )
        assert updated.rowcount == 1
    with pytest.raises(DeviceSyncError) as raised:
        await manifest_store.read_actions(context, run.manifest_run_id)
    assert raised.value.code is DeviceSyncErrorCode.MANIFEST_STATE_INVALID


@pytest.mark.asyncio
async def test_action_read_on_collecting_run_is_state_invalid(
    manifest_store: ManifestStoreHarness,
) -> None:
    population = await seed_reconciliation_population(manifest_store.engine)
    context = population.workspace.context()
    run = await manifest_store.start(context)
    with pytest.raises(DeviceSyncError) as raised:
        await manifest_store.read_actions(context, run.manifest_run_id)
    assert raised.value.code is DeviceSyncErrorCode.MANIFEST_STATE_INVALID


# --- policy advance and database expiry ----------------------------------------------


@pytest.mark.asyncio
async def test_policy_stale_rejection_precedes_the_collecting_state_rejection(
    manifest_store: ManifestStoreHarness,
) -> None:
    """An action read of a policy-stale collecting run reports the policy
    advance (and fails the run with that closed reason), not the plain
    collecting-state rejection: the durable invalidation outranks the state
    shape in the read path."""

    population = await seed_reconciliation_population(manifest_store.engine)
    context = population.workspace.context()
    run = await manifest_store.start(context)
    await manifest_store.advance_policy(population.workspace)

    with pytest.raises(DeviceSyncError) as raised:
        await manifest_store.read_actions(context, run.manifest_run_id)
    assert raised.value.code is DeviceSyncErrorCode.MANIFEST_POLICY_ADVANCED
    row = await manifest_store.run_row(run.manifest_run_id)
    assert row.state == "failed"
    assert row.safe_error_code == DeviceSyncErrorCode.MANIFEST_POLICY_ADVANCED.value


@pytest.mark.asyncio
async def test_policy_advance_fails_the_run_on_first_read_and_completion(
    manifest_store: ManifestStoreHarness,
) -> None:
    population = await seed_reconciliation_population(manifest_store.engine)
    context = population.workspace.context()
    run, final_digest = await manifest_store.drive_planned_run(population)

    await manifest_store.advance_policy(population.workspace)

    with pytest.raises(DeviceSyncError) as raised:
        await manifest_store.read_actions(context, run.manifest_run_id)
    assert raised.value.code is DeviceSyncErrorCode.MANIFEST_POLICY_ADVANCED
    row = await manifest_store.run_row(run.manifest_run_id)
    assert row.state == "failed"
    assert row.safe_error_code == DeviceSyncErrorCode.MANIFEST_POLICY_ADVANCED.value

    with pytest.raises(DeviceSyncError) as complete_raised:
        await manifest_store.complete(context, run.manifest_run_id, final_digest=final_digest)
    assert complete_raised.value.code is DeviceSyncErrorCode.MANIFEST_STATE_INVALID
    assert await manifest_store.cursor_row(population.workspace) is None


@pytest.mark.asyncio
async def test_expired_run_rejects_pages_and_completion_and_frees_a_new_run(
    manifest_store: ManifestStoreHarness,
) -> None:
    population = await seed_reconciliation_population(manifest_store.engine)
    context = population.workspace.context()
    run, final_digest = await manifest_store.drive_planned_run(population)
    await manifest_store.read_actions(context, run.manifest_run_id, limit=1)
    await manifest_store.expire_run(run.manifest_run_id)

    with pytest.raises(DeviceSyncError) as raised:
        await manifest_store.append_page(
            context, run.manifest_run_id, page_number=1, entries=(), page_digest=_digest("late")
        )
    assert raised.value.code is DeviceSyncErrorCode.MANIFEST_EXPIRED
    assert (await manifest_store.run_row(run.manifest_run_id)).state == "expired"

    with pytest.raises(DeviceSyncError) as complete_raised:
        await manifest_store.complete(context, run.manifest_run_id, final_digest=final_digest)
    assert complete_raised.value.code is DeviceSyncErrorCode.MANIFEST_EXPIRED
    assert await manifest_store.cursor_row(population.workspace) is None

    fresh = await manifest_store.start(context, generation=9)
    assert fresh.manifest_run_id != run.manifest_run_id
    assert fresh.state is ManifestRunState.COLLECTING


# --- the completion fence and the cursor ----------------------------------------------


@pytest.mark.asyncio
async def test_completion_advances_the_cursor_to_the_checkpoint_without_a_watermark(
    manifest_store: ManifestStoreHarness,
) -> None:
    population = await seed_reconciliation_population(manifest_store.engine)
    context = population.workspace.context()
    run, final_digest = await manifest_store.drive_planned_run(population)
    checkpoint = await workspace_checkpoint(manifest_store.engine, context.workspace_id)
    assert checkpoint > 0
    assert await manifest_store.cursor_row(population.workspace) is None

    await manifest_store.read_actions(context, run.manifest_run_id, limit=1)
    receipt = await manifest_store.complete(context, run.manifest_run_id, final_digest=final_digest)

    assert receipt.acknowledged_sequence == checkpoint
    assert receipt.delivered_through_sequence >= checkpoint
    assert await manifest_store.cursor_row(population.workspace) == (
        receipt.acknowledged_sequence,
        receipt.delivered_through_sequence,
    )
    assert (await manifest_store.run_row(run.manifest_run_id)).state == "completed"


@pytest.mark.asyncio
async def test_completion_requires_the_applying_state_and_exact_digest(
    manifest_store: ManifestStoreHarness,
) -> None:
    population = await seed_reconciliation_population(manifest_store.engine)
    context = population.workspace.context()
    run, final_digest = await manifest_store.drive_planned_run(population)

    with pytest.raises(DeviceSyncError) as raised:
        await manifest_store.complete(context, run.manifest_run_id, final_digest=final_digest)
    assert raised.value.code is DeviceSyncErrorCode.MANIFEST_STATE_INVALID
    assert await manifest_store.cursor_row(population.workspace) is None

    await manifest_store.read_actions(context, run.manifest_run_id, limit=1)
    with pytest.raises(DeviceSyncError) as digest_raised:
        await manifest_store.complete(
            context, run.manifest_run_id, final_digest=_digest("wrong-completion-digest")
        )
    assert digest_raised.value.code is DeviceSyncErrorCode.MANIFEST_DIGEST_MISMATCH
    assert await manifest_store.cursor_row(population.workspace) is None


@pytest.mark.asyncio
async def test_lost_completion_response_exact_replay_returns_the_same_cursor(
    manifest_store: ManifestStoreHarness,
) -> None:
    population = await seed_reconciliation_population(manifest_store.engine)
    context = population.workspace.context()
    run, final_digest = await manifest_store.drive_planned_run(population)
    await manifest_store.read_actions(context, run.manifest_run_id, limit=1)

    first = await manifest_store.complete(context, run.manifest_run_id, final_digest=final_digest)
    replayed = await manifest_store.complete(
        context, run.manifest_run_id, final_digest=final_digest
    )
    assert replayed == first
    assert await manifest_store.cursor_row(population.workspace) == (
        first.acknowledged_sequence,
        first.delivered_through_sequence,
    )

    with pytest.raises(DeviceSyncError) as raised:
        await manifest_store.complete(
            context, run.manifest_run_id, final_digest=_digest("replayed-wrong-digest")
        )
    assert raised.value.code is DeviceSyncErrorCode.MANIFEST_DIGEST_MISMATCH


@pytest.mark.asyncio
async def test_foreign_run_grants_no_pages_actions_or_advance(
    manifest_store: ManifestStoreHarness,
) -> None:
    population = await seed_reconciliation_population(manifest_store.engine)
    run, final_digest = await manifest_store.drive_planned_run(population)

    foreign_workspace = await seed_device_sync_workspace(manifest_store.engine)
    await publish_workspace_policy(manifest_store.engine, foreign_workspace)
    foreign_context = foreign_workspace.context()

    with pytest.raises(DeviceSyncError) as page_raised:
        await manifest_store.append_page(
            foreign_context,
            run.manifest_run_id,
            page_number=1,
            entries=(),
            page_digest=_digest("foreign"),
        )
    assert page_raised.value.code is DeviceSyncErrorCode.MANIFEST_NOT_FOUND
    with pytest.raises(DeviceSyncError) as actions_raised:
        await manifest_store.read_actions(foreign_context, run.manifest_run_id)
    assert actions_raised.value.code is DeviceSyncErrorCode.MANIFEST_NOT_FOUND
    with pytest.raises(DeviceSyncError) as complete_raised:
        await manifest_store.complete(
            foreign_context, run.manifest_run_id, final_digest=final_digest
        )
    assert complete_raised.value.code is DeviceSyncErrorCode.MANIFEST_NOT_FOUND
    assert await manifest_store.cursor_row(foreign_workspace) is None
    assert await manifest_store.cursor_row(population.workspace) is None


@pytest.mark.asyncio
async def test_concurrent_completions_advance_the_cursor_exactly_once(
    manifest_store: ManifestStoreHarness,
) -> None:
    population = await seed_reconciliation_population(manifest_store.engine)
    context = population.workspace.context()
    run, final_digest = await manifest_store.drive_planned_run(population)
    await manifest_store.read_actions(context, run.manifest_run_id, limit=1)

    first, second = await asyncio.gather(
        manifest_store.complete(context, run.manifest_run_id, final_digest=final_digest),
        manifest_store.complete(context, run.manifest_run_id, final_digest=final_digest),
    )
    assert first == second
    acknowledged, delivered = await manifest_store.cursor_row(population.workspace)
    assert acknowledged == first.acknowledged_sequence
    assert delivered == first.delivered_through_sequence


@pytest.mark.asyncio
async def test_concurrent_conflicting_completions_grant_one_advance(
    manifest_store: ManifestStoreHarness,
) -> None:
    population = await seed_reconciliation_population(manifest_store.engine)
    context = population.workspace.context()
    run, final_digest = await manifest_store.drive_planned_run(population)
    await manifest_store.read_actions(context, run.manifest_run_id, limit=1)

    outcomes = await asyncio.gather(
        manifest_store.complete(context, run.manifest_run_id, final_digest=final_digest),
        manifest_store.complete(
            context, run.manifest_run_id, final_digest=_digest("conflicting-digest")
        ),
        return_exceptions=True,
    )
    receipts = [outcome for outcome in outcomes if not isinstance(outcome, BaseException)]
    rejections = [outcome for outcome in outcomes if isinstance(outcome, BaseException)]
    assert len(receipts) == 1
    assert len(rejections) == 1
    assert isinstance(rejections[0], DeviceSyncError)
    assert rejections[0].code is DeviceSyncErrorCode.MANIFEST_DIGEST_MISMATCH
    acknowledged, delivered = await manifest_store.cursor_row(population.workspace)
    assert acknowledged == receipts[0].acknowledged_sequence
    assert delivered == receipts[0].delivered_through_sequence
