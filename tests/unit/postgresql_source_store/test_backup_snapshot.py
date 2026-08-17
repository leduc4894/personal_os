"""Quiesced exported-snapshot adapter statements and object-set hydration.

These tests pin the pure pieces of the PostgreSQL snapshot adapter (spec 9.2)
without a database: the fixed nine-table ``SHARE MODE NOWAIT`` lock order, the
parameter-bound pending-writer probe, the schema-qualified referenced-objects
and pointer-resolution reads, and the fail-closed hydration of referenced
content objects into expected-object requests. The snapshot transaction's
runtime behavior is integration territory (Task 13).
"""

from __future__ import annotations

import hashlib
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from personal_os.error_contracts.codes import ErrorCode
from personal_os.object_storage import ContentDigest, derive_canonical_object_key
from personal_os.recovery.contracts import (
    MAXIMUM_OBJECT_SIZE_BYTES,
    RecoveryComponent,
    RecoveryError,
)
from postgresql_source_store.backup_snapshot import (
    SNAPSHOT_LOCK_ORDER,
    SNAPSHOT_LOCK_TIMEOUT_SECONDS,
    build_share_lock_statements,
    current_pointer_resolution_statement,
    hydrate_referenced_objects,
    pending_writer_count_statement,
    referenced_objects_statement,
)
from postgresql_source_store.tables import SOURCE_STORE_SCHEMA

_CONTENT_A = hashlib.sha256(b"backup-snapshot-object-a").hexdigest()
_KEY_A = derive_canonical_object_key(ContentDigest.parse(_CONTENT_A)).value
_CONTENT_B = hashlib.sha256(b"backup-snapshot-object-b").hexdigest()
_KEY_B = derive_canonical_object_key(ContentDigest.parse(_CONTENT_B)).value


def _object_row(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "content_hash": _CONTENT_A,
        "object_key": _KEY_A,
        "byte_size": 128,
        "media_type": "text/markdown",
    }
    return {**base, **overrides}


# --- fixed share-lock order and statements -----------------------------------


def test_snapshot_lock_order_covers_the_canonical_and_policy_tables() -> None:
    assert SNAPSHOT_LOCK_ORDER == (
        "users",
        "workspaces",
        "devices",
        "content_objects",
        "sources",
        "source_versions",
        "sync_events",
        "projection_intents",
        "audit_events",
        "workspace_policy_state",
        "policy_signing_keys",
        "policy_keysets",
        "policy_keyset_signatures",
        "source_policies",
        "policy_rules",
        "policy_drafts",
        "policy_draft_rules",
        "policy_evaluations",
        "policy_reconciliation_intents",
    )
    assert len(SNAPSHOT_LOCK_ORDER) == len(set(SNAPSHOT_LOCK_ORDER))


def test_snapshot_lock_order_excludes_ephemeral_preview_tables() -> None:
    # Preview rows and their results are reconstructible evidence (spec 10/22):
    # they never join the canonical quiesced snapshot, so a restore cannot
    # resurrect stale preview state.
    assert "policy_previews" not in SNAPSHOT_LOCK_ORDER
    assert "policy_preview_results" not in SNAPSHOT_LOCK_ORDER


def test_snapshot_lock_timeout_is_fifteen_seconds() -> None:
    assert SNAPSHOT_LOCK_TIMEOUT_SECONDS == 15


def test_share_lock_statements_follow_fixed_spec_order() -> None:
    statements = build_share_lock_statements()
    texts = [str(s.compile(dialect=postgresql.dialect())) for s in statements]
    assert len(texts) == 19
    for text, table in zip(texts, SNAPSHOT_LOCK_ORDER, strict=True):
        assert f'{SOURCE_STORE_SCHEMA}."{table}"' in text
        assert "SHARE MODE NOWAIT" in text
        assert text.startswith("LOCK TABLE")


# --- referenced-objects and pointer-resolution read shapes -------------------


def test_referenced_objects_statement_is_schema_qualified_and_distinct() -> None:
    statement = referenced_objects_statement()
    assert isinstance(statement, sa.Select)
    compiled = str(statement.compile(dialect=postgresql.dialect()))
    assert "SELECT DISTINCT" in compiled
    assert "knowledge.content_objects" in compiled
    assert "knowledge.source_versions" in compiled
    columns = {column.key for column in statement.exported_columns}
    assert columns == {"content_hash", "object_key", "byte_size", "media_type"}


