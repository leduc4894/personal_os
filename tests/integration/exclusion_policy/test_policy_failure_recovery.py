"""Failure and recovery contracts around publication (spec 15/22/23.4).

Four failure modes against the real disposable stack: an ambiguous commit
acknowledgement recovers through evidence lookup by a completely fresh
service instance — the retry after an operator restart returns the exact
replay without duplicating revision, audit or reconciliation intent; a
Temporal outage after publication is tolerated by the leased reconciliation
outbox (the dispatcher releases the durable intent back to pending with the
bounded backoff, the committed revision never moves, and the scan converges
once dispatch succeeds again); an unreachable PostgreSQL fails closed at the
enforcement guard — no path falls back to allow; and a workspace whose
persisted trust-anchor row vanished denies every read as
signing-unavailable.
"""

from __future__ import annotations

import asyncio
import hashlib
import shutil
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from api_runtime.exclusion_policy_commands import create_or_load_policy_signing_key
from api_runtime.exclusion_policy_crypto import TrustAnchorEd25519Verifier
from tests.integration.exclusion_policy.conftest import (
    PolicyMigrationHarness,
    PolicyMigrationStack,
)
from tests.integration.exclusion_policy.test_policy_ambiguous_commit import (
    KEY_FILE_NAME as AMBIGUITY_KEY_FILE_NAME,
)
from tests.integration.exclusion_policy.test_policy_ambiguous_commit import (
    AmbiguityHarness,
    _PostCommitLossStore,
)
from tests.integration.exclusion_policy.test_source_publication_enforcement import (
    KEY_FILE_NAME as ENFORCEMENT_KEY_FILE_NAME,
)
from tests.integration.exclusion_policy.test_source_publication_enforcement import (
    EnforcementHarness,
    _activate_forged_revision,
    _context,
)
from workflow_worker.policy_reconciliation_workflow import PolicyReconciliationStartOutcome
from workflow_worker.policy_workflow_runtime import PolicyReconciliationDispatchRuntime

from personal_os.error_contracts.codes import ErrorCode
from personal_os.exclusion_policy.contracts import RuleKind
from personal_os.exclusion_policy.errors import ExclusionPolicyError
from personal_os.exclusion_policy.normalization import normalize_rule
from personal_os.exclusion_policy.reconciliation import ReconciliationCounters
from personal_os.object_storage import CanonicalMediaType, ContentDigest, ExpectedObject
from personal_os.sources.commands import SourceType
from personal_os.sources.metrics import InMemoryCanonicalReadMetrics
from personal_os.sources.reading import (
    CanonicalSourceReadService,
    CanonicalSourceReference,
    ReadCurrentSourceCommand,
)
from postgresql_source_store.canonical_read import PostgresqlCanonicalSourceReadStore
from postgresql_source_store.engine import (
    create_source_store_engine,
    dispose_source_store_engine,
)
from postgresql_source_store.policy_enforcement import compose_policy_enforcement
from postgresql_source_store.policy_reconciliation import (
    RECONCILIATION_COMPLETED_AUDIT_ACTION,
    PostgresqlPolicyReconciliationStore,
)
from postgresql_source_store.tables import workspace_policy_state

pytestmark = pytest.mark.local_stack


class _TemporalOutageStarter:
    """Start port failing exactly like the Temporal RPC outage mapping.

    The real ``TemporalPolicyReconciliationStarter`` maps a Temporal RPC
    unavailability to the retryable typed ``commit_outcome_unknown`` failure;
    the dispatcher's contract is identical for that exact failure, proven
    here against the durable store without tearing the shared stack's real
    Temporal service down mid-module.
    """

    def __init__(self) -> None:
        self.start_attempts = 0

    async def start_policy_reconciliation(self, reference: Any) -> PolicyReconciliationStartOutcome:
        del reference
        self.start_attempts += 1
        raise ExclusionPolicyError(ErrorCode.EXCLUSION_POLICY_COMMIT_OUTCOME_UNKNOWN)


