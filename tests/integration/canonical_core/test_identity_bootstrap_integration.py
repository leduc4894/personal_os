"""Atomic identity bootstrap against a disposable PostgreSQL 18.4.

Every case runs against the real migrated baseline through the real async
engine and :class:`PostgresqlIdentityBootstrapStore`: the empty-state bootstrap
creates exactly one active user, workspace and bootstrap device plus one
``identity.bootstrap_completed`` audit row sharing the single transaction
timestamp; the exact replay returns the original ids and the stored workspace
creation timestamp without adding any row; and partial state, changed display
name and a revoked bootstrap device each conflict terminally without repair —
writing at most one standalone ``identity.bootstrap_rejected`` audit row (only
when a trusted workspace exists) or only the registered rejection event.

``audit_events`` rows are append-only by migration trigger and FK-RESTRICT
the workspaces they name, so the identity graph cannot be reset by deletion;
every case instead runs against its own pristine disposable database migrated
with the real Alembic baseline (the ``disposable_identity_database`` fixture).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
import pytest_asyncio
import sqlalchemy as sa
from tests.integration.canonical_core.conftest import (
    CanonicalCoreHarness,
    DisposableIdentityDatabase,
)

from personal_os.diagnostics.events import EventName
from personal_os.error_contracts.codes import ErrorCode
from personal_os.identity.contracts import (
    BootstrapIdentityOutcome,
    IdentityBootstrapError,
    validate_bootstrap_identity_command,
)
from postgresql_source_store.identity_bootstrap import (
    IDENTITY_BOOTSTRAP_AUDIT_ACTION,
    IDENTITY_REJECTION_AUDIT_ACTION,
    IDENTITY_REJECTION_REASON,
)
from postgresql_source_store.tables import audit_events, devices, users, workspaces

pytestmark = pytest.mark.local_stack

_AUDIT_RESULT_SUCCEEDED = "succeeded"
_AUDIT_RESULT_REJECTED = "rejected"
_AUDIT_ACTOR_KIND_SYSTEM = "system"
_AUDIT_TARGET_KIND_WORKSPACE = "workspace"


@pytest_asyncio.fixture
async def identity_harness(
    disposable_identity_database: DisposableIdentityDatabase,
) -> CanonicalCoreHarness:
    """The harness bound to this case's pristine disposable database."""
    return disposable_identity_database.harness


def _bootstrap_command(**overrides: str):
    values: dict[str, str] = {
        "username": "ci-owner",
        "user_display_name": "Canonical CI Owner",
        "workspace_key": "ci-workspace",
        "workspace_display_name": "Canonical CI Workspace",
        "device_name": "CI Bootstrap Device",
        "device_kind": "obsidian",
    }
    values.update(overrides)
    return validate_bootstrap_identity_command(**values)


async def _fetch_identity_graph(
    harness: CanonicalCoreHarness,
) -> tuple[list[Any], list[Any], list[Any]]:
    engine = harness.engine
    async with engine.connect() as connection:
        user_rows = (await connection.execute(sa.select(users))).mappings().all()
        workspace_rows = (await connection.execute(sa.select(workspaces))).mappings().all()
        device_rows = (await connection.execute(sa.select(devices))).mappings().all()
    return list(user_rows), list(workspace_rows), list(device_rows)


async def _fetch_audit_rows(harness: CanonicalCoreHarness, workspace_id: UUID) -> list[Any]:
    async with harness.engine.connect() as connection:
        rows = (
            await connection.execute(
                sa.select(audit_events).where(audit_events.c.workspace_id == workspace_id)
            )
        ).all()
    return list(rows)


async def _count_all_audit_rows(harness: CanonicalCoreHarness) -> int:
    async with harness.engine.connect() as connection:
        return int(
            (
                await connection.execute(sa.select(sa.func.count()).select_from(audit_events))
            ).scalar_one()
        )


async def _revoke_bootstrap_device(harness: CanonicalCoreHarness, device_id: UUID) -> None:
    async with harness.engine.begin() as connection:
        await connection.execute(
            sa.update(devices)
            .values(status="revoked", revoked_at=sa.text("CURRENT_TIMESTAMP"))
            .where(devices.c.device_id == device_id)
        )


