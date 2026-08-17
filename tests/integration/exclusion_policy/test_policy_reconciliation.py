"""Integration contracts of the publication reconciliation over the local stack.

The disposable stack's PostgreSQL and Temporal services back every case: the
leased dispatcher claims the durable intent publication committed, starts the
deterministic ``exclusion-policy-reconciliation/{workspace_id}/{policy_revision_id}``
workflow whose registered activities execute the store's bounded batches, and
the revision's sources receive exactly one immutable evaluation per
``(revision, source, subject_event_sequence)`` and exactly one deterministic
policy-transition intent per ``(revision, source, projection_kind)`` where a
transition requires it. The cases cover the first publication's fail-closed
previous-decision upserts, the parent-revision transition table
(allowed→excluded deletes, excluded→allowed upserts, unchanged none), the
null-current-version gate, the superseded stop without later projection
effects, exactly-once replay after duplicate batches and workflow starts, and
two workers claiming disjointly. Serialized workflow inputs and the audit row
carry no title, locator or operand sentinels.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
import temporalio.api.history.v1
from temporalio.client import Client, WorkflowExecutionStatus
from tests.integration.exclusion_policy.conftest import PolicyMigrationHarness
from workflow_worker.policy_reconciliation_workflow import (
    POLICY_RECONCILIATION_REFERENCE_CONTRACT,
    PolicyReconciliationStartOutcome,
    TemporalPolicyReconciliationStarter,
    reconciliation_input_for_lease,
)
from workflow_worker.policy_workflow_runtime import build_policy_reconciliation_process

from personal_os.exclusion_policy.contracts import RuleKind
from personal_os.exclusion_policy.normalization import normalize_rule
from personal_os.exclusion_policy.reconciliation import ReconciliationCounters
from postgresql_source_store.policy_publication import (
    build_policy_rule_values,
    build_reconciliation_intent_values,
    reconciliation_workflow_id,
)
from postgresql_source_store.policy_reconciliation import (
    RECONCILIATION_COMPLETED_AUDIT_ACTION,
    PostgresqlPolicyReconciliationStore,
)
from postgresql_source_store.tables import (
    policy_drafts,
    policy_reconciliation_intents,
    policy_rules,
    sources,
    sync_events,
    users,
    workspace_policy_state,
    workspaces,
)

pytestmark = pytest.mark.local_stack

_TEMPORAL_NAMESPACE = "knowledge"
_CONVERGENCE_TIMEOUT_SECONDS = 90.0
_SENTINEL_TITLE_FRAGMENT = "Reconciliation Sentinel"

_MEDIA_TYPE_TEXT = "text/markdown"
_BYTE_SIZE = 128


def _temporal_target() -> str:
    return f"127.0.0.1:{os.environ.get('TEMPORAL_GRPC_PORT', '7233')}"


def _sha256_hex(nonce: str) -> str:
    return hashlib.sha256(nonce.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class SeededRevision:
    """One seeded published revision graph plus its durable intent."""

    policy_revision_id: UUID
    revision_number: int
    reconciliation_intent_id: UUID


class ReconciliationHarness:
    """Seed and inspection helpers for one isolated workspace per test."""

    def __init__(self, base: PolicyMigrationHarness, store: PostgresqlPolicyReconciliationStore):
        self.base = base
        self.engine = base.engine
        self.store = store
        self.workspace_owners: dict[UUID, UUID] = {}

    async def database_now(self) -> datetime:
        async with self.engine.connect() as connection:
            result = await connection.execute(sa.text("SELECT CURRENT_TIMESTAMP"))
            value = result.scalar_one()
        assert isinstance(value, datetime)
        return value

    async def seed_workspace(self) -> UUID:
        """Create one isolated user, workspace and policy-state row."""
        workspace_id = uuid4()
        owner_user_id = uuid4()
        nonce = uuid4().hex
        async with self.engine.begin() as connection:
            await connection.execute(
                sa.insert(users).values(
                    user_id=owner_user_id,
                    username=f"recon-owner-{nonce[:12]}",
                    display_name="Reconciliation Owner",
                )
            )
            await connection.execute(
                sa.insert(workspaces).values(
                    workspace_id=workspace_id,
                    owner_user_id=owner_user_id,
                    workspace_key=f"ws-{nonce[:12]}",
                    display_name="Reconciliation Workspace",
                )
            )
            await connection.execute(
                sa.insert(workspace_policy_state).values(
                    workspace_id=workspace_id,
                    active_policy_revision_id=None,
                    active_revision_number=0,
                )
            )
            await connection.execute(
                sa.insert(policy_drafts).values(
                    policy_draft_id=uuid4(),
                    workspace_id=workspace_id,
                    draft_version=1,
                    base_policy_revision_id=None,
                )
            )
        self.workspace_owners[workspace_id] = owner_user_id
        return workspace_id

    async def seed_sources(
        self, workspace_id: UUID, count: int, *, source_type: str = "markdown"
    ) -> list[UUID]:
        """Seed sources with canonical events; only the first gets a version."""
        source_ids = [uuid4() for _ in range(count)]
        async with self.engine.begin() as connection:
            await connection.execute(
                sa.insert(sources).values(
                    [
                        {
                            "source_id": source_id,
                            "workspace_id": workspace_id,
                            "source_type": source_type,
                            "title": f"{_SENTINEL_TITLE_FRAGMENT} {uuid4().hex[:8]}",
                        }
                        for source_id in source_ids
                    ]
                )
            )
            for index, source_id in enumerate(source_ids):
                nonce = uuid4().hex
                await connection.execute(
                    sa.insert(sync_events).values(
                        event_id=uuid4(),
                        workspace_id=workspace_id,
                        source_id=source_id,
                        idempotency_key=f"recon-{nonce}",
                        request_fingerprint=_sha256_hex(nonce),
                        event_type="create",
                    )
                )
                if index == 0:
                    content_object_id = uuid4()
                    source_version_id = uuid4()
                    content_hash = _sha256_hex(f"content-{nonce}")
                    await connection.execute(
                        sa.text(
                            "INSERT INTO knowledge.content_objects"
                            " (content_object_id, content_hash, object_key, byte_size,"
                            " media_type, verified_at)"
                            " VALUES (:content_object_id, :content_hash, :object_key,"
                            " :byte_size, :media_type,"
                            " CURRENT_TIMESTAMP - interval '1 second')"
                        ),
                        {
                            "content_object_id": content_object_id,
                            "content_hash": content_hash,
                            "object_key": (
                                f"objects/sha256/{content_hash[:2]}/{content_hash[2:4]}"
                                f"/{content_hash}"
                            ),
                            "byte_size": _BYTE_SIZE,
                            "media_type": _MEDIA_TYPE_TEXT,
                        },
                    )
                    await connection.execute(
                        sa.text(
                            "INSERT INTO knowledge.source_versions"
                            " (source_version_id, workspace_id, source_id,"
                            " content_object_id, content_version, author_kind)"
                            " VALUES (:source_version_id, :workspace_id, :source_id,"
                            " :content_object_id, 1, 'system')"
                        ),
                        {
                            "source_version_id": source_version_id,
                            "workspace_id": workspace_id,
                            "source_id": source_id,
                            "content_object_id": content_object_id,
                        },
                    )
                    await connection.execute(
                        sa.update(sources)
                        .values(current_version_id=source_version_id, sync_state="active")
                        .where(sources.c.source_id == source_id)
                    )
        return source_ids

    async def seed_published_revision(
        self, workspace_id: UUID, *, rules: tuple[dict[str, Any], ...] = ()
    ) -> SeededRevision:
        """Publish the workspace's next revision with its durable intent."""
        nonce = uuid4().hex
        signing_key_id = uuid4()
        policy_revision_id = uuid4()
        policy_preview_id = uuid4()
        reconciliation_intent_id = uuid4()
        async with self.engine.begin() as connection:
            draft_row = await connection.execute(
                sa.text(
                    "SELECT policy_draft_id FROM knowledge.policy_drafts"
                    " WHERE workspace_id = :workspace_id"
                ),
                {"workspace_id": workspace_id},
            )
            draft_id = draft_row.scalar_one()
            revision_row = await connection.execute(
                sa.text(
                    "SELECT policy_revision_id, revision_number"
                    " FROM knowledge.source_policies WHERE workspace_id = :workspace_id"
                    " ORDER BY revision_number DESC LIMIT 1"
                ),
                {"workspace_id": workspace_id},
            )
            previous = revision_row.first()
            revision_number = 1 if previous is None else int(previous[1]) + 1
            parent_id = None if previous is None else previous[0]
            checkpoint_row = await connection.execute(
                sa.text(
                    "SELECT COALESCE(MAX(event_sequence), 0) FROM knowledge.sync_events"
                    " WHERE workspace_id = :workspace_id"
                ),
                {"workspace_id": workspace_id},
            )
            checkpoint = int(checkpoint_row.scalar_one())
            await connection.execute(
                sa.text(
                    "INSERT INTO knowledge.policy_signing_keys"
                    " (signing_key_id, workspace_id, public_key_bytes,"
                    " introduced_keyset_revision)"
                    " VALUES (:signing_key_id, :workspace_id, :public_key_bytes, 1)"
                ),
                {
                    "signing_key_id": signing_key_id,
                    "workspace_id": workspace_id,
                    "public_key_bytes": _sha256_hex(nonce)[:32].encode("ascii"),
                },
            )
            await connection.execute(
                sa.text(
                    "INSERT INTO knowledge.policy_previews"
                    " (policy_preview_id, workspace_id, policy_draft_id, draft_version,"
                    " draft_sha256, source_checkpoint_event_sequence, state,"
                    " impact_digest, created_by_user_id, ready_at)"
                    " VALUES (:policy_preview_id, :workspace_id, :policy_draft_id, 1,"
                    " :draft_sha256, :checkpoint, 'ready', :impact_digest,"
                    " :created_by_user_id, CURRENT_TIMESTAMP)"
                ),
                {
                    "policy_preview_id": policy_preview_id,
                    "workspace_id": workspace_id,
                    "policy_draft_id": draft_id,
                    "draft_sha256": _sha256_hex(f"draft-{nonce}"),
                    "impact_digest": _sha256_hex(f"impact-{nonce}"),
                    "checkpoint": checkpoint,
                    "created_by_user_id": self.workspace_owners[workspace_id],
                },
            )
            await connection.execute(
                sa.text(
                    "INSERT INTO knowledge.source_policies"
                    " (policy_revision_id, workspace_id, revision_number,"
                    " parent_policy_revision_id, source_checkpoint_event_sequence,"
                    " policy_preview_id, publication_idempotency_key,"
                    " request_fingerprint, snapshot_contract, snapshot_payload_bytes,"
                    " snapshot_payload_sha256, signing_key_id, signature_bytes,"
                    " published_by_user_id)"
                    " VALUES (:policy_revision_id, :workspace_id, :revision_number,"
                    " :parent_id, :checkpoint, :policy_preview_id, :idempotency_key,"
                    " :request_fingerprint, 'exclusion_policy_snapshot/v1',"
                    " :payload_bytes, :payload_sha256, :signing_key_id,"
                    " :signature_bytes, :published_by_user_id)"
                ),
                {
                    "policy_revision_id": policy_revision_id,
                    "workspace_id": workspace_id,
                    "revision_number": revision_number,
                    "parent_id": parent_id,
                    "checkpoint": checkpoint,
                    "policy_preview_id": policy_preview_id,
                    "idempotency_key": f"publish-{nonce}",
                    "request_fingerprint": _sha256_hex(f"request-{nonce}"),
                    "payload_bytes": b"{}",
                    "payload_sha256": _sha256_hex(f"payload-{nonce}"),
                    "signing_key_id": signing_key_id,
                    "signature_bytes": _sha256_hex(nonce).encode("ascii"),
                    "published_by_user_id": self.workspace_owners[workspace_id],
                },
            )
            for index, rule_values in enumerate(rules):
                rule = normalize_rule(
                    uuid4(),
                    RuleKind(rule_values["rule_kind"]),
                    source_id_operand=rule_values.get("source_id_operand"),
                    text_operand=rule_values.get("text_operand"),
                    size_bytes_operand=rule_values.get("size_bytes_operand"),
                    rule_index=index,
                )
                await connection.execute(
                    sa.insert(policy_rules).values(
                        **build_policy_rule_values(policy_revision_id, rule)
                    )
                )
            swapped = await connection.execute(
                sa.text(
                    "UPDATE knowledge.workspace_policy_state"
                    " SET active_policy_revision_id = :policy_revision_id,"
                    " active_revision_number = :revision_number, updated_at = CURRENT_TIMESTAMP"
                    " WHERE workspace_id = :workspace_id"
                ),
                {
                    "policy_revision_id": policy_revision_id,
                    "revision_number": revision_number,
                    "workspace_id": workspace_id,
                },
            )
            assert swapped.rowcount == 1
            occurred_at = await self.database_now()
            await connection.execute(
                sa.insert(policy_reconciliation_intents).values(
                    **build_reconciliation_intent_values(
                        policy_reconciliation_intent_id=reconciliation_intent_id,
                        workspace_id=workspace_id,
                        policy_revision_id=policy_revision_id,
                        workflow_id=reconciliation_workflow_id(workspace_id, policy_revision_id),
                        occurred_at=occurred_at,
                    )
                )
            )
        return SeededRevision(
            policy_revision_id=policy_revision_id,
            revision_number=revision_number,
            reconciliation_intent_id=reconciliation_intent_id,
        )

    # --- inspection helpers ---------------------------------------------------

    async def reconciliation_state(self, workspace_id: UUID) -> str:
        return str(
            await self.base.fetch_scalar(
                "SELECT state FROM knowledge.policy_reconciliation_intents"
                " WHERE workspace_id = :workspace_id",
                {"workspace_id": workspace_id},
            )
        )

    async def evaluation_rows(self, policy_revision_id: UUID) -> list[Any]:
        return await self.base.fetch_all(
            "SELECT source_id, subject_event_sequence, raw_decision, enforced_decision"
            " FROM knowledge.policy_evaluations WHERE policy_revision_id = :revision",
            {"revision": policy_revision_id},
        )

    async def policy_transition_rows(self, policy_revision_id: UUID) -> list[Any]:
        return await self.base.fetch_all(
            "SELECT source_id, projection_kind, operation, status, source_version_id,"
            " event_id, attempt_count"
            " FROM knowledge.projection_intents"
            " WHERE origin_kind = 'policy_transition'"
            " AND policy_revision_id = :revision ORDER BY source_id, projection_kind",
            {"revision": policy_revision_id},
        )

    async def completion_audit_count(self, policy_revision_id: UUID) -> int:
        return int(
            await self.base.fetch_scalar(
                "SELECT count(*) FROM knowledge.audit_events"
                " WHERE action = :action AND target_id = :revision",
                {"action": RECONCILIATION_COMPLETED_AUDIT_ACTION, "revision": policy_revision_id},
            )
        )

    async def run_all_batches_directly(
        self, seeded: SeededRevision, workspace_id: UUID
    ) -> ReconciliationCounters:
        """Drive the store's batches without Temporal for deterministic cases."""
        counters = ReconciliationCounters()
        after_source_id: UUID | None = None
        for _ in range(100):
            outcome = await self.store.run_reconciliation_batch(
                workspace_id, seeded.policy_revision_id, 0, after_source_id
            )
            assert outcome.superseded is False
            counters = ReconciliationCounters(
                evaluated_sources=counters.evaluated_sources + outcome.evaluated_sources,
                to_excluded_sources=counters.to_excluded_sources + outcome.to_excluded_sources,
                to_allowed_sources=counters.to_allowed_sources + outcome.to_allowed_sources,
                unchanged_sources=counters.unchanged_sources + outcome.unchanged_sources,
            )
            if not outcome.has_more:
                break
            after_source_id = outcome.last_source_id
        else:
            pytest.fail("reconciliation did not converge within the bounded attempts")
        return counters