class _ConvergedStarter:
    """Start port whose dispatch always converges."""

    def __init__(self) -> None:
        self.start_attempts = 0

    async def start_policy_reconciliation(self, reference: Any) -> PolicyReconciliationStartOutcome:
        del reference
        self.start_attempts += 1
        return PolicyReconciliationStartOutcome.STARTED


def _read_service(harness: EnforcementHarness) -> CanonicalSourceReadService:
    return CanonicalSourceReadService(
        store=PostgresqlCanonicalSourceReadStore(
            harness.base.engine, policy_verifier=harness.policy_verifier
        ),
        object_store=harness.object_store,
        metrics=InMemoryCanonicalReadMetrics(),
        policy_guard=compose_policy_enforcement(
            harness.base.engine, verifier=harness.policy_verifier
        ),
    )


def _resolved_reference(workspace_id: UUID, source_id: UUID) -> CanonicalSourceReference:
    """One structurally valid resolved reference for direct guard calls."""
    payload = b"unreachable-guard-probe"
    digest = ContentDigest.parse(hashlib.sha256(payload).hexdigest())
    return CanonicalSourceReference(
        workspace_id=workspace_id,
        source_id=source_id,
        source_version_id=uuid4(),
        content_version=1,
        source_type=SourceType.MARKDOWN,
        expected_object=ExpectedObject(
            content_digest=digest,
            size_bytes=len(payload),
            media_type=CanonicalMediaType.parse("text/markdown"),
        ),
        committed_at=datetime.now(UTC),
    )


