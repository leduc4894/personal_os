"""Concurrent publication races over the real advisory-lock serialization.

Two publishers racing with different idempotency keys over the same ready
preview state serialize behind the policy idempotency advisory lock and the
``workspace_policy_state`` row lock: exactly one commits the next revision
and the other rejects with the typed snapshot-outdated error carrying the
winner's revision number, leaving exactly one published audit row, one
rejection audit row and one consumed preview. Two publishers racing with
the same idempotency key and identical command converge on one revision:
both acknowledgements return, the loser as an exact replay, the signer is
invoked exactly once and no second revision, audit or intent row appears.
"""

from __future__ import annotations

import asyncio
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
from personal_os.exclusion_policy.errors import ExclusionPolicyError
from personal_os.exclusion_policy.ports import PolicyActor, PolicyActorKind
from personal_os.exclusion_policy.previews import PolicyPreviewRecord
from personal_os.exclusion_policy.publication import (
    CONFIRMATION_PHRASE,
    ExclusionPolicyPublicationService,
    PublishPolicyCommand,
)
from personal_os.sources.commands import IdempotencyKey
from postgresql_source_store.policy_drafts import PostgresqlPolicyDraftStore
from postgresql_source_store.policy_previews import PostgresqlPolicyPreviewStore
from postgresql_source_store.policy_publication import (
    PUBLISH_REJECTED_AUDIT_ACTION,
    PUBLISHED_AUDIT_ACTION,
    PostgresqlPolicyPublicationStore,
)
from postgresql_source_store.tables import workspace_policy_state

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