def _assert_completed_audit_shape(audit_row: Any, workspace_id: UUID, context: Any) -> None:
    assert audit_row.action == IDENTITY_BOOTSTRAP_AUDIT_ACTION
    assert audit_row.actor_kind == _AUDIT_ACTOR_KIND_SYSTEM
    assert audit_row.actor_id is None
    assert audit_row.target_kind == _AUDIT_TARGET_KIND_WORKSPACE
    assert audit_row.target_id == workspace_id
    assert audit_row.request_id == context.request_id
    assert audit_row.client_request_id == context.client_request_id
    assert audit_row.trace_id == context.trace.trace_id.value
    assert audit_row.result == _AUDIT_RESULT_SUCCEEDED
    assert audit_row.reason_code is None


# --- empty-state create ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_bootstrap_creates_exact_graph_and_audit(
    identity_harness: CanonicalCoreHarness,
) -> None:
    command = _bootstrap_command()
    context = identity_harness.diagnostic_context()

    result = await identity_harness.identity_store.bootstrap(command, context)

    assert result.outcome is BootstrapIdentityOutcome.CREATED
    user_rows, workspace_rows, device_rows = await _fetch_identity_graph(identity_harness)
    assert len(user_rows) == 1
    assert len(workspace_rows) == 1
    assert len(device_rows) == 1

    user_row = user_rows[0]
    workspace_row = workspace_rows[0]
    device_row = device_rows[0]

    assert user_row["user_id"] == result.user_id
    assert user_row["username"] == "ci-owner"
    assert user_row["display_name"] == "Canonical CI Owner"
    assert user_row["status"] == "active"
    assert user_row["created_at"] == result.committed_at
    assert user_row["updated_at"] == result.committed_at

    assert workspace_row["workspace_id"] == result.workspace_id
    assert workspace_row["owner_user_id"] == result.user_id
    assert workspace_row["workspace_key"] == "ci-workspace"
    assert workspace_row["display_name"] == "Canonical CI Workspace"
    assert workspace_row["status"] == "active"
    assert workspace_row["created_at"] == result.committed_at

    assert device_row["device_id"] == result.device_id
    assert device_row["workspace_id"] == result.workspace_id
    assert device_row["user_id"] == result.user_id
    assert device_row["device_name"] == "CI Bootstrap Device"
    assert device_row["device_kind"] == "obsidian"
    assert device_row["status"] == "active"
    assert device_row["registered_at"] == result.committed_at
    assert device_row["last_seen_at"] is None
    assert device_row["revoked_at"] is None

    audit_rows = await _fetch_audit_rows(identity_harness, result.workspace_id)
    assert len(audit_rows) == 1
    _assert_completed_audit_shape(audit_rows[0], result.workspace_id, context)
    assert audit_rows[0].occurred_at == result.committed_at


# --- exact replay ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exact_bootstrap_replay_creates_no_row_and_returns_original_timestamp(
    identity_harness: CanonicalCoreHarness,
) -> None:
    command = _bootstrap_command()
    first = await identity_harness.identity_store.bootstrap(
        command, identity_harness.diagnostic_context()
    )
    assert first.outcome is BootstrapIdentityOutcome.CREATED

    second = await identity_harness.identity_store.bootstrap(
        command, identity_harness.diagnostic_context()
    )

    assert second.outcome is BootstrapIdentityOutcome.EXISTING
    assert second.user_id == first.user_id
    assert second.workspace_id == first.workspace_id
    assert second.device_id == first.device_id
    assert second.committed_at == first.committed_at

    user_rows, workspace_rows, device_rows = await _fetch_identity_graph(identity_harness)
    assert len(user_rows) == 1
    assert len(workspace_rows) == 1
    assert len(device_rows) == 1
    # No extra audit row: exactly the single completed bootstrap remains.
    audit_rows = await _fetch_audit_rows(identity_harness, first.workspace_id)
    assert len(audit_rows) == 1
    assert audit_rows[0].action == IDENTITY_BOOTSTRAP_AUDIT_ACTION
    # The replayed timestamp is the stored workspace creation timestamp.
    assert workspace_rows[0]["created_at"] == second.committed_at
    assert identity_harness.identity_diagnostics.of(EventName.IDENTITY_BOOTSTRAP_REJECTED) == []


# --- drift conflicts ---------------------------------------------------------------------


