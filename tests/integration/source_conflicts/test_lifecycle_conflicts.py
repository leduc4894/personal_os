"""Lifecycle races routed into the shared conflict service (Child 8 Task 5).

The service-routed races prove the two durable capture receipts the brief
demands: a delete that lost to a remote edit becomes a byteless
``delete_remote_edit`` conflict, and a rename onto a locator another active
source holds becomes a ``locator_collision`` conflict that preserves the
locator snapshot without rebinding any locator or current pointer. Both
captures are deterministic: a same-identity redelivery replays the stored
conflict instead of duplicating it, and the losing lifecycle command still
surfaces its original typed rejection so the device retry contract is
unchanged.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4, uuid7

import pytest
import pytest_asyncio
import sqlalchemy as sa
from api_runtime.exclusion_policy_crypto import TrustAnchorEd25519Verifier
from tests.integration.source_conflicts.conftest import (
    ConflictStoreHarness,
    SeededWorkspace,
)
from tools.signed_policy_seed import seed_signed_policy

from personal_os.diagnostics.context import DiagnosticContext, create_diagnostic_context
from personal_os.exclusion_policy.contracts import PolicySubject
from personal_os.source_conflicts import InMemorySourceConflictMetrics, SourceConflictService
from personal_os.source_conflicts.contracts import ConflictKind, ConflictStatus
from personal_os.source_lifecycle.commands import LifecycleOperation, SourceLifecycleCommand
from personal_os.source_lifecycle.errors import SourceLifecycleError, SourceLifecycleErrorCode
from personal_os.source_lifecycle.metrics import InMemorySourceLifecycleMetrics
from personal_os.source_lifecycle.ports import (
    LifecycleDeviceContext,
    LifecyclePolicyDecision,
    LifecyclePolicyOutcome,
)
from personal_os.source_lifecycle.service import SourceLifecycleService
from personal_os.source_locators import NormalizedLocator
from postgresql_source_store.lifecycle_store import PostgresqlSourceLifecycleStore
from postgresql_source_store.policy_enforcement import compose_policy_enforcement
from postgresql_source_store.tables import (
    content_objects,
    source_conflicts,
    source_locators,
    source_tombstones,
    source_versions,
    sources,
    sync_events,
    workspace_policy_state,
)

pytestmark = pytest.mark.local_stack


def _diagnostic_context() -> DiagnosticContext:
    return create_diagnostic_context().context


class _AllowingLifecyclePolicy:
    """Service-level policy double returning one closed ALLOWED verdict.

    The store's own transaction re-evaluates the seeded signed policy under
    the lock; this double only feeds the service's pre-transaction decision
    the store never trusts.
    """

    async def evaluate_lifecycle(
        self,
        command: SourceLifecycleCommand,
        device_context: LifecycleDeviceContext,
    ) -> LifecyclePolicyDecision:
        locator = command.target_locator or command.expected_locator
        return LifecyclePolicyDecision(
            workspace_id=device_context.workspace_id,
            outcome=LifecyclePolicyOutcome.ALLOWED,
            policy_revision_number=1,
            subject=PolicySubject(
                workspace_id=device_context.workspace_id,
                source_id=command.source_id,
                normalized_locator=locator.value if locator is not None else None,
            ),
            expected_locator=command.expected_locator,
            target_locator=command.target_locator,
        )


@dataclass(frozen=True, slots=True)
class SeededLifecycleSource:
    """One canonical active source with a locator and a current version."""

    source_id: UUID
    current_version_id: UUID
    locator: NormalizedLocator


class LifecycleConflictHarness:
    """Compose the real lifecycle service with the shared conflict capture."""

    def __init__(
        self,
        store_harness: ConflictStoreHarness,
    ) -> None:
        from api_runtime.small_file_sync_composition import (
            PolicyEnforcementConflictCaptureGuard,
        )
        from api_runtime.source_lifecycle_composition import (
            PostgresqlLifecycleConflictCaptureGateway,
        )

        self._engine = store_harness.engine
        self._store_harness = store_harness
        self._conflict_store = store_harness.store
        verifier = TrustAnchorEd25519Verifier()
        enforcement = compose_policy_enforcement(self._engine, verifier=verifier)
        conflict_service = SourceConflictService(
            store=self._conflict_store,
            policy_guard=PolicyEnforcementConflictCaptureGuard(enforcement=enforcement),
            metrics=InMemorySourceConflictMetrics(),
        )
        self.service = SourceLifecycleService(
            store=PostgresqlSourceLifecycleStore(self._engine, policy_verifier=verifier),
            policy=_AllowingLifecyclePolicy(),
            conflict_capture=PostgresqlLifecycleConflictCaptureGateway(
                engine=self._engine,
                conflict_service=conflict_service,
            ),
            metrics=InMemorySourceLifecycleMetrics(),
        )

    async def seed_workspace(self) -> SeededWorkspace:
        workspace = await self._store_harness.seed_workspace()
        async with self._engine.begin() as connection:
            await connection.execute(
                sa.insert(workspace_policy_state).values(
                    workspace_id=workspace.workspace_id,
                    active_policy_revision_id=None,
                    active_revision_number=0,
                )
            )
        await seed_signed_policy(
            self._engine,
            workspace_id=workspace.workspace_id,
            published_by_user_id=workspace.owner_user_id,
        )
        return workspace

    async def seed_active_source_with_locator(
        self,
        *,
        workspace: SeededWorkspace,
        source_id: UUID,
        locator: NormalizedLocator,
    ) -> SeededLifecycleSource:
        content_object_id = uuid4()
        source_version_id = uuid4()
        event_id = uuid4()
        digest = hashlib.sha256(str(source_id).encode("ascii")).hexdigest()
        async with self._engine.begin() as connection:
            await connection.execute(
                sa.insert(content_objects).values(
                    content_object_id=content_object_id,
                    content_hash=digest,
                    object_key=f"objects/sha256/{digest[:2]}/{digest[2:4]}/{digest}",
                    byte_size=1,
                    media_type="text/markdown",
                    verified_at=sa.text("CURRENT_TIMESTAMP - interval '1 second'"),
                )
            )
            await connection.execute(
                sa.insert(sources).values(
                    source_id=source_id,
                    workspace_id=workspace.workspace_id,
                    source_type="markdown",
                    title="Lifecycle race source",
                )
            )
            await connection.execute(
                sa.insert(source_versions).values(
                    source_version_id=source_version_id,
                    workspace_id=workspace.workspace_id,
                    source_id=source_id,
                    content_object_id=content_object_id,
                    content_version=1,
                    author_kind="user",
                    author_id=workspace.owner_user_id,
                )
            )
            await connection.execute(
                sa.update(sources)
                .where(sources.c.source_id == source_id)
                .values(sync_state="active", current_version_id=source_version_id)
            )
            event_result = await connection.execute(
                sa.insert(sync_events)
                .values(
                    event_id=event_id,
                    workspace_id=workspace.workspace_id,
                    source_id=source_id,
                    device_id=workspace.device_id,
                    committed_version_id=source_version_id,
                    idempotency_key=str(uuid4()),
                    request_fingerprint=hashlib.sha256(event_id.bytes).hexdigest(),
                    event_type="create",
                )
                .returning(sync_events.c.event_sequence)
            )
            opened_sequence = int(event_result.scalar_one())
            await connection.execute(
                sa.insert(source_locators).values(
                    source_locator_id=uuid4(),
                    workspace_id=workspace.workspace_id,
                    source_id=source_id,
                    normalized_locator=locator.value,
                    display_locator=locator.value,
                    opened_event_id=event_id,
                    opened_sequence=opened_sequence,
                )
            )
        return SeededLifecycleSource(
            source_id=source_id,
            current_version_id=source_version_id,
            locator=locator,
        )

    async def advance_current_version(
        self,
        *,
        workspace: SeededWorkspace,
        seeded: SeededLifecycleSource,
    ) -> UUID:
        """Model the remote edit: a second committed version becomes current."""
        content_object_id = uuid4()
        next_version_id = uuid4()
        digest = hashlib.sha256(f"remote-edit-{next_version_id}".encode("ascii")).hexdigest()
        async with self._engine.begin() as connection:
            await connection.execute(
                sa.insert(content_objects).values(
                    content_object_id=content_object_id,
                    content_hash=digest,
                    object_key=f"objects/sha256/{digest[:2]}/{digest[2:4]}/{digest}",
                    byte_size=1,
                    media_type="text/markdown",
                    verified_at=sa.text("CURRENT_TIMESTAMP - interval '1 second'"),
                )
            )
            await connection.execute(
                sa.insert(source_versions).values(
                    source_version_id=next_version_id,
                    workspace_id=workspace.workspace_id,
                    source_id=seeded.source_id,
                    content_object_id=content_object_id,
                    content_version=2,
                    parent_version_id=seeded.current_version_id,
                    author_kind="user",
                    author_id=workspace.owner_user_id,
                )
            )
            guarded = await connection.execute(
                sa.update(sources)
                .values(current_version_id=next_version_id)
                .where(
                    sources.c.source_id == seeded.source_id,
                    sources.c.current_version_id == seeded.current_version_id,
                )
            )
            assert guarded.rowcount == 1, "remote edit must move the guarded pointer"
        return next_version_id

    def device_context(self, workspace: SeededWorkspace) -> LifecycleDeviceContext:
        return LifecycleDeviceContext(
            workspace_id=workspace.workspace_id,
            device_id=workspace.device_id,
            user_id=workspace.owner_user_id,
            device_kind="obsidian",
        )

    def rename_command(
        self,
        *,
        seeded: SeededLifecycleSource,
        target: NormalizedLocator,
    ) -> SourceLifecycleCommand:
        return SourceLifecycleCommand(
            source_id=seeded.source_id,
            event_id=uuid7(),
            idempotency_key=f"lifecycle-rename-{uuid4()}",
            operation=LifecycleOperation.RENAME,
            expected_version_id=seeded.current_version_id,
            expected_locator=seeded.locator,
            target_locator=target,
            tombstone_id=None,
            policy_revision=1,
            client_timestamp=datetime(2026, 9, 2, 1, 2, 3, tzinfo=UTC),
        )

    def delete_command(
        self,
        *,
        seeded: SeededLifecycleSource,
        base_version_id: UUID,
    ) -> SourceLifecycleCommand:
        return SourceLifecycleCommand(
            source_id=seeded.source_id,
            event_id=uuid7(),
            idempotency_key=f"lifecycle-delete-{uuid4()}",
            operation=LifecycleOperation.DELETE,
            expected_version_id=base_version_id,
            expected_locator=seeded.locator,
            target_locator=None,
            tombstone_id=None,
            policy_revision=1,
            client_timestamp=datetime(2026, 9, 2, 1, 2, 4, tzinfo=UTC),
        )

    async def captured_conflict(
        self,
        *,
        workspace: SeededWorkspace,
        event_id: UUID,
    ):
        return await self._conflict_store.find_captured_conflict(
            event_id, workspace.workspace_id, _diagnostic_context()
        )

    async def conflict_locator_snapshot(self, conflict_id: UUID) -> str | None:
        async with self._engine.connect() as connection:
            result = await connection.execute(
                sa.select(source_conflicts.c.normalized_locator).where(
                    source_conflicts.c.conflict_id == conflict_id
                )
            )
            return result.scalar_one_or_none()

    async def source_row(self, source_id: UUID):
        async with self._engine.connect() as connection:
            result = await connection.execute(
                sa.select(
                    sources.c.sync_state,
                    sources.c.current_version_id,
                ).where(sources.c.source_id == source_id)
            )
            return result.one_or_none()

    async def open_locator_rows(self, source_id: UUID) -> list[str]:
        async with self._engine.connect() as connection:
            result = await connection.execute(
                sa.select(source_locators.c.normalized_locator)
                .where(
                    source_locators.c.source_id == source_id,
                    source_locators.c.closed_event_id.is_(None),
                )
                .order_by(source_locators.c.normalized_locator)
            )
            return [row.normalized_locator for row in result.all()]

    async def tombstone_count(self, source_id: UUID) -> int:
        async with self._engine.connect() as connection:
            result = await connection.execute(
                sa.select(sa.func.count())
                .select_from(source_tombstones)
                .where(source_tombstones.c.source_id == source_id)
            )
            return int(result.scalar_one())

    async def count_conflicts(self, workspace: SeededWorkspace) -> int:
        async with self._engine.connect() as connection:
            result = await connection.execute(
                sa.select(sa.func.count())
                .select_from(source_conflicts)
                .where(source_conflicts.c.workspace_id == workspace.workspace_id)
            )
            return int(result.scalar_one())


@pytest_asyncio.fixture
async def lifecycle_conflict_harness(
    conflict_harness: ConflictStoreHarness,
) -> LifecycleConflictHarness:
    return LifecycleConflictHarness(conflict_harness)


# --- delete versus remote edit -----------------------------------------------


@pytest.mark.asyncio
async def test_delete_against_remote_edit_creates_no_byte_conflict(
    lifecycle_conflict_harness: LifecycleConflictHarness,
) -> None:
    """The brief's delete-vs-remote-edit receipt: byteless evidence only."""

    workspace = await lifecycle_conflict_harness.seed_workspace()
    source_id = uuid4()
    seeded = await lifecycle_conflict_harness.seed_active_source_with_locator(
        workspace=workspace,
        source_id=source_id,
        locator=NormalizedLocator("notes/remote-edit-race.md"),
    )
    remote_version_id = await lifecycle_conflict_harness.advance_current_version(
        workspace=workspace, seeded=seeded
    )
    command = lifecycle_conflict_harness.delete_command(
        seeded=seeded, base_version_id=seeded.current_version_id
    )

    with pytest.raises(SourceLifecycleError) as failure:
        await lifecycle_conflict_harness.service.commit(
            command,
            lifecycle_conflict_harness.device_context(workspace),
            _diagnostic_context(),
        )
    assert failure.value.code is SourceLifecycleErrorCode.VERSION_CONFLICT

    receipt = await lifecycle_conflict_harness.captured_conflict(
        workspace=workspace, event_id=command.event_id
    )
    assert receipt is not None
    assert receipt.conflict_kind is ConflictKind.DELETE_REMOTE_EDIT
    assert receipt.candidate.verified_candidate_object_id is None
    assert receipt.status is ConflictStatus.OPEN
    assert receipt.base_version_id == seeded.current_version_id
    assert receipt.observed_remote_version_id == remote_version_id
    # No current-pointer mutation and no invented delete evidence.
    row = await lifecycle_conflict_harness.source_row(source_id)
    assert row is not None
    assert row.sync_state == "active"
    assert row.current_version_id == remote_version_id
    assert await lifecycle_conflict_harness.tombstone_count(source_id) == 0