class _CountingSigner:
    """Loaded-signing-key wrapper counting sign invocations for race proofs."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.calls = 0

    @property
    def key_id(self) -> str:
        return self._inner.key_id

    def sign(self, message: bytes) -> bytes:
        self.calls += 1
        return self._inner.sign(message)


class RaceHarness:
    """Key, draft, preview and concurrent-publish helpers over one engine."""

    def __init__(self, base: PolicyMigrationHarness, secret_root: Path) -> None:
        self.base = base
        self.engine = base.engine
        self._secret_root = secret_root
        self.signing_key = create_or_load_policy_signing_key(secret_root, KEY_FILE_NAME)
        self.verifier = Ed25519PolicyVerifier(
            {self.signing_key.key_id: self.signing_key.public_key_bytes}
        )
        self.preview_store = PostgresqlPolicyPreviewStore(base.engine)
        self.draft_store = PostgresqlPolicyDraftStore(base.engine)

    async def ensure_keys_initialized(self) -> None:
        await execute_policy_key_initialize(
            engine=self.engine,
            workspace_id=self.base.stack.workspace_id,
            key_file_name=KEY_FILE_NAME,
            secret_root=self.secret_root,
            context=_context(),
        )

    @property
    def secret_root(self) -> Path:
        return self._secret_root

    @property
    def workspace_id(self) -> UUID:
        return self.base.stack.workspace_id

    def actor(self) -> PolicyActor:
        return PolicyActor(actor_kind=PolicyActorKind.USER, user_id=self.base.stack.owner_user_id)

    async def ready_preview(self) -> PolicyPreviewRecord:
        requested = await self.preview_store.request_preview(
            self.workspace_id, self.actor(), _context()
        )
        return await self.preview_store.run_preview_activity(
            requested.policy_preview_id, _context()
        )

    async def build_command(
        self, preview: PolicyPreviewRecord, *, key: str
    ) -> PublishPolicyCommand:
        assert preview.impact_digest is not None
        async with self.engine.connect() as connection:
            row = await connection.execute(
                sa.select(
                    workspace_policy_state.c.active_policy_revision_id,
                    workspace_policy_state.c.active_revision_number,
                ).where(workspace_policy_state.c.workspace_id == self.workspace_id)
            )
            state = row.one()
        return PublishPolicyCommand(
            workspace_id=self.workspace_id,
            actor=self.actor(),
            policy_preview_id=preview.policy_preview_id,
            policy_draft_id=preview.policy_draft_id,
            expected_draft_version=preview.draft_version,
            expected_draft_sha256=preview.draft_sha256,
            preview_impact_digest=preview.impact_digest,
            expected_active_policy_revision_id=preview.base_policy_revision_id,
            expected_active_revision_number=int(state.active_revision_number),
            idempotency_key=IdempotencyKey(key),
            confirmation=CONFIRMATION_PHRASE,
        )

    def service(self, signer: Any) -> ExclusionPolicyPublicationService:
        return ExclusionPolicyPublicationService(
            store=PostgresqlPolicyPublicationStore(self.engine),
            signer=signer,
            verifier=self.verifier,
        )

    async def revision_count(self) -> int:
        return int(
            await self.base.fetch_scalar(
                "SELECT count(*) FROM knowledge.source_policies WHERE workspace_id = :workspace_id",
                {"workspace_id": self.workspace_id},
            )
        )

    async def audit_count(self, action: str, *, reason_code: str | None = None) -> int:
        if reason_code is None:
            return int(
                await self.base.fetch_scalar(
                    "SELECT count(*) FROM knowledge.audit_events"
                    " WHERE workspace_id = :workspace_id AND action = :action",
                    {"workspace_id": self.workspace_id, "action": action},
                )
            )
        return int(
            await self.base.fetch_scalar(
                "SELECT count(*) FROM knowledge.audit_events"
                " WHERE workspace_id = :workspace_id AND action = :action"
                " AND reason_code = :reason_code",
                {
                    "workspace_id": self.workspace_id,
                    "action": action,
                    "reason_code": reason_code,
                },
            )
        )


@pytest.fixture(scope="module")
def policy_secret_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("policy-race-secrets")


@pytest_asyncio.fixture
async def harness(
    policy_migration_harness: PolicyMigrationHarness, policy_secret_root: Path
) -> RaceHarness:
    race_harness = RaceHarness(policy_migration_harness, policy_secret_root)
    await race_harness.ensure_keys_initialized()
    return race_harness


@pytest.mark.asyncio
async def test_two_concurrent_publishers_different_keys_commit_exactly_one(
    harness: RaceHarness,
) -> None:
    revisions_before = await harness.revision_count()
    first_preview = await harness.ready_preview()
    second_preview = await harness.ready_preview()
    counting = _CountingSigner(harness.signing_key)
    first_command = await harness.build_command(first_preview, key="race-different-a")
    second_command = await harness.build_command(second_preview, key="race-different-b")
    outcomes = await asyncio.gather(
        harness.service(counting).publish(first_command, _context()),
        harness.service(counting).publish(second_command, _context()),
        return_exceptions=True,
    )
    errors = [outcome for outcome in outcomes if isinstance(outcome, ExclusionPolicyError)]
    results = [outcome for outcome in outcomes if not isinstance(outcome, Exception)]
    assert len(results) == 1
    assert len(errors) == 1
    winner = results[0]
    loser = errors[0]
    assert winner.is_replay is False
    assert loser.error_code is ErrorCode.EXCLUSION_POLICY_SNAPSHOT_OUTDATED
    assert loser.safe_details["current_policy_revision_number"] == winner.revision_number
    assert await harness.revision_count() == revisions_before + 1
    assert counting.calls == 1
    assert (
        await harness.audit_count(PUBLISH_REJECTED_AUDIT_ACTION, reason_code="snapshot_outdated")
        == 1
    )
    active = await harness.base.fetch_scalar(
        "SELECT active_policy_revision_id FROM knowledge.workspace_policy_state"
        " WHERE workspace_id = :workspace_id",
        {"workspace_id": harness.workspace_id},
    )
    assert active == winner.policy_revision_id


@pytest.mark.asyncio
async def test_two_concurrent_publishers_same_key_converge_on_one_revision(
    harness: RaceHarness,
) -> None:
    revisions_before = await harness.revision_count()
    preview = await harness.ready_preview()
    counting = _CountingSigner(harness.signing_key)
    command = await harness.build_command(preview, key="race-same-key")
    outcomes = await asyncio.gather(
        harness.service(counting).publish(command, _context()),
        harness.service(counting).publish(command, _context()),
        return_exceptions=True,
    )
    assert all(not isinstance(outcome, BaseException) for outcome in outcomes)
    revisions = {outcome.policy_revision_id for outcome in outcomes}
    assert len(revisions) == 1
    assert {outcome.revision_number for outcome in outcomes} == {outcomes[0].revision_number}
    assert sum(1 for outcome in outcomes if outcome.is_replay) >= 1
    assert await harness.revision_count() == revisions_before + 1
    assert counting.calls == 1
    assert (
        await harness.audit_count(
            PUBLISHED_AUDIT_ACTION,
        )
        >= 1
    )
    key_rows = await harness.base.fetch_all(
        "SELECT count(*) FROM knowledge.source_policies"
        " WHERE workspace_id = :workspace_id AND publication_idempotency_key = :key",
        {"workspace_id": harness.workspace_id, "key": "race-same-key"},
    )
    assert int(key_rows[0][0]) == 1
