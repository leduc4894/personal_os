"""Replay classification and hydration of committed publication results.

These tests pin the exact replay-shape classification table from the design
spec (section 8.9) verbatim, the hydration of a committed lookup row into the
canonical :class:`SourceVersionPublicationResult`, the containment rechecks
that reject impossible shapes as ``source_concurrency_invariant_failed``, and
that every preflight lookup statement stays schema-qualified and
parameter-bound.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from personal_os.error_contracts.codes import ErrorCode
from personal_os.object_storage import CanonicalMediaType, ContentDigest, ExpectedObject
from personal_os.sources.actors import ActorKind, SourceActor
from personal_os.sources.commands import (
    CreateSourceVersion,
    IdempotencyKey,
    SourceTitle,
    SourceType,
    UpdateSourceVersion,
)
from personal_os.sources.errors import SourcePublicationError
from personal_os.sources.results import PublicationOutcome
from postgresql_source_store.publication_store import (
    ReplayLookupRow,
    classify_replay,
    hydrate_replay_result,
    replay_lookup_by_event_statement,
    replay_lookup_by_key_statement,
)

_USER_ID = uuid4()
_WORKSPACE_ID = uuid4()
_SOURCE_ID = uuid4()
_EVENT_ID = uuid4()
_SOURCE_VERSION_ID = uuid4()
_BASE_VERSION_ID = uuid4()
_CONTENT_HASH = hashlib.sha256(b"replay-hydration-unit-bytes").hexdigest()
_COMMITTED_AT = datetime(2026, 8, 14, 12, 0, 0, tzinfo=UTC)


def _expected_object() -> ExpectedObject:
    return ExpectedObject(
        content_digest=ContentDigest.parse(_CONTENT_HASH),
        size_bytes=42,
        media_type=CanonicalMediaType.parse("text/markdown"),
    )


def _create_command() -> CreateSourceVersion:
    return CreateSourceVersion(
        workspace_id=_WORKSPACE_ID,
        source_id=_SOURCE_ID,
        event_id=_EVENT_ID,
        idempotency_key=IdempotencyKey("replay-create-unit-1"),
        source_type=SourceType.MARKDOWN,
        title=SourceTitle("Replay hydration note"),
        actor=SourceActor(ActorKind.USER, _USER_ID),
        expected_object=_expected_object(),
        client_timestamp=None,
    )


def _update_command() -> UpdateSourceVersion:
    return UpdateSourceVersion(
        workspace_id=_WORKSPACE_ID,
        source_id=_SOURCE_ID,
        event_id=_EVENT_ID,
        idempotency_key=IdempotencyKey("replay-update-unit-1"),
        base_version_id=_BASE_VERSION_ID,
        actor=SourceActor(ActorKind.USER, _USER_ID),
        expected_object=_expected_object(),
        client_timestamp=None,
    )


def _lookup_row(
    *,
    event_type: str = "create",
    base_version_id: UUID | None = None,
    committed_version_id: UUID | None = _SOURCE_VERSION_ID,
    source_version_id: UUID | None = _SOURCE_VERSION_ID,
    content_version: int | None = 1,
    content_hash: str | None = _CONTENT_HASH,
    event_sequence: int = 7,
    workspace_id: UUID = _WORKSPACE_ID,
    source_id: UUID = _SOURCE_ID,
    committed_at: datetime = _COMMITTED_AT,
) -> ReplayLookupRow:
    return ReplayLookupRow(
        workspace_id=workspace_id,
        source_id=source_id,
        event_id=_EVENT_ID,
        event_sequence=event_sequence,
        event_type=event_type,
        base_version_id=base_version_id,
        committed_version_id=committed_version_id,
        idempotency_key="replay-create-unit-1",
        request_fingerprint="a" * 64,
        committed_at=committed_at,
        source_version_id=source_version_id,
        content_version=content_version,
        content_hash=content_hash,
    )


# --- exact replay-shape classification (design section 8.9) -------------------


def test_create_with_null_base_and_committed_version_is_published() -> None:
    assert classify_replay("create", None, _SOURCE_VERSION_ID) is PublicationOutcome.PUBLISHED


def test_update_with_committed_equal_to_base_is_no_change() -> None:
    assert classify_replay("update", _BASE_VERSION_ID, _BASE_VERSION_ID) is (
        PublicationOutcome.NO_CHANGE
    )


def test_update_with_distinct_committed_version_is_published() -> None:
    assert classify_replay("update", _BASE_VERSION_ID, _SOURCE_VERSION_ID) is (
        PublicationOutcome.PUBLISHED
    )


@pytest.mark.parametrize(
    ("event_type", "base_version_id", "committed_id"),
    [
        ("create", _BASE_VERSION_ID, _SOURCE_VERSION_ID),
        ("create", None, None),
        ("update", None, _SOURCE_VERSION_ID),
        ("update", _BASE_VERSION_ID, None),
        ("update", None, None),
        ("rename", None, _SOURCE_VERSION_ID),
        ("delete", _BASE_VERSION_ID, _SOURCE_VERSION_ID),
    ],
)
def test_impossible_event_shapes_fail_the_concurrency_invariant(
    event_type: str, base_version_id: UUID | None, committed_id: UUID | None
) -> None:
    with pytest.raises(SourcePublicationError) as captured:
        classify_replay(event_type, base_version_id, committed_id)
    assert captured.value.error_code is ErrorCode.SOURCE_CONCURRENCY_INVARIANT_FAILED


# --- hydration of a committed lookup row ---------------------------------------


def test_hydrates_create_replay_with_canonical_event_sequence_and_time() -> None:
    result = hydrate_replay_result(_lookup_row(), _create_command())
    assert result.source_id == _SOURCE_ID
    assert result.source_version_id == _SOURCE_VERSION_ID
    assert result.content_version == 1
    assert result.event_id == _EVENT_ID
    assert result.event_sequence == 7
    assert result.content_digest.hexadecimal == _CONTENT_HASH
    assert result.outcome is PublicationOutcome.PUBLISHED
    assert result.committed_at == _COMMITTED_AT


def test_hydrates_no_change_update_replay_against_the_base_version() -> None:
    row = _lookup_row(
        event_type="update",
        base_version_id=_BASE_VERSION_ID,
        committed_version_id=_BASE_VERSION_ID,
        source_version_id=_BASE_VERSION_ID,
        content_version=3,
    )
    result = hydrate_replay_result(row, _update_command())
    assert result.outcome is PublicationOutcome.NO_CHANGE
    assert result.source_version_id == _BASE_VERSION_ID
    assert result.content_version == 3


def test_hydrates_changed_update_replay_with_the_new_version() -> None:
    row = _lookup_row(
        event_type="update",
        base_version_id=_BASE_VERSION_ID,
        committed_version_id=_SOURCE_VERSION_ID,
        content_version=2,
    )
    result = hydrate_replay_result(row, _update_command())
    assert result.outcome is PublicationOutcome.PUBLISHED
    assert result.source_version_id == _SOURCE_VERSION_ID


def test_hydration_rejects_rows_from_another_workspace() -> None:
    row = _lookup_row(workspace_id=uuid4())
    with pytest.raises(SourcePublicationError) as captured:
        hydrate_replay_result(row, _create_command())
    assert captured.value.error_code is ErrorCode.SOURCE_CONCURRENCY_INVARIANT_FAILED
    assert dict(captured.value.safe_details) == {"source_id": _SOURCE_ID}


def test_hydration_rejects_rows_from_another_source() -> None:
    row = _lookup_row(source_id=uuid4())
    with pytest.raises(SourcePublicationError) as captured:
        hydrate_replay_result(row, _create_command())
    assert captured.value.error_code is ErrorCode.SOURCE_CONCURRENCY_INVARIANT_FAILED


@pytest.mark.parametrize(
    "row",
    [
        _lookup_row(committed_version_id=None, source_version_id=None),
        _lookup_row(source_version_id=None),
        _lookup_row(content_version=None),
        _lookup_row(content_hash=None),
        _lookup_row(event_sequence=0),
        _lookup_row(content_version=0),
    ],
)
def test_hydration_rejects_missing_or_nonpositive_canonical_fields(row: ReplayLookupRow) -> None:
    with pytest.raises(SourcePublicationError) as captured:
        hydrate_replay_result(row, _create_command())
    assert captured.value.error_code is ErrorCode.SOURCE_CONCURRENCY_INVARIANT_FAILED


def test_hydration_rejects_a_naive_committed_timestamp() -> None:
    row = _lookup_row(committed_at=datetime(2026, 8, 14, 12, 0, 0))
    with pytest.raises(SourcePublicationError) as captured:
        hydrate_replay_result(row, _create_command())
    assert captured.value.error_code is ErrorCode.SOURCE_CONCURRENCY_INVARIANT_FAILED


def test_hydration_rejects_an_impossible_event_shape() -> None:
    row = _lookup_row(event_type="create", base_version_id=_BASE_VERSION_ID)
    with pytest.raises(SourcePublicationError) as captured:
        hydrate_replay_result(row, _create_command())
    assert captured.value.error_code is ErrorCode.SOURCE_CONCURRENCY_INVARIANT_FAILED
    assert dict(captured.value.safe_details) == {"source_id": _SOURCE_ID}


# --- schema-qualified, parameter-bound lookup statements -----------------------


def test_key_lookup_statement_is_schema_qualified_and_parameter_bound() -> None:
    statement = replay_lookup_by_key_statement(
        _WORKSPACE_ID, IdempotencyKey("replay-create-unit-1")
    )
    compiled = str(statement.compile())
    for qualified_name in (
        "knowledge.sync_events",
        "knowledge.source_versions",
        "knowledge.content_objects",
    ):
        assert qualified_name in compiled, qualified_name
    assert str(_WORKSPACE_ID) not in compiled
    assert "replay-create-unit-1" not in compiled
    assert ":workspace_id" in compiled and ":idempotency_key" in compiled


def test_event_lookup_statement_is_schema_qualified_and_parameter_bound() -> None:
    statement = replay_lookup_by_event_statement(_EVENT_ID)
    compiled = str(statement.compile())
    assert "knowledge.sync_events" in compiled
    assert str(_EVENT_ID) not in compiled
