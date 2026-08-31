"""Exclusion policy schema migration against the real migrated PostgreSQL.

The module fixture stages the upgrade through the Child 2 head with one
existing workspace and one pending source-event intent, so these tests prove
spec section 8.7 directly: the per-workspace bootstrap seeding, the
preservation/backfill of existing projection intents, catalog reflection of
columns, partial indexes and append-only triggers, the unique identities
(revision number, evaluation triple, preview-result preview binding), both
projection-intent origin shapes against the database CHECK, claim isolation of
policy-transition intents from the source dispatcher, the migration-level
refusal of a downgrade with protected rows, and the deterministic gated
downgrade back to the Child 2 head.
"""

from __future__ import annotations

import hashlib
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic.util import CommandError
from tests.integration.exclusion_policy.conftest import (
    PolicyMigrationHarness,
    run_guarded_alembic,
    run_inprocess_alembic_downgrade,
)

from personal_os.sources.projection_dispatch import PROJECTION_CLAIM_BATCH_LIMIT


def _fingerprint(*, nonce: str) -> str:
    """Lowercase SHA-256 hex suitable for the semantic_fingerprint CHECKs."""

    return hashlib.sha256(nonce.encode("utf-8")).hexdigest()


pytestmark = pytest.mark.local_stack

_POLICY_TABLES: frozenset[str] = frozenset(
    {
        "workspace_policy_state",
        "policy_drafts",
        "policy_draft_rules",
        "source_policies",
        "policy_rules",
        "policy_previews",
        "policy_preview_results",
        "policy_evaluations",
        "policy_reconciliation_intents",
        "policy_signing_keys",
        "policy_keysets",
        "policy_keyset_signatures",
    }
)

_APPEND_ONLY_TRIGGER_TABLES: tuple[str, ...] = (
    "source_policies",
    "policy_rules",
    "policy_evaluations",
    "policy_signing_keys",
    "policy_keysets",
    "policy_keyset_signatures",
)


async def _table_exists(harness: PolicyMigrationHarness, table_name: str) -> bool:
    count = await harness.fetch_scalar(
        "SELECT count(*) FROM information_schema.tables"
        " WHERE table_schema = 'knowledge' AND table_name = :table_name",
        {"table_name": table_name},
    )
    return int(count) == 1


async def _application_table_count(harness: PolicyMigrationHarness) -> int:
    count = await harness.fetch_scalar(
        "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'knowledge'", {}
    )
    return int(count)


async def _schema_head(harness: PolicyMigrationHarness) -> str:
    head = await harness.fetch_scalar("SELECT version_num FROM public.alembic_version", {})
    return str(head)


@pytest.mark.asyncio
async def test_upgrade_seeds_policy_state_and_empty_draft_per_existing_workspace(
    policy_migration_harness: PolicyMigrationHarness,
) -> None:
    workspace_id = policy_migration_harness.stack.workspace_id

    state = await policy_migration_harness.fetch_all(
        "SELECT active_policy_revision_id, active_revision_number"
        " FROM knowledge.workspace_policy_state WHERE workspace_id = :workspace_id",
        {"workspace_id": workspace_id},
    )
    assert len(state) == 1
    assert state[0][0] is None
    assert int(state[0][1]) == 0

    workspace_count = await policy_migration_harness.fetch_scalar(
        "SELECT count(*) FROM knowledge.workspaces", {}
    )
    state_count = await policy_migration_harness.fetch_scalar(
        "SELECT count(*) FROM knowledge.workspace_policy_state", {}
    )
    assert int(state_count) == int(workspace_count)

    draft = await policy_migration_harness.fetch_all(
        "SELECT draft_version, base_policy_revision_id, created_by_user_id"
        " FROM knowledge.policy_drafts WHERE workspace_id = :workspace_id",
        {"workspace_id": workspace_id},
    )
    assert len(draft) == 1
    assert int(draft[0][0]) == 1
    assert draft[0][1] is None
    assert draft[0][2] is None

    draft_rule_count = await policy_migration_harness.fetch_scalar(
        "SELECT count(*) FROM knowledge.policy_draft_rules", {}
    )
    assert int(draft_rule_count) == 0


