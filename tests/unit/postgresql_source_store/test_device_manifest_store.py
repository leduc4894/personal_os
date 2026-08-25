"""Manifest store statement scope, state-machine seams and the final digest.

These tests pin the pure pieces of the PostgreSQL manifest adapter without a
database: every statement is parameter-bound and credential-scoped (no
literal workspace, device, run, page or checkpoint value appears in compiled
SQL), the run ownership read locks exactly one workspace/device/run row, the
terminal run transitions guard on the exact prior state, the completion
cursor advance is monotonic and raises only the acknowledged watermark, the
identity candidate reads are checkpoint-bounded over locator/tombstone
history, the canonical-only download read excludes the run's resolved
sources through one array-typed parameter whose statement parameter count
never grows with the run size (the 65,535 extended-protocol ceiling holds
even at the schema's full 100,000 resolved ids), the finalize-time id
lookups merge their chunked rows without loss, and the canonical-JSON final
digest over the run's ordered pages is deterministic and pinned to golden
vectors. Durable transaction behavior is integration territory (the
disposable stack suite).
"""

from __future__ import annotations

import re
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncEngine

from postgresql_source_store.device_manifest_store import (
    MANIFEST_ID_LOOKUP_CHUNK_SIZE,
    PostgresqlDeviceManifestStore,
    chunk_id_lookups,
    compute_manifest_final_digest,
    device_cursor_completion_advance_statement,
    device_cursor_completion_bootstrap_statement,
    manifest_canonical_only_downloads_statement,
    manifest_canonical_source_state_statement,
    manifest_locator_candidates_statement,
    manifest_page_select_statement,
    manifest_run_applying_transition_statement,
    manifest_run_completion_transition_statement,
    manifest_run_expire_statement,
    manifest_run_fail_statement,
    manifest_run_planned_statement,
    manifest_run_select_statement,
    manifest_tombstone_candidates_statement,
    manifest_unfinished_run_select_statement,
    workspace_active_policy_revision_statement,
)

_WORKSPACE_ID = UUID("018f47a0-7b00-7000-8000-000000000001")
_DEVICE_ID = UUID("018f47a0-7b00-7000-8000-000000000002")
_MANIFEST_RUN_ID = UUID("018f47a0-7b00-7000-8000-000000000003")
_SOURCE_ID = UUID("018f47a0-7b00-7000-8000-000000000004")
_SECOND_SOURCE_ID = UUID("018f47a0-7b00-7000-8000-000000000005")
_DEVICE_CURSOR_ID = UUID("018f47a0-7b00-7000-8000-000000000006")

_PAGE_DIGEST = "a" * 64
_FINAL_DIGEST = "b" * 64
_SAFE_ERROR_CODE = "device_manifest_policy_advanced"

_CHECKPOINT_SEQUENCE = 900


def _bind_marker(text: str, parameter: str) -> bool:
    """Check whether a parameter-bound marker is in the SQL text."""

    if f"%({parameter})s" in text:
        return True
    return any(
        marker in text
        for marker in (
            f"%({parameter}_1)s",
            f"%({parameter}_2)s",
            f"%({parameter}_3)s",
        )
    )


# --- run ownership and lifecycle ------------------------------------------------


def test_run_select_statement_locks_the_exact_credential_scoped_row() -> None:
    locked_text = str(
        manifest_run_select_statement(
            _WORKSPACE_ID, _DEVICE_ID, _MANIFEST_RUN_ID, for_update=True
        ).compile(dialect=postgresql.dialect())
    )
    assert _bind_marker(locked_text, "workspace_id")
    assert _bind_marker(locked_text, "device_id")
    assert _bind_marker(locked_text, "manifest_run_id")
    assert "FOR UPDATE" in locked_text
    assert str(_WORKSPACE_ID) not in locked_text
    assert str(_DEVICE_ID) not in locked_text
    assert str(_MANIFEST_RUN_ID) not in locked_text
    unlocked_text = str(
        manifest_run_select_statement(
            _WORKSPACE_ID, _DEVICE_ID, _MANIFEST_RUN_ID
        ).compile(dialect=postgresql.dialect())
    )
    assert "FOR UPDATE" not in unlocked_text


def test_unfinished_run_select_statement_covers_the_unfinished_vocabulary() -> None:
    statement = manifest_unfinished_run_select_statement(_WORKSPACE_ID, _DEVICE_ID)
    text = str(statement.compile(dialect=postgresql.dialect()))
    assert _bind_marker(text, "workspace_id")
    assert _bind_marker(text, "device_id")
    for state in ("collecting", "planned", "applying"):
        assert f"'{state}'" in text
    assert str(_WORKSPACE_ID) not in text


