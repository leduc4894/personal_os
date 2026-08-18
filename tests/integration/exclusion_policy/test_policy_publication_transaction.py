"""One signed publication transaction over the real policy schema (spec 11).

The disposable stack backs every case. The happy paths prove the exact
committed graph: an explicit empty initial policy signs and publishes
revision 1 with a null parent (no implicit revision zero), a non-empty
policy persists the signed snapshot whose Ed25519 signature verifies over
the canonical payload bytes, the immutable rules, the swapped active
pointer, the pending reconciliation intent with its deterministic workflow
identity, the ``exclusion_policy.published`` audit row, the consumed preview
and the rebased/incremented draft — all from one commit. The rejection
paths prove the recheck chain: an expired or draft-mutated preview, an
advanced source checkpoint, an advanced active revision, idempotency-key
reuse with a different fingerprint, a wrong actor and a wrong confirmation
each reject with their typed error and leave no revision behind; business
rejections after the trust boundary append exactly one
``exclusion_policy.publish_rejected`` audit row with a closed reason.
Dependency failures roll the whole transaction back: a crashing signer or a
signer whose key is not the workspace's persisted trust anchor leaves no
revision, no pointer swap, no consumed preview and no rebase.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from api_runtime.exclusion_policy_commands import (
    create_or_load_policy_signing_key,
    execute_policy_key_initialize,
)
from api_runtime.exclusion_policy_crypto import Ed25519PolicyVerifier
from tests.integration.exclusion_policy.conftest import PolicyMigrationHarness

from personal_os.diagnostics.context import DiagnosticContext, TraceContext
from personal_os.diagnostics.trace_context import SpanId, TraceId
from personal_os.error_contracts.codes import ErrorCode
from personal_os.exclusion_policy.contracts import ExclusionRule, RuleKind
from personal_os.exclusion_policy.errors import ExclusionPolicyError
from personal_os.exclusion_policy.metrics import (
    InMemoryExclusionPolicyMetrics,
    PublicationMetricOutcome,
)
from personal_os.exclusion_policy.normalization import normalize_rule
from personal_os.exclusion_policy.ports import PolicyActor, PolicyActorKind
from personal_os.exclusion_policy.previews import PolicyPreviewRecord
from personal_os.exclusion_policy.publication import (
    CONFIRMATION_PHRASE,
    ExclusionPolicyPublicationService,
    PublishPolicyCommand,
)
from personal_os.exclusion_policy.signatures import (
    SNAPSHOT_SIGNING_DOMAIN,
    build_signed_message,
    derive_ed25519_key_id,
)
from personal_os.sources.commands import IdempotencyKey
from postgresql_source_store.policy_drafts import PostgresqlPolicyDraftStore
from postgresql_source_store.policy_previews import PostgresqlPolicyPreviewStore
from postgresql_source_store.policy_publication import (
    PUBLISH_REJECTED_AUDIT_ACTION,
    PUBLISHED_AUDIT_ACTION,
    RECONCILIATION_STATE_PENDING,
    PostgresqlPolicyPublicationStore,
    reconciliation_workflow_id,
)
from postgresql_source_store.tables import (
    policy_previews,
    sync_events,
    workspace_policy_state,
)

pytestmark = pytest.mark.local_stack

_TRACE = TraceContext(
    trace_id=TraceId("0123456789abcdef0123456789abcdef"),
    remote_parent_span_id=None,
    local_span_id=SpanId("0123456789abcdef"),
    trace_flags=0,
)

KEY_FILE_NAME = "policy_signing_initial.pem"


def _context() -> DiagnosticContext:
    return DiagnosticContext(request_id=uuid4(), client_request_id=None, trace=_TRACE)


def _actor(user_id: UUID) -> PolicyActor:
    return PolicyActor(actor_kind=PolicyActorKind.USER, user_id=user_id)


def _folder_rule() -> ExclusionRule:
    return normalize_rule(
        uuid4(), RuleKind.FOLDER_PREFIX, text_operand="private/notes", rule_index=0
    )


def _extension_rule() -> ExclusionRule:
    return normalize_rule(uuid4(), RuleKind.EXTENSION, text_operand=".tmp", rule_index=0)


class _CountingSigner:
    """Loaded-signing-key wrapper counting sign invocations for replay proofs."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.calls = 0

    @property
    def key_id(self) -> str:
        return self._inner.key_id

    def sign(self, message: bytes) -> bytes:
        self.calls += 1
        return self._inner.sign(message)