@pytest.mark.asyncio
async def test_upgrade_preserves_and_backfills_existing_source_event_intents(
    policy_migration_harness: PolicyMigrationHarness,
) -> None:
    stack = policy_migration_harness.stack
    row = await policy_migration_harness.fetch_all(
        "SELECT origin_kind, event_id, source_version_id, policy_revision_id, status, operation"
        " FROM knowledge.projection_intents WHERE event_id = :event_id",
        {"event_id": stack.seeded_event_id},
    )
    assert len(row) == 1
    origin_kind, event_id, source_version_id, policy_revision_id, status, operation = row[0]
    assert origin_kind == "source_event"
    assert event_id == stack.seeded_event_id
    assert source_version_id == stack.seeded_source_version_id
    assert policy_revision_id is None
    assert status == "pending"
    assert operation == "delete"


@pytest.mark.asyncio
async def test_upgraded_schema_reflects_the_policy_contract(
    policy_migration_harness: PolicyMigrationHarness,
) -> None:
    for table_name in _POLICY_TABLES:
        assert await _table_exists(policy_migration_harness, table_name), table_name
    assert await _application_table_count(policy_migration_harness) == 39

    origin_column = await policy_migration_harness.fetch_all(
        "SELECT is_nullable FROM information_schema.columns"
        " WHERE table_schema = 'knowledge' AND table_name = 'projection_intents'"
        " AND column_name = 'origin_kind'",
        {},
    )
    assert len(origin_column) == 1
    assert origin_column[0][0] == "NO"

    partial_indexes = await policy_migration_harness.fetch_all(
        "SELECT pg_get_indexdef(indexrelid) FROM pg_index"
        " WHERE indrelid IN ("
        "   'knowledge.policy_previews'::regclass,"
        "   'knowledge.policy_reconciliation_intents'::regclass,"
        "   'knowledge.projection_intents'::regclass)"
        " AND indpred IS NOT NULL",
        {},
    )
    index_definitions = {row[0] for row in partial_indexes}
    assert any(
        "ix_policy_previews__pending_dispatch" in definition
        and "state = 'pending'::text" in definition
        for definition in index_definitions
    )
    assert any(
        "ix_policy_reconciliation_intents__pending_dispatch" in definition
        and "state = 'pending'::text" in definition
        for definition in index_definitions
    )
    assert any(
        "uq_projection_intents__policy_transition" in definition
        and "origin_kind = 'policy_transition'::text" in definition
        for definition in index_definitions
    )

    trigger_names = {
        row[0]
        for row in await policy_migration_harness.fetch_all(
            "SELECT tgname FROM pg_trigger"
            " JOIN pg_class ON pg_trigger.tgrelid = pg_class.oid"
            " JOIN pg_namespace ON pg_class.relnamespace = pg_namespace.oid"
            " WHERE pg_namespace.nspname = 'knowledge' AND NOT pg_trigger.tgisinternal",
            {},
        )
    }
    for table_name in _APPEND_ONLY_TRIGGER_TABLES:
        assert f"trg_{table_name}__reject_mutation" in trigger_names, table_name

    origin_check = await policy_migration_harness.fetch_all(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint"
        " WHERE conrelid = 'knowledge.projection_intents'::regclass"
        " AND conname = 'ck_projection_intents__origin'",
        {},
    )
    assert len(origin_check) == 1
    assert "origin_kind = 'source_event'::text) = (event_id IS NOT NULL" in origin_check[0][0]
    assert (
        "origin_kind = 'policy_transition'::text) = (policy_revision_id IS NOT NULL"
        in origin_check[0][0]
    )


