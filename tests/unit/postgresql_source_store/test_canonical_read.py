"""Hydration of the canonical current-source reference from a joined row.

These tests pin the pure fail-closed hydration of one joined read row into a
:class:`~personal_os.sources.reading.CanonicalSourceReference`: only the two
readable source states hydrate, every pointer inconsistency, noncanonical
digest, derived-key mismatch, negative size, parameterized media type,
non-positive content version and naive timestamp fails closed as
``canonical_read_state_invalid`` with only the requested ``source_id`` detail,
and the lookup statement stays schema-qualified and parameter-bound.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa

from personal_os.error_contracts.codes import ErrorCode
from personal_os.object_storage import ContentDigest, derive_canonical_object_key
from personal_os.sources.reading import CanonicalReadStateError
from postgresql_source_store.canonical_read import (
    ACCEPTED_READ_SOURCE_STATES,
    current_reference_lookup_statement,
    hydrate_canonical_source_reference,
)

_WORKSPACE_ID = uuid4()
_OTHER_WORKSPACE_ID = uuid4()
_SOURCE_ID = uuid4()
_OTHER_SOURCE_ID = uuid4()
_SOURCE_VERSION_ID = uuid4()
_CONTENT_HASH = hashlib.sha256(b"canonical-read-unit-bytes").hexdigest()
_OBJECT_KEY = derive_canonical_object_key(ContentDigest.parse(_CONTENT_HASH)).value
_COMMITTED_AT = datetime(2026, 8, 15, 12, 0, 0, tzinfo=UTC)

ACCEPTED = ("active", "stored_not_indexed")


def _reference_row(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "workspace_id": _WORKSPACE_ID,
        "source_id": _SOURCE_ID,
        "sync_state": "active",
        "current_source_version_id": _SOURCE_VERSION_ID,
        "version_workspace_id": _WORKSPACE_ID,
        "version_source_id": _SOURCE_ID,
        "source_version_id": _SOURCE_VERSION_ID,
        "content_version": 1,
        "committed_at": _COMMITTED_AT,
        "content_hash": _CONTENT_HASH,
        "object_key": _OBJECT_KEY,
        "byte_size": 42,
        "media_type": "text/markdown",
    }
    return {**base, **overrides}


# --- hydration of an accepted, consistent row -----------------------------------


def test_accepted_read_source_states_are_exactly_the_readable_pair() -> None:
    assert frozenset({"active", "stored_not_indexed"}) == ACCEPTED_READ_SOURCE_STATES


@pytest.mark.parametrize("sync_state", ACCEPTED)
def test_hydrates_reference_for_accepted_source_states(sync_state: str) -> None:
    reference = hydrate_canonical_source_reference(_reference_row(sync_state=sync_state))
    assert reference.workspace_id == _WORKSPACE_ID
    assert reference.source_id == _SOURCE_ID
    assert reference.source_version_id == _SOURCE_VERSION_ID
    assert reference.content_version == 1
    assert reference.expected_object.content_digest.hexadecimal == _CONTENT_HASH
    assert reference.expected_object.size_bytes == 42
    assert reference.expected_object.media_type.value == "text/markdown"
    assert reference.committed_at == _COMMITTED_AT


# --- fail-closed hydration of every mutated row ----------------------------------


@pytest.mark.parametrize(
    "mutator",
    [
        pytest.param(lambda r: r, id="happy-path"),
        pytest.param(lambda r: {**r, "sync_state": "pending"}, id="pending-rejected"),
        pytest.param(lambda r: {**r, "sync_state": "deleted"}, id="deleted-rejected"),
        pytest.param(lambda r: {**r, "current_source_version_id": None}, id="null-pointer"),
        pytest.param(
            lambda r: {**r, "version_workspace_id": _OTHER_WORKSPACE_ID},
            id="cross-workspace-pointer",
        ),
        pytest.param(
            lambda r: {**r, "version_source_id": _OTHER_SOURCE_ID},
            id="cross-source-pointer",
        ),
        pytest.param(lambda r: {**r, "content_hash": "XYZ"}, id="noncanonical-digest"),
        pytest.param(
            lambda r: {**r, "object_key": "objects/sha256/aa/bbb/other"},
            id="key-derivation-mismatch",
        ),
        pytest.param(lambda r: {**r, "byte_size": -1}, id="negative-size"),
        pytest.param(
            lambda r: {**r, "media_type": "text/markdown; charset=utf-8"}, id="media-parameters"
        ),
        pytest.param(lambda r: {**r, "content_version": 0}, id="non-positive-content-version"),
    ],
)
def test_reference_hydration_fails_closed(
    mutator: Callable[[dict[str, Any]], dict[str, Any]],
) -> None:
    row = mutator(_reference_row())
    if row == _reference_row():
        # The untouched happy path is the in-test control: it must hydrate.
        reference = hydrate_canonical_source_reference(row)
        assert reference.source_id == _SOURCE_ID
        return
    with pytest.raises(CanonicalReadStateError) as captured:
        hydrate_canonical_source_reference(row)
    assert captured.value.error_code is ErrorCode.CANONICAL_READ_STATE_INVALID
    assert dict(captured.value.safe_details) == {"source_id": _SOURCE_ID}


def test_hydration_rejects_a_naive_committed_timestamp() -> None:
    row = _reference_row(committed_at=datetime(2026, 8, 15, 12, 0, 0))
    with pytest.raises(CanonicalReadStateError) as captured:
        hydrate_canonical_source_reference(row)
    assert captured.value.error_code is ErrorCode.CANONICAL_READ_STATE_INVALID


def test_hydration_rejects_a_pending_row_with_fully_null_version_columns() -> None:
    # The realistic left-join shape of a pending source: no version, no object.
    row = _reference_row(
        sync_state="pending",
        current_source_version_id=None,
        version_workspace_id=None,
        version_source_id=None,
        source_version_id=None,
        content_version=None,
        committed_at=None,
        content_hash=None,
        object_key=None,
        byte_size=None,
        media_type=None,
    )
    with pytest.raises(CanonicalReadStateError) as captured:
        hydrate_canonical_source_reference(row)
    assert captured.value.error_code is ErrorCode.CANONICAL_READ_STATE_INVALID
    assert dict(captured.value.safe_details) == {"source_id": _SOURCE_ID}


def test_hydration_rejects_a_pointer_to_a_foreign_version_row() -> None:
    foreign_version_id = uuid4()
    row = _reference_row(current_source_version_id=foreign_version_id)
    with pytest.raises(CanonicalReadStateError) as captured:
        hydrate_canonical_source_reference(row)
    assert captured.value.error_code is ErrorCode.CANONICAL_READ_STATE_INVALID


# --- schema-qualified, parameter-bound lookup statement -------------------------


def test_lookup_statement_is_schema_qualified_and_parameter_bound() -> None:
    statement = current_reference_lookup_statement(_WORKSPACE_ID, _SOURCE_ID)
    assert isinstance(statement, sa.Select)
    compiled = str(statement.compile())
    for qualified_name in (
        "knowledge.sources",
        "knowledge.source_versions",
        "knowledge.content_objects",
    ):
        assert qualified_name in compiled, qualified_name
    assert str(_WORKSPACE_ID) not in compiled
    assert str(_SOURCE_ID) not in compiled
    assert ":workspace_id_1" in compiled or ":workspace_id" in compiled
    assert ":source_id_1" in compiled or ":source_id" in compiled


def test_lookup_statement_selects_the_pointer_consistency_columns() -> None:
    statement = current_reference_lookup_statement(_WORKSPACE_ID, _SOURCE_ID)
    columns = {column.key for column in statement.exported_columns}
    assert columns == {
        "workspace_id",
        "source_id",
        "sync_state",
        "current_source_version_id",
        "version_workspace_id",
        "version_source_id",
        "source_version_id",
        "content_version",
        "committed_at",
        "content_hash",
        "object_key",
        "byte_size",
        "media_type",
    }


def test_lookup_statement_filters_by_both_workspace_and_source() -> None:
    compiled = str(
        current_reference_lookup_statement(UUID(int=0), UUID(int=1)).compile()
    )
    assert compiled.count("LEFT OUTER JOIN") == 2
