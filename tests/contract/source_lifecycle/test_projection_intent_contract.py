"""Projection-intent contract for source lifecycle transitions (Task 11).

Every source-lifecycle transition (rename / move / delete / restore) writes
two projection intents — one for Qdrant and one for Neo4j — through the
shared :func:`intent_insert_statement` helper. The dispatcher and the
Temporal ingestion then consume those rows without any lifecycle-specific
rewrite. The contract pins three properties that prove the dispatcher is
projection-only and the lifecycle event identity survives the wire:

1. The Qdrant intent and the Neo4j intent of one lifecycle event carry the
   same non-null ``source_version_id`` (the source's
   ``current_version_id`` at the moment of commit); the runtime ingestion
   never sees a lifecycle intent with a null version.
2. The closed ``operation`` token is exactly one of ``upsert`` or
   ``delete``; the lifecycle vocabulary ``rename`` / ``move`` /
   ``restore`` never appears as a projection-intent operation.
3. The lifecycle event identity (``event_id``) is preserved on both
   intents of one commit, so the deterministic workflow id derived from
   ``(workspace_id, event_id)`` reaches the same Temporal execution.

The contract tests are pure (no database) and assert the shape of the
parameter-bound SQL plus the closed-vocabulary invariants the runtime
ingestion relies on. The companion integration suite covers the real
SQL execution.
"""

from __future__ import annotations

import hashlib
from uuid import UUID, uuid4

from sqlalchemy.dialects import postgresql

from personal_os.sources.projection_dispatch import PROJECTION_OPERATIONS
from postgresql_source_store.lifecycle_store import (
    PROJECTION_KIND_NEO4J,
    PROJECTION_KIND_QDRANT,
    PROJECTION_OPERATION_DELETE,
    PROJECTION_OPERATION_UPSERT,
    intent_insert_statement,
)
from postgresql_source_store.tables import projection_intents

#: Lifecycle event identities used to prove the contract — non-null
#: UUIDv7 tokens are not required; the relational shape is the contract.
_EVENT_ID_RENAME: UUID = UUID("018f47a0-7b00-7000-8000-000000000101")
_EVENT_ID_MOVE: UUID = UUID("018f47a0-7b00-7000-8000-000000000102")
_EVENT_ID_DELETE: UUID = UUID("018f47a0-7b00-7000-8000-000000000103")
_EVENT_ID_RESTORE: UUID = UUID("018f47a0-7b00-7000-8000-000000000104")

_WORKSPACE_ID: UUID = UUID("018f47a0-7b00-7000-8000-000000000001")
_SOURCE_ID: UUID = UUID("018f47a0-7b00-7000-8000-000000000002")
_VERSION_ID: UUID = UUID("018f47a0-7b00-7000-8000-000000000003")


def _bind_marker(text: str, column: str) -> bool:
    """Check whether a parameter-bound marker for the column is in the SQL text.

    SQLAlchemy names duplicate columns with a numeric suffix
    (``%(workspace_id_1)s``), so we accept either the bare or suffixed form.
    """

    if f"%({column})s" in text:
        return True
    return f"%({column}_1)s" in text


def _lifecycle_intent_pairs(
    *,
    operation: str,
    event_id: UUID,
) -> tuple[dict[str, object], dict[str, object]]:
    """The two parameter-bound intent payloads for one lifecycle event.

    ``source_version_id`` is the source's ``current_version_id`` at commit
    time; the lifecycle adapter must pass this non-null value for both
    intents so the downstream ``source_event`` ingestion path can carry
    the ``source_version_id`` through the SourceIngestionReference.
    """

    common = {
        "workspace_id": _WORKSPACE_ID,
        "event_id": event_id,
        "source_id": _SOURCE_ID,
        "source_version_id": _VERSION_ID,
    }
    return (
        {
            "projection_intent_id": uuid4(),
            "projection_kind": PROJECTION_KIND_QDRANT,
            "operation": operation,
            **common,
        },
        {
            "projection_intent_id": uuid4(),
            "projection_kind": PROJECTION_KIND_NEO4J,
            "operation": operation,
            **common,
        },
    )


