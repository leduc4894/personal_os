"""Indexed-access acceptance for source-lifecycle queries (Task 11).

The fixture bulk-seeds 10,000 active sources, 10,000 source versions, 10,000
canonical create events, 10,000 source locators (one open locator per source)
and 10,000 pending projection intents (one per source) through schema-qualified
Core batch inserts, runs ``ANALYZE`` so the planner sees real statistics, and
then captures ``EXPLAIN (FORMAT JSON)`` for the four lifecycle hot queries:

- replay by event (``sync_event_lookup_by_event_statement``)
- replay by idempotency (``sync_event_lookup_by_key_statement``)
- active locator by source (the canonical partial unique index)
- active locator by workspace + path (the lifecycle partial unique index)
- pending projection intent claim (the existing pending-dispatch index)

Every query must use at least one index created by migrations
``20260813_01`` and ``20260820_01``, every index the plans touch must belong
to the approved set, and no plan may sequentially scan a populated relation.

The integration tests are gated by the ``local_stack`` marker; the
disposable CI stack is the gate.
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

from postgresql_source_store.engine import (
    create_source_store_engine,
    dispose_source_store_engine,
)
from postgresql_source_store.lifecycle_store import (
    sync_event_lookup_by_event_statement,
    sync_event_lookup_by_key_statement,
)
from postgresql_source_store.projection_intents import claim_available_select_statement
from postgresql_source_store.tables import (
    content_objects,
    projection_intents,
    source_locators,
    source_versions,
    sources,
    sync_events,
    users,
    workspaces,
)

pytestmark = pytest.mark.local_stack

_BASE_MIGRATIONS = (
    Path(__file__).resolve().parents[3]
    / "migrations"
    / "versions"
    / "20260813_01_create_canonical_postgresql_baseline.py",
    Path(__file__).resolve().parents[3]
    / "migrations"
    / "versions"
    / "20260820_01_add_source_locator_lifecycle.py",
)

#: The plan-level acceptance population bound.
_POPULATED_ROW_MINIMUM: Final[int] = 10_000
_SEED_ROW_COUNT: Final[int] = 10_000
_INSERT_BATCH_SIZE: Final[int] = 1_000

#: Every relation populated at scale; a sequential scan over one of these is
#: the unbounded access the plan forbids.
_POPULATED_RELATIONS: Final[frozenset[str]] = frozenset(
    {
        "sources",
        "source_versions",
        "sync_events",
        "projection_intents",
        "content_objects",
        "source_locators",
    }
)

#: The index each hot query must use, keyed by query name.
_EXPECTED_INDEX_BY_QUERY: Final[dict[str, frozenset[str]]] = {
    # The replay-by-event statement is a global ``event_id`` lookup with no
    # workspace/source predicate, so the primary key is the canonical access
    # path; the composite unique indexes lead with workspace_id / source_id
    # and cannot serve this statement shape.
    "replay_by_event": frozenset({"pk_sync_events"}),
    "replay_by_idempotency": frozenset({"uq_sync_events__idempotency_key"}),
    "active_locator_by_source": frozenset({"uq_source_locators_active_source"}),
    "active_locator_by_workspace_path": frozenset({"uq_source_locators_active_workspace_path"}),
    "pending_intent_claim": frozenset({"ix_projection_intents__pending_dispatch"}),
}


def _approved_index_names() -> frozenset[str]:
    """Derive the approved index set from the baseline and lifecycle migration sources.

    ``op.create_index`` first arguments plus the named primary-key and
    unique constraints (whose backing indexes carry the constraint name
    in PostgreSQL) are exactly the indexes the migrations created.
    """

    names: set[str] = set()
    for migration_path in _BASE_MIGRATIONS:
        tree = ast.parse(migration_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if (
                node.func.attr == "create_index"
                and node.args
                and isinstance(node.args[0], ast.Constant)
            ):
                names.add(str(node.args[0].value))
            elif node.func.attr in {"PrimaryKeyConstraint", "UniqueConstraint"}:
                for keyword in node.keywords:
                    if keyword.arg == "name" and isinstance(keyword.value, ast.Constant):
                        names.add(str(keyword.value.value))
    assert names, "the migrations must declare named indexes and constraints"
    return frozenset(names)


@dataclass(frozen=True, slots=True)
class LifecycleQueryPlanPopulation:
    """The populated disposable database and the sample identities to probe."""

    engine: AsyncEngine
    workspace_id: UUID
    sample_source_id: UUID
    sample_event_id: UUID
    sample_idempotency_key: str
    sample_locator: str
    row_counts: dict[str, int]


def _batches(rows: list[dict[str, Any]]) -> Iterator[list[dict[str, Any]]]:
    for start in range(0, len(rows), _INSERT_BATCH_SIZE):
        yield rows[start : start + _INSERT_BATCH_SIZE]


async def _seed_population(engine: AsyncEngine) -> LifecycleQueryPlanPopulation:
    nonce = uuid4().hex
    workspace_id = uuid4()
    owner_user_id = uuid4()
    content_object_ids = [uuid4() for _ in range(_SEED_ROW_COUNT)]
    source_ids = [uuid4() for _ in range(_SEED_ROW_COUNT)]
    version_ids = [uuid4() for _ in range(_SEED_ROW_COUNT)]
    event_ids = [uuid4() for _ in range(_SEED_ROW_COUNT)]
    intent_ids = [uuid4() for _ in range(_SEED_ROW_COUNT)]
    idempotency_keys = [f"qplan-{nonce}-{index:06d}" for index in range(_SEED_ROW_COUNT)]
    locator_paths = [f"notes/qplan-{nonce}-{index:06d}.md" for index in range(_SEED_ROW_COUNT)]

    content_object_rows: list[dict[str, Any]] = []
    for index in range(_SEED_ROW_COUNT):
        salt = f"lifecycle-query-plan-fixture-{nonce}-{index}"
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
    # The sources <-> source_versions foreign keys are circular, so the
    # sources are inserted pending first and activated once their versions
    # exist — the order the canonical writer produces.
    source_rows = [
        {
            "source_id": source_ids[index],
            "workspace_id": workspace_id,
            "source_type": "markdown",
            "title": f"Lifecycle query plan source {index:05d}",
            "sync_state": "pending",
            "current_version_id": None,
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
    locator_rows = [
        {
            "source_locator_id": uuid4(),
            "workspace_id": workspace_id,
            "source_id": source_ids[index],
            "normalized_locator": locator_paths[index],
            "display_locator": locator_paths[index],
            "opened_event_id": event_ids[index],
            "opened_sequence": 1,
        }
        for index in range(_SEED_ROW_COUNT)
    ]
    intent_rows = [
        {
            "projection_intent_id": intent_ids[index],
            "workspace_id": workspace_id,
            "event_id": event_ids[index],
            "source_id": source_ids[index],
            "source_version_id": version_ids[index],
            "projection_kind": "qdrant",
            "operation": "upsert",
        }
        for index in range(_SEED_ROW_COUNT)
    ]
    # Each intent's event parent must be one of the canonical create events
    # this batch inserts (an FK-valid parent) and must never be one of the
    # source-version identities — the historical version-as-event bug class.
    # The check compares against independent identity sets, not the
    # construction expression, so rewiring the rows fails before the insert.
    batch_event_ids = {row["event_id"] for row in event_rows}
    batch_version_ids = set(version_ids)
    assert all(row["event_id"] in batch_event_ids for row in intent_rows)
    assert all(row["event_id"] not in batch_version_ids for row in intent_rows)

    async with engine.begin() as connection:
        await connection.execute(
            sa.insert(users).values(
                user_id=owner_user_id,
                username=f"lqplan-{nonce[:12]}",
                display_name="Lifecycle query plan owner",
            )
        )
        await connection.execute(
            sa.insert(workspaces).values(
                workspace_id=workspace_id,
                owner_user_id=owner_user_id,
                workspace_key=f"lqplan-{nonce[:12]}",
                display_name="Lifecycle query plan workspace",
            )
        )
        for table, rows in (
            (content_objects, content_object_rows),
            (sources, source_rows),
            (source_versions, version_rows),
        ):
            for batch in _batches(rows):
                await connection.execute(sa.insert(table), batch)
        await connection.execute(
            sa.update(sources)
            .values(
                sync_state="active",
                current_version_id=sa.bindparam("pointer_version_id"),
                updated_at=sa.text("CURRENT_TIMESTAMP"),
            )
            .where(
                sources.c.workspace_id == workspace_id,
                sources.c.source_id == sa.bindparam("b_source_id"),
            ),
            [
                {"pointer_version_id": version_id, "b_source_id": source_id}
                for version_id, source_id in zip(version_ids, source_ids, strict=True)
            ],
        )
        for table, rows in (
            (sync_events, event_rows),
            (source_locators, locator_rows),
            (projection_intents, intent_rows),
        ):
            for batch in _batches(rows):
                await connection.execute(sa.insert(table), batch)
        await connection.execute(
            sa.text(
                "ANALYZE knowledge.users, knowledge.workspaces, knowledge.content_objects, "
                "knowledge.sources, knowledge.source_versions, knowledge.sync_events, "
                "knowledge.source_locators, knowledge.projection_intents"
            )
        )

    counts: dict[str, int] = {}
    async with engine.connect() as connection:
        for table in (
            sources,
            source_versions,
            sync_events,
            content_objects,
            source_locators,
        ):
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
    return LifecycleQueryPlanPopulation(
        engine=engine,
        workspace_id=workspace_id,
        sample_source_id=source_ids[sample_index],
        sample_event_id=event_ids[sample_index],
        sample_idempotency_key=idempotency_keys[sample_index],
        sample_locator=locator_paths[sample_index],
        row_counts=counts,
    )


@pytest_asyncio.fixture
async def populated_lifecycle_store(
    source_publication_stack,
) -> Iterator[LifecycleQueryPlanPopulation]:
    """Seed the population once per test on that test's own event loop."""

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
    populated_lifecycle_store: LifecycleQueryPlanPopulation,
) -> None:
    """The acceptance evidence is only valid above the plan's population bound."""

    assert populated_lifecycle_store.row_counts["source_versions"] >= _POPULATED_ROW_MINIMUM
    assert populated_lifecycle_store.row_counts["pending_intents"] >= _POPULATED_ROW_MINIMUM
    assert populated_lifecycle_store.row_counts["source_locators"] >= _POPULATED_ROW_MINIMUM
    for relation in _POPULATED_RELATIONS:
        assert populated_lifecycle_store.row_counts[relation] >= _POPULATED_ROW_MINIMUM, relation


