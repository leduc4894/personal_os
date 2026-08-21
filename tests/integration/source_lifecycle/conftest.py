"""Disposable PostgreSQL integration fixtures for source-lifecycle transitions.

This conftest builds on the canonical ``tests/integration/source_publication/conftest.py``
fixtures: the module-scoped ``source_publication_stack`` already provisions
the migrated baseline through the Task 2 ``20260820_01`` migration, and the
function-scoped ``inspection_engine`` and ``preflight_harness`` provide
seeded canonical sources and active devices. The lifecycle-specific helper
extends the existing ``PreflightHarness`` with a :class:`PostgresqlSourceLifecycleStore`
instance and a wider seed helper that drops a known initial source locator
and source version so the lifecycle adapter can replay the source-publication
``CreateSourceVersion`` path. The lifecycle store shares the same engine the
publication store uses; the seed helpers therefore write through
schema-qualified, parameter-bound Core statements and never fabricate a
state shape the real adapter cannot produce.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from api_runtime.exclusion_policy_crypto import TrustAnchorEd25519Verifier
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.source_publication.conftest import (
    PreflightHarness,
    SeededVersion,
    SeededWorkspace,
    SourcePublicationStack,
    source_publication_stack,
)

from personal_os.source_locators import NormalizedLocator
from postgresql_source_store.engine import create_source_store_engine, dispose_source_store_engine
from postgresql_source_store.lifecycle_store import PostgresqlSourceLifecycleStore
from postgresql_source_store.publication_store import PostgresqlSourcePublicationStore
from postgresql_source_store.tables import (
    SOURCE_STORE_TABLES,
    audit_events,
    content_objects,
    source_locators,
    source_versions,
    sources,
    sync_events,
    users,
    workspace_policy_state,
    workspaces,
)

pytest_plugins = (
    "tests.integration.source_publication.conftest",
    "tests.integration.source_lifecycle.conftest",
)
pytestmark = pytest.mark.local_stack

__all__ = [
    "PreflightHarness",
    "SeededSourceLocator",
    "SeededVersion",
    "SeededWorkspace",
    "SourcePublicationStack",
    "lifecycle_harness",
    "source_publication_stack",
]


@dataclass(frozen=True, slots=True)
class SeededSourceLocator:
    """A canonical source with an initial locator and one committed version.

    ``current_version_id`` is the source's ``sources.current_version_id``
    pointer at the moment the lifecycle harness is built; it must be passed
    to lifecycle commands as ``expected_version_id`` so the adapter's
    version conflict check is exercised against a real committed row.
    ``initial_locator`` is the workspace-unique path the source's first
    ``source_locators`` row carries.
    """

    workspace_id: UUID
    owner_user_id: UUID
    device_id: UUID
    source_id: UUID
    current_version_id: UUID
    initial_locator: NormalizedLocator


class LifecycleHarness:
    """Seeded helpers for the lifecycle integration tests."""

    def __init__(
        self,
        engine: AsyncEngine,
        publication_store: PostgresqlSourcePublicationStore,
        lifecycle_store: PostgresqlSourceLifecycleStore,
    ) -> None:
        self._engine = engine
        self._publication_store = publication_store
        self._lifecycle_store = lifecycle_store

    @property
    def publication_store(self) -> PostgresqlSourcePublicationStore:
        return self._publication_store

    @property
    def lifecycle_store(self) -> PostgresqlSourceLifecycleStore:
        return self._lifecycle_store

    async def seed_workspace(self) -> SeededWorkspace:
        # Reuse the publication-stack helper: it already provisions the
        # workspace policy state and the empty signed policy seed.
        return await self._seed_publication_workspace()

    async def seed_active_source_with_locator(
        self,
        *,
        workspace: SeededWorkspace,
        source_id: UUID,
        locator: NormalizedLocator,
        title: str = "Lifecycle source",
        source_type: str = "markdown",
    ) -> SeededSourceLocator:
        owner_user_id = workspace.owner_user_id
        workspace_id = workspace.workspace_id
        device_id = workspace.device_id
        content_object_id = uuid4()
        source_version_id = uuid4()
        event_id = uuid4()
        digest = hashlib.sha256(str(source_id).encode("ascii")).hexdigest()
        async with self._engine.begin() as connection:
            await self._insert_content_object(
                connection, content_object_id, digest
            )
            await connection.execute(
                sa.insert(sources).values(
                    source_id=source_id,
                    workspace_id=workspace_id,
                    source_type=source_type,
                    title=title,
                )
            )
            await connection.execute(
                sa.insert(source_versions).values(
                    source_version_id=source_version_id,
                    workspace_id=workspace_id,
                    source_id=source_id,
                    content_object_id=content_object_id,
                    content_version=1,
                    author_kind="user",
                    author_id=owner_user_id,
                )
            )
            await connection.execute(
                sa.update(sources)
                .where(sources.c.source_id == source_id)
                .values(
                    sync_state="active",
                    current_version_id=source_version_id,
                )
            )
            event_result = await connection.execute(
                sa.insert(sync_events)
                .values(
                    event_id=event_id,
                    workspace_id=workspace_id,
                    source_id=source_id,
                    device_id=device_id,
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
                    workspace_id=workspace_id,
                    source_id=source_id,
                    normalized_locator=locator.value,
                    display_locator=locator.value,
                    opened_event_id=event_id,
                    opened_sequence=opened_sequence,
                )
            )
        return SeededSourceLocator(
            workspace_id=workspace_id,
            owner_user_id=owner_user_id,
            device_id=device_id,
            source_id=source_id,
            current_version_id=source_version_id,
            initial_locator=locator,
        )

    async def table_row_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        async with self._engine.connect() as connection:
            for table_name, table in SOURCE_STORE_TABLES.items():
                result = await connection.execute(sa.select(sa.func.count()).select_from(table))
                counts[table_name] = int(result.scalar_one())
        return counts

    async def fetch_source_row(self, source_id: UUID) -> Any:
        async with self._engine.connect() as connection:
            result = await connection.execute(
                sa.select(
                    sources.c.source_id,
                    sources.c.workspace_id,
                    sources.c.source_type,
                    sources.c.title,
                    sources.c.sync_state,
                    sources.c.current_version_id,
                    sources.c.deleted_at,
                ).where(sources.c.source_id == source_id)
            )
            return result.one_or_none()

    async def fetch_active_locator(self, source_id: UUID) -> Any:
        async with self._engine.connect() as connection:
            result = await connection.execute(
                sa.select(
                    source_locators.c.source_locator_id,
                    source_locators.c.workspace_id,
                    source_locators.c.source_id,
                    source_locators.c.normalized_locator,
                    source_locators.c.display_locator,
                    source_locators.c.opened_event_id,
                    source_locators.c.opened_sequence,
                    source_locators.c.closed_event_id,
                    source_locators.c.closed_sequence,
                )
                .where(
                    source_locators.c.source_id == source_id,
                    source_locators.c.closed_event_id.is_(None),
                )
                .with_for_update()
            )
            return result.one_or_none()

    async def fetch_locator_history(self, source_id: UUID) -> list[Any]:
        async with self._engine.connect() as connection:
            result = await connection.execute(
                sa.select(
                    source_locators.c.source_locator_id,
                    source_locators.c.normalized_locator,
                    source_locators.c.opened_event_id,
                    source_locators.c.opened_sequence,
                    source_locators.c.closed_event_id,
                    source_locators.c.closed_sequence,
                )
                .where(source_locators.c.source_id == source_id)
                .order_by(source_locators.c.opened_sequence)
            )
            return list(result.all())

    async def fetch_tombstone(self, source_id: UUID) -> Any:
        async with self._engine.connect() as connection:
            result = await connection.execute(
                sa.select(
                    source_locators_audit_columns()
                )
                .select_from(sa.table("source_tombstones"))
                .where(source_tombstones_clause(source_id))
            )
            return result.one_or_none()

    async def fetch_event_row(self, event_id: UUID) -> Any:
        async with self._engine.connect() as connection:
            result = await connection.execute(
                sa.select(
                    sync_events.c.event_id,
                    sync_events.c.event_sequence,
                    sync_events.c.event_type,
                    sync_events.c.base_version_id,
                    sync_events.c.committed_version_id,
                    sync_events.c.idempotency_key,
                    sync_events.c.request_fingerprint,
                    sync_events.c.device_id,
                    sync_events.c.committed_at,
                ).where(sync_events.c.event_id == event_id)
            )
            return result.one_or_none()

    async def fetch_intent_rows(self, event_id: UUID) -> list[Any]:
        async with self._engine.connect() as connection:
            result = await connection.execute(
                sa.select(
                    projection_intents_columns()
                )
                .select_from(sa.table("projection_intents"))
                .where(sync_event_clause(event_id))
                .order_by(sa.column("projection_kind"))
            )
            return list(result.all())

    async def fetch_audit_rows(self, workspace_id: UUID) -> list[Any]:
        async with self._engine.connect() as connection:
            result = await connection.execute(
                sa.select(
                    audit_events.c.audit_event_id,
                    audit_events.c.action,
                    audit_events.c.target_kind,
                    audit_events.c.target_id,
                    audit_events.c.actor_kind,
                    audit_events.c.actor_id,
                    audit_events.c.result,
                    audit_events.c.reason_code,
                    audit_events.c.safe_diff_hash,
                ).where(audit_events.c.workspace_id == workspace_id)
            )
            return list(result.all())

    async def _seed_publication_workspace(self) -> SeededWorkspace:

        owner_user_id = uuid4()
        workspace_id = uuid4()
        device_id = uuid4()
        nonce = uuid4().hex
        async with self._engine.begin() as connection:
            await connection.execute(
                sa.insert(users).values(
                    user_id=owner_user_id,
                    username=f"lifecycle-{nonce[:12]}",
                    display_name="Lifecycle Owner",
                )
            )
            await connection.execute(
                sa.insert(workspaces).values(
                    workspace_id=workspace_id,
                    owner_user_id=owner_user_id,
                    workspace_key=f"ws-{nonce[:12]}",
                    display_name="Lifecycle Workspace",
                )
            )
            await connection.execute(
                sa.insert(
                    sa.table(
                        "devices",
                        sa.column("device_id", sa.Uuid()),
                        sa.column("workspace_id", sa.Uuid()),
                        sa.column("user_id", sa.Uuid()),
                        sa.column("device_name", sa.String()),
                        sa.column("device_kind", sa.Text()),
                    )
                ).values(
                    device_id=device_id,
                    workspace_id=workspace_id,
                    user_id=owner_user_id,
                    device_name="Lifecycle Device",
                    device_kind="obsidian",
                )
            )
            await connection.execute(
                sa.insert(workspace_policy_state).values(
                    workspace_id=workspace_id,
                    active_policy_revision_id=None,
                    active_revision_number=0,
                )
            )
        # Seed the empty signed policy so locked policy enforcement is a no-op.
        from tools.signed_policy_seed import seed_signed_policy

        await seed_signed_policy(
            self._engine,
            workspace_id=workspace_id,
            published_by_user_id=owner_user_id,
        )
        return SeededWorkspace(
            owner_user_id=owner_user_id,
            workspace_id=workspace_id,
            device_id=device_id,
        )

    async def _insert_content_object(
        self,
        connection: Any,
        content_object_id: UUID,
        content_hash: str,
    ) -> None:
        object_key = (
            f"objects/sha256/{content_hash[:2]}/"
            f"{content_hash[2:4]}/{content_hash}"
        )
        await connection.execute(
            sa.insert(content_objects).values(
                content_object_id=content_object_id,
                content_hash=content_hash,
                object_key=object_key,
                byte_size=1,
                media_type="text/markdown",
                verified_at=sa.text("CURRENT_TIMESTAMP"),
            )
        )


def source_locators_audit_columns() -> Any:
    from postgresql_source_store.tables import source_tombstones

    return sa.select(
        source_tombstones.c.source_tombstone_id,
        source_tombstones.c.workspace_id,
        source_tombstones.c.source_id,
        source_tombstones.c.delete_event_id,
        source_tombstones.c.retained_version_id,
        source_tombstones.c.retained_locator,
        source_tombstones.c.actor_kind,
        source_tombstones.c.actor_id,
        source_tombstones.c.deleted_at,
        source_tombstones.c.restore_event_id,
        source_tombstones.c.restored_at,
    )


def source_tombstones_clause(source_id: UUID) -> Any:
    from postgresql_source_store.tables import source_tombstones

    return source_tombstones.c.source_id == source_id


def sync_event_clause(event_id: UUID) -> Any:
    from postgresql_source_store.tables import projection_intents

    return projection_intents.c.event_id == event_id


def projection_intents_columns() -> Any:
    from postgresql_source_store.tables import projection_intents

    return sa.select(
        projection_intents.c.projection_intent_id,
        projection_intents.c.event_id,
        projection_intents.c.source_id,
        projection_intents.c.source_version_id,
        projection_intents.c.projection_kind,
        projection_intents.c.operation,
        projection_intents.c.status,
        projection_intents.c.attempt_count,
    )


@pytest_asyncio.fixture
async def lifecycle_harness(
    source_publication_stack: SourcePublicationStack,
) -> Iterator[LifecycleHarness]:
    engine = create_source_store_engine(
        source_publication_stack.settings, source_publication_stack.password
    )
    try:
        publication_store = PostgresqlSourcePublicationStore(
            engine, policy_verifier=TrustAnchorEd25519Verifier()
        )
        lifecycle_store = PostgresqlSourceLifecycleStore(
            engine, policy_verifier=TrustAnchorEd25519Verifier()
        )
        yield LifecycleHarness(engine, publication_store, lifecycle_store)
    finally:
        await dispose_source_store_engine(engine)