"""Disposable PostgreSQL integration fixtures for device sync persistence.

The conftest reuses the canonical
``tests/integration/source_publication/conftest.py`` module-scoped
``source_publication_stack`` fixture: it provisions the disposable
``knowledge-ci-*`` project, applies the real Alembic baseline through the
current head and tears the project down afterwards. On top of it this module
provides the workspace/device seeding every device sync integration test
needs: cursor and manifest rows are owned by exactly one workspace and one
device, so each test seeds that credential-derived ownership triple through
schema-qualified, parameter-bound Core statements, plus one complete
canonical event history covering all six device event types with their
locator, tombstone, version and content operands.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.source_publication.conftest import source_publication_stack

from personal_os.device_sync.contracts import DeviceSyncContext
from postgresql_source_store.tables import (
    content_objects,
    devices,
    source_locators,
    source_tombstones,
    source_versions,
    sources,
    sync_events,
    users,
    workspaces,
)

__all__ = [
    "DeviceEventHistory",
    "DeviceRenameUpdateHistory",
    "DeviceSyncWorkspace",
    "seed_device_event_history",
    "seed_device_rename_update_history",
    "seed_device_sync_workspace",
    "source_publication_stack",
]


def pytest_asyncio_loop_factories(
    config: pytest.Config, item: pytest.Item
) -> dict[str, Callable[[], asyncio.AbstractEventLoop]]:
    """Run every asyncio test and fixture on a selector event loop.

    psycopg async cannot run on the Windows proactor loop, and
    SelectorEventLoop is already the default loop on the Linux CI integration
    runs.
    """
    del config, item
    return {"selector": asyncio.SelectorEventLoop}


@dataclass(frozen=True, slots=True)
class DeviceSyncWorkspace:
    """The credential-derived ownership triple of one device sync unit of work."""

    owner_user_id: UUID
    workspace_id: UUID
    device_id: UUID

    def context(self) -> DeviceSyncContext:
        """Return the device sync context of this workspace's seeded device."""

        return DeviceSyncContext(
            workspace_id=self.workspace_id,
            device_id=self.device_id,
            user_id=self.owner_user_id,
        )


async def seed_device_sync_workspace(engine: AsyncEngine) -> DeviceSyncWorkspace:
    """Seed one owner user, workspace and active device as cursor/run owner."""

    owner_user_id = uuid4()
    workspace_id = uuid4()
    device_id = uuid4()
    nonce = uuid4().hex
    async with engine.begin() as connection:
        await connection.execute(
            sa.insert(users).values(
                user_id=owner_user_id,
                username=f"device-sync-{nonce[:16]}",
                display_name="Device Sync Owner",
            )
        )
        await connection.execute(
            sa.insert(workspaces).values(
                workspace_id=workspace_id,
                owner_user_id=owner_user_id,
                workspace_key=f"device-sync-{nonce[:16]}",
                display_name="Device Sync Workspace",
            )
        )
        await connection.execute(
            sa.insert(devices).values(
                device_id=device_id,
                workspace_id=workspace_id,
                user_id=owner_user_id,
                device_name="Device Sync Device",
                device_kind="obsidian",
            )
        )
    return DeviceSyncWorkspace(
        owner_user_id=owner_user_id, workspace_id=workspace_id, device_id=device_id
    )


async def _insert_sync_event(
    connection: sa.ext.asyncio.AsyncConnection,
    *,
    workspace: DeviceSyncWorkspace,
    source_id: UUID,
    event_id: UUID,
    idempotency_key: str,
    event_type: str,
    committed_version_id: UUID,
    base_version_id: UUID | None,
    origin_device_id: UUID | None,
) -> int:
    result = await connection.execute(
        sa.insert(sync_events)
        .values(
            event_id=event_id,
            workspace_id=workspace.workspace_id,
            source_id=source_id,
            device_id=origin_device_id,
            committed_version_id=committed_version_id,
            base_version_id=base_version_id,
            idempotency_key=idempotency_key,
            request_fingerprint=hashlib.sha256(idempotency_key.encode("ascii")).hexdigest(),
            event_type=event_type,
        )
        .returning(sync_events.c.event_sequence)
    )
    return int(result.scalar_one())