@pytest_asyncio.fixture
async def reconciliation_harness(
    policy_migration_harness: PolicyMigrationHarness,
) -> ReconciliationHarness:
    store = PostgresqlPolicyReconciliationStore(
        policy_migration_harness.engine, lease_token_generator=uuid4
    )
    return ReconciliationHarness(policy_migration_harness, store)


@pytest_asyncio.fixture
async def temporal_reconciliation(
    policy_migration_harness: PolicyMigrationHarness,
) -> Any:
    engine = policy_migration_harness.engine
    client = await Client.connect(_temporal_target(), namespace=_TEMPORAL_NAMESPACE)
    process = build_policy_reconciliation_process(
        engine=engine, temporal_client=client, lease_token_generator=uuid4
    )
    async with process.worker:
        yield {
            "base": policy_migration_harness,
            "client": client,
            "process": process,
            "store": process.dispatch_runtime._store,
        }


async def _wait_until_completed(harness: ReconciliationHarness, revision: UUID) -> None:
    deadline = asyncio.get_running_loop().time() + _CONVERGENCE_TIMEOUT_SECONDS
    while asyncio.get_running_loop().time() < deadline:
        if await harness.completion_audit_count(revision) == 1:
            return
        await asyncio.sleep(0.25)
    pytest.fail("reconciliation did not complete before the bounded deadline")


