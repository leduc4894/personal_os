"""Statement shape and fail-closed hydration of the conflict store.

These tests pin the pure, database-free halves of
:class:`postgresql_source_store.conflict_store.PostgresqlSourceConflictStore`:
every lookup is schema-qualified and parameter-bound, the resolution read
locks the row, the open listing pages in stable conflict-identity order, the
request fingerprints are deterministic sha256 digests over the command's
opaque identity, and hydration of a stored row into
:class:`~personal_os.source_conflicts.contracts.SourceConflict` fails closed
on any shape a canonical row may never carry. No connection is opened.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa

from personal_os.error_contracts.codes import ErrorCode
from personal_os.source_conflicts.commands import (
    CaptureConflictCommand,
    ResolveConflictCommand,
)
from personal_os.source_conflicts.contracts import (
    ConflictCandidate,
    ConflictIdempotencyKey,
    ConflictKind,
    ConflictResolutionKind,
    ConflictStatus,
)
from personal_os.source_conflicts.errors import SourceConflictError
from personal_os.source_locators import NormalizedLocator
from postgresql_source_store.conflict_store import (
    MAX_OPEN_CONFLICT_PAGE,
    capture_replay_by_key_statement,
    captured_conflict_by_event_statement,
    compute_capture_request_fingerprint,
    compute_resolution_request_fingerprint,
    conflict_read_statement,
    hydrate_source_conflict,
    list_open_conflicts_statement,
)
from postgresql_source_store.locks import (
    IDEMPOTENCY_LOCK_NAMESPACE,
    conflict_idempotency_lock_key,
    signed_first_sha256_word,
)

_WORKSPACE_ID = uuid4()
_OTHER_WORKSPACE_ID = uuid4()
_SOURCE_ID = uuid4()
_CONFLICT_ID = uuid4()
_ORIGINATING_EVENT_ID = uuid4()
_ORIGINATING_DEVICE_ID = uuid4()
_BASE_VERSION_ID = uuid4()
_REMOTE_VERSION_ID = uuid4()
_CANDIDATE_OBJECT_ID = uuid4()
_CAPTURE_KEY = ConflictIdempotencyKey("0b6c8f1e-2d3a-4f5b-8a7c-9d0e1f2a3b4c")
_RESOLUTION_KEY = ConflictIdempotencyKey("1c7d9e2f-3e4b-4f5b-8a7c-9d0e1f2a3b4d")
_CAPTURED_AT = datetime(2026, 9, 2, 10, 0, 0, tzinfo=UTC)


def _capture_command(**overrides: Any) -> CaptureConflictCommand:
    values: dict[str, Any] = {
        "workspace_id": _WORKSPACE_ID,
        "source_id": _SOURCE_ID,
        "conflict_kind": ConflictKind.STALE_CONTENT,
        "originating_event_id": _ORIGINATING_EVENT_ID,
        "originating_device_id": _ORIGINATING_DEVICE_ID,
        "idempotency_key": _CAPTURE_KEY,
        "base_version_id": _BASE_VERSION_ID,
        "observed_remote_version_id": _REMOTE_VERSION_ID,
        "candidate": ConflictCandidate.content(_CANDIDATE_OBJECT_ID),
        "normalized_locator": None,
    }
    return CaptureConflictCommand(**{**values, **overrides})


def _resolve_command(**overrides: Any) -> ResolveConflictCommand:
    values: dict[str, Any] = {
        "conflict_id": _CONFLICT_ID,
        "reviewed_remote_version_id": _REMOTE_VERSION_ID,
        "resolution_kind": ConflictResolutionKind.KEEP_LOCAL,
        "resolution_event_id": uuid4(),
        "idempotency_key": _RESOLUTION_KEY,
        "verified_candidate_object_id": None,
    }
    return ResolveConflictCommand(**{**values, **overrides})


def _stored_row(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "conflict_id": _CONFLICT_ID,
        "workspace_id": _WORKSPACE_ID,
        "source_id": _SOURCE_ID,
        "conflict_kind": "stale_content",
        "status": "open",
        "originating_event_id": _ORIGINATING_EVENT_ID,
        "originating_device_id": _ORIGINATING_DEVICE_ID,
        "capture_idempotency_key": _CAPTURE_KEY.value,
        "base_version_id": _BASE_VERSION_ID,
        "observed_remote_version_id": _REMOTE_VERSION_ID,
        "candidate_kind": "content",
        "verified_candidate_object_id": _CANDIDATE_OBJECT_ID,
        "normalized_locator": None,
        "resolution_kind": None,
        "resolution_event_id": None,
        "resolution_idempotency_key": None,
        "resulting_version_id": None,
        "successor_conflict_id": None,
        "captured_at": _CAPTURED_AT,
        "closed_at": None,
    }
    return {**base, **overrides}


# --- hydration -----------------------------------------------------------------


def test_hydrates_an_open_conflict_row() -> None:
    conflict = hydrate_source_conflict(_stored_row())
    assert conflict.conflict_id == _CONFLICT_ID
    assert conflict.workspace_id == _WORKSPACE_ID
    assert conflict.source_id == _SOURCE_ID
    assert conflict.conflict_kind is ConflictKind.STALE_CONTENT
    assert conflict.status is ConflictStatus.OPEN
    assert conflict.candidate.candidate_kind.value == "content"
    assert conflict.candidate.verified_candidate_object_id == _CANDIDATE_OBJECT_ID
    assert conflict.captured_at == _CAPTURED_AT


def test_hydrates_a_locator_collision_row_without_a_source() -> None:
    conflict = hydrate_source_conflict(
        _stored_row(
            source_id=None,
            conflict_kind="locator_collision",
            base_version_id=None,
            observed_remote_version_id=None,
            candidate_kind="delete",
            verified_candidate_object_id=None,
            normalized_locator="notes/collision.md",
        )
    )
    assert conflict.source_id is None
    assert conflict.conflict_kind is ConflictKind.LOCATOR_COLLISION


def test_hydration_fails_closed_on_any_noncanonical_shape() -> None:
    for mutator in (
        lambda row: {**row, "conflict_kind": "mystery_kind"},
        lambda row: {**row, "status": "mystery_status"},
        lambda row: {**row, "candidate_kind": "mystery_candidate"},
        lambda row: {**row, "candidate_kind": "delete", "verified_candidate_object_id": None},
        lambda row: {**row, "captured_at": datetime(2026, 9, 2, 10, 0, 0)},
        lambda row: {**row, "closed_at": "not-a-timestamp"},
        lambda row: {**row, "status": "resolved", "resolution_kind": "keep_remote"},
        lambda row: {**row, "conflict_kind": "delete_remote_edit"},
    ):
        with pytest.raises(SourceConflictError) as captured:
            hydrate_source_conflict(mutator(_stored_row()))
        assert captured.value.error_code is ErrorCode.SOURCE_CONFLICT_STATE_INVALID
        assert not dict(captured.value.safe_details)


# --- schema-qualified, parameter-bound statements -------------------------------


def test_capture_replay_statement_is_schema_qualified_and_parameter_bound() -> None:
    statement = capture_replay_by_key_statement(_WORKSPACE_ID, _CAPTURE_KEY)
    assert isinstance(statement, sa.Select)
    compiled = str(statement.compile())
    assert "knowledge.source_conflicts" in compiled
    assert str(_WORKSPACE_ID) not in compiled
    assert _CAPTURE_KEY.value not in compiled
    assert "capture_idempotency_key" in compiled


def test_captured_conflict_by_event_statement_is_parameter_bound() -> None:
    statement = captured_conflict_by_event_statement(_ORIGINATING_EVENT_ID, _WORKSPACE_ID)
    compiled = str(statement.compile())
    assert "knowledge.source_conflicts" in compiled
    assert str(_ORIGINATING_EVENT_ID) not in compiled
    assert str(_WORKSPACE_ID) not in compiled
    assert "originating_event_id" in compiled


def test_conflict_read_statement_scopes_to_the_workspace() -> None:
    statement = conflict_read_statement(_CONFLICT_ID, _WORKSPACE_ID)
    compiled = str(statement.compile())
    assert "knowledge.source_conflicts" in compiled
    assert "workspace_id" in compiled
    assert str(_CONFLICT_ID) not in compiled
    assert "FOR UPDATE" not in compiled


def test_conflict_read_statement_locks_when_read_for_resolution() -> None:
    statement = conflict_read_statement(_CONFLICT_ID, _WORKSPACE_ID, for_update=True)
    assert "FOR UPDATE" in str(statement.compile())


def test_list_open_statement_pages_in_conflict_identity_order() -> None:
    statement = list_open_conflicts_statement(
        _WORKSPACE_ID, limit=25, exclusive_start_conflict_id=_CONFLICT_ID
    )
    compiled = str(statement.compile())
    assert "knowledge.source_conflicts" in compiled
    assert "status" in compiled
    assert "conflict_id ASC" in compiled
    assert "LIMIT" in compiled
    assert str(_CONFLICT_ID) not in compiled
    columns = {column.key for column in statement.exported_columns}
    assert "conflict_id" in columns
    assert "capture_idempotency_key" in columns


@pytest.mark.parametrize("limit", [0, -1, MAX_OPEN_CONFLICT_PAGE + 1])
def test_list_open_statement_rejects_out_of_bound_limits(limit: int) -> None:
    with pytest.raises(ValueError, match="limit"):
        list_open_conflicts_statement(_WORKSPACE_ID, limit=limit, exclusive_start_conflict_id=None)


def test_list_open_statement_accepts_a_null_exclusive_start() -> None:
    statement = list_open_conflicts_statement(
        _WORKSPACE_ID, limit=25, exclusive_start_conflict_id=None
    )
    compiled = str(statement.compile())
    assert "conflict_id >" not in compiled


# --- request fingerprints --------------------------------------------------------


def test_capture_fingerprint_is_a_deterministic_sha256_over_the_command_identity() -> None:
    first = compute_capture_request_fingerprint(_capture_command())
    second = compute_capture_request_fingerprint(_capture_command())
    assert first == second
    assert len(first) == 64
    assert first == first.lower()
    different_kind = compute_capture_request_fingerprint(
        _capture_command(conflict_kind=ConflictKind.EDIT_REMOTE_DELETE)
    )
    assert different_kind != first
    different_candidate = compute_capture_request_fingerprint(
        _capture_command(candidate=ConflictCandidate.content(uuid4()))
    )
    assert different_candidate != first
    different_locator = compute_capture_request_fingerprint(
        _capture_command(
            conflict_kind=ConflictKind.LOCATOR_COLLISION,
            normalized_locator=NormalizedLocator("notes/collision.md"),
        )
    )
    assert different_locator != first


def test_resolution_fingerprint_is_deterministic_and_identity_bound() -> None:
    command = _resolve_command()
    first = compute_resolution_request_fingerprint(command)
    assert first == compute_resolution_request_fingerprint(command)
    different_event = compute_resolution_request_fingerprint(
        _resolve_command(resolution_event_id=uuid4())
    )
    assert different_event != first
    merged = compute_resolution_request_fingerprint(
        _resolve_command(
            resolution_kind=ConflictResolutionKind.SAVE_MERGED,
            verified_candidate_object_id=uuid4(),
        )
    )
    assert merged != first


# --- advisory lock derivation -----------------------------------------------------


def test_conflict_idempotency_lock_key_uses_the_shared_derivation() -> None:
    derived = conflict_idempotency_lock_key(_WORKSPACE_ID, _CAPTURE_KEY)
    material = _WORKSPACE_ID.bytes + b"\x00" + _CAPTURE_KEY.value.encode("ascii")
    assert derived == signed_first_sha256_word(material)


def test_conflict_idempotency_lock_uses_the_idempotency_namespace() -> None:
    from postgresql_source_store.locks import conflict_idempotency_lock_statement

    statement = conflict_idempotency_lock_statement(_WORKSPACE_ID, _CAPTURE_KEY)
    compiled = str(statement.compile())
    assert "pg_advisory_xact_lock" in compiled
    assert ":namespace" in compiled
    parameters = dict(statement.compile().params)
    assert parameters["namespace"] == IDEMPOTENCY_LOCK_NAMESPACE
    assert parameters["derived_key"] == conflict_idempotency_lock_key(_WORKSPACE_ID, _CAPTURE_KEY)
