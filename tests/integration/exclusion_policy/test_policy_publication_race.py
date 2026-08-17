"""Concurrent source-publication and policy-publication race serialization.

Proves the frozen global row-lock order over the real baseline: a source
commit holds the publication idempotency advisory lock, then blocks on the
``workspace_policy_state`` row lock, and observes exactly the policy state the
concurrent holder committed — a policy revision that activates while the
source commit waits serializes before it and denies the publication (the
in-flight commit never sees a torn pointer); a policy-state holder that
outlives the transaction lock timeout fails the source commit with the
retryable contention error and no canonical row; and concurrent source
commits across sources complete without any inverse-order deadlock while a
policy revision publishes.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from tests.integration.exclusion_policy.conftest import PolicyMigrationHarness
from tests.integration.exclusion_policy.test_source_publication_enforcement import (
    PAYLOAD,
    EnforcementHarness,
    _context,
    _rule,
)

from personal_os.error_contracts.codes import ErrorCode
from personal_os.exclusion_policy.contracts import ExclusionPolicyRevision, RuleKind
from personal_os.exclusion_policy.errors import ExclusionPolicyError
from personal_os.sources.errors import SourcePublicationError
from personal_os.sources.fingerprint import compute_request_fingerprint
from postgresql_source_store.tables import (
    policy_signing_keys,
    source_policies,
    workspace_policy_state,
)

pytestmark = pytest.mark.local_stack


@pytest.fixture(scope="module")
def race_secret_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("publication-race-secrets")


async def _seed_rules_revision(harness: EnforcementHarness) -> int:
    """Seed a signed empty revision directly (no preview machinery)."""

    from tools.signed_policy_seed import seed_signed_policy

    receipt = await seed_signed_policy(
        harness.base.engine,
        workspace_id=harness.workspace_id,
        published_by_user_id=harness.owner_user_id,
    )
    return receipt.revision_number


@pytest_asyncio.fixture
async def race_harness(
    policy_migration_harness: PolicyMigrationHarness, race_secret_root: Path
) -> EnforcementHarness:
    harness = EnforcementHarness(policy_migration_harness, race_secret_root)
    await harness.ensure_keys_initialized()
    return harness


async def _insert_denying_revision_in_transaction(
    harness: EnforcementHarness,
    connection: sa.AsyncConnection,
) -> int:
    """Insert and activate a denying signed revision on an open transaction.

    The caller already holds the ``workspace_policy_state`` row lock; the
    guarded pointer swap below therefore cannot race any other writer.
    """


    workspace_id = harness.workspace_id
    state = (
        await connection.execute(
            sa.select(
                workspace_policy_state.c.active_policy_revision_id,
                workspace_policy_state.c.active_revision_number,
            ).where(workspace_policy_state.c.workspace_id == workspace_id)
        )
    ).one()
    parent_revision_id = state[0]
    active_number = int(state[1])
    revision_number = active_number + 1
    policy_revision_id = uuid4()
    signing_key_id = uuid4()
    occurred_at = datetime.now(UTC)
    revision = ExclusionPolicyRevision(
        policy_revision_id=policy_revision_id,
        workspace_id=workspace_id,
        revision_number=revision_number,
        rules=(_rule(RuleKind.MEDIA_TYPE, "text/markdown"),),
    )
    from personal_os.exclusion_policy.signatures import (
        SNAPSHOT_SIGNING_DOMAIN,
        build_signed_message,
        build_snapshot_payload,
        compute_payload_sha256_hex,
    )

    payload_bytes = build_snapshot_payload(
        revision,
        parent_policy_revision_id=parent_revision_id,
        published_at=occurred_at,
    )
    payload_sha256 = compute_payload_sha256_hex(payload_bytes)
    # Sign with the workspace's initialized signer: the revision row binds to
    # that key row, so verification must succeed and evaluation must deny.
    signature_bytes = harness.signing_key.sign(
        build_signed_message(SNAPSHOT_SIGNING_DOMAIN, payload_bytes)
    )
    existing_key_row = (
        await connection.execute(
            sa.select(policy_signing_keys.c.signing_key_id)
            .where(
                policy_signing_keys.c.workspace_id == workspace_id,
                policy_signing_keys.c.public_key_bytes
                == harness.signing_key.public_key_bytes,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing_key_row is None:
        await connection.execute(
            sa.insert(policy_signing_keys).values(
                signing_key_id=signing_key_id,
                workspace_id=workspace_id,
                algorithm="Ed25519",
                public_key_bytes=harness.signing_key.public_key_bytes,
                introduced_keyset_revision=1,
                created_at=occurred_at,
            )
        )
    else:
        signing_key_id = existing_key_row
    await connection.execute(
        sa.insert(source_policies).values(
            policy_revision_id=policy_revision_id,
            workspace_id=workspace_id,
            revision_number=revision_number,
            parent_policy_revision_id=parent_revision_id,
            source_checkpoint_event_sequence=0,
            policy_preview_id=await _insert_bound_preview_row(
                connection, harness, parent_revision_id
            ),
            publication_idempotency_key=f"race-{uuid4().hex}",
            request_fingerprint=uuid4().hex + uuid4().hex,
            snapshot_contract="exclusion_policy_snapshot/v1",
            snapshot_payload_bytes=payload_bytes,
            snapshot_payload_sha256=payload_sha256,
            signing_key_id=signing_key_id,
            signature_bytes=signature_bytes,
            published_by_user_id=harness.owner_user_id,
            published_at=occurred_at,
        )
    )
    swapped = await connection.execute(
        sa.update(workspace_policy_state)
        .values(
            active_policy_revision_id=policy_revision_id,
            active_revision_number=revision_number,
            updated_at=occurred_at,
        )
        .where(
            workspace_policy_state.c.workspace_id == workspace_id,
            workspace_policy_state.c.active_policy_revision_id == parent_revision_id,
            workspace_policy_state.c.active_revision_number == active_number,
        )
    )
    assert swapped.rowcount == 1, "guarded in-transaction pointer swap failed"
    return revision_number


async def _insert_bound_preview_row(
    connection: sa.AsyncConnection, harness: EnforcementHarness, base_revision_id: UUID | None
) -> UUID:
    """Insert one ready preview row the in-transaction revision binds to.

    The revision row carries unique foreign keys into ``policy_previews``
    (one revision per preview), so each seeded revision binds to its own
    ready row over the workspace's existing draft.
    """

    import hashlib as _hashlib

    draft_id = (
        await connection.execute(
            sa.text(
                "SELECT policy_draft_id FROM knowledge.policy_drafts"
                " WHERE workspace_id = :workspace_id"
            ),
            {"workspace_id": str(harness.workspace_id)},
        )
    ).scalar_one()
    preview_id = uuid4()
    await connection.execute(
        sa.text(
            "INSERT INTO knowledge.policy_previews"
            " (policy_preview_id, workspace_id, policy_draft_id, draft_version,"
            " draft_sha256, base_policy_revision_id,"
            " source_checkpoint_event_sequence, state, newly_excluded_count,"
            " still_excluded_count, newly_allowed_count, still_allowed_count,"
            " indeterminate_count, impact_digest, created_by_user_id, created_at,"
            " ready_at, expires_at)"
            " VALUES (:policy_preview_id, :workspace_id, :policy_draft_id, 1,"
            " :draft_sha256, :base_revision_id, 0, 'ready', 0, 0, 0, 0, 0,"
            " :impact_digest, :created_by, CURRENT_TIMESTAMP,"
            " CURRENT_TIMESTAMP, CURRENT_TIMESTAMP + interval '15 minutes')"
        ),
        {
            "policy_preview_id": str(preview_id),
            "workspace_id": str(harness.workspace_id),
            "policy_draft_id": str(draft_id),
            "draft_sha256": _hashlib.sha256(b"race-draft").hexdigest(),
            "base_revision_id": None if base_revision_id is None else str(base_revision_id),
            "impact_digest": _hashlib.sha256(b"race-impact").hexdigest(),
            "created_by": str(harness.owner_user_id),
        },
    )
    return preview_id


@pytest.mark.asyncio
async def test_in_flight_commit_serializes_behind_the_policy_state_row_lock(
    race_harness: EnforcementHarness,
) -> None:
    await _seed_rules_revision(race_harness)
    command = race_harness.build_create_command(PAYLOAD)
    receipt = race_harness.object_store._receipt(PAYLOAD, "text/markdown")

    async with race_harness.base.engine.connect() as holder:
        async with holder.begin():
            await holder.execute(
                sa.select(workspace_policy_state.c.workspace_id)
                .where(workspace_policy_state.c.workspace_id == race_harness.workspace_id)
                .with_for_update()
            )
            denial_revision = await _insert_denying_revision_in_transaction(
                race_harness, holder
            )
            # The source commit starts while the policy-state row is still
            # locked; it must block at the recheck, not read a torn pointer.
            commit_task = asyncio.create_task(
                race_harness.source_store.commit_create(
                    command,
                    compute_request_fingerprint(command),
                    receipt,
                    _context(),
                )
            )
            await asyncio.sleep(0.5)
            assert not commit_task.done(), "commit must block on the policy-state row"

        # Releasing the transaction publishes the denying revision; the
        # serialized commit now re-evaluates under it and fails closed.
        with pytest.raises(ExclusionPolicyError) as raised:
            await commit_task
        assert raised.value.error_code is ErrorCode.EXCLUSION_POLICY_DENIED
        assert raised.value.safe_details == {"policy_revision_number": denial_revision}
        assert await race_harness.new_rows("sources") == 0
        assert await race_harness.new_rows("sync_events") == 0


@pytest.mark.asyncio
async def test_policy_state_lock_outliving_the_timeout_fails_retryable(
    race_harness: EnforcementHarness,
) -> None:
    await _seed_rules_revision(race_harness)
    command = race_harness.build_create_command(PAYLOAD)
    receipt = race_harness.object_store._receipt(PAYLOAD, "text/markdown")
    holder_ready = asyncio.Event()
    release = asyncio.Event()

    async def hold_policy_state_row() -> None:
        async with race_harness.base.engine.connect() as holder, holder.begin():
            await holder.execute(
                sa.select(workspace_policy_state.c.workspace_id).where(
                    workspace_policy_state.c.workspace_id == race_harness.workspace_id
                ).with_for_update()
            )
            holder_ready.set()
            await release.wait()

    holder_task = asyncio.create_task(hold_policy_state_row())
    await holder_ready.wait()
    try:
        with pytest.raises(SourcePublicationError) as raised:
            await race_harness.source_store.commit_create(
                command,
                compute_request_fingerprint(command),
                receipt,
                _context(),
            )
        # The transaction lock timeout maps onto the retryable busy error;
        # nothing was published.
        assert raised.value.error_code is ErrorCode.SOURCE_CONCURRENCY_BUSY
        assert raised.value.is_retryable is True
        assert await race_harness.new_rows("sources") == 0
    finally:
        release.set()
        await holder_task


@pytest.mark.asyncio
async def test_concurrent_source_commits_and_policy_publication_deadlock_free(
    race_harness: EnforcementHarness,
) -> None:
    await _seed_rules_revision(race_harness)
    results: list[BaseException | None] = []

    async def publish_one(index: int) -> None:
        payload = f"# Subject {index}\n\n{uuid4().hex}\n".encode()
        command = race_harness.build_create_command(payload)

        async def stream():  # type: ignore[no-untyped-def]
            yield payload

        try:
            await race_harness.publication_service.publish_create(
                command=command, stream=stream(), diagnostic_context=_context()
            )
            results.append(None)
        except Exception as error:
            results.append(error)

    async def publish_policy_concurrently() -> None:
        # A directly seeded signed revision models the concurrent policy
        # activation without the preview/checkpoint machinery; it competes
        # for the same policy-state row lock as every source commit.
        await asyncio.sleep(0.05)
        await _seed_rules_revision(race_harness)

    await asyncio.gather(
        *(publish_one(index) for index in range(4)),
        publish_policy_concurrently(),
    )

    assert results == [None, None, None, None]
    assert await race_harness.new_rows("sources") == 4