class _FailingSigner:
    """Signer whose crash simulates a private-key dependency failure."""

    @property
    def key_id(self) -> str:
        return "ed25519-sha256-" + "A" * 43

    def sign(self, message: bytes) -> bytes:
        del message
        raise RuntimeError("signing dependency crashed")


class PublicationHarness:
    """Key, draft, preview and command helpers over one engine."""

    def __init__(self, base: PolicyMigrationHarness, secret_root: Path) -> None:
        self.base = base
        self.engine = base.engine
        self.secret_root = secret_root
        self.metrics = InMemoryExclusionPolicyMetrics()
        self.signing_key = create_or_load_policy_signing_key(secret_root, KEY_FILE_NAME)
        self.verifier = Ed25519PolicyVerifier(
            {self.signing_key.key_id: self.signing_key.public_key_bytes}
        )
        self.draft_store = PostgresqlPolicyDraftStore(base.engine)
        self.preview_store = PostgresqlPolicyPreviewStore(base.engine)

    async def ensure_keys_initialized(self) -> None:
        await execute_policy_key_initialize(
            engine=self.engine,
            workspace_id=self.base.stack.workspace_id,
            key_file_name=KEY_FILE_NAME,
            secret_root=self.secret_root,
            context=_context(),
        )

    def service(self, signer: Any | None = None) -> ExclusionPolicyPublicationService:
        return ExclusionPolicyPublicationService(
            store=PostgresqlPolicyPublicationStore(self.engine),
            signer=signer if signer is not None else self.signing_key,
            verifier=self.verifier,
            metrics=self.metrics,
        )

    @property
    def workspace_id(self) -> UUID:
        return self.base.stack.workspace_id

    @property
    def owner_user_id(self) -> UUID:
        return self.base.stack.owner_user_id

    def actor(self) -> PolicyActor:
        return _actor(self.owner_user_id)

    async def replace_draft_rules(self, rules: tuple[ExclusionRule, ...]) -> int:
        draft = await self.draft_store.load_draft(self.workspace_id, _context())
        updated = await self.draft_store.replace_rules(
            draft.draft_id, draft.draft_version, rules, self.actor(), _context()
        )
        return updated.draft_version

    async def ready_preview(self) -> PolicyPreviewRecord:
        requested = await self.preview_store.request_preview(
            self.workspace_id, self.actor(), _context()
        )
        return await self.preview_store.run_preview_activity(
            requested.policy_preview_id, _context()
        )

    async def policy_state(self) -> tuple[UUID | None, int]:
        async with self.engine.connect() as connection:
            row = await connection.execute(
                sa.select(
                    workspace_policy_state.c.active_policy_revision_id,
                    workspace_policy_state.c.active_revision_number,
                ).where(workspace_policy_state.c.workspace_id == self.workspace_id)
            )
            state = row.one()
        return state.active_policy_revision_id, int(state.active_revision_number)

    async def build_command(
        self,
        preview: PolicyPreviewRecord,
        *,
        key: str,
        confirmation: str = CONFIRMATION_PHRASE,
        actor: PolicyActor | None = None,
        policy_preview_id: UUID | None = None,
    ) -> PublishPolicyCommand:
        assert preview.impact_digest is not None
        _, active_number = await self.policy_state()
        return PublishPolicyCommand(
            workspace_id=self.workspace_id,
            actor=actor if actor is not None else self.actor(),
            policy_preview_id=policy_preview_id or preview.policy_preview_id,
            policy_draft_id=preview.policy_draft_id,
            expected_draft_version=preview.draft_version,
            expected_draft_sha256=preview.draft_sha256,
            preview_impact_digest=preview.impact_digest,
            expected_active_policy_revision_id=preview.base_policy_revision_id,
            expected_active_revision_number=active_number,
            idempotency_key=IdempotencyKey(key),
            confirmation=confirmation,
        )

    async def publish(
        self, preview: PolicyPreviewRecord, *, key: str, signer: Any | None = None
    ) -> Any:
        command = await self.build_command(preview, key=key)
        return await self.service(signer).publish(command, _context())

    async def revision_count(self) -> int:
        return int(
            await self.base.fetch_scalar(
                "SELECT count(*) FROM knowledge.source_policies WHERE workspace_id = :workspace_id",
                {"workspace_id": self.workspace_id},
            )
        )

    async def audit_rows(self, action: str) -> list[Any]:
        return list(
            await self.base.fetch_all(
                "SELECT action, target_id, reason_code, safe_diff_hash, result"
                " FROM knowledge.audit_events"
                " WHERE workspace_id = :workspace_id AND action = :action",
                {"workspace_id": self.workspace_id, "action": action},
            )
        )

    async def advance_checkpoint(self) -> None:
        async with self.engine.begin() as connection:
            referenced = await connection.execute(
                sa.select(sync_events.c.source_id)
                .where(sync_events.c.workspace_id == self.workspace_id)
                .limit(1)
            )
            source_id = referenced.scalar_one()
            nonce = uuid4().hex
            await connection.execute(
                sa.insert(sync_events).values(
                    event_id=uuid4(),
                    workspace_id=self.workspace_id,
                    source_id=source_id,
                    idempotency_key=f"publication-{nonce}",
                    request_fingerprint=hashlib.sha256(nonce.encode()).hexdigest(),
                    event_type="update",
                )
            )

    async def backdate_preview_expiry(self, preview_id: UUID) -> None:
        async with self.engine.begin() as connection:
            await connection.execute(
                sa.update(policy_previews)
                .values(
                    created_at=sa.text("CURRENT_TIMESTAMP - interval '20 minutes'"),
                    available_at=sa.text("CURRENT_TIMESTAMP - interval '20 minutes'"),
                    ready_at=sa.text("CURRENT_TIMESTAMP - interval '16 minutes'"),
                    expires_at=sa.text("CURRENT_TIMESTAMP - interval '1 minute'"),
                )
                .where(policy_previews.c.policy_preview_id == preview_id)
            )

    async def preview_state(self, preview_id: UUID) -> str:
        return str(
            await self.base.fetch_scalar(
                "SELECT state FROM knowledge.policy_previews"
                " WHERE policy_preview_id = :policy_preview_id",
                {"policy_preview_id": preview_id},
            )
        )

    async def draft_version(self) -> int:
        return int(
            await self.base.fetch_scalar(
                "SELECT draft_version FROM knowledge.policy_drafts"
                " WHERE workspace_id = :workspace_id",
                {"workspace_id": self.workspace_id},
            )
        )