def test_fail_statement_is_guarded_on_the_exact_run() -> None:
    text = str(
        manifest_run_fail_statement(_MANIFEST_RUN_ID, _SAFE_ERROR_CODE).compile(
            dialect=postgresql.dialect()
        )
    )
    assert "UPDATE knowledge.manifest_runs" in text
    assert "'failed'" in text
    assert _bind_marker(text, "safe_error_code")
    assert _bind_marker(text, "manifest_run_id")
    assert "completed_at" not in text


def test_expire_statement_changes_only_the_state_column() -> None:
    text = str(
        manifest_run_expire_statement(_MANIFEST_RUN_ID).compile(dialect=postgresql.dialect())
    )
    assert "UPDATE knowledge.manifest_runs" in text
    assert "'expired'" in text
    values_clause = text.split("WHERE", 1)[0]
    assert "final_digest" not in values_clause
    assert "safe_error_code" not in values_clause
    assert "completed_at" not in values_clause


def test_planned_statement_writes_planning_evidence_only() -> None:
    text = str(
        manifest_run_planned_statement(_MANIFEST_RUN_ID, _FINAL_DIGEST).compile(
            dialect=postgresql.dialect()
        )
    )
    assert "UPDATE knowledge.manifest_runs" in text
    assert "'planned'" in text
    assert _bind_marker(text, "final_digest")
    assert "planned_at" in text
    assert "'collecting'" in text  # guarded on the exact collecting state


def test_applying_transition_is_guarded_on_the_exact_planned_state() -> None:
    text = str(
        manifest_run_applying_transition_statement(_MANIFEST_RUN_ID).compile(
            dialect=postgresql.dialect()
        )
    )
    assert "UPDATE knowledge.manifest_runs" in text
    assert "'applying'" in text
    assert "'planned'" in text


def test_completion_transition_is_guarded_on_the_exact_applying_state() -> None:
    text = str(
        manifest_run_completion_transition_statement(_MANIFEST_RUN_ID).compile(
            dialect=postgresql.dialect()
        )
    )
    assert "UPDATE knowledge.manifest_runs" in text
    assert "'completed'" in text
    assert "'applying'" in text
    assert "completed_at" in text


def test_page_select_statement_is_keyed_by_run_and_page() -> None:
    text = str(
        manifest_page_select_statement(_MANIFEST_RUN_ID, 4).compile(
            dialect=postgresql.dialect()
        )
    )
    assert "FROM knowledge.manifest_pages" in text
    assert _bind_marker(text, "manifest_run_id")
    assert _bind_marker(text, "page_number")


# --- completion cursor fence ------------------------------------------------------


def test_completion_bootstrap_conflict_tolerantly_seeds_the_checkpoint() -> None:
    text = str(
        device_cursor_completion_bootstrap_statement(
            device_cursor_id=_DEVICE_CURSOR_ID,
            workspace_id=_WORKSPACE_ID,
            device_id=_DEVICE_ID,
            checkpoint_sequence=_CHECKPOINT_SEQUENCE,
        ).compile(dialect=postgresql.dialect())
    )
    assert "INSERT INTO knowledge.device_cursors" in text
    assert "ON CONFLICT (workspace_id, device_id) DO NOTHING" in text
    assert _bind_marker(text, "acknowledged_sequence")
    assert _bind_marker(text, "delivered_through_sequence")


def test_completion_advance_is_monotonic_and_never_lowers_the_delivered_watermark() -> None:
    text = str(
        device_cursor_completion_advance_statement(
            _WORKSPACE_ID, _DEVICE_ID, checkpoint_sequence=_CHECKPOINT_SEQUENCE
        ).compile(dialect=postgresql.dialect())
    )
    assert "UPDATE knowledge.device_cursors" in text
    assert "greatest" in text.lower()
    assert "acknowledged_sequence <" in text
    assert _bind_marker(text, "checkpoint_sequence")
    assert _bind_marker(text, "workspace_id")
    assert _bind_marker(text, "device_id")


# --- policy recheck and identity candidate reads -----------------------------------


def test_active_policy_revision_statement_reads_the_workspace_pointer() -> None:
    text = str(
        workspace_active_policy_revision_statement(_WORKSPACE_ID).compile(
            dialect=postgresql.dialect()
        )
    )
    assert "FROM knowledge.workspace_policy_state" in text
    assert "active_revision_number" in text
    assert "active_policy_revision_id" in text
    assert _bind_marker(text, "workspace_id")
    assert str(_WORKSPACE_ID) not in text