@pytest.mark.asyncio
async def test_dispatched_workflow_reconciles_a_first_publication(
    temporal_reconciliation: Any,
) -> None:
    base: PolicyMigrationHarness = temporal_reconciliation["base"]
    harness = ReconciliationHarness(base, temporal_reconciliation["store"])
    workspace_id = await harness.seed_workspace()
    source_ids = await harness.seed_sources(workspace_id, 3)
    versioned_source = source_ids[0]
    unversioned_source = source_ids[1]
    seeded = await harness.seed_published_revision(workspace_id)

    runtime = temporal_reconciliation["process"].dispatch_runtime
    assert await runtime.dispatch_pending_reconciliations_once() == 1
    await _wait_until_completed(harness, seeded.policy_revision_id)

    # Exactly one immutable evaluation per source; the durable intent rests at
    # dispatched with the workflow owning it.
    evaluations = await harness.evaluation_rows(seeded.policy_revision_id)
    assert len(evaluations) == 3
    assert all(row.enforced_decision == "allowed" for row in evaluations)
    assert all(int(row.subject_event_sequence) >= 1 for row in evaluations)
    assert await harness.reconciliation_state(workspace_id) == "dispatched"

    # The fail-closed no-policy previous decision (excluded) proposes upserts
    # for exactly the sources with a current version.
    intents = await harness.policy_transition_rows(seeded.policy_revision_id)
    assert [(row.source_id, row.projection_kind) for row in intents] == [
        (versioned_source, "neo4j"),
        (versioned_source, "qdrant"),
    ]
    for row in intents:
        assert row.operation == "upsert"
        assert row.status == "pending"
        assert row.event_id is None
        assert row.attempt_count == 0
        assert row.source_version_id is not None

    # The versionless source got the evaluation evidence only.
    unversioned_intents = [row for row in intents if row.source_id == unversioned_source]
    assert unversioned_intents == []

    workflow_id = reconciliation_workflow_id(workspace_id, seeded.policy_revision_id)
    handle = temporal_reconciliation["client"].get_workflow_handle(workflow_id)
    result = await asyncio.wait_for(handle.result(), timeout=_CONVERGENCE_TIMEOUT_SECONDS)
    assert result == "completed"
    description = await handle.describe()
    assert description.status is WorkflowExecutionStatus.COMPLETED

    history = await handle.fetch_history()
    serialized = temporalio.api.history.v1.History(events=history.events).SerializeToString()
    assert _SENTINEL_TITLE_FRAGMENT.encode() not in serialized
    started_event = next(
        event
        for event in history.events
        if event.HasField("workflow_execution_started_event_attributes")
    )
    (input_payload,) = started_event.workflow_execution_started_event_attributes.input.payloads
    decoded_input = json.loads(input_payload.data)
    assert decoded_input["contract"] == POLICY_RECONCILIATION_REFERENCE_CONTRACT
    assert decoded_input["workspace_id"] == str(workspace_id)
    assert decoded_input["policy_revision_id"] == str(seeded.policy_revision_id)


