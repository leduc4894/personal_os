"""Large-fixture concurrency acceptance: 100 replays beside 100 independent publishes.

One committed source is updated by 100 exact concurrent replays of the same
command while 100 independent sources publish their own creates, all inside
one finite task-group deadline against a dedicated test engine whose pool is
sized for the fixture (the production store keeps its pinned 4+4 pool; the
Task 9 note about the thin 100-replay margin is answered here by exposing a
test-owned pool instead of weakening the store). The replays must converge on
exactly one canonical committed event with equivalent results, every
independent publish must succeed, the pool must show no leaked checkout after
the gather, and completing inside the deadline block proves no deadline was
missed — without asserting any machine-specific latency.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Final
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from personal_os.diagnostics.context import create_diagnostic_context
from personal_os.object_storage import (
    CanonicalMediaType,
    ContentDigest,
    ExpectedObject,
    VerificationMethod,
    VerifiedObjectReceipt,
    derive_canonical_object_key,
)
from personal_os.sources.actors import ActorKind, SourceActor
from personal_os.sources.commands import (
    CreateSourceVersion,
    IdempotencyKey,
    SourceTitle,
    SourceType,
    UpdateSourceVersion,
)
from personal_os.sources.fingerprint import compute_request_fingerprint
from personal_os.sources.results import PublicationOutcome
from postgresql_source_store.engine import (
    TRANSACTION_ISOLATION_LEVEL,
    build_source_database_url,
    build_source_store_connect_arguments,
    dispose_source_store_engine,
)
from postgresql_source_store.publication_store import PostgresqlSourcePublicationStore
from postgresql_source_store.tables import source_versions, sources, sync_events

pytestmark = pytest.mark.local_stack

#: Finite deadline for the whole gather: expiry raises, completion proves it held.
GATHER_DEADLINE_SECONDS: Final[float] = 240.0
EXACT_REPLAY_COUNT: Final[int] = 100
INDEPENDENT_PUBLISH_COUNT: Final[int] = 100

#: Test-owned pool bounds for the 200-coroutine fixture. The production store
#: keeps the pinned 4+4 pool from ``postgresql_source_store.settings``; only
#: this acceptance harness widens the pool so the 5-second lock timeout of the
#: serialized replays is never spent waiting for a checkout.
LARGE_FIXTURE_POOL_SIZE: Final[int] = 32
LARGE_FIXTURE_MAX_OVERFLOW: Final[int] = 16
LARGE_FIXTURE_POOL_TIMEOUT_SECONDS: Final[int] = 30


def _diagnostic_context():
    return create_diagnostic_context().context


def _receipt(salt: str) -> VerifiedObjectReceipt:
    digest = ContentDigest.parse(hashlib.sha256(salt.encode("utf-8")).hexdigest())
    return VerifiedObjectReceipt(
        content_digest=digest,
        object_key=derive_canonical_object_key(digest),
        size_bytes=len(salt),
        media_type=CanonicalMediaType.parse("text/markdown"),
        verified_at=datetime.now(UTC),
        verification_method=VerificationMethod.UPLOADED_FULL_READ,
    )


def _expected_object(receipt: VerifiedObjectReceipt) -> ExpectedObject:
    return ExpectedObject(
        content_digest=receipt.content_digest,
        size_bytes=receipt.size_bytes,
        media_type=receipt.media_type,
    )


def _create_command(
    workspace, salt: str, *, idempotency_value: str, source_id: UUID
) -> CreateSourceVersion:
    return CreateSourceVersion(
        workspace_id=workspace.workspace_id,
        source_id=source_id,
        event_id=uuid4(),
        idempotency_key=IdempotencyKey(idempotency_value),
        source_type=SourceType.MARKDOWN,
        title=SourceTitle("Large fixture note"),
        actor=SourceActor(ActorKind.USER, workspace.owner_user_id),
        expected_object=_expected_object(_receipt(salt)),
        client_timestamp=None,
    )


def _update_command(
    workspace,
    salt: str,
    *,
    source_id: UUID,
    base_version_id: UUID,
    idempotency_value: str,
) -> UpdateSourceVersion:
    return UpdateSourceVersion(
        workspace_id=workspace.workspace_id,
        source_id=source_id,
        event_id=uuid4(),
        idempotency_key=IdempotencyKey(idempotency_value),
        base_version_id=base_version_id,
        actor=SourceActor(ActorKind.USER, workspace.owner_user_id),
        expected_object=_expected_object(_receipt(salt)),
        client_timestamp=None,
    )


@pytest_asyncio.fixture
async def large_fixture_engine(source_publication_stack) -> Iterator[AsyncEngine]:
    engine = create_async_engine(
        build_source_database_url(
            source_publication_stack.settings, source_publication_stack.password
        ),
        pool_pre_ping=True,
        pool_size=LARGE_FIXTURE_POOL_SIZE,
        max_overflow=LARGE_FIXTURE_MAX_OVERFLOW,
        pool_timeout=LARGE_FIXTURE_POOL_TIMEOUT_SECONDS,
        isolation_level=TRANSACTION_ISOLATION_LEVEL,
        connect_args=dict(build_source_store_connect_arguments(source_publication_stack.settings)),
    )
    try:
        yield engine
    finally:
        await dispose_source_store_engine(engine)


async def _capture(coro) -> object:
    """Return the coroutine's result, or its raised exception as a value."""
    try:
        return await coro
    except BaseException as error:  # test double captures every failure outcome
        return error