def test_locator_candidates_statement_is_checkpoint_bounded_and_ordered() -> None:
    statement = manifest_locator_candidates_statement(
        _WORKSPACE_ID,
        _CHECKPOINT_SEQUENCE,
        ("notes/alpha.md", "notes/beta.md"),
    )
    text = str(statement.compile(dialect=postgresql.dialect()))
    assert "FROM knowledge.source_locators" in text
    assert _bind_marker(text, "workspace_id")
    assert _bind_marker(text, "checkpoint_sequence")
    assert "opened_sequence <=" in text
    assert "ORDER BY knowledge.source_locators.normalized_locator ASC" in text
    assert "notes/alpha.md" not in text


def test_tombstone_candidates_statement_matches_retained_locators_only_open() -> None:
    statement = manifest_tombstone_candidates_statement(
        _WORKSPACE_ID,
        _CHECKPOINT_SEQUENCE,
        ("notes/alpha.md",),
    )
    text = str(statement.compile(dialect=postgresql.dialect()))
    assert "knowledge.source_tombstones.retained_locator" in text
    assert _bind_marker(text, "checkpoint_sequence")
    assert "restore_event_id IS NULL" in text
    assert "notes/alpha.md" not in text


def test_canonical_source_state_statement_binds_the_checkpoint_per_source() -> None:
    statement = manifest_canonical_source_state_statement(
        _WORKSPACE_ID, _CHECKPOINT_SEQUENCE, (_SOURCE_ID,)
    )
    text = str(statement.compile(dialect=postgresql.dialect()))
    assert "FROM knowledge.sources" in text
    assert _bind_marker(text, "checkpoint_sequence")
    assert "event_sequence <=" in text
    assert str(_SOURCE_ID) not in text


def test_canonical_only_downloads_exclude_resolved_sources() -> None:
    statement = manifest_canonical_only_downloads_statement(
        _WORKSPACE_ID, _CHECKPOINT_SEQUENCE, (_SOURCE_ID,)
    )
    text = str(statement.compile(dialect=postgresql.dialect()))
    assert "NOT IN" in text
    assert _bind_marker(text, "checkpoint_sequence")
    assert "restore_event_id IS NULL" in text
    assert str(_SOURCE_ID) not in text


# --- bind-parameter chunking of the finalize-time lookups ---------------------------


def test_chunk_id_lookups_partition_in_order_without_loss() -> None:
    ids = [uuid4() for _ in range(2 * MANIFEST_ID_LOOKUP_CHUNK_SIZE + 3)]
    chunks = chunk_id_lookups(ids)
    assert all(len(chunk) <= MANIFEST_ID_LOOKUP_CHUNK_SIZE for chunk in chunks)
    assert [len(chunk) for chunk in chunks] == [
        MANIFEST_ID_LOOKUP_CHUNK_SIZE,
        MANIFEST_ID_LOOKUP_CHUNK_SIZE,
        3,
    ]
    # The chunks concatenate back to the input: no id lost, duplicated or
    # reordered, so per-chunk merges reproduce the single-statement lookup.
    assert [source_id for chunk in chunks for source_id in chunk] == ids
    assert chunk_id_lookups(()) == ()


def test_canonical_only_downloads_bind_the_exclusion_as_one_array_parameter() -> None:
    """The exclusion must respect the extended-protocol ceiling: even a run
    that resolved the schema's full 100,000 ids binds ONE array-typed
    parameter, so the compiled statement's distinct bind markers are
    identical for a handful of ids and for 100,000 (the round-1 chunked
    ``NOT IN`` conjuncts failed exactly here — every id remained a
    parameter of the same statement, crossing the ceiling near 65,530
    resolved sources)."""

    few_ids = [uuid4() for _ in range(3)]
    full_ids = [uuid4() for _ in range(100_000)]
    few = manifest_canonical_only_downloads_statement(
        _WORKSPACE_ID, _CHECKPOINT_SEQUENCE, few_ids
    ).compile(dialect=postgresql.dialect())
    full = manifest_canonical_only_downloads_statement(
        _WORKSPACE_ID, _CHECKPOINT_SEQUENCE, full_ids
    ).compile(dialect=postgresql.dialect())
    few_markers = set(re.findall(r"%\((\w+)\)s", str(few)))
    full_markers = set(re.findall(r"%\((\w+)\)s", str(full)))
    assert "resolved_source_ids" in full_markers
    assert full_markers == few_markers
    assert len(full_markers) <= MANIFEST_ID_LOOKUP_CHUNK_SIZE
    assert list(full.params["resolved_source_ids"]) == full_ids
    assert "unnest" in str(full)


class _RowMappingStub(dict[str, Any]):
    """A RowMapping stand-in: attribute access over the dict keys."""

    def __getattr__(self, name: str) -> Any:
        return self[name]