@pytest.mark.asyncio
async def test_dispatch_is_idempotent_for_an_already_dispatched_revision(
    temporal_reconciliation: Any,
) -> None:
    harness = ReconciliationHarness(
        temporal_reconciliation["base"], temporal_reconciliation["store"]
    )
    workspace_id = await harness.seed_workspace()
    await harness.seed_sources(workspace_id, 1)
    seeded = await harness.seed_published_revision(workspace_id)

    runtime = temporal_reconciliation["process"].dispatch_runtime
    assert await runtime.dispatch_pending_reconciliations_once() == 1
    await _wait_until_completed(harness, seeded.policy_revision_id)

    # The acknowledged row is no longer claimable; a second cycle claims nothing.
    assert await runtime.dispatch_pending_reconciliations_once() == 0
    assert await harness.completion_audit_count(seeded.policy_revision_id) == 1


@pytest.mark.asyncio
async def test_parent_evaluations_drive_the_closed_transition_table(
    reconciliation_harness: ReconciliationHarness,
) -> None:
    harness = reconciliation_harness
    workspace_id = await harness.seed_workspace()
    # Three versioned sources: two markdown (excluded by the second revision)
    # and one text (stays allowed).
    markdown_a = (await harness.seed_sources(workspace_id, 1))[0]
    markdown_b = (await harness.seed_sources(workspace_id, 1))[0]
    text_source = (await harness.seed_sources(workspace_id, 1, source_type="text"))[0]

    first_publication = await harness.seed_published_revision(workspace_id)
    counters = await harness.run_all_batches_directly(first_publication, workspace_id)
    assert counters.evaluated_sources == 3
    assert counters.to_allowed_sources == 3

    second = await harness.seed_published_revision(
        workspace_id,
        rules=({"rule_kind": "source_type", "text_operand": "markdown"},),
    )
    second_counters = await harness.run_all_batches_directly(second, workspace_id)
    assert second_counters.evaluated_sources == 3
    assert second_counters.to_excluded_sources == 2

    # allowed -> excluded derives one delete per projection kind for the two
    # markdown sources; the unchanged text source derives none.
    second_intents = await harness.policy_transition_rows(second.policy_revision_id)
    assert sorted(
        (row.source_id, row.projection_kind, row.operation) for row in second_intents
    ) == sorted(
        [
            (markdown_a, "qdrant", "delete"),
            (markdown_a, "neo4j", "delete"),
            (markdown_b, "qdrant", "delete"),
            (markdown_b, "neo4j", "delete"),
        ]
    )
    assert all(row.source_id != text_source for row in second_intents)

    # The prior revision's upsert intents remain untouched and pending.
    first_intents = await harness.policy_transition_rows(first_publication.policy_revision_id)
    assert len(first_intents) == 6  # three versioned sources x two kinds
    assert all(row.operation == "upsert" and row.status == "pending" for row in first_intents)


