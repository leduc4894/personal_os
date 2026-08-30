"""Indexed-access acceptance: EXPLAIN proofs over a 10,000-row disposable database.

The fixture bulk-seeds 10,000 active sources, 10,000 source versions, 10,000
canonical create events and 10,000 pending projection intents (one content
object per version) through schema-qualified Core batch inserts, runs
``ANALYZE`` so the planner sees real statistics, and then captures
``EXPLAIN (FORMAT JSON)`` for the four hot publication queries: the locked
current-pointer lookup, the idempotent-replay hydration, the per-source
version history and the pending-intent claim. Every query must use at least
one index created by migration ``20260813_01`` (the approved set is derived
from the migration source, not hand-copied), every index the plans touch must
belong to that approved set, and no plan may sequentially scan a populated
relation.
"""

from __future__ import annotations

import ast
import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from personal_os.sources.commands import IdempotencyKey
from postgresql_source_store.engine import create_source_store_engine, dispose_source_store_engine
from postgresql_source_store.projection_intents import claim_available_select_statement
from postgresql_source_store.publication_store import replay_lookup_by_key_statement
from postgresql_source_store.tables import (
    content_objects,
    projection_intents,
    source_versions,
    sources,
    sync_events,
    users,
    workspaces,
)

pytestmark = pytest.mark.local_stack

_WORKTREE_ROOT: Final[Path] = Path(__file__).resolve().parents[3]
_BASELINE_MIGRATION: Final[Path] = (
    _WORKTREE_ROOT
    / "migrations"
    / "versions"
    / "20260813_01_create_canonical_postgresql_baseline.py"
)

#: The plan-level acceptance population bound (plan section: at least 10,000).
POPULATED_ROW_MINIMUM: Final[int] = 10_000
_SEED_ROW_COUNT: Final[int] = 10_000
_INSERT_BATCH_SIZE: Final[int] = 1_000

#: Every relation populated at scale; a sequential scan over one of these is
#: the unbounded access the plan forbids.
POPULATED_RELATIONS: Final[frozenset[str]] = frozenset(
    {"sources", "source_versions", "sync_events", "projection_intents", "content_objects"}
)

#: The index each hot query must use, keyed by query name.
EXPECTED_INDEX_BY_QUERY: Final[dict[str, frozenset[str]]] = {
    "current_pointer": frozenset({"pk_sources", "uq_sources__workspace_source"}),
    "idempotent_replay": frozenset({"uq_sync_events__idempotency_key"}),
    "version_history": frozenset({"uq_source_versions__source_ordinal"}),
    "pending_claim": frozenset({"ix_projection_intents__pending_dispatch"}),
}


def _approved_index_names_from_source(source: str) -> frozenset[str]:
    """Derive the approved index set from baseline-migration source text.

    ``op.create_index`` first arguments plus the named primary-key and unique
    constraints (whose backing indexes carry the constraint name in
    PostgreSQL) are exactly the indexes migration ``20260813_01`` created.
    The helper is a pure function of its source text so its name-shape
    contract can be pinned without the disposable stack.
    """

    tree = ast.parse(source)
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr == "create_index":
            if not node.args or not isinstance(node.args[0], ast.Constant):
                raise ValueError("unsupported index-name shape in baseline migration")
            names.add(str(node.args[0].value))
        elif node.func.attr in {"PrimaryKeyConstraint", "UniqueConstraint"}:
            for keyword in node.keywords:
                if keyword.arg != "name":
                    continue
                if not isinstance(keyword.value, ast.Constant):
                    raise ValueError("unsupported index-name shape in baseline migration")
                names.add(str(keyword.value.value))
    assert names, "the baseline migration must declare named indexes and constraints"
    return frozenset(names)


def _approved_index_names() -> frozenset[str]:
    """Derive the approved index set from the baseline migration file."""

    return _approved_index_names_from_source(_BASELINE_MIGRATION.read_text(encoding="utf-8"))