@pytest.fixture(scope="module")
def policy_secret_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("policy-publication-secrets")


@pytest_asyncio.fixture
async def harness(
    policy_migration_harness: PolicyMigrationHarness, policy_secret_root: Path
) -> PublicationHarness:
    publication_harness = PublicationHarness(policy_migration_harness, policy_secret_root)
    await publication_harness.ensure_keys_initialized()
    return publication_harness


# --- happy paths ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publishes_explicit_empty_initial_policy_revision_one(
    harness: PublicationHarness,
) -> None:
    active_id, active_number = await harness.policy_state()
    assert active_id is None and active_number == 0
    preview = await harness.ready_preview()
    result = await harness.publish(preview, key="initial-empty-001")
    assert result.is_replay is False
    assert result.revision_number == 1
    assert result.parent_policy_revision_id is None
    assert result.rule_count == 0
    assert result.reconciliation_status == RECONCILIATION_STATE_PENDING
    assert result.signing_key_id == harness.signing_key.key_id
    active_id, active_number = await harness.policy_state()
    assert active_id == result.policy_revision_id
    assert active_number == 1
    assert harness.metrics.publication_count(PublicationMetricOutcome.PUBLISHED) == 1


@pytest.mark.asyncio
async def test_persists_signed_snapshot_rules_intent_audit_and_rebase(
    harness: PublicationHarness,
) -> None:
    rules = (_folder_rule(), _extension_rule())
    await harness.replace_draft_rules(rules)
    preview = await harness.ready_preview()
    draft_version_before = await harness.draft_version()
    result = await harness.publish(preview, key="signed-nonempty-001")
    assert result.rule_count == 2

    row = (
        await harness.base.fetch_all(
            "SELECT snapshot_payload_bytes, snapshot_payload_sha256, signature_bytes,"
            " signing_key_id, publication_idempotency_key, request_fingerprint,"
            " parent_policy_revision_id, revision_number, default_decision"
            " FROM knowledge.source_policies WHERE policy_revision_id = :policy_revision_id",
            {"policy_revision_id": result.policy_revision_id},
        )
    )[0]
    payload_bytes = bytes(row[0])
    assert row[1] == result.payload_sha256
    assert hashlib.sha256(payload_bytes).hexdigest() == result.payload_sha256
    assert row[3] is not None
    assert row[4] == "signed-nonempty-001"
    assert row[5] is not None and len(row[5]) == 64
    assert row[8] == "allowed"
    assert payload_bytes.startswith(b'{"contract":"exclusion_policy_snapshot/v1"')
    assert f'"workspace_id":"{harness.workspace_id}"'.encode() in payload_bytes
    assert f'"revision_number":{result.revision_number}'.encode() in payload_bytes
    assert b'"default_decision":"allowed"' in payload_bytes
    assert b'"rules":[' in payload_bytes

    # The persisted signature verifies over the domain-separated payload with
    # the persisted public key: the server never regenerates or resigns.
    public_key_bytes = bytes(
        await harness.base.fetch_scalar(
            "SELECT public_key_bytes FROM knowledge.policy_signing_keys"
            " WHERE signing_key_id = :signing_key_id",
            {"signing_key_id": row[3]},
        )
    )
    verifier = Ed25519PolicyVerifier({derive_ed25519_key_id(public_key_bytes): public_key_bytes})
    assert verifier.verify(
        derive_ed25519_key_id(public_key_bytes),
        bytes(row[2]),
        build_signed_message(SNAPSHOT_SIGNING_DOMAIN, payload_bytes),
    )

    rule_rows = await harness.base.fetch_all(
        "SELECT rule_id FROM knowledge.policy_rules WHERE policy_revision_id = :policy_revision_id",
        {"policy_revision_id": result.policy_revision_id},
    )
    assert {str(row[0]) for row in rule_rows} == {str(rule.rule_id) for rule in rules}

    intent_row = (
        await harness.base.fetch_all(
            "SELECT state, workflow_id FROM knowledge.policy_reconciliation_intents"
            " WHERE policy_revision_id = :policy_revision_id",
            {"policy_revision_id": result.policy_revision_id},
        )
    )[0]
    assert intent_row[0] == RECONCILIATION_STATE_PENDING
    assert intent_row[1] == reconciliation_workflow_id(
        harness.workspace_id, result.policy_revision_id
    )

    published_audit = await harness.audit_rows(PUBLISHED_AUDIT_ACTION)
    matching = [row for row in published_audit if row[1] == result.policy_revision_id]
    assert len(matching) == 1
    assert matching[0][3] == result.payload_sha256

    assert await harness.preview_state(preview.policy_preview_id) == "consumed"

    assert await harness.draft_version() == draft_version_before + 1
    draft_base = await harness.base.fetch_scalar(
        "SELECT base_policy_revision_id FROM knowledge.policy_drafts"
        " WHERE workspace_id = :workspace_id",
        {"workspace_id": harness.workspace_id},
    )
    assert draft_base == result.policy_revision_id

    active_id, active_number = await harness.policy_state()
    assert active_id == result.policy_revision_id
    assert active_number == result.revision_number


