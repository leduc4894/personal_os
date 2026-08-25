"""Indexed-access acceptance for device cursor pull and action pagination.

The fixture bulk-seeds 10,000 canonical events (one workspace, the full
create/update/delete/restore composition), their 10,000 sources, versions
and content objects, the locators each operation-shaped event opened or
closed, 300 tombstones, one planned manifest run and 10,000 frozen manifest
actions through schema-qualified Core batch inserts, runs ``ANALYZE`` so the
planner sees real statistics, and then captures ``EXPLAIN (FORMAT JSON)``
for the five device sync hot queries:

- the pull statement checkpoint (one descending head read)
- the bounded pull page (hydration joins included)
- the acknowledge cursor-row lock
- the workspace compaction floor over active devices
- the manifest action pagination

Every query must use at least one index created by the shipped migrations,
every index the plans touch must belong to the approved set, and no plan may
sequentially scan a populated relation. ``source_tombstones`` is deliberately
pinned at 300 rows and outside the populated set: the schema authority owns
no ``restore_event_id`` index (the delete-side unique index covers the
delete join), so the restore-side hydration join stays bounded by this
pinned fixture size rather than by an index this child may not add; the
driving access and every other populated relation must still be indexed.

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
from tests.integration.device_sync.conftest import seed_device_sync_workspace
from tests.integration.source_publication.conftest import SourcePublicationStack

from postgresql_source_store.device_event_store import (
    device_cursor_select_statement,
    device_event_checkpoint_statement,
    device_pull_page_statement,
    manifest_action_page_statement,
    workspace_minimum_acknowledged_statement,
)
from postgresql_source_store.engine import (
    create_source_store_engine,
    dispose_source_store_engine,
)
from postgresql_source_store.tables import (
    content_objects,
    device_cursors,
    devices,
    manifest_actions,
    manifest_runs,
    source_locators,
    source_tombstones,
    source_versions,
    sources,
    sync_events,
    users,
    workspaces,
)

pytestmark = pytest.mark.local_stack

#: Every shipped migration source, derived from the versions directory so a
#: new migration's indexes join the approved set without editing this file.
_MIGRATIONS_DIRECTORY = Path(__file__).resolve().parents[3] / "migrations" / "versions"
_MIGRATIONS: Final[tuple[Path, ...]] = tuple(sorted(_MIGRATIONS_DIRECTORY.glob("*.py")))

#: The plan-level acceptance population bound.
_POPULATED_ROW_MINIMUM: Final[int] = 10_000
#: Event population; 12,000 events keep the locator population (88% of
#: events: every non-update, non-delete event opens exactly one locator)
#: above the ten-thousand-row bound.
_SEED_ROW_COUNT: Final[int] = 12_000
#: Background cursor population: one thousand foreign workspaces with five
#: active devices each, so the cursor and floor plans see a realistically
#: populated ``device_cursors`` relation instead of a single-row table the
#: planner would always sequentially scan.
_CURSOR_WORKSPACE_COUNT: Final[int] = 1_000
_CURSOR_DEVICES_PER_WORKSPACE: Final[int] = 5
_INSERT_BATCH_SIZE: Final[int] = 1_000

#: Every relation populated at scale; a sequential scan over one of these is
#: the unbounded access the plan forbids. ``source_tombstones`` stays pinned
#: at a smaller size outside this set (see the module docstring).
_POPULATED_RELATIONS: Final[frozenset[str]] = frozenset(
    {
        "sync_events",
        "sources",
        "source_versions",
        "content_objects",
        "source_locators",
        "manifest_actions",
    }
)

#: The index each hot query must use, keyed by query name.
_EXPECTED_INDEX_BY_QUERY: Final[dict[str, frozenset[str]]] = {
    "pull_checkpoint": frozenset({"uq_sync_events__event_sequence"}),
    "pull_page": frozenset({"uq_sync_events__event_sequence"}),
    "cursor_lock": frozenset({"uq_device_cursors_workspace_device"}),
    "compaction_floor": frozenset({"uq_device_cursors_workspace_device"}),
    "action_page": frozenset({"pk_manifest_actions"}),
}


def _approved_index_names() -> frozenset[str]:
    """Derive the approved index set from the shipped migration sources.

    ``op.create_index`` first arguments plus the named primary-key and
    unique constraints (whose backing indexes carry the constraint name
    in PostgreSQL) are exactly the indexes the migrations created.
    """

    names: set[str] = set()
    for migration_path in _MIGRATIONS:
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
class DeviceSyncQueryPlanPopulation:
    """The populated disposable database and the probe identities."""

    engine: AsyncEngine
    workspace_id: UUID
    device_id: UUID
    manifest_run_id: UUID
    median_event_sequence: int
    maximum_event_sequence: int
    row_counts: dict[str, int]


def _batches(rows: list[dict[str, Any]]) -> Iterator[list[dict[str, Any]]]:
    for start in range(0, len(rows), _INSERT_BATCH_SIZE):
        yield rows[start : start + _INSERT_BATCH_SIZE]


async def _seed_population(engine: AsyncEngine) -> DeviceSyncQueryPlanPopulation:
    nonce = uuid4().hex
    workspace = await seed_device_sync_workspace(engine)
    workspace_id = workspace.workspace_id
    device_id = workspace.device_id
    content_object_ids = [uuid4() for _ in range(_SEED_ROW_COUNT)]
    source_ids = [uuid4() for _ in range(_SEED_ROW_COUNT)]
    version_ids = [uuid4() for _ in range(_SEED_ROW_COUNT)]
    event_ids = [uuid4() for _ in range(_SEED_ROW_COUNT)]
    now = datetime.now(UTC)

    def event_type_for(index: int) -> str:
        if index % 100 == 0:
            return "restore"
        if index % 50 == 0:
            return "delete"
        if index % 10 == 0:
            return "update"
        return "create"

    event_types = [event_type_for(index) for index in range(_SEED_ROW_COUNT)]

    content_object_rows: list[dict[str, Any]] = []
    for index in range(_SEED_ROW_COUNT):
        content_hash = hashlib.sha256(f"ds-plan-{nonce}-{index}".encode("ascii")).hexdigest()
        content_object_rows.append(
            {
                "content_object_id": content_object_ids[index],
                "content_hash": content_hash,
                "object_key": (
                    f"objects/sha256/{content_hash[:2]}/{content_hash[2:4]}/{content_hash}"
                ),
                "byte_size": 96,
                "media_type": "text/markdown",
                "verified_at": now,
            }
        )
    source_rows = [
        {
            "source_id": source_ids[index],
            "workspace_id": workspace_id,
            "source_type": "markdown",
            "title": f"Device plan source {index:05d}",
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
            "author_kind": "device",
            "author_id": device_id,
        }
        for index in range(_SEED_ROW_COUNT)
    ]
    event_rows = [
        {
            "event_id": event_ids[index],
            "workspace_id": workspace_id,
            "source_id": source_ids[index],
            "device_id": device_id if index % 3 == 0 else None,
            "committed_version_id": version_ids[index],
            "base_version_id": version_ids[index] if event_types[index] != "create" else None,
            "idempotency_key": f"ds-plan-{nonce}-{index:06d}",
            "request_fingerprint": hashlib.sha256(
                f"ds-plan-{nonce}-{index:06d}".encode("ascii")
            ).hexdigest(),
            "event_type": event_types[index],
        }
        for index in range(_SEED_ROW_COUNT)
    ]

    manifest_run_id = uuid4()
    async with engine.begin() as connection:
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
        sequence_by_event_id: dict[UUID, int] = {}
        for batch in _batches(event_rows):
            inserted = await connection.execute(
                sa.insert(sync_events)
                .values(batch)
                .returning(sync_events.c.event_id, sync_events.c.event_sequence)
            )
            for row in inserted:
                sequence_by_event_id[row.event_id] = int(row.event_sequence)

        locator_rows: list[dict[str, Any]] = []
        tombstone_rows: list[dict[str, Any]] = []
        for index, event_type in enumerate(event_types):
            event_id = event_ids[index]
            sequence = sequence_by_event_id[event_id]
            if event_type == "delete":
                # Deletes contribute their tombstone evidence. The prior
                # locator closure is not materialized here: the fixture gives
                # every source exactly one event and the closure foreign key
                # requires the opening and closing events of one source.
                # Hydration correctness for closures is proven by the
                # transaction suite; this fixture feeds the planner.
                tombstone_rows.append(
                    {
                        "source_tombstone_id": uuid4(),
                        "workspace_id": workspace_id,
                        "source_id": source_ids[index],
                        "delete_event_id": event_id,
                        "retained_version_id": version_ids[index],
                        "retained_locator": f"notes/{nonce}/{index:06d}.md",
                        "actor_kind": "device",
                        "actor_id": device_id,
                        "deleted_at": now,
                        "restore_event_id": None,
                        "restored_at": None,
                    }
                )
                continue
            if event_type == "restore":
                locator_rows.append(
                    {
                        "source_locator_id": uuid4(),
                        "workspace_id": workspace_id,
                        "source_id": source_ids[index],
                        "normalized_locator": f"notes/{nonce}/r{index:06d}.md",
                        "display_locator": f"notes/{nonce}/r{index:06d}.md",
                        "opened_event_id": event_id,
                        "opened_sequence": sequence,
                    }
                )
                # The tombstone foreign keys are composite per source and the
                # fixture gives every source exactly one event, so the
                # restored tombstone references its own restore event on both
                # sides; both joins then carry representative rows for the
                # planner. Real restore shapes are proven by the transaction
                # suite.
                tombstone_rows.append(
                    {
                        "source_tombstone_id": uuid4(),
                        "workspace_id": workspace_id,
                        "source_id": source_ids[index],
                        "delete_event_id": event_id,
                        "retained_version_id": version_ids[index],
                        "retained_locator": f"notes/{nonce}/r{index:06d}.md",
                        "actor_kind": "device",
                        "actor_id": device_id,
                        "deleted_at": now,
                        "restore_event_id": event_id,
                        "restored_at": now,
                    }
                )
                continue
            if event_type == "create":
                locator_rows.append(
                    {
                        "source_locator_id": uuid4(),
                        "workspace_id": workspace_id,
                        "source_id": source_ids[index],
                        "normalized_locator": f"notes/{nonce}/{index:06d}.md",
                        "display_locator": f"notes/{nonce}/{index:06d}.md",
                        "opened_event_id": event_id,
                        "opened_sequence": sequence,
                    }
                )
        for batch in _batches(locator_rows):
            await connection.execute(sa.insert(source_locators), batch)
        for batch in _batches(tombstone_rows):
            await connection.execute(sa.insert(source_tombstones), batch)

        await connection.execute(
            sa.insert(manifest_runs).values(
                manifest_run_id=manifest_run_id,
                workspace_id=workspace_id,
                device_id=device_id,
                base_acknowledged_sequence=0,
                checkpoint_sequence=_SEED_ROW_COUNT,
                policy_revision_number=1,
                client_observation_generation=0,
                state="planned",
                next_page_number=0,
                entry_count=_SEED_ROW_COUNT,
                final_digest=hashlib.sha256(f"ds-plan-{nonce}".encode("ascii")).hexdigest(),
                planned_at=now,
            )
        )
        action_rows = [
            {
                "manifest_run_id": manifest_run_id,
                "action_index": index,
                "action_kind": "no_change",
                "local_entry_id": f"ds-plan-{nonce}-{index:06d}",
                "source_id": source_ids[index],
                "source_version_id": version_ids[index],
            }
            for index in range(_SEED_ROW_COUNT)
        ]
        for batch in _batches(action_rows):
            await connection.execute(sa.insert(manifest_actions), batch)
        await connection.execute(
            sa.insert(device_cursors).values(
                device_cursor_id=uuid4(),
                workspace_id=workspace_id,
                device_id=device_id,
                acknowledged_sequence=0,
                delivered_through_sequence=0,
            )
        )
        # The background cursor population: foreign workspaces with active
        # devices and their cursors, so the probed workspace's rows are the
        # selective minority the indexed plans must find.
        background_user_ids = [uuid4() for _ in range(_CURSOR_WORKSPACE_COUNT)]
        background_workspace_ids = [uuid4() for _ in range(_CURSOR_WORKSPACE_COUNT)]
        background_user_rows = [
            {
                "user_id": background_user_ids[index],
                "username": f"ds-plan-bg-{nonce[:8]}-{index:05d}",
                "display_name": "Device plan background owner",
            }
            for index in range(_CURSOR_WORKSPACE_COUNT)
        ]
        background_workspace_rows = [
            {
                "workspace_id": background_workspace_ids[index],
                "owner_user_id": background_user_ids[index],
                "workspace_key": f"ds-plan-bg-{nonce[:8]}-{index:05d}",
                "display_name": "Device plan background workspace",
            }
            for index in range(_CURSOR_WORKSPACE_COUNT)
        ]
        background_device_rows = []
        background_cursor_rows = []
        for workspace_index in range(_CURSOR_WORKSPACE_COUNT):
            for device_index in range(_CURSOR_DEVICES_PER_WORKSPACE):
                background_device_id = uuid4()
                background_device_rows.append(
                    {
                        "device_id": background_device_id,
                        "workspace_id": background_workspace_ids[workspace_index],
                        "user_id": background_user_ids[workspace_index],
                        "device_name": f"Background device {workspace_index}/{device_index}",
                        "device_kind": "obsidian",
                    }
                )
                acknowledged = (workspace_index * 31 + device_index * 7) % 9_000
                background_cursor_rows.append(
                    {
                        "device_cursor_id": uuid4(),
                        "workspace_id": background_workspace_ids[workspace_index],
                        "device_id": background_device_id,
                        "acknowledged_sequence": acknowledged,
                        "delivered_through_sequence": acknowledged + 100,
                    }
                )
        for batch in _batches(background_user_rows):
            await connection.execute(sa.insert(users), batch)
        for batch in _batches(background_workspace_rows):
            await connection.execute(sa.insert(workspaces), batch)
        for batch in _batches(background_device_rows):
            await connection.execute(sa.insert(devices), batch)
        for batch in _batches(background_cursor_rows):
            await connection.execute(sa.insert(device_cursors), batch)
        await connection.execute(
            sa.text(
                "ANALYZE knowledge.users, knowledge.workspaces, "
                "knowledge.content_objects, knowledge.sources, "
                "knowledge.source_versions, knowledge.sync_events, "
                "knowledge.source_locators, knowledge.source_tombstones, "
                "knowledge.manifest_runs, knowledge.manifest_actions, "
                "knowledge.device_cursors, knowledge.devices"
            )
        )

    counts: dict[str, int] = {}
    sequences: dict[str, int] = {}
    async with engine.connect() as connection:
        for table in (
            sync_events,
            sources,
            source_versions,
            content_objects,
            source_locators,
            manifest_actions,
            source_tombstones,
        ):
            total = int(
                (
                    await connection.execute(sa.select(sa.func.count()).select_from(table))
                ).scalar_one()
            )
            counts[table.name] = total
        bounds = (
            await connection.execute(
                sa.select(
                    sa.func.min(sync_events.c.event_sequence),
                    sa.func.max(sync_events.c.event_sequence),
                ).where(sync_events.c.workspace_id == workspace_id)
            )
        ).one()
        sequences["minimum"] = int(bounds[0])
        sequences["maximum"] = int(bounds[1])

    return DeviceSyncQueryPlanPopulation(
        engine=engine,
        workspace_id=workspace_id,
        device_id=device_id,
        manifest_run_id=manifest_run_id,
        median_event_sequence=(sequences["minimum"] + sequences["maximum"]) // 2,
        maximum_event_sequence=sequences["maximum"],
        row_counts=counts,
    )


@pytest_asyncio.fixture
async def populated_device_sync_store(
    source_publication_stack: SourcePublicationStack,
) -> Iterator[DeviceSyncQueryPlanPopulation]:
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
    populated_device_sync_store: DeviceSyncQueryPlanPopulation,
) -> None:
    """The acceptance evidence is only valid above the plan's population bound."""

    for relation in _POPULATED_RELATIONS:
        assert populated_device_sync_store.row_counts[relation] >= _POPULATED_ROW_MINIMUM, relation
    assert populated_device_sync_store.row_counts["source_tombstones"] >= 200


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
        f"{query_name}: plan uses indexes absent from the shipped migrations: "
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
async def test_pull_checkpoint_read_is_indexed(
    populated_device_sync_store: DeviceSyncQueryPlanPopulation,
) -> None:
    """The descending head read must stop on the event-sequence unique index."""

    statement = device_event_checkpoint_statement(populated_device_sync_store.workspace_id)
    async with populated_device_sync_store.engine.connect() as connection:
        payload = await _explain(connection, statement)
    _assert_indexed_access(payload, "pull_checkpoint")


