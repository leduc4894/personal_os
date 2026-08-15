"""Identity bootstrap pure-helper contracts of the PostgreSQL adapter.

These tests pin the reserved bootstrap advisory-lock namespace and its signed
SHA-256 first-word derivation from the frozen ``username:workspace_key``
material, the hydration of mapped row shapes into the provider-neutral
:class:`ExistingIdentityState` (including the single-workspace device filter),
and the exact audit-row value dictionaries for the completed and rejected
bootstrap actions — all compiled or computed without touching a database.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from personal_os.identity.bootstrap import ExistingIdentityDevice
from personal_os.identity.contracts import BootstrapDeviceKind, BootstrapIdentityCommand
from postgresql_source_store.identity_bootstrap import (
    IDENTITY_BOOTSTRAP_AUDIT_ACTION,
    IDENTITY_BOOTSTRAP_LOCK_NAMESPACE,
    IDENTITY_REJECTION_AUDIT_ACTION,
    IDENTITY_REJECTION_REASON,
    bootstrap_lock_key,
    bootstrap_lock_statement,
    build_identity_audit_values,
    build_identity_rejection_audit_values,
    hydrate_identity_state,
)
from postgresql_source_store.locks import (
    IDEMPOTENCY_LOCK_NAMESPACE,
    SOURCE_LOCK_NAMESPACE,
)

USER_ID = UUID("018f47a0-7b00-7000-8000-000000000101")
WORKSPACE_ID = UUID("018f47a0-7b00-7000-8000-000000000102")
DEVICE_ID = UUID("018f47a0-7b00-7000-8000-000000000103")
REQUEST_ID = uuid4()
COMMITTED_AT = datetime(2026, 8, 15, 9, 0, 0, tzinfo=UTC)

_SIGNED_INT32_MIN = -(2**31)
_SIGNED_INT32_MAX = 2**31 - 1


def _frozen_signed_first_sha256_word(material: bytes) -> int:
    """The frozen derivation algorithm, re-implemented for the test."""
    return int.from_bytes(hashlib.sha256(material).digest()[:4], "big", signed=True)


def _build_command() -> BootstrapIdentityCommand:
    return BootstrapIdentityCommand(
        username="ductx",
        user_display_name="Duc Tran",
        workspace_key="personal",
        workspace_display_name="Personal Knowledge",
        device_name="Obsidian desktop",
        device_kind=BootstrapDeviceKind.OBSIDIAN,
    )


def _user_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "user_id": USER_ID,
        "username": "ductx",
        "display_name": "Duc Tran",
        "status": "active",
    }
    row.update(overrides)
    return row


def _workspace_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "workspace_id": WORKSPACE_ID,
        "owner_user_id": USER_ID,
        "workspace_key": "personal",
        "display_name": "Personal Knowledge",
        "status": "active",
        "created_at": COMMITTED_AT,
    }
    row.update(overrides)
    return row


def _device_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "device_id": DEVICE_ID,
        "workspace_id": WORKSPACE_ID,
        "user_id": USER_ID,
        "device_name": "Obsidian desktop",
        "device_kind": "obsidian",
        "status": "active",
        "revoked_at": None,
    }
    row.update(overrides)
    return row


# --- reserved bootstrap advisory-lock namespace and derivation -----------------


def test_bootstrap_lock_statement_uses_reserved_namespace_and_derived_key() -> None:
    command = _build_command()
    statement = bootstrap_lock_statement(command)
    assert isinstance(statement, sa.TextClause)
    assert str(statement) == "SELECT pg_advisory_xact_lock(:namespace, :derived_key)"
    compiled = statement.compile(dialect=postgresql.dialect())
    assert "pg_advisory_xact_lock" in str(compiled)
    assert compiled.params["namespace"] == IDENTITY_BOOTSTRAP_LOCK_NAMESPACE
    assert compiled.params["derived_key"] == bootstrap_lock_key(command)


def test_bootstrap_lock_namespace_is_reserved_and_distinct() -> None:
    # ``"SVCB"`` ASCII, pinned literally as a compatibility guard.
    assert IDENTITY_BOOTSTRAP_LOCK_NAMESPACE == 0x53564342
    assert IDENTITY_BOOTSTRAP_LOCK_NAMESPACE != IDEMPOTENCY_LOCK_NAMESPACE
    assert IDENTITY_BOOTSTRAP_LOCK_NAMESPACE != SOURCE_LOCK_NAMESPACE


def test_bootstrap_lock_key_derives_from_username_and_workspace_key_material() -> None:
    command = _build_command()
    expected = _frozen_signed_first_sha256_word(
        f"{command.username}:{command.workspace_key}".encode()
    )
    derived_key = bootstrap_lock_key(command)
    assert derived_key == expected
    assert _SIGNED_INT32_MIN <= derived_key <= _SIGNED_INT32_MAX
    assert bootstrap_lock_key(command) == bootstrap_lock_key(command)


def test_bootstrap_lock_key_separates_distinct_commands() -> None:
    command = _build_command()
    other_key = BootstrapIdentityCommand(
        username="ductx",
        user_display_name="Duc Tran",
        workspace_key="archive",
        workspace_display_name="Personal Knowledge",
        device_name="Obsidian desktop",
        device_kind=BootstrapDeviceKind.OBSIDIAN,
    )
    assert bootstrap_lock_key(command) != bootstrap_lock_key(other_key)


# --- provider-neutral state hydration from mapped row shapes -------------------


def test_hydrate_identity_state_builds_core_state_from_row_shapes() -> None:
    state = hydrate_identity_state(
        [_user_row()],
        [_workspace_row()],
        [_device_row(), _device_row(device_id=uuid4())],
    )
    assert len(state.users) == 1
    assert state.users[0].user_id == USER_ID
    assert state.users[0].username == "ductx"
    assert state.users[0].status == "active"
    assert len(state.workspaces) == 1
    assert state.workspaces[0].workspace_id == WORKSPACE_ID
    assert state.workspaces[0].owner_user_id == USER_ID
    assert state.workspaces[0].created_at == COMMITTED_AT
    assert len(state.devices) == 2


def test_hydrate_identity_state_filters_devices_to_the_single_workspace() -> None:
    foreign_workspace_id = uuid4()
    state = hydrate_identity_state(
        [_user_row()],
        [_workspace_row()],
        [
            _device_row(),
            _device_row(device_id=uuid4(), workspace_id=foreign_workspace_id),
        ],
    )
    assert len(state.workspaces) == 1
    assert len(state.devices) == 1
    assert state.devices[0].workspace_id == WORKSPACE_ID


def test_hydrate_identity_state_passes_all_devices_through_under_workspace_drift() -> None:
    # With zero or multiple workspaces no filter applies, so the classifier
    # sees the raw device cardinality and fails closed on the drift.
    foreign_workspace_id = uuid4()
    state = hydrate_identity_state(
        [_user_row()],
        [_workspace_row(), _workspace_row(workspace_id=foreign_workspace_id)],
        [
            _device_row(),
            _device_row(device_id=uuid4(), workspace_id=foreign_workspace_id),
        ],
    )
    assert len(state.workspaces) == 2
    assert len(state.devices) == 2


def test_hydrate_identity_state_carries_device_fields_without_mutation() -> None:
    revoked_at = datetime(2026, 8, 15, 8, 0, 0, tzinfo=UTC)
    device = hydrate_identity_state(
        [],
        [],
        [_device_row(status="revoked", revoked_at=revoked_at)],
    ).devices[0]
    assert isinstance(device, ExistingIdentityDevice)
    assert device.device_kind == "obsidian"
    assert device.status == "revoked"
    assert device.revoked_at == revoked_at


# --- audit-row value builders ---------------------------------------------------


def test_build_identity_audit_values_uses_completed_action_and_workspace_target() -> None:
    values = build_identity_audit_values(
        workspace_id=WORKSPACE_ID,
        request_id=REQUEST_ID,
        occurred_at=COMMITTED_AT,
    )
    assert values["action"] == IDENTITY_BOOTSTRAP_AUDIT_ACTION
    assert values["action"] == "identity.bootstrap_completed"
    assert values["actor_kind"] == "system"
    assert values["target_kind"] == "workspace"
    assert values["target_id"] == WORKSPACE_ID
    assert values["result"] == "succeeded"
    assert values["workspace_id"] == WORKSPACE_ID
    assert values["request_id"] == REQUEST_ID
    assert values["occurred_at"] == COMMITTED_AT
    assert values["reason_code"] is None
    assert values["safe_diff_hash"] is None
    assert values["audit_event_id"]


def test_build_identity_audit_values_carries_diagnostic_correlation_optionally() -> None:
    client_request_id = uuid4()
    values = build_identity_audit_values(
        workspace_id=WORKSPACE_ID,
        request_id=REQUEST_ID,
        occurred_at=COMMITTED_AT,
        client_request_id=client_request_id,
        trace_id="0123456789abcdef0123456789abcdef",
    )
    assert values["client_request_id"] == client_request_id
    assert values["trace_id"] == "0123456789abcdef0123456789abcdef"
    bare = build_identity_audit_values(
        workspace_id=WORKSPACE_ID,
        request_id=REQUEST_ID,
        occurred_at=COMMITTED_AT,
    )
    assert bare["client_request_id"] is None
    assert bare["trace_id"] is None


def test_build_identity_rejection_audit_values_uses_rejected_action_and_reason() -> None:
    values = build_identity_rejection_audit_values(
        workspace_id=WORKSPACE_ID,
        request_id=REQUEST_ID,
        occurred_at=COMMITTED_AT,
    )
    assert values["action"] == IDENTITY_REJECTION_AUDIT_ACTION
    assert values["action"] == "identity.bootstrap_rejected"
    assert values["actor_kind"] == "system"
    assert values["target_kind"] == "workspace"
    assert values["target_id"] == WORKSPACE_ID
    assert values["result"] == "rejected"
    assert values["reason_code"] == IDENTITY_REJECTION_REASON
    assert values["reason_code"] == "identity_state_conflict"
    assert values["safe_diff_hash"] is None
    assert values["request_id"] == REQUEST_ID