@pytest.mark.asyncio
async def test_null_current_version_sources_never_receive_intents(
    reconciliation_harness: ReconciliationHarness,
) -> None:
    harness = reconciliation_harness
    workspace_id = await harness.seed_workspace()
    source_ids = await harness.seed_sources(workspace_id, 2)
    versionless_source = source_ids[1]
    seeded = await harness.seed_published_revision(
        workspace_id,
        rules=({"rule_kind": "source_type", "text_operand": "markdown"},),
    )
    counters = await harness.run_all_batches_directly(seeded, workspace_id)

    # Both sources evaluate (the fail-closed previous decision was excluded,
    # so a still-excluded subject is unchanged), and neither receives an
    # intent: the excluded one is unchanged and the versionless gate holds.
    assert counters.evaluated_sources == 2
    assert counters.to_allowed_sources == 0
    assert counters.to_excluded_sources == 0
    assert await harness.policy_transition_rows(seeded.policy_revision_id) == []
    evaluations = await harness.evaluation_rows(seeded.policy_revision_id)
    assert {row.source_id for row in evaluations} == set(source_ids)
    versionless_evaluations = [row for row in evaluations if row.source_id == versionless_source]
    assert len(versionless_evaluations) == 1
    assert versionless_evaluations[0].enforced_decision == "excluded"