@pytest.mark.asyncio
async def test_revision_number_is_unique_per_workspace(
    policy_migration_harness: PolicyMigrationHarness,
) -> None:
    graph = await policy_migration_harness.seed_published_policy()
    nonce = uuid4().hex
    with pytest.raises(sa.exc.IntegrityError):
        async with policy_migration_harness.engine.begin() as connection:
            await connection.execute(
                sa.text(
                    "INSERT INTO knowledge.source_policies"
                    " (policy_revision_id, workspace_id, revision_number,"
                    " source_checkpoint_event_sequence, policy_preview_id,"
                    " publication_idempotency_key, request_fingerprint,"
                    " snapshot_contract, snapshot_payload_bytes, snapshot_payload_sha256,"
                    " signing_key_id, signature_bytes, published_by_user_id)"
                    " VALUES (:policy_revision_id, :workspace_id, :revision_number, 0,"
                    " :policy_preview_id,"
                    " :idempotency_key, :request_fingerprint, 'exclusion_policy_snapshot/v1',"
                    " '{}', :payload_sha256, :signing_key_id, :signature_bytes,"
                    " :published_by_user_id)"
                ),
                {
                    "policy_revision_id": uuid4(),
                    "workspace_id": policy_migration_harness.stack.workspace_id,
                    "revision_number": graph.revision_number,
                    "policy_preview_id": graph.policy_preview_id,
                    "idempotency_key": f"duplicate-{nonce}",
                    "request_fingerprint": nonce,
                    "payload_sha256": nonce,
                    "signing_key_id": graph.signing_key_id,
                    "signature_bytes": nonce.encode("ascii")[:64].ljust(64, b"0"),
                    "published_by_user_id": policy_migration_harness.stack.owner_user_id,
                },
            )


@pytest.mark.asyncio
async def test_immutable_policy_history_rejects_update_and_delete(
    policy_migration_harness: PolicyMigrationHarness,
) -> None:
    graph = await policy_migration_harness.seed_published_policy()
    source_id = await policy_migration_harness.seed_policy_source()
    evaluation_id = await policy_migration_harness.seed_evaluation(
        graph.policy_revision_id, source_id
    )

    with pytest.raises(sa.exc.SQLAlchemyError):
        async with policy_migration_harness.engine.begin() as connection:
            await connection.execute(
                sa.text(
                    "UPDATE knowledge.source_policies SET revision_number = 2"
                    " WHERE policy_revision_id = :policy_revision_id"
                ),
                {"policy_revision_id": graph.policy_revision_id},
            )

    with pytest.raises(sa.exc.SQLAlchemyError):
        async with policy_migration_harness.engine.begin() as connection:
            await connection.execute(
                sa.text(
                    "DELETE FROM knowledge.policy_signing_keys"
                    " WHERE signing_key_id = :signing_key_id"
                ),
                {"signing_key_id": graph.signing_key_id},
            )

    for statement in (
        "UPDATE knowledge.policy_evaluations SET raw_decision = 'excluded'"
        f" WHERE policy_evaluation_id = '{evaluation_id}'",
        f"DELETE FROM knowledge.policy_evaluations WHERE policy_evaluation_id = '{evaluation_id}'",
    ):
        with pytest.raises(sa.exc.SQLAlchemyError):
            async with policy_migration_harness.engine.begin() as connection:
                await connection.execute(sa.text(statement))


@pytest.mark.asyncio
async def test_evaluation_identity_is_unique_per_revision_source_sequence(
    policy_migration_harness: PolicyMigrationHarness,
) -> None:
    graph = await policy_migration_harness.seed_published_policy()
    source_id = await policy_migration_harness.seed_policy_source()
    await policy_migration_harness.seed_evaluation(graph.policy_revision_id, source_id)

    with pytest.raises(sa.exc.IntegrityError):
        await policy_migration_harness.seed_evaluation(graph.policy_revision_id, source_id)

    await policy_migration_harness.seed_evaluation(
        graph.policy_revision_id, source_id, subject_event_sequence=2
    )


@pytest.mark.asyncio
async def test_preview_result_identity_includes_the_exact_preview(
    policy_migration_harness: PolicyMigrationHarness,
) -> None:
    graph = await policy_migration_harness.seed_published_policy()
    source_id = await policy_migration_harness.seed_policy_source()
    await policy_migration_harness.seed_preview_result(graph.policy_preview_id, source_id)

    with pytest.raises(sa.exc.IntegrityError):
        await policy_migration_harness.seed_preview_result(graph.policy_preview_id, source_id)


