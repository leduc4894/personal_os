"""Disposable PostgreSQL migration coverage for device cursors and manifests."""

from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from tests.integration.device_sync.conftest import (
    DeviceSyncWorkspace,
    seed_device_sync_workspace,
)
from tests.integration.source_publication.conftest import SourcePublicationStack

from postgresql_source_store.engine import create_source_store_engine, dispose_source_store_engine
from postgresql_source_store.tables import (
    device_cursors,
    devices,
    manifest_actions,
    manifest_entry_resolutions,
    manifest_pages,
    manifest_runs,
)

pytestmark = pytest.mark.local_stack

REPO_ROOT = Path(__file__).resolve().parents[3]
_REVISION = "20260826_01"
_PREDECESSOR_REVISION = "20260820_01"
_MIGRATION_COMMAND_TIMEOUT_SECONDS = 60
_PAGE_DIGEST = hashlib.sha256(b"device-sync-page-zero").hexdigest()
_ENTRY_SHA256 = hashlib.sha256(b"device-sync-entry").hexdigest()
_LOCATOR_DIGEST = hashlib.sha256(b"notes/device-sync.md").hexdigest()


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


def _digest_text(seed: str) -> str:
    return hashlib.sha256(seed.encode("ascii")).hexdigest()


async def _insert_cursor(
    connection: sa.ext.asyncio.AsyncConnection,
    workspace: DeviceSyncWorkspace,
    *,
    acknowledged_sequence: int = 0,
    delivered_through_sequence: int = 0,
) -> None:
    await connection.execute(
        sa.insert(device_cursors).values(
            device_cursor_id=uuid4(),
            workspace_id=workspace.workspace_id,
            device_id=workspace.device_id,
            acknowledged_sequence=acknowledged_sequence,
            delivered_through_sequence=delivered_through_sequence,
        )
    )


async def _insert_manifest_run(
    connection: sa.ext.asyncio.AsyncConnection,
    workspace: DeviceSyncWorkspace,
    *,
    manifest_run_id: UUID,
    state: str = "collecting",
    checkpoint_sequence: int = 5,
    entry_count: int = 0,
    final_digest: str | None = None,
    planned_at: object | None = None,
    completed_at: object | None = None,
    safe_error_code: str | None = None,
) -> None:
    await connection.execute(
        sa.insert(manifest_runs).values(
            manifest_run_id=manifest_run_id,
            workspace_id=workspace.workspace_id,
            device_id=workspace.device_id,
            base_acknowledged_sequence=0,
            checkpoint_sequence=checkpoint_sequence,
            policy_revision_number=1,
            client_observation_generation=0,
            state=state,
            next_page_number=0,
            entry_count=entry_count,
            final_digest=final_digest,
            planned_at=planned_at,
            completed_at=completed_at,
            safe_error_code=safe_error_code,
        )
    )


async def _insert_manifest_page(
    connection: sa.ext.asyncio.AsyncConnection,
    manifest_run_id: UUID,
    *,
    page_number: int = 0,
    entry_count: int = 2,
    page_digest: str = _PAGE_DIGEST,
) -> None:
    await connection.execute(
        sa.insert(manifest_pages).values(
            manifest_run_id=manifest_run_id,
            page_number=page_number,
            entry_count=entry_count,
            page_digest=page_digest,
        )
    )


async def _insert_entry_resolution(
    connection: sa.ext.asyncio.AsyncConnection,
    manifest_run_id: UUID,
    *,
    entry_index: int = 0,
    match_kind: str = "unproven",
    resolved_source_id: UUID | None = None,
    resolved_source_version_id: UUID | None = None,
) -> None:
    await connection.execute(
        sa.insert(manifest_entry_resolutions).values(
            manifest_run_id=manifest_run_id,
            page_number=0,
            entry_index=entry_index,
            local_entry_id=f"device-sync-entry-{entry_index}",
            submitted_sha256=_ENTRY_SHA256,
            submitted_size_bytes=128,
            submitted_media_type="text/markdown",
            locator_evidence_digest=_LOCATOR_DIGEST,
            resolved_source_id=resolved_source_id,
            resolved_source_version_id=resolved_source_version_id,
            match_kind=match_kind,
        )
    )