@dataclass(frozen=True, slots=True)
class QueryPlanPopulation:
    """The populated disposable database and the sample identities to probe."""

    engine: AsyncEngine
    workspace_id: UUID
    sample_source_id: UUID
    sample_version_id: UUID
    sample_idempotency_key: str
    row_counts: dict[str, int]


def _batches(rows: list[dict[str, Any]]) -> Iterator[list[dict[str, Any]]]:
    for start in range(0, len(rows), _INSERT_BATCH_SIZE):
        yield rows[start : start + _INSERT_BATCH_SIZE]


async def _seed_population(engine: AsyncEngine) -> QueryPlanPopulation:
    nonce = uuid4().hex
    workspace_id = uuid4()
    owner_user_id = uuid4()
    content_object_ids = [uuid4() for _ in range(_SEED_ROW_COUNT)]
    source_ids = [uuid4() for _ in range(_SEED_ROW_COUNT)]
    version_ids = [uuid4() for _ in range(_SEED_ROW_COUNT)]
    event_ids = [uuid4() for _ in range(_SEED_ROW_COUNT)]
    idempotency_keys = [f"qplan-{nonce}-{index:06d}" for index in range(_SEED_ROW_COUNT)]

    content_object_rows: list[dict[str, Any]] = []
    for index in range(_SEED_ROW_COUNT):
        salt = f"query-plan-fixture-{nonce}-{index}"
        content_hash = hashlib.sha256(salt.encode("utf-8")).hexdigest()
        content_object_rows.append(
            {
                "content_object_id": content_object_ids[index],
                "content_hash": content_hash,
                "object_key": (
                    f"objects/sha256/{content_hash[:2]}/{content_hash[2:4]}/{content_hash}"
                ),
                "byte_size": len(salt),
                "media_type": "text/markdown",
                "verified_at": datetime.now(UTC),
            }
        )
    source_rows = [
        {
            "source_id": source_ids[index],
            "workspace_id": workspace_id,
            "source_type": "markdown",
            "title": f"Query plan source {index:05d}",
            "sync_state": "active",
            "current_version_id": version_ids[index],
        }
        for index in range(_SEED_ROW_COUNT)
    ]
    version_rows = [
        {
            "source_version_id": version_ids[index],
            "workspace_id": workspace_id,
            "source_id": source_ids[index],
            "content_object_id": content_object_ids[index],
            "content_version": 1,
            "parent_version_id": None,
            "author_kind": "system",
            "author_id": None,
        }
        for index in range(_SEED_ROW_COUNT)
    ]
    event_rows = [
        {
            "event_id": event_ids[index],
            "workspace_id": workspace_id,
            "source_id": source_ids[index],
            "device_id": None,
            "committed_version_id": version_ids[index],
            "base_version_id": None,
            "idempotency_key": idempotency_keys[index],
            "request_fingerprint": hashlib.sha256(
                idempotency_keys[index].encode("utf-8")
            ).hexdigest(),
            "event_type": "create",
        }
        for index in range(_SEED_ROW_COUNT)
    ]
    intent_rows = [
        {
            "projection_intent_id": uuid4(),
            "workspace_id": workspace_id,
            "event_id": event_ids[index],
            "source_id": source_ids[index],
            "source_version_id": version_ids[index],
            "projection_kind": "qdrant",
            "operation": "upsert",
        }
        for index in range(_SEED_ROW_COUNT)
    ]

    async with engine.begin() as connection:
        # The deferrable current-version pointer lets sources be inserted with
        # their final active pointer before the referenced versions exist.
        await connection.execute(sa.text("SET CONSTRAINTS ALL DEFERRED"))
        await connection.execute(
            sa.insert(users).values(
                user_id=owner_user_id,
                username=f"qplan-{nonce[:12]}",
                display_name="Query plan owner",
            )
        )
        await connection.execute(
            sa.insert(workspaces).values(
                workspace_id=workspace_id,
                owner_user_id=owner_user_id,
                workspace_key=f"qplan-{nonce[:12]}",
                display_name="Query plan workspace",
            )
        )
        for table, rows in (
            (content_objects, content_object_rows),
            (sources, source_rows),
            (source_versions, version_rows),
            (sync_events, event_rows),
            (projection_intents, intent_rows),
        ):
            for batch in _batches(rows):
                await connection.execute(sa.insert(table), batch)
        await connection.execute(
            sa.text(
                "ANALYZE knowledge.users, knowledge.workspaces, knowledge.content_objects, "
                "knowledge.sources, knowledge.source_versions, knowledge.sync_events, "
                "knowledge.projection_intents"
            )
        )

    counts: dict[str, int] = {}
    async with engine.connect() as connection:
        for table in (sources, source_versions, sync_events, content_objects):
            total = int(
                (
                    await connection.execute(sa.select(sa.func.count()).select_from(table))
                ).scalar_one()
            )
            counts[table.name] = total
        pending = int(
            (
                await connection.execute(
                    sa.select(sa.func.count())
                    .select_from(projection_intents)
                    .where(projection_intents.c.status == "pending")
                )
            ).scalar_one()
        )
        counts["projection_intents"] = pending
        counts["pending_intents"] = pending

    sample_index = _SEED_ROW_COUNT // 2
    return QueryPlanPopulation(
        engine=engine,
        workspace_id=workspace_id,
        sample_source_id=source_ids[sample_index],
        sample_version_id=version_ids[sample_index],
        sample_idempotency_key=idempotency_keys[sample_index],
        row_counts=counts,
    )