@pytest.mark.asyncio
async def test_superseded_revision_stops_without_later_projection_effects(
    reconciliation_harness: ReconciliationHarness,
) -> None:
    harness = reconciliation_harness
    workspace_id = await harness.seed_workspace()
    await harness.seed_sources(workspace_id, 2)
    superseded = await harness.seed_published_revision(workspace_id)
    newer = await harness.seed_published_revision(workspace_id)

    outcome = await harness.store.run_reconciliation_batch(
        workspace_id, superseded.policy_revision_id, 0, None
    )
    assert outcome.superseded is True
    assert outcome.has_more is False
    assert await harness.evaluation_rows(superseded.policy_revision_id) == []
    assert await harness.policy_transition_rows(superseded.policy_revision_id) == []

    # The newer revision reconciles normally afterwards.
    counters = await harness.run_all_batches_directly(newer, workspace_id)
    assert counters.evaluated_sources == 2


@pytest.mark.asyncio
async def test_duplicate_batches_and_completions_stay_exactly_once(
    reconciliation_harness: ReconciliationHarness,
) -> None:
    harness = reconciliation_harness
    workspace_id = await harness.seed_workspace()
    source_ids = await harness.seed_sources(workspace_id, 2)
    seeded = await harness.seed_published_revision(workspace_id)

    first = await harness.store.run_reconciliation_batch(
        workspace_id, seeded.policy_revision_id, 0, None
    )
    assert first.has_more is False
    # A retry of the same batch (crash replay / duplicate activity) replays
    # the identical page and verifies instead of duplicating.
    second = await harness.store.run_reconciliation_batch(
        workspace_id, seeded.policy_revision_id, 0, None
    )
    assert second.has_more is False

    assert len(await harness.evaluation_rows(seeded.policy_revision_id)) == len(source_ids)
    assert len(await harness.policy_transition_rows(seeded.policy_revision_id)) == 2

    completed_once = await harness.store.complete_reconciliation(
        workspace_id, seeded.policy_revision_id, ReconciliationCounters()
    )
    completed_again = await harness.store.complete_reconciliation(
        workspace_id, seeded.policy_revision_id, ReconciliationCounters()
    )
    assert completed_once is True
    assert completed_again is False
    assert await harness.completion_audit_count(seeded.policy_revision_id) == 1