async def _count_rows(engine: AsyncEngine, table, *conditions) -> int:
    async with engine.connect() as connection:
        statement = sa.select(sa.func.count()).select_from(table)
        for condition in conditions:
            statement = statement.where(condition)
        return int((await connection.execute(statement)).scalar_one())


@pytest.mark.asyncio
async def test_hundred_replays_and_hundred_independent_publishes_hold_their_bounds(
    preflight_harness, large_fixture_engine: AsyncEngine
) -> None:
    workspace = await preflight_harness.seed_workspace()
    from api_runtime.exclusion_policy_crypto import TrustAnchorEd25519Verifier

    store = PostgresqlSourcePublicationStore(
        large_fixture_engine, policy_verifier=TrustAnchorEd25519Verifier()
    )

    seed_salt = f"large-fixture-seed-{uuid4()}"
    seed_command = _create_command(
        workspace, seed_salt, idempotency_value=f"large-fixture-seed-{uuid4()}", source_id=uuid4()
    )
    seeded = await store.commit_create(
        seed_command,
        compute_request_fingerprint(seed_command),
        _receipt(seed_salt),
        _diagnostic_context(),
    )

    replay_salt = f"large-fixture-replay-{uuid4()}"
    replay_command = _update_command(
        workspace,
        replay_salt,
        source_id=seeded.source_id,
        base_version_id=seeded.source_version_id,
        idempotency_value="large-fixture-replay-1",
    )
    replay_fingerprint = compute_request_fingerprint(replay_command)
    replay_receipt = _receipt(replay_salt)

    independent_commands = [
        _create_command(
            workspace,
            f"large-fixture-independent-{uuid4()}",
            idempotency_value=f"large-fixture-independent-{index}",
            source_id=uuid4(),
        )
        for index in range(INDEPENDENT_PUBLISH_COUNT)
    ]

    # One finite deadline governs the whole 200-task gather: a missed deadline
    # raises ``TimeoutError`` here instead of asserting elapsed wall time.
    async with asyncio.timeout(GATHER_DEADLINE_SECONDS):
        replay_outcomes, independent_outcomes = await asyncio.gather(
            asyncio.gather(
                *(
                    _capture(
                        store.commit_update(
                            replay_command,
                            replay_fingerprint,
                            replay_receipt,
                            _diagnostic_context(),
                        )
                    )
                    for _ in range(EXACT_REPLAY_COUNT)
                )
            ),
            asyncio.gather(
                *(
                    _capture(
                        store.commit_create(
                            command,
                            compute_request_fingerprint(command),
                            _independent_receipt(command),
                            _diagnostic_context(),
                        )
                    )
                    for command in independent_commands
                )
            ),
        )

    # --- 100 exact replays: no failure, one canonical event, equal results ----
    assert not any(isinstance(outcome, BaseException) for outcome in replay_outcomes)
    replay_results = replay_outcomes
    assert all(result == replay_results[0] for result in replay_results)
    assert replay_results[0].outcome is PublicationOutcome.PUBLISHED
    assert replay_results[0].content_version == 2
    assert replay_results[0].event_id == replay_command.event_id
    assert (
        await _count_rows(
            large_fixture_engine, sync_events, sync_events.c.event_id == replay_command.event_id
        )
        == 1
    )

    # --- 100 independent publishes: every one commits its own source ---------
    assert not any(isinstance(outcome, BaseException) for outcome in independent_outcomes)
    for command, result in zip(independent_commands, independent_outcomes, strict=True):
        assert result.outcome is PublicationOutcome.PUBLISHED
        assert result.source_id == command.source_id
        assert result.content_version == 1
    assert len({result.source_id for result in independent_outcomes}) == INDEPENDENT_PUBLISH_COUNT
    assert (
        await _count_rows(
            large_fixture_engine,
            sources,
            sources.c.workspace_id == workspace.workspace_id,
        )
        == INDEPENDENT_PUBLISH_COUNT + 1
    )
    assert (
        await _count_rows(
            large_fixture_engine,
            source_versions,
            source_versions.c.workspace_id == workspace.workspace_id,
        )
        == INDEPENDENT_PUBLISH_COUNT + 2
    )

    # --- no pool leak: every checked-out connection returned after the gather -
    assert large_fixture_engine.pool.checkedout() == 0
    assert "Current Checked out connections: 0" in large_fixture_engine.pool.status()

    # No business rejection was audited anywhere in the workspace.
    assert await preflight_harness.rejection_audit_rows(workspace_id=workspace.workspace_id) == []


def _independent_receipt(command: CreateSourceVersion) -> VerifiedObjectReceipt:
    """Rebuild the receipt the command's expected object was derived from."""

    digest = command.expected_object.content_digest
    return VerifiedObjectReceipt(
        content_digest=digest,
        object_key=derive_canonical_object_key(digest),
        size_bytes=command.expected_object.size_bytes,
        media_type=command.expected_object.media_type,
        verified_at=datetime.now(UTC),
        verification_method=VerificationMethod.UPLOADED_FULL_READ,
    )