@pytest.mark.asyncio
async def test_projection_intent_origin_shapes_and_claim_isolation(
    policy_migration_harness: PolicyMigrationHarness,
) -> None:
    stack = policy_migration_harness.stack
    graph = await policy_migration_harness.seed_published_policy()
    source_id = await policy_migration_harness.seed_policy_source()
    policy_intent_id = await policy_migration_harness.seed_policy_transition_intent(
        graph.policy_revision_id, source_id
    )

    # A source_event row without its event reference violates only the origin
    # CHECK: its source-version evidence is otherwise valid at the lifecycle
    # head, so PostgreSQL cannot reject it through the stronger version CHECK.
    with pytest.raises(sa.exc.IntegrityError) as source_event_outcome:
        async with policy_migration_harness.engine.begin() as connection:
            await connection.execute(
                sa.text(
                    "INSERT INTO knowledge.projection_intents"
                    " (projection_intent_id, workspace_id, event_id, source_id,"
                    " source_version_id, projection_kind, operation, status, available_at)"
                    " VALUES (:projection_intent_id, :workspace_id, NULL, :source_id,"
                    " :source_version_id, 'qdrant', 'delete', 'pending', CURRENT_TIMESTAMP)"
                ),
                {
                    "projection_intent_id": uuid4(),
                    "workspace_id": stack.workspace_id,
                    "source_id": stack.seeded_source_id,
                    "source_version_id": stack.seeded_source_version_id,
                },
            )
    assert source_event_outcome.value.orig.diag.constraint_name == ("ck_projection_intents__origin")

    # A policy_transition row without its revision reference violates the
    # origin CHECK's biconditional: the fresh intent carries the closed
    # policy-transition origin with both references NULL, so the rejection can
    # only come from the origin CHECK (no other unique index is in play).
    with pytest.raises(sa.exc.IntegrityError) as policy_transition_outcome:
        async with policy_migration_harness.engine.begin() as connection:
            await connection.execute(
                sa.text(
                    "INSERT INTO knowledge.projection_intents"
                    " (projection_intent_id, workspace_id, origin_kind, event_id,"
                    " policy_revision_id, source_id, projection_kind, operation,"
                    " status, available_at)"
                    " VALUES (:projection_intent_id, :workspace_id,"
                    " 'policy_transition', NULL, NULL, :source_id, 'qdrant',"
                    " 'delete', 'pending', CURRENT_TIMESTAMP)"
                ),
                {
                    "projection_intent_id": uuid4(),
                    "workspace_id": stack.workspace_id,
                    "source_id": source_id,
                },
            )
    assert policy_transition_outcome.value.orig.diag.constraint_name == (
        "ck_projection_intents__origin"
    )

    # A row whose origin is outside the closed vocabulary is rejected even
    # though both references are NULL: the CHECK must close the vocabulary
    # itself, because the two biconditionals alone hold vacuously for such a
    # row (``false = false`` on both arms).
    with pytest.raises(sa.exc.IntegrityError) as garbage_outcome:
        async with policy_migration_harness.engine.begin() as connection:
            await connection.execute(
                sa.text(
                    "INSERT INTO knowledge.projection_intents"
                    " (projection_intent_id, workspace_id, origin_kind, event_id,"
                    " policy_revision_id, source_id, projection_kind, operation,"
                    " status, available_at)"
                    " VALUES (:projection_intent_id, :workspace_id, 'garbage', NULL,"
                    " NULL, :source_id, 'qdrant', 'delete', 'pending',"
                    " CURRENT_TIMESTAMP)"
                ),
                {
                    "projection_intent_id": uuid4(),
                    "workspace_id": stack.workspace_id,
                    "source_id": source_id,
                },
            )
    assert garbage_outcome.value.orig.diag.constraint_name == ("ck_projection_intents__origin")

    now = await policy_migration_harness.database_now()
    claimed = await policy_migration_harness.store.claim_batch(now, PROJECTION_CLAIM_BATCH_LIMIT)
    assert claimed, "the seeded due source-event intents must be claimable"
    for intent in claimed:
        assert intent.origin_kind.value == "source_event"
        assert intent.event_id is not None
        assert intent.policy_revision_id is None

    policy_intent = await policy_migration_harness.fetch_all(
        "SELECT status FROM knowledge.projection_intents"
        " WHERE projection_intent_id = :projection_intent_id",
        {"projection_intent_id": policy_intent_id},
    )
    assert len(policy_intent) == 1
    assert policy_intent[0][0] == "pending"


