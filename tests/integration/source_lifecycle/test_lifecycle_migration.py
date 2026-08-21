"""Disposable PostgreSQL migration coverage for canonical locator history."""

from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from tests.integration.source_publication.conftest import SourcePublicationStack

from postgresql_source_store.engine import create_source_store_engine, dispose_source_store_engine
from postgresql_source_store.tables import (
    content_objects,
    source_locators,
    source_versions,
    sources,
    sync_events,
    users,
    workspaces,
)

pytestmark = pytest.mark.local_stack

REPO_ROOT = Path(__file__).resolve().parents[3]
_REVISION = "20260820_01"
_PREDECESSOR_REVISION = "20260818_01"
_MIGRATION_COMMAND_TIMEOUT_SECONDS = 60


@dataclass(frozen=True)
class _LifecycleEvidence:
    workspace_id: UUID
    active_source_id: UUID
    active_source_version_id: UUID
    deleted_source_id: UUID


def _migration_environment() -> dict[str, str]:
    """Build the standard test loader environment without reading any secret."""

    environment = dict(os.environ)
    for key in [name for name in environment if name.startswith("KNOWLEDGE_")]:
        del environment[key]
    environment.update(
        {
            "KNOWLEDGE_ENVIRONMENT": "test",
            "KNOWLEDGE_SECRET_ROOT": str(REPO_ROOT / ".local" / "stack-secrets"),
            "KNOWLEDGE_DATABASE_HOST": "127.0.0.1",
            "KNOWLEDGE_DATABASE_PORT": os.environ.get("POSTGRES_PORT", "5432"),
            "KNOWLEDGE_DATABASE_NAME": "knowledge",
            "KNOWLEDGE_DATABASE_USER": "knowledge_app",
            "KNOWLEDGE_DATABASE_PASSWORD_FILE": "postgres_application_password",
            "KNOWLEDGE_DATABASE_SSL_MODE": "disable",
        }
    )
    return environment


def _alembic(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["uv", "run", "alembic", *arguments],
        cwd=REPO_ROOT,
        env=_migration_environment(),
        capture_output=True,
        text=True,
        check=False,
        timeout=_MIGRATION_COMMAND_TIMEOUT_SECONDS,
    )