@dataclass(frozen=True, slots=True)
class DeviceEventHistory:
    """One canonical six-type event history with every hydration operand.

    ``event_sequences`` maps the canonical event type tokens to the database
    identity sequence each committed event received; ``locator_ids`` keys the
    opened locators by path suffix and ``tombstone_id`` is the exact delete
    tombstone the delete opened and the restore closed.
    """

    workspace: DeviceSyncWorkspace
    source_id: UUID
    content_object_ids: tuple[UUID, UUID]
    version_ids: tuple[UUID, UUID]
    event_ids: tuple[UUID, ...]
    event_sequences: dict[str, int]
    locator_ids: dict[str, UUID]
    tombstone_id: UUID


@dataclass(frozen=True, slots=True)
class DeviceRenameUpdateHistory:
    """One canonical create → rename → update history for at-sequence pins.

    ``locator_paths`` keys the opened locators by path suffix; the update
    commits strictly after the rename, so the locator active at its own
    sequence is the post-rename path.
    """

    workspace: DeviceSyncWorkspace
    source_id: UUID
    version_id: UUID
    event_ids: tuple[UUID, UUID, UUID]
    event_sequences: dict[str, int]
    locator_paths: dict[str, str]


async def seed_device_rename_update_history(
    engine: AsyncEngine,
) -> DeviceRenameUpdateHistory:
    """Seed one source whose update commits after a rename.

    The history commits, in order: create (opens ``alpha.md``), rename
    (``alpha.md`` -> ``beta.md``) and one update with no locator operand of
    its own — the exact live shape whose hydrated ``resulting_locator``
    must resolve the post-rename active locator at the update's sequence
    (never the pre-rename path and never a null operand).
    """

    workspace = await seed_device_sync_workspace(engine)
    nonce = uuid4().hex
    source_id = uuid4()
    content_object_id = uuid4()
    version_id = uuid4()
    event_ids = (uuid4(), uuid4(), uuid4())
    committed_at = datetime.now(UTC)

    async with engine.begin() as connection:
        salt = f"device-rename-update-history-{nonce}"
        content_hash = hashlib.sha256(salt.encode("ascii")).hexdigest()
        await connection.execute(
            sa.insert(content_objects).values(
                content_object_id=content_object_id,
                content_hash=content_hash,
                object_key=f"objects/sha256/{content_hash[:2]}/{content_hash[2:4]}/{content_hash}",
                byte_size=len(salt),
                media_type="text/markdown",
                verified_at=committed_at,
            )
        )
        await connection.execute(
            sa.insert(sources).values(
                source_id=source_id,
                workspace_id=workspace.workspace_id,
                source_type="markdown",
                title=f"Device rename update history {nonce[:12]}",
                sync_state="pending",
                current_version_id=None,
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
                committed_at=committed_at,
            )
        )
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

        async def commit_event(
            *,
            event_id: UUID,
            event_type: str,
            base_version_id: UUID | None,
        ) -> int:
            return await _insert_sync_event(
                connection,
                workspace=workspace,
                source_id=source_id,
                event_id=event_id,
                idempotency_key=f"device-rename-update-{nonce}-{event_id.hex[:16]}",
                event_type=event_type,
                committed_version_id=version_id,
                base_version_id=base_version_id,
                origin_device_id=workspace.device_id,
            )

        sequence_create = await commit_event(
            event_id=event_ids[0], event_type="create", base_version_id=None
        )
        sequence_rename = await commit_event(
            event_id=event_ids[1], event_type="rename", base_version_id=version_id
        )
        sequence_update = await commit_event(
            event_id=event_ids[2], event_type="update", base_version_id=version_id
        )

        alpha_path = f"notes/{nonce}/alpha.md"
        beta_path = f"notes/{nonce}/beta.md"
        await connection.execute(
            sa.insert(source_locators).values(
                source_locator_id=uuid4(),
                workspace_id=workspace.workspace_id,
                source_id=source_id,
                normalized_locator=alpha_path,
                display_locator=alpha_path,
                opened_event_id=event_ids[0],
                opened_sequence=sequence_create,
                closed_event_id=event_ids[1],
                closed_sequence=sequence_rename,
                closed_at=sa.text("CURRENT_TIMESTAMP"),
            )
        )
        await connection.execute(
            sa.insert(source_locators).values(
                source_locator_id=uuid4(),
                workspace_id=workspace.workspace_id,
                source_id=source_id,
                normalized_locator=beta_path,
                display_locator=beta_path,
                opened_event_id=event_ids[1],
                opened_sequence=sequence_rename,
            )
        )

    return DeviceRenameUpdateHistory(
        workspace=workspace,
        source_id=source_id,
        version_id=version_id,
        event_ids=event_ids,
        event_sequences={
            "create": sequence_create,
            "rename": sequence_rename,
            "update": sequence_update,
        },
        locator_paths={"alpha": alpha_path, "beta": beta_path},
    )