@pytest.mark.asyncio
async def test_workspace_admits_exactly_one_working_draft(
    policy_migration_harness: PolicyMigrationHarness,
) -> None:
    stack = policy_migration_harness.stack
    with pytest.raises(sa.exc.IntegrityError):
        async with policy_migration_harness.engine.begin() as connection:
            await connection.execute(
                sa.text(
                    "INSERT INTO knowledge.policy_drafts"
                    " (policy_draft_id, workspace_id, draft_version)"
                    " VALUES (:policy_draft_id, :workspace_id, 1)"
                ),
                {"policy_draft_id": uuid4(), "workspace_id": stack.workspace_id},
            )


@pytest.mark.asyncio
async def test_consumed_preview_keeps_its_ready_evidence(
    policy_migration_harness: PolicyMigrationHarness,
) -> None:
    """Spec 8.4: a consumed preview retains ready_at, expiry and digest.

    ``consumed_at`` is set exactly on consumption while the ready evidence
    stays on the row, so the state CHECKs must accept
    ``state = 'consumed'`` together with non-null ``ready_at`` /
    ``expires_at`` / ``impact_digest``.
    """
    stack = policy_migration_harness.stack
    policy_preview_id = uuid4()
    draft_id = await policy_migration_harness.fetch_scalar(
        "SELECT policy_draft_id FROM knowledge.policy_drafts WHERE workspace_id = :workspace_id",
        {"workspace_id": stack.workspace_id},
    )
    async with policy_migration_harness.engine.begin() as connection:
        await connection.execute(
            sa.text(
                "INSERT INTO knowledge.policy_previews"
                " (policy_preview_id, workspace_id, policy_draft_id, draft_version,"
                " draft_sha256, source_checkpoint_event_sequence, state,"
                " impact_digest, created_by_user_id, ready_at, expires_at, consumed_at)"
                " VALUES (:policy_preview_id, :workspace_id, :policy_draft_id, 1,"
                " :draft_sha256, 0, 'consumed', :impact_digest, :created_by_user_id,"
                " CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {
                "policy_preview_id": policy_preview_id,
                "workspace_id": stack.workspace_id,
                "policy_draft_id": draft_id,
                "draft_sha256": _fingerprint(nonce="draft"),
                "impact_digest": _fingerprint(nonce="impact"),
                "created_by_user_id": stack.owner_user_id,
            },
        )
    consumed = await policy_migration_harness.fetch_all(
        "SELECT state, ready_at, expires_at, consumed_at, impact_digest"
        " FROM knowledge.policy_previews WHERE policy_preview_id = :policy_preview_id",
        {"policy_preview_id": policy_preview_id},
    )
    assert len(consumed) == 1
    assert consumed[0][0] == "consumed"
    assert consumed[0][1] is not None
    assert consumed[0][2] is not None
    assert consumed[0][3] is not None
    assert consumed[0][4] is not None


@pytest.mark.asyncio
async def test_extension_operand_bounds_follow_the_closed_grammar(
    policy_migration_harness: PolicyMigrationHarness,
) -> None:
    """Spec 6.2: an extension is 2-64 ASCII characters starting with one dot.

    The database CHECK must mirror the domain normalization exactly: multi
    -suffix values and any mix of lowercase letters, digits, dots, hyphens
    and underscores after the leading dot insert, while one character beyond
    the 64-character ceiling is rejected.
    """
    stack = policy_migration_harness.stack
    draft_id = await policy_migration_harness.fetch_scalar(
        "SELECT policy_draft_id FROM knowledge.policy_drafts WHERE workspace_id = :workspace_id",
        {"workspace_id": stack.workspace_id},
    )
    for accepted_operand in (".tar.gz", "._ledger", ".a"):
        async with policy_migration_harness.engine.begin() as connection:
            await connection.execute(
                sa.text(
                    "INSERT INTO knowledge.policy_draft_rules"
                    " (policy_draft_id, rule_id, rule_kind, text_operand,"
                    " semantic_fingerprint)"
                    " VALUES (:policy_draft_id, :rule_id, 'extension',"
                    " :text_operand, :semantic_fingerprint)"
                ),
                {
                    "policy_draft_id": draft_id,
                    "rule_id": uuid4(),
                    "text_operand": accepted_operand,
                    "semantic_fingerprint": _fingerprint(nonce=accepted_operand),
                },
            )
    with pytest.raises(sa.exc.IntegrityError):
        async with policy_migration_harness.engine.begin() as connection:
            await connection.execute(
                sa.text(
                    "INSERT INTO knowledge.policy_draft_rules"
                    " (policy_draft_id, rule_id, rule_kind, text_operand,"
                    " semantic_fingerprint)"
                    " VALUES (:policy_draft_id, :rule_id, 'extension',"
                    " :text_operand, :semantic_fingerprint)"
                ),
                {
                    "policy_draft_id": draft_id,
                    "rule_id": uuid4(),
                    "text_operand": ".a" + "b" * 63,
                    "semantic_fingerprint": _fingerprint(nonce="oversized"),
                },
            )