@pytest.mark.asyncio
async def test_delete_race_capture_replays_deterministically(
    lifecycle_conflict_harness: LifecycleConflictHarness,
) -> None:
    """A same-identity redelivery replays the stored conflict, never a second row."""

    workspace = await lifecycle_conflict_harness.seed_workspace()
    seeded = await lifecycle_conflict_harness.seed_active_source_with_locator(
        workspace=workspace,
        source_id=uuid4(),
        locator=NormalizedLocator("notes/replay-race.md"),
    )
    await lifecycle_conflict_harness.advance_current_version(workspace=workspace, seeded=seeded)
    command = lifecycle_conflict_harness.delete_command(
        seeded=seeded, base_version_id=seeded.current_version_id
    )
    device_context = lifecycle_conflict_harness.device_context(workspace)

    with pytest.raises(SourceLifecycleError):
        await lifecycle_conflict_harness.service.commit(
            command, device_context, _diagnostic_context()
        )
    first = await lifecycle_conflict_harness.captured_conflict(
        workspace=workspace, event_id=command.event_id
    )
    assert first is not None

    with pytest.raises(SourceLifecycleError) as failure:
        await lifecycle_conflict_harness.service.commit(
            command, device_context, _diagnostic_context()
        )
    assert failure.value.code is SourceLifecycleErrorCode.VERSION_CONFLICT

    second = await lifecycle_conflict_harness.captured_conflict(
        workspace=workspace, event_id=command.event_id
    )
    assert second is not None
    assert second.conflict_id == first.conflict_id
    assert second.captured_at == first.captured_at
    assert await lifecycle_conflict_harness.count_conflicts(workspace) == 1


