"""Ambiguous publication commit acknowledgements over the real stack (spec 11.2).

The network, not the database, decides whether the client sees the response
of a committed publication. The tests drive the real service and store over
a disposable stack and inject the ambiguous case at the honest seams: a
retry of the exact committed command replays the original revision number,
IDs, hash, key ID, publication time and reconciliation status without
signing or inserting again; a driver-class connection failure raised AFTER
the commit landed resolves through the fresh-connection evidence lookup into
the exact replay — never a second revision, never a second signature; and a
connection failure raised BEFORE any commit resolves as proven absence,
permits the one bounded retry and lands exactly one revision. PostgreSQL
unavailability that cannot prove either side surfaces as the retryable
commit-outcome-unknown error and never assumes a rollback.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
import sqlalchemy.exc as sa_exc
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
from postgresql_source_store.policy_drafts import PolicyDatabaseRetryPolicy
from postgresql_source_store.policy_previews import PostgresqlPolicyPreviewStore
from postgresql_source_store.policy_publication import (
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


class _ConnectionLostDriverError(Exception):
    """Driver exception carrying the connection-failure SQLSTATE 08006."""

    sqlstate = "08006"


def _connection_lost_failure() -> sa_exc.DBAPIError:
    return sa_exc.DBAPIError(
        "SELECT do-not-emit-sql FROM knowledge.source_policies",
        {},
        _ConnectionLostDriverError(),
    )


class _CountingSigner:
    """Loaded-signing-key wrapper counting sign invocations."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.calls = 0

    @property
    def key_id(self) -> str:
        return self._inner.key_id

    def sign(self, message: bytes) -> bytes:
        self.calls += 1
        return self._inner.sign(message)


class _PostCommitLossStore(PostgresqlPolicyPublicationStore):
    """Store whose first commit acknowledgement is lost after the commit.

    The overridden once-hook delegates to the real transaction — which
    commits — and then raises the connection-class driver failure exactly
    like a network cut between the server's commit and the client's
    acknowledgement.
    """

    def __init__(self, engine: Any) -> None:
        super().__init__(engine)
        self.committed_once = False

    async def _commit_publication_once(
        self,
        command: PublishPolicyCommand,
        fingerprint: Any,
        build_signed_snapshot: Any,
        context: DiagnosticContext,
        identities: Any,
    ) -> Any:
        result = await super()._commit_publication_once(
            command, fingerprint, build_signed_snapshot, context, identities
        )
        if not self.committed_once and not result.is_replay:
            self.committed_once = True
            raise _connection_lost_failure()
        return result


class _PreCommitLossStore(PostgresqlPolicyPublicationStore):
    """Store losing its first connection before any commit lands.

    The first attempt raises the connection-class driver failure without
    touching the database, so the recovery lookup proves absence and the
    bounded retry re-runs the normal commit path.
    """

    def __init__(self, engine: Any) -> None:
        super().__init__(engine)
        self.failed_once = False

    async def _commit_publication_once(
        self,
        command: PublishPolicyCommand,
        fingerprint: Any,
        build_signed_snapshot: Any,
        context: DiagnosticContext,
        identities: Any,
    ) -> Any:
        if not self.failed_once:
            self.failed_once = True
            raise _connection_lost_failure()
        return await super()._commit_publication_once(
            command, fingerprint, build_signed_snapshot, context, identities
        )


class _UnprovingRecoverStore(PostgresqlPolicyPublicationStore):
    """Store whose evidence lookup also fails: the outcome stays unknown."""

    def __init__(self, engine: Any) -> None:
        super().__init__(engine)
        self.commit_failed = False

    async def _commit_publication_once(
        self,
        command: PublishPolicyCommand,
        fingerprint: Any,
        build_signed_snapshot: Any,
        context: DiagnosticContext,
        identities: Any,
    ) -> Any:
        if not self.commit_failed:
            self.commit_failed = True
            raise _connection_lost_failure()
        return await super()._commit_publication_once(
            command, fingerprint, build_signed_snapshot, context, identities
        )

    async def _resolve_committed_once(
        self,
        command: PublishPolicyCommand,
        fingerprint: Any,
        context: DiagnosticContext,
    ) -> Any:
        if self.commit_failed:
            # Every evidence lookup fails while the outcome is unknown.
            raise _connection_lost_failure()
        return await super()._resolve_committed_once(command, fingerprint, context)


def _context() -> DiagnosticContext:
    return DiagnosticContext(request_id=uuid4(), client_request_id=None, trace=_TRACE)