@pytest.mark.asyncio
async def test_pull_page_statement_is_indexed(
    populated_device_sync_store: DeviceSyncQueryPlanPopulation,
) -> None:
    """The bounded pull page must drive on the event-sequence unique index."""

    statement = device_pull_page_statement(
        populated_device_sync_store.workspace_id,
        after_sequence=populated_device_sync_store.median_event_sequence,
        through_sequence=populated_device_sync_store.maximum_event_sequence,
        limit=201,
    )
    async with populated_device_sync_store.engine.connect() as connection:
        payload = await _explain(connection, statement)
    _assert_indexed_access(payload, "pull_page")


@pytest.mark.asyncio
async def test_cursor_lock_is_indexed(
    populated_device_sync_store: DeviceSyncQueryPlanPopulation,
) -> None:
    """The acknowledge row lock must go through the workspace/device unique index."""

    statement = device_cursor_select_statement(
        populated_device_sync_store.workspace_id,
        populated_device_sync_store.device_id,
        for_update=True,
    )
    async with populated_device_sync_store.engine.connect() as connection:
        payload = await _explain(connection, statement)
    _assert_indexed_access(payload, "cursor_lock")


@pytest.mark.asyncio
async def test_workspace_compaction_floor_is_indexed(
    populated_device_sync_store: DeviceSyncQueryPlanPopulation,
) -> None:
    """The active-device minimum acknowledgement must use the cursor unique index."""

    statement = workspace_minimum_acknowledged_statement(
        populated_device_sync_store.workspace_id
    )
    async with populated_device_sync_store.engine.connect() as connection:
        payload = await _explain(connection, statement)
    _assert_indexed_access(payload, "compaction_floor")


@pytest.mark.asyncio
async def test_manifest_action_pagination_is_indexed(
    populated_device_sync_store: DeviceSyncQueryPlanPopulation,
) -> None:
    """The action page must walk the manifest action primary key in order."""

    statement = manifest_action_page_statement(
        populated_device_sync_store.manifest_run_id,
        after_action_index=5_000,
        limit=501,
    )
    async with populated_device_sync_store.engine.connect() as connection:
        payload = await _explain(connection, statement)
    _assert_indexed_access(payload, "action_page")