# --- concurrent rename onto one target locator --------------------------------


@pytest.mark.asyncio
async def test_locator_collision_preserves_locator_snapshot_without_rebinding(
    lifecycle_conflict_harness: LifecycleConflictHarness,
) -> None:
    """The brief's concurrent-rename receipt: the snapshot is retained and no
    locator, pointer or tombstone moves."""

    workspace = await lifecycle_conflict_harness.seed_workspace()
    winner = await lifecycle_conflict_harness.seed_active_source_with_locator(
        workspace=workspace,
        source_id=uuid4(),
        locator=NormalizedLocator("notes/collision-winner.md"),
    )
    loser = await lifecycle_conflict_harness.seed_active_source_with_locator(
        workspace=workspace,
        source_id=uuid4(),
        locator=NormalizedLocator("notes/collision-loser.md"),
    )
    contested = NormalizedLocator("notes/collision-contested.md")
    device_context = lifecycle_conflict_harness.device_context(workspace)
    winner_command = lifecycle_conflict_harness.rename_command(seeded=winner, target=contested)
    await lifecycle_conflict_harness.service.commit(
        winner_command, device_context, _diagnostic_context()
    )

    loser_command = lifecycle_conflict_harness.rename_command(seeded=loser, target=contested)
    with pytest.raises(SourceLifecycleError) as failure:
        await lifecycle_conflict_harness.service.commit(
            loser_command, device_context, _diagnostic_context()
        )
    assert failure.value.code is SourceLifecycleErrorCode.LOCATOR_CONFLICT

    receipt = await lifecycle_conflict_harness.captured_conflict(
        workspace=workspace, event_id=loser_command.event_id
    )
    assert receipt is not None
    assert receipt.conflict_kind is ConflictKind.LOCATOR_COLLISION
    assert receipt.candidate.verified_candidate_object_id is None
    assert receipt.source_id == loser.source_id
    assert (
        await lifecycle_conflict_harness.conflict_locator_snapshot(receipt.conflict_id)
        == contested.value
    )
    # No rebinding: the loser keeps its own locator, the winner keeps the target.
    assert await lifecycle_conflict_harness.open_locator_rows(loser.source_id) == [
        loser.locator.value
    ]
    assert await lifecycle_conflict_harness.open_locator_rows(winner.source_id) == [contested.value]
    loser_row = await lifecycle_conflict_harness.source_row(loser.source_id)
    winner_row = await lifecycle_conflict_harness.source_row(winner.source_id)
    assert loser_row is not None and loser_row.current_version_id == loser.current_version_id
    assert winner_row is not None and winner_row.current_version_id == winner.current_version_id