async def seed_device_event_history(engine: AsyncEngine) -> DeviceEventHistory:
    """Seed one source whose history exercises every device event type.

    The history commits, in order: create (opens ``alpha.md``), update,
    rename (``alpha.md`` -> ``beta.md``), delete (closes ``beta.md`` and
    opens the tombstone), restore (closes the tombstone and opens
    ``gamma.md``) and one trailing update with no locator or tombstone
    operands. The trailing update and the mid-history update are the only
    rows no lifecycle table references, so a test can remove exactly those
    two events to simulate compacted retained history.
    """

    workspace = await seed_device_sync_workspace(engine)
    nonce = uuid4().hex
    source_id = uuid4()
    content_object_ids = (uuid4(), uuid4())
    version_ids = (uuid4(), uuid4())
    event_ids = tuple(uuid4() for _ in range(6))
    locator_ids: dict[str, UUID] = {}
    committed_at = datetime.now(UTC)

    async with engine.begin() as connection:
        for index, content_object_id in enumerate(content_object_ids):
            salt = f"device-event-history-{nonce}-{index}"
            content_hash = hashlib.sha256(salt.encode("ascii")).hexdigest()
            await connection.execute(
                sa.insert(content_objects).values(
                    content_object_id=content_object_id,
                    content_hash=content_hash,
                    object_key=f"objects/sha256/{content_hash[:2]}/{content_hash[2:4]}/{content_hash}",
                    byte_size=len(salt),
                    media_type="text/markdown",
                    verified_at=committed_at,
                )
            )
        await connection.execute(
            sa.insert(sources).values(
                source_id=source_id,
                workspace_id=workspace.workspace_id,
                source_type="markdown",
                title=f"Device event history {nonce[:12]}",
                sync_state="pending",
                current_version_id=None,
            )
        )
        for index, version_id in enumerate(version_ids):
            await connection.execute(
                sa.insert(source_versions).values(
                    source_version_id=version_id,
                    workspace_id=workspace.workspace_id,
                    source_id=source_id,
                    content_object_id=content_object_ids[index],
                    content_version=index + 1,
                    parent_version_id=version_ids[index - 1] if index else None,
                    author_kind="device",
                    author_id=workspace.device_id,
                    committed_at=committed_at,
                )
            )
        await connection.execute(
            sa.update(sources)
            .values(
                sync_state="active",
                current_version_id=version_ids[1],
                updated_at=sa.text("CURRENT_TIMESTAMP"),
            )
            .where(
                sources.c.workspace_id == workspace.workspace_id,
                sources.c.source_id == source_id,
            )
        )

        async def commit_event(
            *,
            event_id: UUID,
            event_type: str,
            committed_version_id: UUID,
            base_version_id: UUID | None,
            origin_device_id: UUID | None,
        ) -> int:
            return await _insert_sync_event(
                connection,
                workspace=workspace,
                source_id=source_id,
                event_id=event_id,
                idempotency_key=f"device-history-{nonce}-{event_id.hex[:16]}",
                event_type=event_type,
                committed_version_id=committed_version_id,
                base_version_id=base_version_id,
                origin_device_id=origin_device_id,
            )

        sequence_create = await commit_event(
            event_id=event_ids[0],
            event_type="create",
            committed_version_id=version_ids[0],
            base_version_id=None,
            origin_device_id=workspace.device_id,
        )
        sequence_update = await commit_event(
            event_id=event_ids[1],
            event_type="update",
            committed_version_id=version_ids[1],
            base_version_id=version_ids[0],
            origin_device_id=None,
        )
        sequence_rename = await commit_event(
            event_id=event_ids[2],
            event_type="rename",
            committed_version_id=version_ids[1],
            base_version_id=version_ids[1],
            origin_device_id=workspace.device_id,
        )
        sequence_delete = await commit_event(
            event_id=event_ids[3],
            event_type="delete",
            committed_version_id=version_ids[1],
            base_version_id=version_ids[1],
            origin_device_id=workspace.device_id,
        )
        sequence_restore = await commit_event(
            event_id=event_ids[4],
            event_type="restore",
            committed_version_id=version_ids[1],
            base_version_id=version_ids[1],
            origin_device_id=workspace.device_id,
        )
        sequence_trailing_update = await commit_event(
            event_id=event_ids[5],
            event_type="update",
            committed_version_id=version_ids[1],
            base_version_id=version_ids[1],
            origin_device_id=None,
        )

        alpha_locator_id = uuid4()
        beta_locator_id = uuid4()
        gamma_locator_id = uuid4()
        locator_ids["alpha"] = alpha_locator_id
        locator_ids["beta"] = beta_locator_id
        locator_ids["gamma"] = gamma_locator_id
        await connection.execute(
            sa.insert(source_locators).values(
                source_locator_id=alpha_locator_id,
                workspace_id=workspace.workspace_id,
                source_id=source_id,
                normalized_locator=f"notes/{nonce}/alpha.md",
                display_locator=f"notes/{nonce}/alpha.md",
                opened_event_id=event_ids[0],
                opened_sequence=sequence_create,
                closed_event_id=event_ids[2],
                closed_sequence=sequence_rename,
                closed_at=sa.text("CURRENT_TIMESTAMP"),
            )
        )
        await connection.execute(
            sa.insert(source_locators).values(
                source_locator_id=beta_locator_id,
                workspace_id=workspace.workspace_id,
                source_id=source_id,
                normalized_locator=f"notes/{nonce}/beta.md",
                display_locator=f"notes/{nonce}/beta.md",
                opened_event_id=event_ids[2],
                opened_sequence=sequence_rename,
                closed_event_id=event_ids[3],
                closed_sequence=sequence_delete,
                closed_at=sa.text("CURRENT_TIMESTAMP"),
            )
        )
        await connection.execute(
            sa.insert(source_locators).values(
                source_locator_id=gamma_locator_id,
                workspace_id=workspace.workspace_id,
                source_id=source_id,
                normalized_locator=f"notes/{nonce}/gamma.md",
                display_locator=f"notes/{nonce}/gamma.md",
                opened_event_id=event_ids[4],
                opened_sequence=sequence_restore,
            )
        )

        tombstone_id = uuid4()
        await connection.execute(
            sa.insert(source_tombstones).values(
                source_tombstone_id=tombstone_id,
                workspace_id=workspace.workspace_id,
                source_id=source_id,
                delete_event_id=event_ids[3],
                retained_version_id=version_ids[1],
                retained_locator=f"notes/{nonce}/beta.md",
                actor_kind="device",
                actor_id=workspace.device_id,
                deleted_at=sa.text("CURRENT_TIMESTAMP"),
                restore_event_id=event_ids[4],
                restored_at=sa.text("CURRENT_TIMESTAMP"),
            )
        )

    return DeviceEventHistory(
        workspace=workspace,
        source_id=source_id,
        content_object_ids=content_object_ids,
        version_ids=version_ids,
        event_ids=event_ids,
        event_sequences={
            "create": sequence_create,
            "update": sequence_update,
            "rename": sequence_rename,
            "delete": sequence_delete,
            "restore": sequence_restore,
            "trailing_update": sequence_trailing_update,
        },
        locator_ids=locator_ids,
        tombstone_id=tombstone_id,
    )