@pytest_asyncio.fixture
async def populated_store(source_publication_stack) -> Iterator[QueryPlanPopulation]:
    """Seed the population once per test on that test's own event loop.

    The function scope keeps the engine, its pool and the seeding on one
    selector loop, matching the loop-scope contract of the shared conftest.
    """

    engine = create_source_store_engine(
        source_publication_stack.settings, source_publication_stack.password
    )
    try:
        population = await _seed_population(engine)
        yield population
    finally:
        await dispose_source_store_engine(engine)


@pytest.mark.asyncio
async def test_fixture_meets_the_ten_thousand_row_population_bound(
    populated_store: QueryPlanPopulation,
) -> None:
    """The acceptance evidence is only valid above the plan's population bound."""

    assert populated_store.row_counts["source_versions"] >= POPULATED_ROW_MINIMUM
    assert populated_store.row_counts["pending_intents"] >= POPULATED_ROW_MINIMUM
    for relation in POPULATED_RELATIONS:
        assert populated_store.row_counts[relation] >= POPULATED_ROW_MINIMUM, relation


async def _explain(
    connection: AsyncConnection, statement: sa.Select[tuple[Any, ...]]
) -> list[dict[str, Any]]:
    # ``literal_binds`` inlines the fixture-generated UUID/timestamp/int
    # literals so the EXPLAIN text carries no bound parameters at all; the
    # psycopg pyformat rendering would otherwise be double-escaped through a
    # second ``sa.text`` compile.
    compiled = statement.compile(
        dialect=postgresql.dialect(),
        compile_kwargs={"literal_binds": True},
    )
    assert "%" not in str(compiled), "the compiled plan probe must stay parameter-free"
    result = await connection.execute(sa.text("EXPLAIN (FORMAT JSON) " + str(compiled)))
    payload = result.scalar_one()
    if isinstance(payload, str):
        payload = json.loads(payload)
    return list(payload)