async def _insert_manifest_action(
    connection: sa.ext.asyncio.AsyncConnection,
    manifest_run_id: UUID,
    *,
    action_index: int,
    action_kind: str,
    local_entry_id: str | None,
    source_id: UUID | None = None,
    source_version_id: UUID | None = None,
    source_tombstone_id: UUID | None = None,
    safe_reason_code: str | None = None,
) -> None:
    await connection.execute(
        sa.insert(manifest_actions).values(
            manifest_run_id=manifest_run_id,
            action_index=action_index,
            action_kind=action_kind,
            local_entry_id=local_entry_id,
            source_id=source_id,
            source_version_id=source_version_id,
            source_tombstone_id=source_tombstone_id,
            safe_reason_code=safe_reason_code,
        )
    )


async def _expect_integrity_error(
    connection: sa.ext.asyncio.AsyncConnection,
    statement: object,
) -> IntegrityError:
    savepoint = await connection.begin_nested()
    try:
        with pytest.raises(IntegrityError) as captured:
            await connection.execute(statement)
    finally:
        await savepoint.rollback()
    return captured.value


def _constraint_name(error: IntegrityError) -> str | None:
    return getattr(error.orig, "diag", None).constraint_name


async def _assert_cursor_and_manifest_constraints(
    stack: SourcePublicationStack,
) -> UUID:
    """Upgrade-state assertions: cursor uniqueness, run bounds, action shapes."""

    engine = create_source_store_engine(stack.settings, stack.password)
    try:
        workspace = await seed_device_sync_workspace(engine)
        async with engine.begin() as connection:
            await _insert_cursor(connection, workspace)

            duplicate_cursor = await _expect_integrity_error(
                connection,
                sa.insert(device_cursors).values(
                    device_cursor_id=uuid4(),
                    workspace_id=workspace.workspace_id,
                    device_id=workspace.device_id,
                    acknowledged_sequence=0,
                    delivered_through_sequence=0,
                ),
            )
            assert _constraint_name(duplicate_cursor) == "uq_device_cursors_workspace_device"

            regressed_cursor = await _expect_integrity_error(
                connection,
                sa.insert(device_cursors).values(
                    device_cursor_id=uuid4(),
                    workspace_id=workspace.workspace_id,
                    device_id=uuid4(),
                    acknowledged_sequence=5,
                    delivered_through_sequence=3,
                ),
            )
            assert _constraint_name(regressed_cursor) == "ck_device_cursors_delivery"

            device_delete = await _expect_integrity_error(
                connection,
                sa.delete(devices).where(devices.c.device_id == workspace.device_id),
            )
            assert _constraint_name(device_delete) == "fk_device_cursors__device"

        async with engine.begin() as connection:
            unfinished_run_id = uuid4()
            completed_run_id = uuid4()
            completed_digest = _digest_text("device-sync-completed-run")
            await _insert_manifest_run(connection, workspace, manifest_run_id=unfinished_run_id)
            await _insert_manifest_run(
                connection,
                workspace,
                manifest_run_id=completed_run_id,
                state="completed",
                entry_count=2,
                final_digest=completed_digest,
                planned_at=sa.text("CURRENT_TIMESTAMP"),
                completed_at=sa.text("CURRENT_TIMESTAMP"),
            )

            competing_run = await _expect_integrity_error(
                connection,
                sa.insert(manifest_runs).values(
                    manifest_run_id=uuid4(),
                    workspace_id=workspace.workspace_id,
                    device_id=workspace.device_id,
                    base_acknowledged_sequence=0,
                    checkpoint_sequence=0,
                    policy_revision_number=1,
                    client_observation_generation=0,
                    state="planned",
                    final_digest=completed_digest,
                    planned_at=sa.text("CURRENT_TIMESTAMP"),
                ),
            )
            assert _constraint_name(competing_run) == "uq_manifest_runs_unfinished_device"

            invalid_state = await _expect_integrity_error(
                connection,
                sa.insert(manifest_runs).values(
                    manifest_run_id=uuid4(),
                    workspace_id=workspace.workspace_id,
                    device_id=uuid4(),
                    base_acknowledged_sequence=0,
                    checkpoint_sequence=0,
                    policy_revision_number=1,
                    client_observation_generation=0,
                    state="paused",
                ),
            )
            assert _constraint_name(invalid_state) == "ck_manifest_runs__state"

            oversized_run = await _expect_integrity_error(
                connection,
                sa.insert(manifest_runs).values(
                    manifest_run_id=uuid4(),
                    workspace_id=workspace.workspace_id,
                    device_id=uuid4(),
                    base_acknowledged_sequence=0,
                    checkpoint_sequence=0,
                    policy_revision_number=1,
                    client_observation_generation=0,
                    state="collecting",
                    entry_count=100001,
                ),
            )
            assert _constraint_name(oversized_run) == "ck_manifest_runs__entry_count"

            regressed_run = await _expect_integrity_error(
                connection,
                sa.insert(manifest_runs).values(
                    manifest_run_id=uuid4(),
                    workspace_id=workspace.workspace_id,
                    device_id=uuid4(),
                    base_acknowledged_sequence=4,
                    checkpoint_sequence=2,
                    policy_revision_number=1,
                    client_observation_generation=0,
                    state="collecting",
                ),
            )
            assert _constraint_name(regressed_run) == "ck_manifest_runs__sequences"

            # Every honest terminal shape is writable: a run failing during
            # collection carries no digest; a run failing or expiring after
            # planning retains its digest and planning time; an expired run
            # never carries an error code or completion time.
            failed_during_collection = uuid4()
            failed_after_planning = uuid4()
            expired_during_collection = uuid4()
            expired_after_planning = uuid4()
            failed_digest = _digest_text("device-sync-failed-run")
            for terminal_run_id, terminal_shape in (
                (failed_during_collection, dict(state="failed")),
                (
                    failed_after_planning,
                    dict(
                        state="failed",
                        final_digest=failed_digest,
                        planned_at=sa.text("CURRENT_TIMESTAMP"),
                    ),
                ),
                (expired_during_collection, dict(state="expired")),
                (
                    expired_after_planning,
                    dict(
                        state="expired",
                        final_digest=completed_digest,
                        planned_at=sa.text("CURRENT_TIMESTAMP"),
                    ),
                ),
            ):
                await _insert_manifest_run(
                    connection,
                    workspace,
                    manifest_run_id=terminal_run_id,
                    safe_error_code=(
                        "device_manifest_replay_mismatch"
                        if terminal_shape.get("state") == "failed"
                        else None
                    ),
                    **terminal_shape,
                )

            reasonless_failure = await _expect_integrity_error(
                connection,
                sa.insert(manifest_runs).values(
                    manifest_run_id=uuid4(),
                    workspace_id=workspace.workspace_id,
                    device_id=uuid4(),
                    base_acknowledged_sequence=0,
                    checkpoint_sequence=0,
                    policy_revision_number=1,
                    client_observation_generation=0,
                    state="failed",
                ),
            )
            assert _constraint_name(reasonless_failure) == "ck_manifest_runs__state_shape"

            expired_with_error = await _expect_integrity_error(
                connection,
                sa.insert(manifest_runs).values(
                    manifest_run_id=uuid4(),
                    workspace_id=workspace.workspace_id,
                    device_id=uuid4(),
                    base_acknowledged_sequence=0,
                    checkpoint_sequence=0,
                    policy_revision_number=1,
                    client_observation_generation=0,
                    state="expired",
                    safe_error_code="device_manifest_run_expired",
                ),
            )
            assert _constraint_name(expired_with_error) == "ck_manifest_runs__state_shape"

        async with engine.begin() as connection:
            await _insert_manifest_page(connection, unfinished_run_id)

            duplicate_page = await _expect_integrity_error(
                connection,
                sa.insert(manifest_pages).values(
                    manifest_run_id=unfinished_run_id,
                    page_number=0,
                    entry_count=1,
                    page_digest=_PAGE_DIGEST,
                ),
            )
            assert _constraint_name(duplicate_page) == "pk_manifest_pages"

            oversized_page = await _expect_integrity_error(
                connection,
                sa.insert(manifest_pages).values(
                    manifest_run_id=unfinished_run_id,
                    page_number=1,
                    entry_count=501,
                    page_digest=_PAGE_DIGEST,
                ),
            )
            assert _constraint_name(oversized_page) == "ck_manifest_pages__entry_count"

            uppercase_digest_page = await _expect_integrity_error(
                connection,
                sa.insert(manifest_pages).values(
                    manifest_run_id=unfinished_run_id,
                    page_number=2,
                    entry_count=1,
                    page_digest=_PAGE_DIGEST.upper(),
                ),
            )
            assert _constraint_name(uppercase_digest_page) == "ck_manifest_pages__page_digest"

            await _insert_entry_resolution(connection, unfinished_run_id)

            proven_unproven = await _expect_integrity_error(
                connection,
                sa.insert(manifest_entry_resolutions).values(
                    manifest_run_id=unfinished_run_id,
                    page_number=0,
                    entry_index=1,
                    local_entry_id="device-sync-entry-1",
                    submitted_sha256=_ENTRY_SHA256,
                    submitted_size_bytes=128,
                    submitted_media_type="text/markdown",
                    locator_evidence_digest=_LOCATOR_DIGEST,
                    resolved_source_id=uuid4(),
                    match_kind="unproven",
                ),
            )
            assert (
                _constraint_name(proven_unproven) == "ck_manifest_entry_resolutions__identity_shape"
            )

            unknown_match_kind = await _expect_integrity_error(
                connection,
                sa.insert(manifest_entry_resolutions).values(
                    manifest_run_id=unfinished_run_id,
                    page_number=0,
                    entry_index=1,
                    local_entry_id="device-sync-entry-1",
                    submitted_sha256=_ENTRY_SHA256,
                    submitted_size_bytes=128,
                    submitted_media_type="text/markdown",
                    locator_evidence_digest=_LOCATOR_DIGEST,
                    # A proven shape keeps the identity check satisfied so the
                    # closed match-kind vocabulary alone rejects the row.
                    resolved_source_id=uuid4(),
                    resolved_source_version_id=uuid4(),
                    match_kind="hash_only",
                ),
            )
            assert (
                _constraint_name(unknown_match_kind) == "ck_manifest_entry_resolutions__match_kind"
            )

        async with engine.begin() as connection:
            canonical_source_id = uuid4()
            canonical_version_id = uuid4()
            tombstone_id = uuid4()
            for action_index, action in enumerate(
                (
                    dict(
                        action_kind="upload",
                        local_entry_id="device-sync-entry-0",
                    ),
                    dict(
                        action_kind="download",
                        local_entry_id=None,
                        source_id=canonical_source_id,
                        source_version_id=canonical_version_id,
                    ),
                    dict(
                        action_kind="apply_tombstone",
                        local_entry_id="device-sync-entry-0",
                        source_id=canonical_source_id,
                        source_tombstone_id=tombstone_id,
                    ),
                    dict(
                        action_kind="conflict",
                        local_entry_id="device-sync-entry-0",
                        safe_reason_code="device_manifest_identity_ambiguous",
                    ),
                    dict(
                        action_kind="no_change",
                        local_entry_id="device-sync-entry-0",
                        source_id=canonical_source_id,
                        source_version_id=canonical_version_id,
                    ),
                    dict(
                        action_kind="excluded",
                        local_entry_id="device-sync-entry-0",
                        safe_reason_code="device_manifest_policy_excluded",
                    ),
                )
            ):
                await _insert_manifest_action(
                    connection, unfinished_run_id, action_index=action_index, **action
                )

            local_download = await _expect_integrity_error(
                connection,
                sa.insert(manifest_actions).values(
                    manifest_run_id=unfinished_run_id,
                    action_index=6,
                    action_kind="download",
                    local_entry_id="device-sync-entry-0",
                    source_id=canonical_source_id,
                    source_version_id=canonical_version_id,
                ),
            )
            assert _constraint_name(local_download) == "ck_manifest_actions__shape"

            reasonless_conflict = await _expect_integrity_error(
                connection,
                sa.insert(manifest_actions).values(
                    manifest_run_id=unfinished_run_id,
                    action_index=6,
                    action_kind="conflict",
                    local_entry_id="device-sync-entry-0",
                ),
            )
            assert _constraint_name(reasonless_conflict) == "ck_manifest_actions__shape"

            tombstoneless_apply = await _expect_integrity_error(
                connection,
                sa.insert(manifest_actions).values(
                    manifest_run_id=unfinished_run_id,
                    action_index=6,
                    action_kind="apply_tombstone",
                    local_entry_id="device-sync-entry-0",
                    source_id=canonical_source_id,
                ),
            )
            assert _constraint_name(tombstoneless_apply) == "ck_manifest_actions__shape"

            localless_upload = await _expect_integrity_error(
                connection,
                sa.insert(manifest_actions).values(
                    manifest_run_id=unfinished_run_id,
                    action_index=6,
                    action_kind="upload",
                    local_entry_id=None,
                ),
            )
            assert _constraint_name(localless_upload) == "ck_manifest_actions__shape"

            unknown_kind = await _expect_integrity_error(
                connection,
                sa.insert(manifest_actions).values(
                    manifest_run_id=unfinished_run_id,
                    action_index=6,
                    action_kind="replicate",
                    local_entry_id="device-sync-entry-0",
                ),
            )
            assert _constraint_name(unknown_kind) == "ck_manifest_actions__action_kind"

        async with engine.begin() as connection:
            await _insert_manifest_page(connection, completed_run_id, page_number=0)
            await _insert_manifest_page(connection, completed_run_id, page_number=1)
            await _insert_entry_resolution(connection, completed_run_id)
            await _insert_manifest_action(
                connection,
                completed_run_id,
                action_index=0,
                action_kind="no_change",
                local_entry_id="device-sync-completed-entry",
                source_id=canonical_source_id,
                source_version_id=canonical_version_id,
            )
            await connection.execute(
                sa.delete(manifest_runs).where(manifest_runs.c.manifest_run_id == completed_run_id)
            )
            for table in (manifest_pages, manifest_entry_resolutions, manifest_actions):
                remaining = await connection.execute(
                    sa.select(sa.func.count())
                    .select_from(table)
                    .where(table.c.manifest_run_id == completed_run_id)
                )
                assert int(remaining.scalar_one()) == 0, table.name
            surviving_cursor = await connection.execute(
                sa.select(sa.func.count()).select_from(device_cursors)
            )
            assert int(surviving_cursor.scalar_one()) == 1
        return workspace.workspace_id
    finally:
        await dispose_source_store_engine(engine)