@pytest.mark.asyncio
async def test_rename_against_remote_delete_keeps_typed_error_without_conflict(
    lifecycle_conflict_harness: LifecycleConflictHarness,
) -> None:
    """A byteless rename against a remotely deleted source captures nothing:
    no content candidate exists to retain and none may be invented."""

    workspace = await lifecycle_conflict_harness.seed_workspace()
    seeded = await lifecycle_conflict_harness.seed_active_source_with_locator(
        workspace=workspace,
        source_id=uuid4(),
        locator=NormalizedLocator("notes/remote-delete-race.md"),
    )
    delete_command = lifecycle_conflict_harness.delete_command(
        seeded=seeded, base_version_id=seeded.current_version_id
    )
    device_context = lifecycle_conflict_harness.device_context(workspace)
    await lifecycle_conflict_harness.service.commit(
        delete_command, device_context, _diagnostic_context()
    )

    rename_command = lifecycle_conflict_harness.rename_command(
        seeded=seeded, target=NormalizedLocator("notes/renamed-too-late.md")
    )
    with pytest.raises(SourceLifecycleError) as failure:
        await lifecycle_conflict_harness.service.commit(
            rename_command, device_context, _diagnostic_context()
        )
    assert failure.value.code is SourceLifecycleErrorCode.LOCATOR_MISSING

    receipt = await lifecycle_conflict_harness.captured_conflict(
        workspace=workspace, event_id=rename_command.event_id
    )
    assert receipt is None
    assert await lifecycle_conflict_harness.count_conflicts(workspace) == 0