async def _explain(
    connection: AsyncConnection, statement: sa.Select[tuple[Any, ...]]
) -> list[dict[str, Any]]:
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
    return [
        str(node["Relation Name"])
        for node in _plan_nodes(payload)
        if node.get("Node Type") == "Seq Scan" and node.get("Relation Name")
    ]


def _assert_indexed_access(payload: list[dict[str, Any]], query_name: str) -> None:
    approved = _approved_index_names()
    used = _index_names(payload)
    assert used, f"{query_name}: the plan must use at least one index"
    unapproved = used - approved
    assert not unapproved, (
        f"{query_name}: plan uses indexes absent from migrations 20260813_01/20260820_01: "
        f"{sorted(unapproved)}"
    )
    assert used & _EXPECTED_INDEX_BY_QUERY[query_name], (
        f"{query_name}: expected one of {sorted(_EXPECTED_INDEX_BY_QUERY[query_name])}, "
        f"plan used {sorted(used)}"
    )
    unbounded = [
        relation
        for relation in _sequential_scan_relations(payload)
        if relation in _POPULATED_RELATIONS
    ]
    assert not unbounded, f"{query_name}: plan sequentially scans populated relations {unbounded}"


@pytest.mark.asyncio
async def test_replay_by_event_lookup_is_indexed(
    populated_lifecycle_store: LifecycleQueryPlanPopulation,
) -> None:
    """The global replay by event_id lookup must go through the primary key index."""

    statement = sync_event_lookup_by_event_statement(populated_lifecycle_store.sample_event_id)
    async with populated_lifecycle_store.engine.connect() as connection:
        payload = await _explain(connection, statement)
    _assert_indexed_access(payload, "replay_by_event")