async def _assert_predecessor_state(stack: SourcePublicationStack, workspace_id: UUID) -> None:
    """Downgrade-state assertions: the five tables are gone, ownership stays."""

    engine = create_source_store_engine(stack.settings, stack.password)
    try:
        async with engine.begin() as connection:
            table_count = await connection.execute(
                sa.text(
                    "SELECT count(*) FROM information_schema.tables"
                    " WHERE table_schema = 'knowledge'"
                )
            )
            assert int(table_count.scalar_one()) == 32
            device_sync_table_count = await connection.execute(
                sa.text(
                    "SELECT count(*) FROM information_schema.tables"
                    " WHERE table_schema = 'knowledge'"
                    " AND table_name IN ("
                    "'device_cursors', 'manifest_runs', 'manifest_pages',"
                    " 'manifest_entry_resolutions', 'manifest_actions')"
                )
            )
            assert int(device_sync_table_count.scalar_one()) == 0
            surviving_workspace = await connection.execute(
                sa.text(
                    "SELECT count(*) FROM knowledge.workspaces WHERE workspace_id = :workspace_id"
                ),
                {"workspace_id": str(workspace_id)},
            )
            assert int(surviving_workspace.scalar_one()) == 1
    finally:
        await dispose_source_store_engine(engine)


async def _assert_reupgraded_state(stack: SourcePublicationStack) -> None:
    """Second-upgrade assertions: the five tables exist again and are empty."""

    engine = create_source_store_engine(stack.settings, stack.password)
    try:
        async with engine.begin() as connection:
            table_count = await connection.execute(
                sa.text(
                    "SELECT count(*) FROM information_schema.tables"
                    " WHERE table_schema = 'knowledge'"
                )
            )
            assert int(table_count.scalar_one()) == 37
            cursor_count = await connection.execute(
                sa.select(sa.func.count()).select_from(device_cursors)
            )
            assert int(cursor_count.scalar_one()) == 0
    finally:
        await dispose_source_store_engine(engine)


def test_device_sync_revision_round_trips_cursor_and_manifest_constraints(
    source_publication_stack: SourcePublicationStack,
) -> None:
    """Upgrade from 20260820_01, enforce the DDL contract, then round-trip."""

    downgrade = _alembic("-x", "allow_destructive=true", "downgrade", _PREDECESSOR_REVISION)
    assert downgrade.returncode == 0
    upgrade = _alembic("upgrade", _REVISION)
    assert upgrade.returncode == 0
    workspace_id = asyncio.run(
        _assert_cursor_and_manifest_constraints(source_publication_stack),
        loop_factory=asyncio.SelectorEventLoop,
    )
    second_downgrade = _alembic("-x", "allow_destructive=true", "downgrade", _PREDECESSOR_REVISION)
    assert second_downgrade.returncode == 0
    asyncio.run(
        _assert_predecessor_state(source_publication_stack, workspace_id),
        loop_factory=asyncio.SelectorEventLoop,
    )
    second_upgrade = _alembic("upgrade", "head")
    assert second_upgrade.returncode == 0
    asyncio.run(
        _assert_reupgraded_state(source_publication_stack),
        loop_factory=asyncio.SelectorEventLoop,
    )