def _plan_nodes(payload: list[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    # ``EXPLAIN (FORMAT JSON)`` wraps the plan tree in a one-element list
    # whose member carries the root under ``Plan``; children live in
    # ``Plans``.
    stack: list[dict[str, Any]] = [
        node for entry in payload for node in (entry.get("Plan", entry),)
    ]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(node.get("Plans", []))


def _index_names(payload: list[dict[str, Any]]) -> set[str]:
    return {
        str(node["Index Name"])
        for node in _plan_nodes(payload)
        if node.get("Index Name") is not None
    }


def _sequential_scan_relations(payload: list[dict[str, Any]]) -> list[str]:
    # ``endswith`` also flags the parallel variants (``Parallel Seq Scan``),
    # which PostgreSQL reports as their own node types beside ``Seq Scan``.
    return [
        str(node["Relation Name"])
        for node in _plan_nodes(payload)
        if str(node["Node Type"]).endswith("Seq Scan") and node.get("Relation Name")
    ]


def _assert_indexed_access(payload: list[dict[str, Any]], query_name: str) -> None:
    approved = _approved_index_names()
    used = _index_names(payload)
    assert used, f"{query_name}: the plan must use at least one index"
    unapproved = used - approved
    assert not unapproved, (
        f"{query_name}: plan uses indexes absent from migration 20260813_01: {sorted(unapproved)}"
    )
    assert used & EXPECTED_INDEX_BY_QUERY[query_name], (
        f"{query_name}: expected one of {sorted(EXPECTED_INDEX_BY_QUERY[query_name])}, "
        f"plan used {sorted(used)}"
    )
    unbounded = [
        relation
        for relation in _sequential_scan_relations(payload)
        if relation in POPULATED_RELATIONS
    ]
    assert not unbounded, f"{query_name}: plan sequentially scans populated relations {unbounded}"


@pytest.mark.asyncio
async def test_current_pointer_lookup_is_indexed(populated_store: QueryPlanPopulation) -> None:
    """The locked source lookup by (source_id, workspace_id) mirrors the
    production ``_select_locked_source`` shape and must go through the
    primary key or the workspace-source unique index."""

    statement = (
        sa.select(
            sources.c.sync_state,
            sources.c.current_version_id,
            sources.c.deleted_at,
        )
        .where(
            sources.c.source_id == populated_store.sample_source_id,
            sources.c.workspace_id == populated_store.workspace_id,
        )
        .with_for_update()
    )
    async with populated_store.engine.connect() as connection:
        payload = await _explain(connection, statement)
    _assert_indexed_access(payload, "current_pointer")


@pytest.mark.asyncio
async def test_idempotent_replay_lookup_is_indexed(
    populated_store: QueryPlanPopulation,
) -> None:
    """The production replay hydration statement must reach the event through
    the (workspace_id, idempotency_key) unique index."""

    statement = replay_lookup_by_key_statement(
        populated_store.workspace_id,
        IdempotencyKey(populated_store.sample_idempotency_key),
    )
    async with populated_store.engine.connect() as connection:
        payload = await _explain(connection, statement)
    _assert_indexed_access(payload, "idempotent_replay")


@pytest.mark.asyncio
async def test_version_history_lookup_is_indexed(populated_store: QueryPlanPopulation) -> None:
    """The newest-first per-source history must run over the
    (workspace_id, source_id, content_version) unique ordinal index."""

    statement = (
        sa.select(
            source_versions.c.source_version_id,
            source_versions.c.content_version,
            source_versions.c.committed_at,
        )
        .where(
            source_versions.c.workspace_id == populated_store.workspace_id,
            source_versions.c.source_id == populated_store.sample_source_id,
        )
        .order_by(source_versions.c.content_version.desc())
        .limit(20)
    )
    async with populated_store.engine.connect() as connection:
        payload = await _explain(connection, statement)
    _assert_indexed_access(payload, "version_history")


@pytest.mark.asyncio
async def test_pending_claim_lookup_is_indexed(populated_store: QueryPlanPopulation) -> None:
    """The production pending-claim select must run over the partial
    pending-dispatch index, never a scan of the 10,000 pending intents."""

    statement = claim_available_select_statement(datetime.now(UTC), 50)
    async with populated_store.engine.connect() as connection:
        payload = await _explain(connection, statement)
    _assert_indexed_access(payload, "pending_claim")
