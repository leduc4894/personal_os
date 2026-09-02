"""Table-metadata contract of the canonical source-conflict aggregate (spec 4).

The aggregate's durable shape is pinned from two sides that must agree:
the Alembic revision that creates ``knowledge.source_conflicts`` (its
column set, primary key, uniqueness, foreign keys with ``ON DELETE
RESTRICT``, the closed check-constraint vocabularies of the spec 4.1/4.2
tables and the three listing/history indexes) and the runtime
SQLAlchemy ``Table`` object every store transaction reads and writes.
A drift on either side — a renamed constraint, a widened vocabulary, a
dropped RESTRICT, a column only one side knows — fails this contract
before any database is touched.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from typing import Final

from postgresql_source_store.tables import source_conflicts as source_conflicts_table

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[3]
_MIGRATION_PATH: Final[Path] = (
    _REPO_ROOT / "migrations" / "versions" / "20260902_01_add_source_conflicts.py"
)


def _load_migration_module() -> object:
    spec = importlib.util.spec_from_file_location("add_source_conflicts_migration", _MIGRATION_PATH)
    if spec is None or spec.loader is None:  # pragma: no cover - path contract
        raise AssertionError("the source-conflicts migration module must be loadable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_MIGRATION = _load_migration_module()
_MIGRATION_SOURCE: Final[str] = _MIGRATION_PATH.read_text(encoding="utf-8")


def _migration_constant(name: str) -> str:
    value = getattr(_MIGRATION, name)
    assert isinstance(value, str)
    return value


# --- the column contract -------------------------------------------------------------------------


def test_runtime_table_columns_match_the_migration_exactly() -> None:
    """Every migrated column exists once, with its exact nullability."""

    created = re.search(
        r'op\.create_table\(\s*"source_conflicts",(.*?)sa\.PrimaryKeyConstraint',
        _MIGRATION_SOURCE,
        flags=re.DOTALL,
    )
    assert created is not None
    migrated_columns = {
        name: nullable == "True"
        for name, nullable in re.findall(
            r'sa\.Column\(\s*"([a-z_]+)",(?:[^()]|\([^()]*\))*?nullable=(True|False)',
            created.group(1),
        )
    }
    runtime_columns = {column.name: column.nullable for column in source_conflicts_table.columns}
    assert migrated_columns == runtime_columns
    assert len(runtime_columns) == 20


def test_primary_key_and_workspace_uniqueness_are_pinned() -> None:
    """The identity and replay-fencing constraints keep their pinned names."""

    assert source_conflicts_table.primary_key.name == "pk_source_conflicts"
    for pinned in (
        '"uq_source_conflicts__capture_idempotency"',
        '"uq_source_conflicts__originating_event"',
        '"uq_source_conflicts__resolution_event"',
    ):
        assert pinned in _MIGRATION_SOURCE


# --- the closed vocabularies of the spec 4.1/4.3 tables -------------------------------------------


def test_conflict_kind_and_candidate_shape_constraints_are_closed() -> None:
    """The check constraints carry exactly the closed spec vocabularies."""

    kind_check = _migration_constant("_CONFLICT_KIND_CHECK")
    assert set(re.findall(r"'([a-z_]+)'", kind_check)) == {
        "stale_content",
        "edit_remote_delete",
        "delete_remote_edit",
        "locator_collision",
    }
    status_check = _migration_constant("_STATUS_CHECK")
    assert set(re.findall(r"'([a-z_]+)'", status_check)) == {
        "open",
        "resolving",
        "resolved",
        "superseded",
    }
    resolution_check = _migration_constant("_RESOLUTION_KIND_CHECK")
    assert set(re.findall(r"'([a-z_]+)'", resolution_check)) == {
        "keep_remote",
        "keep_local",
        "save_merged",
    }
    candidate_kind_check = _migration_constant("_CANDIDATE_KIND_CHECK")
    assert set(re.findall(r"'([a-z_]+)'", candidate_kind_check)) == {"content", "delete"}
    # A conflict can never be partly a deletion and partly a content
    # candidate, and the kind/candidate matrix of the spec 4.1 table holds.
    shape_check = _migration_constant("_CANDIDATE_SHAPE_CHECK")
    assert "(candidate_kind = 'content') = (verified_candidate_object_id IS NOT NULL)" in (
        shape_check
    )
    kind_candidate_check = _migration_constant("_KIND_CANDIDATE_CHECK")
    assert "conflict_kind NOT IN ('stale_content', 'edit_remote_delete')" in (kind_candidate_check)
    assert "conflict_kind <> 'delete_remote_edit' OR candidate_kind = 'delete'" in (
        kind_candidate_check
    )
    # A source may be null only for a locator collision, and only a locator
    # collision requires the locator snapshot.
    assert "source_id IS NOT NULL OR conflict_kind = 'locator_collision'" in _migration_constant(
        "_SOURCE_BINDING_CHECK"
    )
    assert (
        "conflict_kind <> 'locator_collision' OR normalized_locator IS NOT NULL"
        in _migration_constant("_LOCATOR_SNAPSHOT_CHECK")
    )


def test_state_machine_shape_constraints_pin_the_terminal_shapes() -> None:
    """Each aggregate state admits exactly its spec 4.3 evidence shape."""

    open_shape = _migration_constant("_OPEN_SHAPE_CHECK")
    assert "resolution_kind IS NULL" in open_shape
    assert "successor_conflict_id IS NULL" in open_shape
    resolved_shape = _migration_constant("_RESOLVED_SHAPE_CHECK")
    assert "resolution_kind = 'keep_remote' AND resulting_version_id IS NULL" in (resolved_shape)
    assert (
        "resolution_kind IN ('keep_local', 'save_merged') "
        "AND resulting_version_id IS NOT NULL" in resolved_shape
    )
    superseded_shape = _migration_constant("_SUPERSEDED_SHAPE_CHECK")
    assert "successor_conflict_id IS NOT NULL" in superseded_shape
    assert "resulting_version_id IS NULL" in superseded_shape
    assert (
        "successor_conflict_id IS NULL OR successor_conflict_id <> conflict_id"
        in _migration_constant("_SUCCESSOR_DISTINCT_CHECK")
    )


# --- evidence preservation ------------------------------------------------------------------------


def test_every_evidence_foreign_key_restricts_deletion() -> None:
    """FK targets keep their pinned names and RESTRICT deletion."""

    for pinned_name in (
        "fk_source_conflicts__workspace",
        "fk_source_conflicts__source",
        "fk_source_conflicts__originating_event",
        "fk_source_conflicts__resolution_event",
        "fk_source_conflicts__device",
        "fk_source_conflicts__base_version",
        "fk_source_conflicts__observed_remote",
        "fk_source_conflicts__resulting_version",
        "fk_source_conflicts__candidate_object",
        "fk_source_conflicts__successor",
    ):
        assert pinned_name in _MIGRATION_SOURCE
    ondelete = re.findall(r'ondelete="RESTRICT"', _MIGRATION_SOURCE)
    assert len(ondelete) == 10


def test_listing_and_history_indexes_are_pinned() -> None:
    """The open-listing, source-history and resolution-replay indexes exist."""

    assert '"ix_source_conflicts__workspace_open_listing"' in _MIGRATION_SOURCE
    assert '"ix_source_conflicts__source_history"' in _MIGRATION_SOURCE
    # The open listing pages only open conflicts and the resolution replay
    # lookup is unique per workspace and resolution event identity.
    assert "postgresql_where=sa.text(\"status = 'open'\")" in _MIGRATION_SOURCE
    assert "resolution_event_id IS NOT NULL" in _MIGRATION_SOURCE


def test_downgrade_is_gated_and_restores_the_predecessor_vocabulary() -> None:
    """Conflict evidence is discarded only under the explicit destructive gate."""

    assert _migration_constant("_DOWNGRADE_REFUSAL_MESSAGE") == (
        "source_conflict_downgrade_requires_explicit_gate"
    )
    predecessor_check = _migration_constant("_PREDECESSOR_SYNC_EVENT_TYPE_CHECK")
    assert "conflict_capture" not in predecessor_check
    assert "conflict_resolve" not in predecessor_check
    successor_check = _migration_constant("_SYNC_EVENT_TYPE_CHECK")
    assert "conflict_capture" in successor_check
    assert "conflict_resolve" in successor_check