@pytest.mark.asyncio
async def test_second_publication_chains_parent_revision(
    harness: PublicationHarness,
) -> None:
    first = await harness.publish(await harness.ready_preview(), key="chain-first-001")
    await harness.replace_draft_rules((_folder_rule(),))
    second = await harness.publish(await harness.ready_preview(), key="chain-second-001")
    assert second.revision_number == first.revision_number + 1
    assert second.parent_policy_revision_id == first.policy_revision_id
    assert second.policy_revision_id != first.policy_revision_id
    active_id, active_number = await harness.policy_state()
    assert active_id == second.policy_revision_id
    assert active_number == second.revision_number


# --- rejections ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expired_preview_rejects_without_any_revision(
    harness: PublicationHarness,
) -> None:
    revisions_before = await harness.revision_count()
    preview = await harness.ready_preview()
    await harness.backdate_preview_expiry(preview.policy_preview_id)
    with pytest.raises(ExclusionPolicyError) as raised:
        await harness.publish(preview, key="expired-preview-001")
    assert raised.value.error_code is ErrorCode.EXCLUSION_POLICY_PREVIEW_EXPIRED
    assert await harness.revision_count() == revisions_before
    rejection_audit = await harness.audit_rows(PUBLISH_REJECTED_AUDIT_ACTION)
    assert any(row[1] == preview.policy_preview_id for row in rejection_audit)
    assert harness.metrics.publication_count(PublicationMetricOutcome.REJECTED) == 1