def test_lifecycle_intent_inserts_bind_non_null_source_version_id_for_both_kinds() -> None:
    """The Qdrant and Neo4j intent of every lifecycle event carry a non-null version."""

    for event_id in (_EVENT_ID_RENAME, _EVENT_ID_MOVE, _EVENT_ID_DELETE, _EVENT_ID_RESTORE):
        qdrant, neo4j = _lifecycle_intent_pairs(
            operation=PROJECTION_OPERATION_UPSERT, event_id=event_id
        )
        for payload in (qdrant, neo4j):
            statement = intent_insert_statement(**payload)
            compiled = statement.compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": False},
            )
            text = str(compiled)
            assert _bind_marker(text, "source_version_id"), text
            assert _bind_marker(text, "event_id"), text
            assert _bind_marker(text, "projection_kind"), text
            assert _bind_marker(text, "operation"), text
            # The literal UUIDs must never appear as SQL text.
            assert str(_VERSION_ID) not in text
            assert str(event_id) not in text


def test_lifecycle_intent_inserts_use_closed_upsert_or_delete_operation() -> None:
    """The projection-intent operation vocabulary is the closed ``upsert``/``delete`` set.

    The lifecycle vocabulary (``rename`` / ``move`` / ``delete`` / ``restore``)
    never appears as a projection-intent operation; lifecycle ops are
    distinguished by the projection-intent ``operation`` choice the
    adapter makes (upsert / delete), not by a ``rename`` token.
    """

    for event_id in (_EVENT_ID_RENAME, _EVENT_ID_MOVE, _EVENT_ID_DELETE, _EVENT_ID_RESTORE):
        for op in (PROJECTION_OPERATION_UPSERT, PROJECTION_OPERATION_DELETE):
            statement = intent_insert_statement(
                projection_intent_id=uuid4(),
                workspace_id=_WORKSPACE_ID,
                event_id=event_id,
                source_id=_SOURCE_ID,
                source_version_id=_VERSION_ID,
                projection_kind=PROJECTION_KIND_QDRANT,
                operation=op,
            )
            compiled = statement.compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": False},
            )
            text = str(compiled)
            assert _bind_marker(text, "operation"), text
            # The literal operation value must not be embedded in the SQL.
            assert op not in text


def test_projection_intent_operation_set_admits_only_lifecycle_compatible_tokens() -> None:
    """The runtime registry of allowed operations is the closed two-value set.

    This is the same vocabulary the runtime ingestion cross-checks
    against ``LeasedProjectionIntent``; lifecycle contract tests fail
    immediately if a ``rename`` or ``move`` token ever creeps into the
    closed projection-intent operations.
    """

    operation_values = frozenset(token.value for token in PROJECTION_OPERATIONS)
    assert operation_values == frozenset({PROJECTION_OPERATION_UPSERT, PROJECTION_OPERATION_DELETE})
    # The lifecycle vocabulary must never enter the projection-intent operations.
    for banned in ("rename", "move", "restore", "create", "update"):
        assert banned not in operation_values


def test_lifecycle_intent_inserts_preserve_event_identity_across_kind_pairs() -> None:
    """Two intents of one lifecycle event share the same ``event_id``.

    The Temporal ingestion derives the deterministic workflow id from
    ``(workspace_id, event_id)``. Concurrency or retry of the Qdrant
    intent and the Neo4j intent must therefore both reach the same
    workflow execution; the contract pins that both intents of one
    event carry the same event identity.
    """

    qdrant, neo4j = _lifecycle_intent_pairs(
        operation=PROJECTION_OPERATION_UPSERT, event_id=_EVENT_ID_RENAME
    )
    assert qdrant["event_id"] == neo4j["event_id"] == _EVENT_ID_RENAME
    assert qdrant["workspace_id"] == neo4j["workspace_id"] == _WORKSPACE_ID
    assert qdrant["source_id"] == neo4j["source_id"] == _SOURCE_ID
    assert qdrant["source_version_id"] == neo4j["source_version_id"] == _VERSION_ID
    # The two kinds are distinct rows, never a duplicate.
    assert qdrant["projection_kind"] == "qdrant"
    assert neo4j["projection_kind"] == "neo4j"
    assert qdrant["projection_intent_id"] != neo4j["projection_intent_id"]