@pytest.mark.asyncio
async def test_concurrent_duplicate_batches_never_duplicate_effects(
    reconciliation_harness: ReconciliationHarness,
) -> None:
    harness = reconciliation_harness
    workspace_id = await harness.seed_workspace()
    await harness.seed_sources(workspace_id, 3)
    seeded = await harness.seed_published_revision(workspace_id)

    outcomes = await asyncio.gather(
        *[
            harness.store.run_reconciliation_batch(workspace_id, seeded.policy_revision_id, 0, None)
            for _ in range(3)
        ]
    )
    assert all(outcome.has_more is False for outcome in outcomes)
    assert len(await harness.evaluation_rows(seeded.policy_revision_id)) == 3
    assert len(await harness.policy_transition_rows(seeded.policy_revision_id)) == 2


@pytest.mark.asyncio
async def test_two_workers_claim_disjointly_and_lost_acks_converge(
    temporal_reconciliation: Any,
) -> None:
    base: PolicyMigrationHarness = temporal_reconciliation["base"]
    harness = ReconciliationHarness(base, temporal_reconciliation["store"])
    workspace_id = await harness.seed_workspace()
    await harness.seed_sources(workspace_id, 1)
    seeded = await harness.seed_published_revision(workspace_id)
    now = await harness.database_now()

    # The shared module stack carries earlier tests' pending intents, so the
    # disjointness proof scopes to this revision's intent: exactly one of the
    # two claimers leases it, the other never sees it again.
    first_claims = await harness.store.claim_pending(now, 20)
    second_claims = await harness.store.claim_pending(now, 20)
    target_claims = [
        lease
        for lease in first_claims + second_claims
        if lease.policy_revision_id == seeded.policy_revision_id
    ]
    assert len(target_claims) == 1
    lease = target_claims[0]
    assert lease in first_claims
    assert lease not in second_claims
    assert lease.workflow_id == reconciliation_workflow_id(workspace_id, seeded.policy_revision_id)
    assert lease.source_checkpoint_event_sequence >= 0

    # Simulate the workflow completing, then a lost acknowledgement: the row
    # returns to pending and the deterministic re-dispatch resolves the
    # completed execution — never a second run.
    starter = TemporalPolicyReconciliationStarter(temporal_reconciliation["client"])
    reference = reconciliation_input_for_lease(lease)
    assert await starter.start_policy_reconciliation(reference) is (
        PolicyReconciliationStartOutcome.STARTED
    )
    await _wait_until_completed(harness, seeded.policy_revision_id)
    await harness.store.acknowledge_dispatched(
        lease.policy_reconciliation_intent_id, lease.lease_token, await harness.database_now()
    )
    # A lost acknowledgement returns the dispatched row to pending.
    async with harness.engine.begin() as connection:
        await connection.execute(
            sa.text(
                "UPDATE knowledge.policy_reconciliation_intents"
                " SET state = 'pending', lease_token = NULL, leased_until = NULL,"
                " dispatched_at = NULL, available_at = CURRENT_TIMESTAMP"
                " WHERE policy_reconciliation_intent_id = :intent_id"
            ),
            {"intent_id": lease.policy_reconciliation_intent_id},
        )
    runtime = temporal_reconciliation["process"].dispatch_runtime
    # The cycle re-dispatches at least this revision (other workspaces' due
    # intents may ride along); ours converges on the one completed execution.
    assert await runtime.dispatch_pending_reconciliations_once() >= 1
    await _wait_until_completed(harness, seeded.policy_revision_id)
    assert await harness.completion_audit_count(seeded.policy_revision_id) == 1
    workflow_id = reconciliation_workflow_id(workspace_id, seeded.policy_revision_id)
    executions = [
        execution
        async for execution in temporal_reconciliation["client"].list_workflows(
            f"WorkflowId='{workflow_id}'"
        )
    ]
    assert len(executions) == 1
    assert await harness.evaluation_rows(seeded.policy_revision_id)