@pytest.mark.asyncio
async def test_replay_by_idempotency_lookup_is_indexed(
    populated_lifecycle_store: LifecycleQueryPlanPopulation,
) -> None:
    """The replay by (workspace_id, idempotency_key) lookup must use the unique index."""

    statement = sync_event_lookup_by_key_statement(
        populated_lifecycle_store.workspace_id,
        populated_lifecycle_store.sample_idempotency_key,
    )
    async with populated_lifecycle_store.engine.connect() as connection:
        payload = await _explain(connection, statement)
    _assert_indexed_access(payload, "replay_by_idempotency")


@pytest.mark.asyncio
async def test_active_locator_by_source_lookup_is_indexed(
    populated_lifecycle_store: LifecycleQueryPlanPopulation,
) -> None:
    """The active locator by source query must use the partial unique index."""

    statement = (
        sa.select(source_locators.c.source_locator_id, source_locators.c.normalized_locator)
        .select_from(source_locators)
        .where(
            source_locators.c.source_id == populated_lifecycle_store.sample_source_id,
            source_locators.c.closed_event_id.is_(None),
        )
        .with_for_update()
    )
    async with populated_lifecycle_store.engine.connect() as connection:
        payload = await _explain(connection, statement)
    _assert_indexed_access(payload, "active_locator_by_source")


@pytest.mark.asyncio
async def test_active_locator_by_workspace_path_lookup_is_indexed(
    populated_lifecycle_store: LifecycleQueryPlanPopulation,
) -> None:
    """The active locator by workspace + path query must use the lifecycle partial unique index."""

    statement = (
        sa.select(source_locators.c.source_locator_id, source_locators.c.source_id)
        .select_from(source_locators)
        .where(
            source_locators.c.workspace_id == populated_lifecycle_store.workspace_id,
            source_locators.c.normalized_locator == populated_lifecycle_store.sample_locator,
            source_locators.c.closed_event_id.is_(None),
        )
        .with_for_update()
    )
    async with populated_lifecycle_store.engine.connect() as connection:
        payload = await _explain(connection, statement)
    _assert_indexed_access(payload, "active_locator_by_workspace_path")


@pytest.mark.asyncio
async def test_pending_intent_claim_lookup_is_indexed(
    populated_lifecycle_store: LifecycleQueryPlanPopulation,
) -> None:
    """The pending projection intent claim must run over the pending-dispatch index."""

    statement = claim_available_select_statement(datetime.now(UTC), 50)
    async with populated_lifecycle_store.engine.connect() as connection:
        payload = await _explain(connection, statement)
    _assert_indexed_access(payload, "pending_intent_claim")