@pytest.mark.asyncio
async def test_draft_mutation_after_preview_rejects(
    harness: PublicationHarness,
) -> None:
    revisions_before = await harness.revision_count()
    preview = await harness.ready_preview()
    # An identical replacement is still an explicit edit: the version moves
    # once and the ready preview bound to the prior version expires.
    await harness.replace_draft_rules(())
    with pytest.raises(ExclusionPolicyError) as raised:
        await harness.publish(preview, key="draft-mutated-001")
    assert raised.value.error_code is ErrorCode.EXCLUSION_POLICY_PREVIEW_EXPIRED
    assert await harness.revision_count() == revisions_before


@pytest.mark.asyncio
async def test_source_checkpoint_advance_rejects_stale(
    harness: PublicationHarness,
) -> None:
    revisions_before = await harness.revision_count()
    preview = await harness.ready_preview()
    await harness.advance_checkpoint()
    with pytest.raises(ExclusionPolicyError) as raised:
        await harness.publish(preview, key="checkpoint-advanced-001")
    assert raised.value.error_code is ErrorCode.EXCLUSION_POLICY_PREVIEW_STALE
    assert str(raised.value.safe_details["reason"]) == "preview_source_checkpoint_stale"
    assert await harness.revision_count() == revisions_before


@pytest.mark.asyncio
async def test_active_revision_advance_rejects_snapshot_outdated(
    harness: PublicationHarness,
) -> None:
    first = await harness.publish(await harness.ready_preview(), key="advance-first-001")
    await harness.replace_draft_rules((_extension_rule(),))
    stale_preview = await harness.ready_preview()
    newer_preview = await harness.ready_preview()
    winner = await harness.publish(newer_preview, key="advance-winner-001")
    assert winner.revision_number == first.revision_number + 1
    with pytest.raises(ExclusionPolicyError) as raised:
        await harness.publish(stale_preview, key="advance-loser-001")
    assert raised.value.error_code is ErrorCode.EXCLUSION_POLICY_SNAPSHOT_OUTDATED
    assert raised.value.safe_details["current_policy_revision_number"] == (winner.revision_number)


@pytest.mark.asyncio
async def test_idempotency_key_reuse_with_different_fingerprint_is_terminal(
    harness: PublicationHarness,
) -> None:
    published = await harness.publish(await harness.ready_preview(), key="reused-key-001")
    other_preview = await harness.ready_preview()
    command = await harness.build_command(other_preview, key="reused-key-001")
    with pytest.raises(ExclusionPolicyError) as raised:
        await harness.service().publish(command, _context())
    assert raised.value.error_code is ErrorCode.EXCLUSION_POLICY_INPUT_INVALID
    assert str(raised.value.safe_details["reason"]) == "idempotency_mismatch"
    assert await harness.revision_count() == published.revision_number
    rejection_audit = await harness.audit_rows(PUBLISH_REJECTED_AUDIT_ACTION)
    assert any(row[2] == "idempotency_mismatch" for row in rejection_audit)


@pytest.mark.asyncio
async def test_identical_replay_returns_original_without_resigning(
    harness: PublicationHarness,
) -> None:
    preview = await harness.ready_preview()
    command = await harness.build_command(preview, key="replay-exact-001")
    counting = _CountingSigner(harness.signing_key)
    first = await harness.service(signer=counting).publish(command, _context())
    assert counting.calls == 1
    audit_before = len(await harness.audit_rows(PUBLISHED_AUDIT_ACTION))
    replay = await harness.service(signer=counting).publish(command, _context())
    assert replay.is_replay is True
    assert replay.policy_revision_id == first.policy_revision_id
    assert replay.revision_number == first.revision_number
    assert replay.payload_sha256 == first.payload_sha256
    assert replay.signing_key_id == first.signing_key_id
    assert replay.published_at == first.published_at
    assert replay.reconciliation_status == first.reconciliation_status
    assert counting.calls == 1  # the replay never signs again
    assert len(await harness.audit_rows(PUBLISHED_AUDIT_ACTION)) == audit_before
    key_rows = await harness.base.fetch_all(
        "SELECT count(*) FROM knowledge.source_policies"
        " WHERE workspace_id = :workspace_id AND publication_idempotency_key = :key",
        {"workspace_id": harness.workspace_id, "key": "replay-exact-001"},
    )
    assert int(key_rows[0][0]) == 1
    assert harness.metrics.publication_count(PublicationMetricOutcome.REPLAYED) >= 1