@pytest.mark.asyncio
async def test_downgrade_refuses_with_protected_rows_without_the_destructive_gate(
    policy_migration_harness: PolicyMigrationHarness,
) -> None:
    graph = await policy_migration_harness.seed_published_policy()
    source_id = await policy_migration_harness.seed_policy_source()
    await policy_migration_harness.seed_policy_transition_intent(
        graph.policy_revision_id, source_id
    )

    # In-process downgrade leaves the environment-level CLI gate aside, so
    # only the migration's own row-level gate decides: protected rows exist
    # and no destructive argument is present, so the downgrade must refuse.
    with pytest.raises(CommandError):
        run_inprocess_alembic_downgrade(policy_migration_harness.stack, destructive=False)

    assert await _schema_head(policy_migration_harness) == "20260817_01"
    assert await _application_table_count(policy_migration_harness) == 29


@pytest.mark.asyncio
async def test_gated_downgrade_returns_exactly_to_the_child_2_head(
    policy_migration_harness: PolicyMigrationHarness,
) -> None:
    """Prove the deterministic destructive downgrade under the open gate.

    This test intentionally runs last: under the explicit destructive gate the
    migration deletes pending policy-transition intents and drops the twelve
    policy tables (their append-only rows go with the tables), returning the
    schema exactly to the Child 2 head with the pre-existing workspace,
    source-event intent and projection-intent shape intact. It re-applies the
    policy head afterwards so the stack teardown observes the latest schema.
    """
    stack = policy_migration_harness.stack

    run_inprocess_alembic_downgrade(stack, destructive=True)

    assert await _schema_head(policy_migration_harness) == "20260816_01"
    assert await _application_table_count(policy_migration_harness) == 17
    for table_name in _POLICY_TABLES:
        assert not await _table_exists(policy_migration_harness, table_name), table_name

    intent_columns = await policy_migration_harness.fetch_all(
        "SELECT column_name, is_nullable FROM information_schema.columns"
        " WHERE table_schema = 'knowledge' AND table_name = 'projection_intents'",
        {},
    )
    columns = {row[0]: row[1] for row in intent_columns}
    assert "origin_kind" not in columns
    assert "policy_revision_id" not in columns
    assert columns["event_id"] == "NO"

    seeded_intent = await policy_migration_harness.fetch_all(
        "SELECT event_id, status FROM knowledge.projection_intents WHERE event_id = :event_id",
        {"event_id": stack.seeded_event_id},
    )
    assert len(seeded_intent) == 1
    # The earlier claim-isolation test may hold a lease on this intent; both
    # lifecycle states prove the row itself survived the downgrade untouched.
    assert seeded_intent[0][1] in {"pending", "leased"}

    workspace = await policy_migration_harness.fetch_scalar(
        "SELECT count(*) FROM knowledge.workspaces WHERE workspace_id = :workspace_id",
        {"workspace_id": stack.workspace_id},
    )
    assert int(workspace) == 1

    reupgrade = run_guarded_alembic(policy_migration_harness.stack, "upgrade", "head")
    assert reupgrade.returncode == 0