async def _assert_conflict(identity_harness: CanonicalCoreHarness, command: Any) -> None:
    with pytest.raises(IdentityBootstrapError) as captured:
        await identity_harness.identity_store.bootstrap(
            command, identity_harness.diagnostic_context()
        )
    assert captured.value.error_code is ErrorCode.IDENTITY_BOOTSTRAP_STATE_CONFLICT


@pytest.mark.asyncio
async def test_partial_identity_state_conflicts_without_repair(
    identity_harness: CanonicalCoreHarness,
) -> None:
    command = _bootstrap_command()
    seeded_user_id = await identity_harness.insert_bare_user("ci-owner", "Canonical CI Owner")
    audit_count_before = await _count_all_audit_rows(identity_harness)

    await _assert_conflict(identity_harness, command)

    user_rows, workspace_rows, device_rows = await _fetch_identity_graph(identity_harness)
    # No repair: the partial state survives untouched and nothing was created.
    assert [row["user_id"] for row in user_rows] == [seeded_user_id]
    assert workspace_rows == []
    assert device_rows == []
    assert await _count_all_audit_rows(identity_harness) == audit_count_before
    # No trusted workspace exists, so only the registered rejection event fired.
    rejection_events = identity_harness.identity_diagnostics.of(
        EventName.IDENTITY_BOOTSTRAP_REJECTED
    )
    assert len(rejection_events) == 1
    assert rejection_events[0] == {"error_code": ErrorCode.IDENTITY_BOOTSTRAP_STATE_CONFLICT}


@pytest.mark.asyncio
async def test_changed_display_name_conflicts(
    identity_harness: CanonicalCoreHarness,
) -> None:
    command = _bootstrap_command()
    first = await identity_harness.identity_store.bootstrap(
        command, identity_harness.diagnostic_context()
    )
    drifted = _bootstrap_command(user_display_name="Renamed CI Owner")

    await _assert_conflict(identity_harness, drifted)

    user_rows, workspace_rows, device_rows = await _fetch_identity_graph(identity_harness)
    assert user_rows[0]["user_id"] == first.user_id
    assert user_rows[0]["display_name"] == "Canonical CI Owner"
    assert workspace_rows[0]["workspace_id"] == first.workspace_id
    assert device_rows[0]["device_id"] == first.device_id
    # The trusted workspace exists, so exactly one standalone rejection audit
    # was written in its own short transaction.
    audit_rows = await _fetch_audit_rows(identity_harness, first.workspace_id)
    completed_audits = [row for row in audit_rows if row.action == IDENTITY_BOOTSTRAP_AUDIT_ACTION]
    rejection_audits = [row for row in audit_rows if row.action == IDENTITY_REJECTION_AUDIT_ACTION]
    assert len(completed_audits) == 1
    assert len(rejection_audits) == 1
    rejection_audit = rejection_audits[0]
    assert rejection_audit.workspace_id == first.workspace_id
    assert rejection_audit.target_id == first.workspace_id
    assert rejection_audit.result == _AUDIT_RESULT_REJECTED
    assert rejection_audit.reason_code == IDENTITY_REJECTION_REASON
    assert rejection_audit.actor_kind == _AUDIT_ACTOR_KIND_SYSTEM


@pytest.mark.asyncio
async def test_revoked_bootstrap_device_conflicts(
    identity_harness: CanonicalCoreHarness,
) -> None:
    command = _bootstrap_command()
    first = await identity_harness.identity_store.bootstrap(
        command, identity_harness.diagnostic_context()
    )
    await _revoke_bootstrap_device(identity_harness, first.device_id)

    await _assert_conflict(identity_harness, command)

    user_rows, workspace_rows, device_rows = await _fetch_identity_graph(identity_harness)
    # No repair: the device stays revoked and every identity keeps its value.
    assert device_rows[0]["device_id"] == first.device_id
    assert device_rows[0]["status"] == "revoked"
    assert device_rows[0]["revoked_at"] is not None
    assert user_rows[0]["user_id"] == first.user_id
    assert workspace_rows[0]["workspace_id"] == first.workspace_id
    audit_rows = await _fetch_audit_rows(identity_harness, first.workspace_id)
    rejection_audits = [row for row in audit_rows if row.action == IDENTITY_REJECTION_AUDIT_ACTION]
    assert len(rejection_audits) == 1
    assert rejection_audits[0].workspace_id == first.workspace_id
    assert rejection_audits[0].reason_code == IDENTITY_REJECTION_REASON