# --- dependency failures and rollback -------------------------------------------------


@pytest.mark.asyncio
async def test_signing_failure_rolls_back_every_written_effect(
    harness: PublicationHarness,
) -> None:
    await harness.replace_draft_rules((_folder_rule(),))
    preview = await harness.ready_preview()
    revisions_before = await harness.revision_count()
    state_before = await harness.policy_state()
    draft_version_before = await harness.draft_version()
    intents_before = int(
        await harness.base.fetch_scalar(
            "SELECT count(*) FROM knowledge.policy_reconciliation_intents"
            " WHERE workspace_id = :workspace_id",
            {"workspace_id": harness.workspace_id},
        )
    )
    audits_before = len(await harness.audit_rows(PUBLISHED_AUDIT_ACTION))
    with pytest.raises(ExclusionPolicyError) as raised:
        await harness.publish(preview, key="signing-crash-001", signer=_FailingSigner())
    assert raised.value.error_code is ErrorCode.EXCLUSION_POLICY_SIGNING_UNAVAILABLE
    assert await harness.revision_count() == revisions_before
    assert await harness.policy_state() == state_before
    assert await harness.preview_state(preview.policy_preview_id) == "ready"
    assert await harness.draft_version() == draft_version_before
    intents_after = int(
        await harness.base.fetch_scalar(
            "SELECT count(*) FROM knowledge.policy_reconciliation_intents"
            " WHERE workspace_id = :workspace_id",
            {"workspace_id": harness.workspace_id},
        )
    )
    assert intents_after == intents_before
    assert len(await harness.audit_rows(PUBLISHED_AUDIT_ACTION)) == audits_before


@pytest.mark.asyncio
async def test_signer_unknown_to_the_trust_anchor_rejects_signing(
    harness: PublicationHarness,
) -> None:
    preview = await harness.ready_preview()
    revisions_before = await harness.revision_count()
    stranger_root = harness.secret_root.parent / "stranger-keys"
    stranger_root.mkdir(exist_ok=True)
    stranger = create_or_load_policy_signing_key(stranger_root, "stranger.pem")
    with pytest.raises(ExclusionPolicyError) as raised:
        await harness.publish(preview, key="stranger-key-001", signer=stranger)
    assert raised.value.error_code is ErrorCode.EXCLUSION_POLICY_SIGNING_UNAVAILABLE
    assert await harness.revision_count() == revisions_before


@pytest.mark.asyncio
async def test_wrong_confirmation_rejects_before_any_transaction(
    harness: PublicationHarness,
) -> None:
    preview = await harness.ready_preview()
    command = await harness.build_command(
        preview, key="wrong-confirmation-001", confirmation="publish exclusion policy"
    )
    revisions_before = await harness.revision_count()
    with pytest.raises(ExclusionPolicyError) as raised:
        await harness.service().publish(command, _context())
    assert raised.value.error_code is ErrorCode.EXCLUSION_POLICY_CONFIRMATION_INVALID
    assert await harness.revision_count() == revisions_before
    rejection_audit = await harness.audit_rows(PUBLISH_REJECTED_AUDIT_ACTION)
    assert all(row[1] != preview.policy_preview_id for row in rejection_audit)
    assert await harness.preview_state(preview.policy_preview_id) == "ready"


@pytest.mark.asyncio
async def test_wrong_actor_rejects_with_audit(harness: PublicationHarness) -> None:
    preview = await harness.ready_preview()
    stranger = _actor(uuid4())
    command = await harness.build_command(preview, key="wrong-actor-001", actor=stranger)
    with pytest.raises(ExclusionPolicyError) as raised:
        await harness.service().publish(command, _context())
    assert raised.value.error_code is ErrorCode.EXCLUSION_POLICY_INPUT_INVALID
    assert str(raised.value.safe_details["reason"]) == "actor_invalid"
    rejection_audit = await harness.audit_rows(PUBLISH_REJECTED_AUDIT_ACTION)
    assert any(row[1] == preview.policy_preview_id for row in rejection_audit)