def test_pending_writer_count_statement_counts_ungranted_relation_locks() -> None:
    statement = pending_writer_count_statement()
    assert isinstance(statement, sa.TextClause)
    compiled = str(statement)
    assert compiled.startswith("SELECT count(*)")
    assert "FROM pg_locks JOIN pg_class ON pg_locks.relation = pg_class.oid" in compiled
    assert "pg_locks.locktype = 'relation'" in compiled
    assert "NOT pg_locks.granted" in compiled
    assert "pg_class.relname = ANY(:tables)" in compiled


def test_current_pointer_resolution_statement_joins_and_fails_closed() -> None:
    statement = current_pointer_resolution_statement()
    assert isinstance(statement, sa.Select)
    compiled = str(statement.compile(dialect=postgresql.dialect())).replace("knowledge.", "")
    assert "FROM sources" in compiled
    assert compiled.count("LEFT OUTER JOIN") == 2
    # The version join must match the pointer within the same source, so a
    # version owned by another source never resolves the pointer.
    assert "source_versions.source_id = sources.source_id" in compiled
    assert "sources.current_version_id IS NULL" in compiled
    assert "source_versions.source_version_id IS NULL" in compiled
    assert "content_objects.content_object_id IS NULL" in compiled


# --- hydration of the referenced object set ----------------------------------


def test_hydrate_referenced_objects_validates_canonical_derivation() -> None:
    objects = hydrate_referenced_objects(
        [
            _object_row(),
            _object_row(
                content_hash=_CONTENT_B,
                object_key=_KEY_B,
                byte_size=4096,
                media_type="application/json",
            ),
        ]
    )
    assert len(objects) == 2
    for expected, (digest_hex, size, media) in zip(
        objects,
        ((_CONTENT_A, 128, "text/markdown"), (_CONTENT_B, 4096, "application/json")),
        strict=True,
    ):
        assert expected.content_digest.hexadecimal == digest_hex
        assert derive_canonical_object_key(expected.content_digest).value == (
            _KEY_A if digest_hex == _CONTENT_A else _KEY_B
        )
        assert expected.size_bytes == size
        assert expected.media_type.value == media


def test_hydrate_referenced_objects_deduplicates_content_objects() -> None:
    objects = hydrate_referenced_objects([_object_row(), _object_row()])
    assert len(objects) == 1
    assert objects[0].content_digest.hexadecimal == _CONTENT_A
    assert objects[0].size_bytes == 128
    assert objects[0].media_type.value == "text/markdown"


@pytest.mark.parametrize(
    "mutator",
    [
        pytest.param(lambda r: {**r, "object_key": "objects/sha256/aa/bb/other"}, id="key"),
        pytest.param(lambda r: {**r, "content_hash": _CONTENT_A.upper()}, id="digest"),
        pytest.param(lambda r: {**r, "content_hash": "not-hex"}, id="digest-length"),
        pytest.param(lambda r: {**r, "media_type": "text/markdown; charset=utf-8"}, id="media"),
        pytest.param(lambda r: {**r, "media_type": "TEXT/MARKDOWN"}, id="media-case"),
        pytest.param(lambda r: {**r, "byte_size": -1}, id="size-negative"),
        pytest.param(lambda r: {**r, "byte_size": MAXIMUM_OBJECT_SIZE_BYTES + 1}, id="size-over"),
        pytest.param(lambda r: {**r, "byte_size": None}, id="size-null"),
        pytest.param(lambda r: {**r, "content_hash": None}, id="digest-null"),
    ],
)
def test_hydrate_rejects_every_object_set_violation(mutator: Any) -> None:
    with pytest.raises(RecoveryError) as captured:
        hydrate_referenced_objects([mutator(_object_row())])
    assert captured.value.error_code is ErrorCode.CANONICAL_RECOVERY_INTEGRITY_FAILED
    assert captured.value.safe_details["component"] is RecoveryComponent.OBJECT_SET


@pytest.mark.parametrize("byte_size", [0, MAXIMUM_OBJECT_SIZE_BYTES])
def test_hydrate_accepts_the_boundary_byte_sizes(byte_size: int) -> None:
    objects = hydrate_referenced_objects([_object_row(byte_size=byte_size)])
    assert objects[0].size_bytes == byte_size


def test_hydrate_rejects_a_conflicting_duplicate_object() -> None:
    with pytest.raises(RecoveryError) as captured:
        hydrate_referenced_objects([_object_row(), _object_row(byte_size=256)])
    assert captured.value.error_code is ErrorCode.CANONICAL_RECOVERY_INTEGRITY_FAILED
    assert captured.value.safe_details["component"] is RecoveryComponent.OBJECT_SET


def test_hydrate_of_no_rows_yields_an_empty_object_set() -> None:
    assert hydrate_referenced_objects([]) == ()
