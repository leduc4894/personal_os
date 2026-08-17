"""Exclusion-policy draft transactions against the real migrated PostgreSQL.

The module-scoped stack fixture of the sibling migration conftest provisions
the disposable ``knowledge-ci-*`` project and the migrated schema, so these
tests prove the draft persistence contract end to end: exact-version
replacement increments the version exactly once, hydrates every rule kind
back through the immutable row-to-domain mapping, expires exactly the ready
previews bound to the prior draft version, serialises concurrent writers
through ``FOR UPDATE`` into one winner plus one typed conflict, isolates
uninitialised and cross-workspace lookups, leaves published append-only rows
untouched, and persists immutable keyset envelopes idempotently with exact
replay recognition.
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError
from tests.integration.exclusion_policy.conftest import PolicyMigrationHarness
from tests.unit.exclusion_policy.fakes import extension_rule, rule

from personal_os.diagnostics.context import DiagnosticContext
from personal_os.diagnostics.trace_context import SpanId, TraceContext, TraceId
from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import InternalApplicationError
from personal_os.exclusion_policy.contracts import RuleKind
from personal_os.exclusion_policy.drafts import compute_draft_semantic_sha256
from personal_os.exclusion_policy.errors import ExclusionPolicyError
from personal_os.exclusion_policy.ports import (
    PolicyActor,
    PolicyActorKind,
    PolicyKeysetEnvelope,
    PolicyKeysetSignatureRecord,
    PolicySigningKeyRecord,
)
from personal_os.exclusion_policy.signatures import (
    ED25519_PUBLIC_KEY_BYTES,
    ED25519_SIGNATURE_BYTES,
)
from postgresql_source_store.policy_drafts import (
    DRAFT_REPLACED_AUDIT_ACTION,
    PostgresqlPolicyDraftStore,
)
from postgresql_source_store.policy_keysets import PostgresqlPolicyKeysetStore
from postgresql_source_store.tables import (
    policy_drafts,
    policy_keysets,
    policy_previews,
    policy_rules,
    users,
    workspace_policy_state,
    workspaces,
)

pytestmark = pytest.mark.local_stack

_TRACE = TraceContext(
    trace_id=TraceId("0123456789abcdef0123456789abcdef"),
    remote_parent_span_id=None,
    local_span_id=SpanId("0123456789abcdef"),
    trace_flags=0,
)

_SOURCE_ID_OPERAND = UUID("018f47a0-7b00-7000-8000-0000000000e1")
_CURRENT_PUBLIC_KEY = bytes(range(ED25519_PUBLIC_KEY_BYTES))
_STAGED_PUBLIC_KEY = bytes(range(ED25519_PUBLIC_KEY_BYTES, ED25519_PUBLIC_KEY_BYTES * 2))
_ROTATED_PUBLIC_KEY = bytes(range(64, 64 + ED25519_PUBLIC_KEY_BYTES))
_RETIRING_PUBLIC_KEY = bytes(range(96, 96 + ED25519_PUBLIC_KEY_BYTES))
_SIGNATURE_BYTES = bytes(range(ED25519_SIGNATURE_BYTES))


def _context() -> DiagnosticContext:
    return DiagnosticContext(request_id=uuid4(), client_request_id=None, trace=_TRACE)


def _actor(user_id: UUID) -> PolicyActor:
    return PolicyActor(actor_kind=PolicyActorKind.USER, user_id=user_id)


def _all_kinds_rules() -> tuple[Any, ...]:
    return (
        rule(RuleKind.EXACT_SOURCE_ID, source_id_operand=_SOURCE_ID_OPERAND),
        rule(RuleKind.FOLDER_PREFIX, text_operand="private/notes"),
        rule(RuleKind.PATH_GLOB, text_operand="attachments/**/*.tmp"),
        rule(RuleKind.EXTENSION, text_operand=".tmp"),
        rule(RuleKind.MEDIA_TYPE, text_operand="text/markdown"),
        rule(RuleKind.MAXIMUM_SIZE, size_bytes_operand=104857600),
        rule(RuleKind.SOURCE_TYPE, text_operand="pdf"),
    )


def _by_rule_id(rules: tuple[Any, ...]) -> tuple[Any, ...]:
    return tuple(sorted(rules, key=lambda item: item.rule_id))


def _draft_store(harness: PolicyMigrationHarness) -> PostgresqlPolicyDraftStore:
    return PostgresqlPolicyDraftStore(harness.engine)


# --- exact-version replacement -----------------------------------------------------


@pytest.mark.asyncio
async def test_replace_rules_increments_version_once_and_round_trips_every_kind(
    policy_migration_harness: PolicyMigrationHarness,
) -> None:
    harness = policy_migration_harness
    store = _draft_store(harness)
    workspace_id = harness.stack.workspace_id
    actor = _actor(harness.stack.owner_user_id)

    current = await store.load_draft(workspace_id, _context())
    assert current.draft_version >= 1
    assert current.rules == ()

    rules = _all_kinds_rules()
    replaced = await store.replace_rules(
        current.draft_id, current.draft_version, rules, actor, _context()
    )
    assert replaced.draft_version == current.draft_version + 1
    assert replaced.draft_id == current.draft_id
    assert _by_rule_id(replaced.rules) == _by_rule_id(rules)

    reloaded = await store.load_draft(workspace_id, _context())
    assert reloaded.draft_version == current.draft_version + 1
    assert _by_rule_id(reloaded.rules) == _by_rule_id(rules)

    row = await harness.fetch_all(
        "SELECT draft_version, updated_by_user_id FROM knowledge.policy_drafts"
        " WHERE policy_draft_id = :draft_id",
        {"draft_id": current.draft_id},
    )
    assert int(row[0][0]) == current.draft_version + 1
    assert row[0][1] == harness.stack.owner_user_id

    # An identical replacement is still an explicit successful edit that
    # increments the draft version exactly once more (spec 9).
    identical = await store.replace_rules(
        reloaded.draft_id, reloaded.draft_version, rules, actor, _context()
    )
    assert identical.draft_version == reloaded.draft_version + 1

    audit_rows = await harness.fetch_all(
        "SELECT actor_kind, actor_id, target_kind, result, reason_code, safe_diff_hash"
        " FROM knowledge.audit_events WHERE action = :action AND target_id = :draft_id"
        " ORDER BY audit_event_id",
        {"action": DRAFT_REPLACED_AUDIT_ACTION, "draft_id": current.draft_id},
    )
    assert len(audit_rows) == 2
    expected_digest = compute_draft_semantic_sha256(rules)
    for audit_row in audit_rows:
        assert audit_row[0] == "user"
        assert audit_row[1] == harness.stack.owner_user_id
        assert audit_row[2] == "policy_draft"
        assert audit_row[3] == "succeeded"
        assert audit_row[4] is None
        assert audit_row[5] == expected_digest
    rendered = repr(audit_rows)
    for forbidden in ("private/notes", ".tmp", "text/markdown", "attachments"):
        assert forbidden not in rendered


@pytest.mark.asyncio
async def test_replace_rules_rejects_stale_draft_version(
    policy_migration_harness: PolicyMigrationHarness,
) -> None:
    harness = policy_migration_harness
    store = _draft_store(harness)
    actor = _actor(harness.stack.owner_user_id)

    current = await store.load_draft(harness.stack.workspace_id, _context())
    replaced = await store.replace_rules(
        current.draft_id, current.draft_version, (extension_rule(".tmp"),), actor, _context()
    )
    assert replaced.draft_version == current.draft_version + 1

    with pytest.raises(ExclusionPolicyError) as raised:
        await store.replace_rules(current.draft_id, current.draft_version, (), actor, _context())
    assert raised.value.error_code is ErrorCode.EXCLUSION_POLICY_DRAFT_CONFLICT
    assert raised.value.safe_details == {"current_draft_version": replaced.draft_version}


# --- ready-preview invalidation ----------------------------------------------------


async def _seed_preview(
    harness: PolicyMigrationHarness,
    draft_id: UUID,
    *,
    draft_version: int,
    state: str,
) -> UUID:
    preview_id = uuid4()
    values: dict[str, Any] = {
        "policy_preview_id": preview_id,
        "workspace_id": harness.stack.workspace_id,
        "policy_draft_id": draft_id,
        "draft_version": draft_version,
        "draft_sha256": hashlib.sha256(uuid4().hex.encode("utf-8")).hexdigest(),
        "source_checkpoint_event_sequence": 0,
        "state": state,
        "created_by_user_id": harness.stack.owner_user_id,
    }
    if state == "ready":
        # The database CHECK requires a ready preview to carry its impact
        # digest and ready timestamp.
        values["impact_digest"] = hashlib.sha256(uuid4().hex.encode("utf-8")).hexdigest()
        values["ready_at"] = sa.text("CURRENT_TIMESTAMP")
    async with harness.engine.begin() as connection:
        await connection.execute(sa.insert(policy_previews).values(**values))
    return preview_id


async def _preview_state(harness: PolicyMigrationHarness, preview_id: UUID) -> str:
    rows = await harness.fetch_all(
        "SELECT state FROM knowledge.policy_previews WHERE policy_preview_id = :preview_id",
        {"preview_id": preview_id},
    )
    assert len(rows) == 1
    return str(rows[0][0])


@pytest.mark.asyncio
async def test_replace_rules_expires_ready_previews_of_the_prior_draft_version(
    policy_migration_harness: PolicyMigrationHarness,
) -> None:
    harness = policy_migration_harness
    store = _draft_store(harness)
    actor = _actor(harness.stack.owner_user_id)

    current = await store.load_draft(harness.stack.workspace_id, _context())
    prior_version = current.draft_version
    ready_preview = await _seed_preview(
        harness, current.draft_id, draft_version=prior_version, state="ready"
    )
    pending_preview = await _seed_preview(
        harness, current.draft_id, draft_version=prior_version, state="pending"
    )
    older_ready_preview: UUID | None = None
    if prior_version > 1:
        older_ready_preview = await _seed_preview(
            harness, current.draft_id, draft_version=prior_version - 1, state="ready"
        )

    await store.replace_rules(
        current.draft_id, prior_version, (extension_rule(".tmp"),), actor, _context()
    )

    assert await _preview_state(harness, ready_preview) == "expired"
    assert await _preview_state(harness, pending_preview) == "pending"
    if older_ready_preview is not None:
        assert await _preview_state(harness, older_ready_preview) == "ready"


# --- concurrency, isolation and published immutability -----------------------------


@pytest.mark.asyncio
async def test_concurrent_writers_produce_one_winner_and_one_conflict(
    policy_migration_harness: PolicyMigrationHarness,
) -> None:
    harness = policy_migration_harness
    store = _draft_store(harness)
    actor = _actor(harness.stack.owner_user_id)

    current = await store.load_draft(harness.stack.workspace_id, _context())
    outcomes = await asyncio.gather(
        store.replace_rules(
            current.draft_id, current.draft_version, (extension_rule(".tmp"),), actor, _context()
        ),
        store.replace_rules(
            current.draft_id, current.draft_version, (extension_rule(".bak"),), actor, _context()
        ),
        return_exceptions=True,
    )
    winners = [outcome for outcome in outcomes if not isinstance(outcome, BaseException)]
    conflicts = [outcome for outcome in outcomes if isinstance(outcome, ExclusionPolicyError)]
    assert len(winners) == 1
    assert len(conflicts) == 1
    assert winners[0].draft_version == current.draft_version + 1
    assert conflicts[0].error_code is ErrorCode.EXCLUSION_POLICY_DRAFT_CONFLICT
    assert conflicts[0].safe_details == {"current_draft_version": current.draft_version + 1}

    reloaded = await store.load_draft(harness.stack.workspace_id, _context())
    assert reloaded.draft_version == current.draft_version + 1


@pytest.mark.asyncio
async def test_uninitialized_workspace_and_unknown_draft_reject_without_disclosure(
    policy_migration_harness: PolicyMigrationHarness,
) -> None:
    harness = policy_migration_harness
    store = _draft_store(harness)
    actor = _actor(harness.stack.owner_user_id)

    with pytest.raises(ExclusionPolicyError) as raised:
        await store.load_draft(uuid4(), _context())
    assert raised.value.error_code is ErrorCode.EXCLUSION_POLICY_NOT_INITIALIZED

    with pytest.raises(ExclusionPolicyError) as status_raised:
        await store.get_policy_status(uuid4(), _context())
    assert status_raised.value.error_code is ErrorCode.EXCLUSION_POLICY_NOT_INITIALIZED

    with pytest.raises(ExclusionPolicyError) as replace_raised:
        await store.replace_rules(uuid4(), 1, (), actor, _context())
    assert replace_raised.value.error_code is ErrorCode.EXCLUSION_POLICY_NOT_INITIALIZED


async def _seed_second_workspace_graph(
    harness: PolicyMigrationHarness,
) -> tuple[UUID, UUID]:
    """Insert a complete second workspace policy graph; return (workspace, draft)."""
    owner_id = uuid4()
    workspace_id = uuid4()
    draft_id = uuid4()
    nonce = uuid4().hex
    async with harness.engine.begin() as connection:
        await connection.execute(
            sa.insert(users).values(
                user_id=owner_id,
                username=f"policy-drafts-{nonce[:12]}",
                display_name="Policy Drafts Owner",
            )
        )
        await connection.execute(
            sa.insert(workspaces).values(
                workspace_id=workspace_id,
                owner_user_id=owner_id,
                workspace_key=f"ws-{nonce[:12]}",
                display_name="Policy Drafts Workspace",
            )
        )
        await connection.execute(
            sa.insert(workspace_policy_state).values(
                workspace_id=workspace_id,
                active_policy_revision_id=None,
                active_revision_number=0,
                created_at=sa.text("CURRENT_TIMESTAMP"),
                updated_at=sa.text("CURRENT_TIMESTAMP"),
            )
        )
        await connection.execute(
            sa.insert(policy_drafts).values(
                policy_draft_id=draft_id,
                workspace_id=workspace_id,
                draft_version=1,
                base_policy_revision_id=None,
                created_at=sa.text("CURRENT_TIMESTAMP"),
                updated_at=sa.text("CURRENT_TIMESTAMP"),
            )
        )
    return workspace_id, draft_id


@pytest.mark.asyncio
async def test_status_reflects_active_revision_and_each_workspace_sees_only_its_draft(
    policy_migration_harness: PolicyMigrationHarness,
) -> None:
    harness = policy_migration_harness
    store = _draft_store(harness)
    actor = _actor(harness.stack.owner_user_id)
    published = await harness.seed_published_policy()

    status = await store.get_policy_status(harness.stack.workspace_id, _context())
    assert status.active_policy_revision_id == published.policy_revision_id
    assert status.active_revision_number == published.revision_number
    assert status.draft.workspace_id == harness.stack.workspace_id

    other_workspace_id, other_draft_id = await _seed_second_workspace_graph(harness)
    other_status = await store.get_policy_status(other_workspace_id, _context())
    assert other_status.draft.draft_id == other_draft_id
    assert other_status.draft.draft_version == 1
    assert other_status.active_policy_revision_id is None
    assert other_status.active_revision_number == 0

    replaced = await store.replace_rules(
        other_draft_id, 1, (extension_rule(".tmp"),), actor, _context()
    )
    assert replaced.draft_version == 2
    first_status = await store.get_policy_status(harness.stack.workspace_id, _context())
    assert first_status.draft.draft_id != other_draft_id
    assert first_status.draft.draft_version == status.draft.draft_version


@pytest.mark.asyncio
async def test_published_rows_are_immutable_and_untouched_by_draft_mutation(
    policy_migration_harness: PolicyMigrationHarness,
) -> None:
    harness = policy_migration_harness
    store = _draft_store(harness)
    actor = _actor(harness.stack.owner_user_id)
    published = await harness.seed_published_policy()

    rule_id = uuid4()
    async with harness.engine.begin() as connection:
        await connection.execute(
            sa.insert(policy_rules).values(
                policy_revision_id=published.policy_revision_id,
                rule_id=rule_id,
                rule_kind="extension",
                text_operand=".tmp",
                semantic_fingerprint=hashlib.sha256(b"published-rule").hexdigest(),
            )
        )
    before = await harness.fetch_all(
        "SELECT rule_kind, text_operand, semantic_fingerprint FROM knowledge.policy_rules"
        " WHERE policy_revision_id = :revision_id ORDER BY rule_id",
        {"revision_id": published.policy_revision_id},
    )
    policies_before = await harness.fetch_all(
        "SELECT revision_number, snapshot_payload_sha256 FROM knowledge.source_policies"
        " WHERE policy_revision_id = :revision_id",
        {"revision_id": published.policy_revision_id},
    )

    current = await store.load_draft(harness.stack.workspace_id, _context())
    await store.replace_rules(
        current.draft_id, current.draft_version, (extension_rule(".bak"),), actor, _context()
    )

    after = await harness.fetch_all(
        "SELECT rule_kind, text_operand, semantic_fingerprint FROM knowledge.policy_rules"
        " WHERE policy_revision_id = :revision_id ORDER BY rule_id",
        {"revision_id": published.policy_revision_id},
    )
    policies_after = await harness.fetch_all(
        "SELECT revision_number, snapshot_payload_sha256 FROM knowledge.source_policies"
        " WHERE policy_revision_id = :revision_id",
        {"revision_id": published.policy_revision_id},
    )
    assert after == before
    assert policies_after == policies_before

    with pytest.raises(DBAPIError):
        async with harness.engine.begin() as connection:
            await connection.execute(
                sa.update(policy_rules)
                .values(text_operand=".bak")
                .where(
                    policy_rules.c.policy_revision_id == published.policy_revision_id,
                    policy_rules.c.rule_id == rule_id,
                )
            )


# --- keyset persistence ------------------------------------------------------------


def _keyset_envelope(
    workspace_id: UUID,
    *,
    keyset_revision: int,
    parent_keyset_revision: int | None,
    policy_keyset_id: UUID,
    payload_bytes: bytes,
    keys: tuple[PolicySigningKeyRecord, ...],
    signatures: tuple[PolicyKeysetSignatureRecord, ...],
) -> PolicyKeysetEnvelope:
    return PolicyKeysetEnvelope(
        policy_keyset_id=policy_keyset_id,
        workspace_id=workspace_id,
        keyset_revision=keyset_revision,
        parent_keyset_revision=parent_keyset_revision,
        canonical_payload_bytes=payload_bytes,
        keys=keys,
        signatures=signatures,
    )


@pytest.mark.asyncio
async def test_persist_keyset_is_idempotent_and_load_latest_returns_newest_chain_link(
    policy_migration_harness: PolicyMigrationHarness,
) -> None:
    harness = policy_migration_harness
    # A dedicated workspace keeps this test independent of the other keyset
    # test: keyset revisions are unique per workspace while raw public-key
    # bytes are globally unique.
    workspace_id, _ = await _seed_second_workspace_graph(harness)
    store = PostgresqlPolicyKeysetStore(harness.engine)

    assert await store.load_latest_keyset(workspace_id, _context()) is None

    # Revision 1: one self-signed current key plus a staged key.
    current_key_id = uuid4()
    staged_key_id = uuid4()
    first_keys = (
        PolicySigningKeyRecord(
            signing_key_id=current_key_id, public_key_bytes=_CURRENT_PUBLIC_KEY
        ),
        PolicySigningKeyRecord(signing_key_id=staged_key_id, public_key_bytes=_STAGED_PUBLIC_KEY),
    )
    first_signatures = (
        PolicyKeysetSignatureRecord(
            signing_key_id=current_key_id, signature_bytes=_SIGNATURE_BYTES
        ),
    )
    first_id = uuid4()
    first_payload = b'{"contract":"exclusion_policy_keyset/v1","n":1}'
    first = await store.persist_keyset(
        _keyset_envelope(
            workspace_id,
            keyset_revision=1,
            parent_keyset_revision=None,
            policy_keyset_id=first_id,
            payload_bytes=first_payload,
            keys=first_keys,
            signatures=first_signatures,
        ),
        _context(),
    )
    assert first.keyset_revision == 1
    assert first.is_replay is False

    replayed = await store.persist_keyset(
        _keyset_envelope(
            workspace_id,
            keyset_revision=1,
            parent_keyset_revision=None,
            policy_keyset_id=first_id,
            payload_bytes=first_payload,
            keys=first_keys,
            signatures=first_signatures,
        ),
        _context(),
    )
    assert replayed.policy_keyset_id == first_id
    assert replayed.is_replay is True

    # Revision 2 keeps the old key rows immutable and cross-signs with the
    # rotated key under fresh key material.
    rotated_key_id = uuid4()
    second_id = uuid4()
    second_payload = b'{"contract":"exclusion_policy_keyset/v1","n":2}'
    second_keys = (
        *first_keys,
        PolicySigningKeyRecord(
            signing_key_id=rotated_key_id, public_key_bytes=_ROTATED_PUBLIC_KEY
        ),
    )
    second_signatures = (
        *first_signatures,
        PolicyKeysetSignatureRecord(
            signing_key_id=rotated_key_id, signature_bytes=_SIGNATURE_BYTES
        ),
    )
    second = await store.persist_keyset(
        _keyset_envelope(
            workspace_id,
            keyset_revision=2,
            parent_keyset_revision=1,
            policy_keyset_id=second_id,
            payload_bytes=second_payload,
            keys=second_keys,
            signatures=second_signatures,
        ),
        _context(),
    )
    assert second.is_replay is False

    latest = await store.load_latest_keyset(workspace_id, _context())
    assert latest is not None
    assert latest.policy_keyset_id == second_id
    assert latest.keyset_revision == 2
    assert latest.parent_keyset_revision == 1
    assert latest.canonical_payload_bytes == second_payload
    assert latest.payload_sha256 == hashlib.sha256(second_payload).hexdigest()
    # The read model returns the signing keys evidenced by signature rows;
    # the unsigned staged key of the rotation stays declared in the payload.
    assert len(latest.keys) == 2
    assert len(latest.signatures) == 2

    with pytest.raises(InternalApplicationError) as raised:
        await store.persist_keyset(
            _keyset_envelope(
                workspace_id,
                keyset_revision=2,
                parent_keyset_revision=1,
                policy_keyset_id=uuid4(),
                payload_bytes=b'{"contract":"exclusion_policy_keyset/v1","n":"other"}',
                keys=first_keys,
                signatures=first_signatures,
            ),
            _context(),
        )
    assert raised.value.error_code is ErrorCode.INTERNAL_ERROR

    assert await store.load_latest_keyset(uuid4(), _context()) is None


@pytest.mark.asyncio
async def test_persisted_keyset_rows_are_append_only(
    policy_migration_harness: PolicyMigrationHarness,
) -> None:
    harness = policy_migration_harness
    workspace_id, _ = await _seed_second_workspace_graph(harness)
    store = PostgresqlPolicyKeysetStore(harness.engine)
    keyset_id = uuid4()
    signing_key_id = uuid4()
    await store.persist_keyset(
        _keyset_envelope(
            workspace_id,
            keyset_revision=1,
            parent_keyset_revision=None,
            policy_keyset_id=keyset_id,
            payload_bytes=b'{"contract":"exclusion_policy_keyset/v1","guarded":true}',
            keys=(
                PolicySigningKeyRecord(
                    signing_key_id=signing_key_id, public_key_bytes=_RETIRING_PUBLIC_KEY
                ),
            ),
            signatures=(
                PolicyKeysetSignatureRecord(
                    signing_key_id=signing_key_id, signature_bytes=_SIGNATURE_BYTES
                ),
            ),
        ),
        _context(),
    )
    with pytest.raises(DBAPIError):
        async with harness.engine.begin() as connection:
            await connection.execute(
                sa.update(policy_keysets)
                .values(canonical_payload_bytes=b"mutated")
                .where(policy_keysets.c.policy_keyset_id == keyset_id)
            )