class AmbiguityHarness:
    """Key, preview and command helpers over one engine."""

    def __init__(self, base: PolicyMigrationHarness, secret_root: Path) -> None:
        self.base = base
        self.engine = base.engine
        self._secret_root = secret_root
        self.signing_key = create_or_load_policy_signing_key(secret_root, KEY_FILE_NAME)
        self.verifier = Ed25519PolicyVerifier(
            {self.signing_key.key_id: self.signing_key.public_key_bytes}
        )
        self.preview_store = PostgresqlPolicyPreviewStore(base.engine)

    async def ensure_keys_initialized(self) -> None:
        await execute_policy_key_initialize(
            engine=self.engine,
            workspace_id=self.base.stack.workspace_id,
            key_file_name=KEY_FILE_NAME,
            secret_root=self._secret_root,
            context=_context(),
        )

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

    def service(
        self, store: PostgresqlPolicyPublicationStore, signer: Any
    ) -> ExclusionPolicyPublicationService:
        return ExclusionPolicyPublicationService(store=store, signer=signer, verifier=self.verifier)

    def plain_store(self) -> PostgresqlPolicyPublicationStore:
        return PostgresqlPolicyPublicationStore(self.engine)

    async def revision_count(self) -> int:
        return int(
            await self.base.fetch_scalar(
                "SELECT count(*) FROM knowledge.source_policies WHERE workspace_id = :workspace_id",
                {"workspace_id": self.workspace_id},
            )
        )

    async def published_audit_count(self) -> int:
        return int(
            await self.base.fetch_scalar(
                "SELECT count(*) FROM knowledge.audit_events"
                " WHERE workspace_id = :workspace_id AND action = :action",
                {"workspace_id": self.workspace_id, "action": PUBLISHED_AUDIT_ACTION},
            )
        )


@pytest.fixture(scope="module")
def policy_secret_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("policy-ambiguity-secrets")


@pytest_asyncio.fixture
async def harness(
    policy_migration_harness: PolicyMigrationHarness, policy_secret_root: Path
) -> AmbiguityHarness:
    ambiguity_harness = AmbiguityHarness(policy_migration_harness, policy_secret_root)
    await ambiguity_harness.ensure_keys_initialized()
    return ambiguity_harness


@pytest.mark.asyncio
async def test_lost_acknowledgement_retry_returns_the_exact_replay(
    harness: AmbiguityHarness,
) -> None:
    revisions_before = await harness.revision_count()
    preview = await harness.ready_preview()
    command = await harness.build_command(preview, key="lost-ack-replay-001")
    counting = _CountingSigner(harness.signing_key)
    committed = await harness.service(harness.plain_store(), counting).publish(command, _context())
    assert committed.is_replay is False
    audits_after_commit = await harness.published_audit_count()
    # The retry arrives after the client lost the acknowledgement.
    replay = await harness.service(harness.plain_store(), counting).publish(command, _context())
    assert replay.is_replay is True
    assert replay.policy_revision_id == committed.policy_revision_id
    assert replay.revision_number == committed.revision_number
    assert replay.parent_policy_revision_id == committed.parent_policy_revision_id
    assert replay.payload_sha256 == committed.payload_sha256
    assert replay.signing_key_id == committed.signing_key_id
    assert replay.published_at == committed.published_at
    assert replay.reconciliation_status == committed.reconciliation_status
    assert counting.calls == 1
    assert await harness.revision_count() == revisions_before + 1
    assert await harness.published_audit_count() == audits_after_commit


@pytest.mark.asyncio
async def test_connection_loss_after_commit_resolves_to_the_replay(
    harness: AmbiguityHarness,
) -> None:
    revisions_before = await harness.revision_count()
    preview = await harness.ready_preview()
    command = await harness.build_command(preview, key="post-commit-loss-001")
    counting = _CountingSigner(harness.signing_key)
    store = _PostCommitLossStore(harness.engine)
    result = await harness.service(store, counting).publish(command, _context())
    # The commit landed and the acknowledgement was lost; the fresh-connection
    # evidence lookup returned the exact replay rather than a second commit.
    assert store.committed_once is True
    assert result.is_replay is True
    assert result.revision_number >= 1
    assert await harness.revision_count() == revisions_before + 1
    assert counting.calls == 1
    key_rows = await harness.base.fetch_all(
        "SELECT count(*) FROM knowledge.source_policies"
        " WHERE workspace_id = :workspace_id AND publication_idempotency_key = :key",
        {"workspace_id": harness.workspace_id, "key": "post-commit-loss-001"},
    )
    assert int(key_rows[0][0]) == 1


@pytest.mark.asyncio
async def test_connection_loss_before_commit_retries_once_and_lands(
    harness: AmbiguityHarness,
) -> None:
    revisions_before = await harness.revision_count()
    preview = await harness.ready_preview()
    command = await harness.build_command(preview, key="pre-commit-loss-001")
    store = _PreCommitLossStore(harness.engine)
    result = await harness.service(store, harness.signing_key).publish(command, _context())
    assert store.failed_once is True
    assert result.is_replay is False
    assert await harness.revision_count() == revisions_before + 1


@pytest.mark.asyncio
async def test_unprovable_outcome_surfaces_retryable_unknown(
    harness: AmbiguityHarness,
) -> None:
    revisions_before = await harness.revision_count()
    preview = await harness.ready_preview()
    command = await harness.build_command(preview, key="unprovable-001")
    store = _UnprovingRecoverStore(harness.engine)
    with pytest.raises(ExclusionPolicyError) as raised:
        await harness.service(store, harness.signing_key).publish(command, _context())
    assert raised.value.error_code is ErrorCode.EXCLUSION_POLICY_COMMIT_OUTCOME_UNKNOWN
    assert raised.value.is_retryable is True
    assert not raised.value.safe_details
    assert await harness.revision_count() == revisions_before


@pytest.mark.asyncio
async def test_retry_policy_bounds_the_attempts() -> None:
    policy = PolicyDatabaseRetryPolicy(maximum_attempts=2)
    assert policy.maximum_attempts == 2
