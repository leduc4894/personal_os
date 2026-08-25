"""Disposable PostgreSQL integration fixtures for device sync persistence.

The conftest reuses the canonical
``tests/integration/source_publication/conftest.py`` module-scoped
``source_publication_stack`` fixture: it provisions the disposable
``knowledge-ci-*`` project, applies the real Alembic baseline through the
current head and tears the project down afterwards. On top of it this module
provides the workspace/device seeding every device sync integration test
needs: cursor and manifest rows are owned by exactly one workspace and one
device, so each test seeds that credential-derived ownership triple through
schema-qualified, parameter-bound Core statements.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.source_publication.conftest import (
    SourcePublicationStack,
    source_publication_stack,
)

from postgresql_source_store.engine import (
    create_source_store_engine,
    dispose_source_store_engine,
)
from postgresql_source_store.tables import devices, users, workspaces

pytestmark = pytest.mark.local_stack


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


__all__ = [
    "DeviceSyncWorkspace",
    "device_sync_engine",
    "seed_device_sync_workspace",
    "source_publication_stack",
]


@dataclass(frozen=True, slots=True)
class DeviceSyncWorkspace:
    """The credential-derived ownership triple of one device sync unit of work."""

    owner_user_id: UUID
    workspace_id: UUID
    device_id: UUID


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


@pytest_asyncio.fixture
async def device_sync_engine(
    source_publication_stack: SourcePublicationStack,
) -> AsyncEngine:
    engine = create_source_store_engine(
        source_publication_stack.settings, source_publication_stack.password
    )
    try:
        yield engine
    finally:
        await dispose_source_store_engine(engine)