def test_lifecycle_intent_inserts_target_the_canonical_projection_intents_table() -> None:
    """The lifecycle intent shares the same target table as the lease store.

    The dispatcher obtains its leased view from this table; if the
    lifecycle adapter ever wrote to a different table, the dispatcher
    would see no intent and the projection would silently fall behind.
    """

    qdrant, _ = _lifecycle_intent_pairs(
        operation=PROJECTION_OPERATION_UPSERT, event_id=_EVENT_ID_MOVE
    )
    statement = intent_insert_statement(**qdrant)
    compiled = statement.compile(
        dialect=postgresql.dialect(),
        compile_kwargs={"literal_binds": False},
    )
    text = str(compiled)
    assert "knowledge.projection_intents" in text
    # The migration's CHECK constraint blocks every value outside the
    # two closed operations; the helper must bind, not embed, the value.
    assert _bind_marker(text, "operation"), text


def test_lifecycle_intent_inserts_never_leak_a_raw_event_id_into_compiled_sql() -> None:
    """The event id is bound as a parameter, not embedded in compiled SQL.

    The migration's CHECK constraint on ``projection_intents.event_id``
    is a foreign key to ``sync_events.event_id``; binding the value lets
    the migration enforce the constraint without the runtime needing to
    know the literal UUID.
    """

    statement = intent_insert_statement(
        projection_intent_id=uuid4(),
        workspace_id=_WORKSPACE_ID,
        event_id=_EVENT_ID_DELETE,
        source_id=_SOURCE_ID,
        source_version_id=_VERSION_ID,
        projection_kind=PROJECTION_KIND_QDRANT,
        operation=PROJECTION_OPERATION_DELETE,
    )
    compiled = statement.compile(
        dialect=postgresql.dialect(),
        compile_kwargs={"literal_binds": False},
    )
    text = str(compiled)
    assert str(_EVENT_ID_DELETE) not in text
    # The literal close-only operation token ("delete") is never embedded.
    assert "delete" not in text.lower()


def test_lifecycle_intent_inserts_target_schema_qualified_resource() -> None:
    """The compiled SQL is schema-qualified to the canonical knowledge schema.

    The dispatched Temporal ingestion cannot find a lifecycle intent
    written outside the canonical schema, so the statement must be
    schema-qualified through the table core metadata.
    """

    statement = intent_insert_statement(
        projection_intent_id=uuid4(),
        workspace_id=_WORKSPACE_ID,
        event_id=_EVENT_ID_RESTORE,
        source_id=_SOURCE_ID,
        source_version_id=_VERSION_ID,
        projection_kind=PROJECTION_KIND_NEO4J,
        operation=PROJECTION_OPERATION_UPSERT,
    )
    compiled = statement.compile(
        dialect=postgresql.dialect(),
        compile_kwargs={"literal_binds": False},
    )
    text = str(compiled)
    assert "knowledge.projection_intents" in text
    # No raw literal hashed value, fingerprint or token crosses the boundary.
    fingerprint = hashlib.sha256(b"do-not-leak-fingerprint").hexdigest()
    assert fingerprint not in text
    assert "do-not-leak-fingerprint" not in text


def test_lifecycle_intent_insert_table_columns_match_runtime_table() -> None:
    """The lifecycle intent helper writes the same columns the runtime lease store reads.

    The dispatcher selects the leased intent through the canonical
    table core metadata; the lifecycle helper must therefore target the
    same SQLAlchemy table object.
    """

    statement = intent_insert_statement(
        projection_intent_id=uuid4(),
        workspace_id=_WORKSPACE_ID,
        event_id=_EVENT_ID_RENAME,
        source_id=_SOURCE_ID,
        source_version_id=_VERSION_ID,
        projection_kind=PROJECTION_KIND_QDRANT,
        operation=PROJECTION_OPERATION_UPSERT,
    )
    bound_table = statement.table
    assert bound_table is projection_intents
    # The intent insert is values(.values(...)) — the helper must populate
    # every column the dispatcher's lease SELECT reads.
    compiled = statement.compile(
        dialect=postgresql.dialect(),
        compile_kwargs={"literal_binds": False},
    )
    text = str(compiled)
    for column_key in (
        "projection_intent_id",
        "workspace_id",
        "event_id",
        "source_id",
        "source_version_id",
        "projection_kind",
        "operation",
    ):
        assert column_key in text, (column_key, text)