@pytest.fixture(scope="module")
def recovery_secret_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The module's one signing-key secret root, shared by both harness flavors.

    The module's one workspace carries exactly one keyset, and the module's
    two harness flavors name their key file differently — the ambiguity
    harness ``policy_signing_initial.pem``, the enforcement harness
    ``enforcement_signing_initial.pem``. Both names are bound to the same
    generated key so whichever harness initializes the keyset first, the
    other acknowledges the replay instead of being refused as a second key.
    """
    root = tmp_path_factory.mktemp("policy-failure-recovery-secrets")
    create_or_load_policy_signing_key(root, AMBIGUITY_KEY_FILE_NAME)
    shutil.copyfile(
        root / AMBIGUITY_KEY_FILE_NAME,
        root / ENFORCEMENT_KEY_FILE_NAME,
    )
    return root


@pytest_asyncio.fixture
async def ambiguity_harness(
    policy_migration_harness: PolicyMigrationHarness,
    recovery_secret_root: Path,
) -> AsyncIterator[AmbiguityHarness]:
    harness = AmbiguityHarness(policy_migration_harness, recovery_secret_root)
    await harness.ensure_keys_initialized()
    return harness


@pytest.mark.asyncio
async def test_ambiguous_commit_recovers_through_a_fresh_process_evidence_lookup(
    ambiguity_harness: AmbiguityHarness,
) -> None:
    revisions_before = await ambiguity_harness.revision_count()
    preview = await ambiguity_harness.ready_preview()
    command = await ambiguity_harness.build_command(preview, key="fresh-process-001")
    signer = ambiguity_harness.signing_key

    # The first process loses the commit acknowledgement: the revision is
    # committed and the connection-class failure surfaces — but the service's
    # bounded retry resolves it through the evidence lookup in the same
    # process, returning the exact replay rather than a second commit.
    losing_store = _PostCommitLossStore(ambiguity_harness.engine)
    recovered = await ambiguity_harness.service(losing_store, signer).publish(command, _context())
    assert losing_store.committed_once is True
    assert recovered.is_replay is True
    assert recovered.revision_number == revisions_before + 1

    # A completely fresh service/store pair — an operator restart — replays
    # the same command and converges on the committed outcome without
    # duplicating revision, audit or reconciliation intent.
    fresh_service = ambiguity_harness.service(ambiguity_harness.plain_store(), signer)
    replayed = await fresh_service.publish(command, _context())
    assert replayed.is_replay is True
    assert replayed.revision_number == recovered.revision_number
    assert await ambiguity_harness.revision_count() == revisions_before + 1
    assert await ambiguity_harness.published_audit_count() == 1
    reconciliation_intents = await ambiguity_harness.base.fetch_all(
        "SELECT count(*) FROM knowledge.policy_reconciliation_intents"
        " WHERE workspace_id = :workspace_id",
        {"workspace_id": ambiguity_harness.workspace_id},
    )
    assert int(reconciliation_intents[0][0]) == 1


@pytest.mark.asyncio
async def test_temporal_outage_after_publication_tolerated_then_converges(
    policy_migration_harness: PolicyMigrationHarness,
    recovery_secret_root: Path,
) -> None:
    harness = EnforcementHarness(policy_migration_harness, recovery_secret_root)
    await harness.ensure_keys_initialized()
    denied_source_id = await policy_migration_harness.seed_policy_source()
    deny_rule = normalize_rule(
        uuid4(), RuleKind.EXACT_SOURCE_ID, source_id_operand=denied_source_id
    )
    revision_number = await harness.publish_revision(deny_rule)
    assert revision_number >= 1
    workspace_id = harness.workspace_id
    # The module stack's workspace accumulates revisions across the module's
    # tests, so the graph row is fetched for exactly this publication.
    revision_rows = await policy_migration_harness.fetch_all(
        "SELECT policy_revision_id, source_checkpoint_event_sequence"
        " FROM knowledge.source_policies"
        " WHERE workspace_id = :workspace_id AND revision_number = :revision_number",
        {"workspace_id": workspace_id, "revision_number": revision_number},
    )
    revision_id = UUID(str(revision_rows[0][0]))

    store = PostgresqlPolicyReconciliationStore(policy_migration_harness.engine)

    async def intent_rows() -> list[Any]:
        return await policy_migration_harness.fetch_all(
            "SELECT state, attempt_count FROM knowledge.policy_reconciliation_intents"
            " WHERE workspace_id = :workspace_id AND policy_revision_id = :revision_id",
            {"workspace_id": workspace_id, "revision_id": revision_id},
        )

    assert [row[0] for row in await intent_rows()] == ["pending"]

    # The Temporal outage: the dispatcher cannot start the workflow and
    # releases every claimed intent back to pending with the bounded backoff.
    # The claim batch covers the workspace's due pending intents — the
    # ambiguous-commit test's intent is equally due — so the claimed count is
    # at least this revision's intent.
    outage_runtime = PolicyReconciliationDispatchRuntime(
        store=store,
        starter=_TemporalOutageStarter(),
        clock=lambda: datetime.now(UTC),
    )
    claimed = await outage_runtime.dispatch_pending_reconciliations_once()
    assert claimed >= 1
    released = await intent_rows()
    assert released[0][0] == "pending"
    assert int(released[0][1]) >= 1

    # The committed publication never moved during the outage.
    pointer = await policy_migration_harness.fetch_all(
        "SELECT active_policy_revision_id, active_revision_number"
        " FROM knowledge.workspace_policy_state WHERE workspace_id = :workspace_id",
        {"workspace_id": workspace_id},
    )
    assert pointer[0][0] == revision_id
    assert int(pointer[0][1]) == revision_number

    # Recovery: dispatch succeeds again. The released intents rest behind
    # their bounded backoff (2^attempt seconds), so recovery waits past that
    # window before the converged runtime claims and dispatches them.
    await asyncio.sleep(3)
    converged_runtime = PolicyReconciliationDispatchRuntime(
        store=store,
        starter=_ConvergedStarter(),
        clock=lambda: datetime.now(UTC),
    )
    converged = await converged_runtime.dispatch_pending_reconciliations_once()
    assert converged >= 1
    after_recovery = await intent_rows()
    assert after_recovery[0][0] == "dispatched"

    total_counters = ReconciliationCounters()
    after_source_id: UUID | None = None
    for _ in range(100):
        outcome = await store.run_reconciliation_batch(
            workspace_id,
            revision_id,
            int(revision_rows[0][1]),
            after_source_id,
        )
        total_counters = ReconciliationCounters(
            evaluated_sources=total_counters.evaluated_sources + outcome.evaluated_sources,
            to_excluded_sources=total_counters.to_excluded_sources + outcome.to_excluded_sources,
            to_allowed_sources=total_counters.to_allowed_sources + outcome.to_allowed_sources,
            unchanged_sources=total_counters.unchanged_sources + outcome.unchanged_sources,
        )
        if outcome.superseded:
            pytest.fail("the active revision must not be superseded during recovery")
        if not outcome.has_more:
            break
        assert outcome.last_source_id is not None
        after_source_id = outcome.last_source_id
    else:
        pytest.fail("the reconciliation scan did not converge within the batch bound")
    assert total_counters.evaluated_sources >= 1

    assert await store.complete_reconciliation(workspace_id, revision_id, total_counters)

    async def completion_rows() -> list[Any]:
        return await policy_migration_harness.fetch_all(
            "SELECT count(*) FROM knowledge.audit_events"
            " WHERE workspace_id = :workspace_id AND action = :action",
            {"workspace_id": workspace_id, "action": RECONCILIATION_COMPLETED_AUDIT_ACTION},
        )

    assert int((await completion_rows())[0][0]) == 1
    # Idempotent completion: the replay acknowledges (nothing fresh to write)
    # without a second audit row.
    completed_again = await store.complete_reconciliation(workspace_id, revision_id, total_counters)
    assert completed_again is False
    assert int((await completion_rows())[0][0]) == 1


@pytest.mark.asyncio
async def test_read_fails_closed_when_postgresql_is_unreachable(
    policy_migration_stack: PolicyMigrationStack,
) -> None:
    unreachable_settings = policy_migration_stack.settings.model_copy(update={"database_port": 1})
    engine = create_source_store_engine(unreachable_settings, policy_migration_stack.password)
    guard = compose_policy_enforcement(engine, verifier=TrustAnchorEd25519Verifier())
    try:
        # The guard evaluates the resolved reference against the locked
        # policy state; with PostgreSQL unreachable it fails — it never
        # degrades to an allow.
        with pytest.raises(Exception) as failure:
            await guard.authorize_read(_resolved_reference(UUID(int=1), UUID(int=2)), _context())
        assert failure.value is not None
    finally:
        await dispose_source_store_engine(engine)


@pytest.mark.asyncio
async def test_read_denies_when_trust_anchor_state_is_unavailable(
    policy_migration_harness: PolicyMigrationHarness,
    recovery_secret_root: Path,
) -> None:
    harness = EnforcementHarness(policy_migration_harness, recovery_secret_root)
    await harness.ensure_keys_initialized()
    await harness.publish_revision()
    payload = b"# Trust anchor sentinel note\n" + b"anchor-bytes" * 32
    published = await harness.publish_source(payload)

    served = await _read_service(harness).read_current_source_bytes(
        ReadCurrentSourceCommand(workspace_id=harness.workspace_id, source_id=published.source_id),
        _context(),
    )
    assert served == payload

    # The signature state becomes unavailable: the append-only triggers make
    # persisted history immutable, so the loss model is the same forged
    # INSERT the enforcement suite uses — the active pointer names a revision
    # whose trust anchor never signed it — and the next read denies as
    # signing-unavailable, never allows. The prior pointer is restored so the
    # module's workspace stays bound to its genuinely signed revision.
    prior_revision_id, prior_revision_number = await _activate_forged_revision(harness)
    calls_before = list(harness.object_store.calls)
    try:
        with pytest.raises(ExclusionPolicyError) as denial:
            await _read_service(harness).read_current_source_bytes(
                ReadCurrentSourceCommand(
                    workspace_id=harness.workspace_id, source_id=published.source_id
                ),
                _context(),
            )
        assert denial.value.error_code is ErrorCode.EXCLUSION_POLICY_SIGNING_UNAVAILABLE
        assert harness.object_store.calls == calls_before
    finally:
        async with harness.base.engine.begin() as connection:
            await connection.execute(
                sa.update(workspace_policy_state)
                .values(
                    active_policy_revision_id=prior_revision_id,
                    active_revision_number=prior_revision_number,
                    updated_at=sa.text("CURRENT_TIMESTAMP"),
                )
                .where(workspace_policy_state.c.workspace_id == harness.workspace_id)
            )