class _ChunkedResultStub:
    """The ``.mappings()`` result shape the store's lookups consume."""

    def __init__(self, rows: list[_RowMappingStub]) -> None:
        self._rows = rows

    def mappings(self) -> list[_RowMappingStub]:
        return self._rows


class _ChunkedConnectionStub:
    """Executes one statement per chunk and records each requested chunk."""

    def __init__(self, rows_by_key: dict[UUID, _RowMappingStub], bind_name: str) -> None:
        self._rows_by_key = rows_by_key
        self._bind_name = bind_name
        self.requested_chunks: list[list[UUID]] = []

    async def execute(self, statement: Any) -> _ChunkedResultStub:
        params = statement.compile(dialect=postgresql.dialect()).params
        requested = list(params[self._bind_name])
        self.requested_chunks.append(requested)
        return _ChunkedResultStub([self._rows_by_key[key] for key in requested])


def _engineless_store() -> PostgresqlDeviceManifestStore:
    """One store whose connection-free seams are under test (no engine use)."""

    return PostgresqlDeviceManifestStore(cast(AsyncEngine, None))


@pytest.mark.asyncio
async def test_canonical_state_lookup_merges_chunked_rows_without_loss() -> None:
    source_ids = [uuid4() for _ in range(2 * MANIFEST_ID_LOOKUP_CHUNK_SIZE + 3)]
    rows = {
        source_id: _RowMappingStub(
            {"source_id": source_id, "active_locator_id": uuid4()}
        )
        for source_id in source_ids
    }
    connection = _ChunkedConnectionStub(rows, bind_name="source_ids")

    merged = await _engineless_store()._canonical_states(
        connection, _WORKSPACE_ID, _CHECKPOINT_SEQUENCE, source_ids
    )

    assert [len(chunk) for chunk in connection.requested_chunks] == [
        MANIFEST_ID_LOOKUP_CHUNK_SIZE,
        MANIFEST_ID_LOOKUP_CHUNK_SIZE,
        3,
    ]
    assert set(merged) == set(source_ids)
    assert merged == {source_id: dict(row) for source_id, row in rows.items()}


@pytest.mark.asyncio
async def test_known_base_fingerprint_lookup_merges_chunked_rows_without_loss() -> None:
    version_ids = [uuid4() for _ in range(MANIFEST_ID_LOOKUP_CHUNK_SIZE + 1)]
    rows = {
        version_id: _RowMappingStub(
            {"source_version_id": version_id, "content_hash": "a" * 64}
        )
        for version_id in version_ids
    }
    connection = _ChunkedConnectionStub(rows, bind_name="version_ids")

    merged = await _engineless_store()._known_base_fingerprints(
        connection, _WORKSPACE_ID, version_ids
    )

    assert [len(chunk) for chunk in connection.requested_chunks] == [
        MANIFEST_ID_LOOKUP_CHUNK_SIZE,
        1,
    ]
    assert set(merged) == set(version_ids)
    assert merged == {version_id: dict(row) for version_id, row in rows.items()}


# --- canonical-JSON final digest ----------------------------------------------------


def test_final_digest_is_deterministic_over_the_ordered_pages() -> None:
    pages = ((0, 2, _PAGE_DIGEST), (1, 3, "c" * 64))
    first = compute_manifest_final_digest(pages)
    second = compute_manifest_final_digest(tuple(reversed(pages)))
    assert first == second
    assert len(first) == 64
    assert first == first.lower()


def test_final_digest_binds_every_page_and_the_empty_manifest() -> None:
    empty = compute_manifest_final_digest(())
    single = compute_manifest_final_digest(((0, 1, _PAGE_DIGEST),))
    changed_count = compute_manifest_final_digest(((0, 2, _PAGE_DIGEST),))
    changed_digest = compute_manifest_final_digest(((0, 1, "d" * 64),))
    assert len({empty, single, changed_count, changed_digest}) == 4


def test_final_digest_matches_the_pinned_golden_vectors() -> None:
    """The final-digest grammar is a wire contract the plugin must mirror,
    so it is pinned byte for byte: a member rename, a key reorder or a shape
    tweak fails here even though the determinism tests would still pass."""

    two_pages = ((0, 2, _PAGE_DIGEST), (1, 3, "c" * 64))
    assert compute_manifest_final_digest(two_pages) == (
        "b048465d54c02d7191f0a736cbc36b2339dd881847292f6a4c6dfd5b27c9b430"
    )
    # The empty manifest still commits to the grammar envelope alone.
    assert compute_manifest_final_digest(()) == (
        "b53f908bd377e91b3784d07d32ed44ca068e8029760afc38fd71cb8a260a7b1d"
    )