async def _seed_active_source(
    connection: sa.ext.asyncio.AsyncConnection,
    *,
    owner_user_id: UUID,
    workspace_id: UUID,
    source_id: UUID,
    locator: str,
) -> tuple[UUID, UUID, int]:
    content_object_id = uuid4()
    source_version_id = uuid4()
    event_id = uuid4()
    digest = hashlib.sha256(str(source_id).encode("ascii")).hexdigest()
    await connection.execute(
        sa.insert(sources).values(
            source_id=source_id,
            workspace_id=workspace_id,
            source_type="markdown",
            title="Lifecycle source",
        )
    )
    await connection.execute(
        sa.insert(content_objects).values(
            content_object_id=content_object_id,
            content_hash=digest,
            object_key=f"objects/sha256/{digest[:2]}/{digest[2:4]}/{digest}",
            byte_size=1,
            media_type="text/markdown",
            verified_at=sa.text("CURRENT_TIMESTAMP"),
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
        .values(sync_state="active", current_version_id=source_version_id)
    )
    event = await connection.execute(
        sa.insert(sync_events)
        .values(
            event_id=event_id,
            workspace_id=workspace_id,
            source_id=source_id,
            committed_version_id=source_version_id,
            idempotency_key=str(uuid4()),
            request_fingerprint=hashlib.sha256(event_id.bytes).hexdigest(),
            event_type="create",
        )
        .returning(sync_events.c.event_sequence)
    )
    event_sequence = int(event.scalar_one())
    await connection.execute(
        sa.insert(source_locators).values(
            source_locator_id=uuid4(),
            workspace_id=workspace_id,
            source_id=source_id,
            normalized_locator=locator,
            display_locator=locator,
            opened_event_id=event_id,
            opened_sequence=event_sequence,
        )
    )
    return event_id, source_version_id, event_sequence


async def _insert_sync_event(
    connection: sa.ext.asyncio.AsyncConnection,
    *,
    workspace_id: UUID,
    source_id: UUID,
    source_version_id: UUID,
    event_type: str,
) -> tuple[UUID, int]:
    event_id = uuid4()
    event = await connection.execute(
        sa.insert(sync_events)
        .values(
            event_id=event_id,
            workspace_id=workspace_id,
            source_id=source_id,
            committed_version_id=source_version_id,
            idempotency_key=str(uuid4()),
            request_fingerprint=hashlib.sha256(event_id.bytes).hexdigest(),
            event_type=event_type,
        )
        .returning(sync_events.c.event_sequence)
    )
    return event_id, int(event.scalar_one())


async def _assert_partial_uniqueness(stack: SourcePublicationStack) -> _LifecycleEvidence:
    engine = create_source_store_engine(stack.settings, stack.password)
    owner_user_id = uuid4()
    workspace_id = uuid4()
    first_source_id = uuid4()
    second_source_id = uuid4()
    try:
        async with engine.begin() as connection:
            nonce = uuid4().hex
            await connection.execute(
                sa.insert(users).values(
                    user_id=owner_user_id,
                    username=f"lifecycle-{nonce[:16]}",
                    display_name="Lifecycle Owner",
                )
            )
            await connection.execute(
                sa.insert(workspaces).values(
                    workspace_id=workspace_id,
                    owner_user_id=owner_user_id,
                    workspace_key=f"lifecycle-{nonce[:16]}",
                    display_name="Lifecycle Workspace",
                )
            )
            first_event_id, first_version_id, first_sequence = await _seed_active_source(
                connection,
                owner_user_id=owner_user_id,
                workspace_id=workspace_id,
                source_id=first_source_id,
                locator="notes/first.md",
            )
            second_event_id, second_version_id, second_sequence = await _seed_active_source(
                connection,
                owner_user_id=owner_user_id,
                workspace_id=workspace_id,
                source_id=second_source_id,
                locator="notes/second.md",
            )
            close_event_id, close_sequence = await _insert_sync_event(
                connection,
                workspace_id=workspace_id,
                source_id=first_source_id,
                source_version_id=first_version_id,
                event_type="update",
            )
            incomplete_closure_savepoint = await connection.begin_nested()
            try:
                with pytest.raises(IntegrityError) as incomplete_closure:
                    await connection.execute(
                        sa.insert(source_locators).values(
                            source_locator_id=uuid4(),
                            workspace_id=workspace_id,
                            source_id=first_source_id,
                            normalized_locator="notes/incomplete-close.md",
                            display_locator="notes/incomplete-close.md",
                            opened_event_id=first_event_id,
                            opened_sequence=first_sequence,
                            closed_event_id=close_event_id,
                            closed_sequence=close_sequence,
                        )
                    )
            finally:
                await incomplete_closure_savepoint.rollback()
            assert getattr(incomplete_closure.value.orig, "diag", None).constraint_name == (
                "ck_source_locators__closure"
            )

        async with engine.begin() as connection:
            duplicate_source_savepoint = await connection.begin_nested()
            try:
                with pytest.raises(IntegrityError) as duplicate_source:
                    await connection.execute(
                        sa.insert(source_locators).values(
                            source_locator_id=uuid4(),
                            workspace_id=workspace_id,
                            source_id=first_source_id,
                            normalized_locator="notes/rebound.md",
                            display_locator="notes/rebound.md",
                            opened_event_id=first_event_id,
                            opened_sequence=first_sequence,
                        )
                    )
            finally:
                await duplicate_source_savepoint.rollback()
            assert getattr(duplicate_source.value.orig, "diag", None).constraint_name == (
                "uq_source_locators_active_source"
            )

        async with engine.begin() as connection:
            duplicate_path_savepoint = await connection.begin_nested()
            try:
                with pytest.raises(IntegrityError) as duplicate_path:
                    await connection.execute(
                        sa.insert(source_locators).values(
                            source_locator_id=uuid4(),
                            workspace_id=workspace_id,
                            source_id=second_source_id,
                            normalized_locator="notes/first.md",
                            display_locator="notes/first.md",
                            opened_event_id=second_event_id,
                            opened_sequence=second_sequence,
                        )
                    )
            finally:
                await duplicate_path_savepoint.rollback()
            assert getattr(duplicate_path.value.orig, "diag", None).constraint_name == (
                "uq_source_locators_active_workspace_path"
            )
            await connection.execute(
                sa.update(sources)
                .where(sources.c.source_id == second_source_id)
                .values(sync_state="deleted", deleted_at=sa.text("CURRENT_TIMESTAMP"))
            )
            await _insert_sync_event(
                connection,
                workspace_id=workspace_id,
                source_id=second_source_id,
                source_version_id=second_version_id,
                event_type="delete",
            )
        return _LifecycleEvidence(
            workspace_id=workspace_id,
            active_source_id=first_source_id,
            active_source_version_id=first_version_id,
            deleted_source_id=second_source_id,
        )
    finally:
        await dispose_source_store_engine(engine)


async def _assert_predecessor_constraints(
    stack: SourcePublicationStack, evidence: _LifecycleEvidence
) -> None:
    engine = create_source_store_engine(stack.settings, stack.password)
    try:
        async with engine.begin() as connection:
            lifecycle_event_savepoint = await connection.begin_nested()
            try:
                with pytest.raises(IntegrityError) as lifecycle_event:
                    await _insert_sync_event(
                        connection,
                        workspace_id=evidence.workspace_id,
                        source_id=evidence.active_source_id,
                        source_version_id=evidence.active_source_version_id,
                        event_type="rename",
                    )
            finally:
                await lifecycle_event_savepoint.rollback()
            assert getattr(lifecycle_event.value.orig, "diag", None).constraint_name == (
                "ck_sync_events__event_type"
            )

            deleted_source = await connection.execute(
                sa.select(sources.c.sync_state, sources.c.deleted_at).where(
                    sources.c.source_id == evidence.deleted_source_id
                )
            )
            deleted_sync_state, deleted_at = deleted_source.one()
            assert deleted_sync_state == "deleted"
            assert deleted_at is not None

            source_deletion_savepoint = await connection.begin_nested()
            try:
                with pytest.raises(IntegrityError) as missing_deleted_at:
                    await connection.execute(
                        sa.update(sources)
                        .where(sources.c.source_id == evidence.deleted_source_id)
                        .values(deleted_at=None)
                    )
            finally:
                await source_deletion_savepoint.rollback()
            assert getattr(missing_deleted_at.value.orig, "diag", None).constraint_name == (
                "ck_sources__deletion"
            )
    finally:
        await dispose_source_store_engine(engine)


def test_lifecycle_revision_round_trips_active_locator_uniqueness(
    source_publication_stack: SourcePublicationStack,
) -> None:
    """Upgrade from 20260818_01, enforce both partial uniques, then round-trip."""

    downgrade = _alembic("-x", "allow_destructive=true", "downgrade", _PREDECESSOR_REVISION)
    assert downgrade.returncode == 0
    upgrade = _alembic("upgrade", _REVISION)
    assert upgrade.returncode == 0
    evidence = asyncio.run(
        _assert_partial_uniqueness(source_publication_stack),
        loop_factory=asyncio.SelectorEventLoop,
    )
    second_downgrade = _alembic("-x", "allow_destructive=true", "downgrade", _PREDECESSOR_REVISION)
    assert second_downgrade.returncode == 0
    asyncio.run(
        _assert_predecessor_constraints(source_publication_stack, evidence),
        loop_factory=asyncio.SelectorEventLoop,
    )
    second_upgrade = _alembic("upgrade", "head")
    assert second_upgrade.returncode == 0