@pytest.mark.asyncio
async def test_dependency_failure_returns_the_intent_to_pending_with_backoff(
    reconciliation_harness: ReconciliationHarness,
) -> None:
    harness = reconciliation_harness
    workspace_id = await harness.seed_workspace()
    await harness.seed_sources(workspace_id, 1)
    seeded = await harness.seed_published_revision(workspace_id)
    # Move the row into the workflow-owned dispatched state.
    async with harness.engine.begin() as connection:
        await connection.execute(
            sa.text(
                "UPDATE knowledge.policy_reconciliation_intents"
                " SET state = 'dispatched', lease_token = NULL, leased_until = NULL,"
                " dispatched_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP"
                " WHERE policy_reconciliation_intent_id = :intent_id"
            ),
            {"intent_id": seeded.reconciliation_intent_id},
        )
    assert await harness.reconciliation_state(workspace_id) == "dispatched"

    from postgresql_source_store.policy_reconciliation import (
        RECONCILIATION_EXECUTION_FAILED_ERROR_CODE,
    )

    released = await harness.store.fail_reconciliation(
        workspace_id,
        seeded.policy_revision_id,
        RECONCILIATION_EXECUTION_FAILED_ERROR_CODE,
        retryable=True,
    )
    assert released is True
    state = await harness.base.fetch_all(
        "SELECT state, safe_error_code, attempt_count, lease_token"
        " FROM knowledge.policy_reconciliation_intents"
        " WHERE workspace_id = :workspace_id",
        {"workspace_id": workspace_id},
    )
    assert state[0].state == "pending"
    assert state[0].safe_error_code == "reconciliation_execution_failed"
    assert int(state[0].attempt_count) == 1
    assert state[0].lease_token is None
    failure_count = await harness.base.fetch_scalar(
        "SELECT count(*) FROM knowledge.audit_events"
        " WHERE action = 'exclusion_policy.reconciliation_failed'"
        " AND target_id = :revision",
        {"revision": seeded.policy_revision_id},
    )
    assert int(failure_count) == 1
