"""Cancellation releases advisory locks, row locks and pool checkout.

A changed update cancelled while it holds the idempotency and source advisory
locks and the locked source rows inside the open transaction must roll back
completely, return its pooled connection, and leave the source immediately
publishable by the next attempt of the exact same event and key — proving the
transaction-scoped locks and the pool checkout were released.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from personal_os.diagnostics.context import DiagnosticContext, create_diagnostic_context
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
from postgresql_source_store.engine import create_source_store_engine, dispose_source_store_engine
from postgresql_source_store.publication_store import PostgresqlSourcePublicationStore
from postgresql_source_store.tables import sources

pytestmark = pytest.mark.local_stack

#: Finite bounds for the cancellation orchestration: no unbounded wait exists.
HANG_REACHED_TIMEOUT_SECONDS: float = 10.0
CANCEL_TIMEOUT_SECONDS: float = 5.0
POOL_SETTLE_TIMEOUT_SECONDS: float = 10.0


def _diagnostic_context() -> DiagnosticContext:
    return create_diagnostic_context().context


def _receipt(salt: str) -> VerifiedObjectReceipt:
    digest = ContentDigest.parse(hashlib.sha256(salt.encode("utf-8")).hexdigest())
    return VerifiedObjectReceipt(
        content_digest=digest,
        object_key=derive_canonical_object_key(digest),
        size_bytes=len(salt),
        media_type=CanonicalMediaType.parse("text/markdown"),
        verified_at=datetime.now(UTC) - timedelta(seconds=1),
        verification_method=VerificationMethod.UPLOADED_FULL_READ,
    )


def _expected_object(receipt: VerifiedObjectReceipt) -> ExpectedObject:
    return ExpectedObject(
        content_digest=receipt.content_digest,
        size_bytes=receipt.size_bytes,
        media_type=receipt.media_type,
    )


@pytest_asyncio.fixture
async def cancellation_engine(source_publication_stack) -> Iterator[AsyncEngine]:
    engine = create_source_store_engine(
        source_publication_stack.settings, source_publication_stack.password
    )
    try:
        yield engine
    finally:
        await dispose_source_store_engine(engine)


class _HangingIntentStore(PostgresqlSourcePublicationStore):
    """Store hanging once inside the open update transaction before an intent.

    The hang happens after the content object, version ``n+1`` and the guarded
    pointer update, while both advisory locks and the source row locks are
    held; cancelling the task from outside proves the transaction unwinds and
    releases everything.
    """

    def __init__(self, engine: AsyncEngine) -> None:
        super().__init__(engine)
        self.hang_reached = asyncio.Event()
        self._hang_armed = True

    async def _insert_projection_intent(
        self,
        connection: AsyncConnection,
        command: CreateSourceVersion | UpdateSourceVersion,
        source_version_id: UUID,
        projection_intent_id: UUID,
        projection_kind: str,
    ) -> None:
        if self._hang_armed and projection_kind == "qdrant":
            self._hang_armed = False
            self.hang_reached.set()
            await asyncio.Event().wait()
        await super()._insert_projection_intent(
            connection,
            command,
            source_version_id,
            projection_intent_id,
            projection_kind,
        )


async def _await_pool_checked_in(engine: AsyncEngine) -> str:
    """Poll the pool status until the cancelled checkout is returned (bounded)."""
    deadline = asyncio.get_running_loop().time() + POOL_SETTLE_TIMEOUT_SECONDS
    status = engine.pool.status()
    while "Checked out connections: 0" not in status:
        if asyncio.get_running_loop().time() >= deadline:
            break
        await asyncio.sleep(0.05)
        status = engine.pool.status()
    return status


@pytest.mark.asyncio
async def test_cancellation_releases_locks_and_pool_checkout(
    preflight_harness, cancellation_engine
) -> None:
    workspace = await preflight_harness.seed_workspace()
    create_salt = f"cancellation-{uuid4()}"
    create_command = CreateSourceVersion(
        workspace_id=workspace.workspace_id,
        source_id=uuid4(),
        event_id=uuid4(),
        idempotency_key=IdempotencyKey("cancellation-seed-1"),
        source_type=SourceType.MARKDOWN,
        title=SourceTitle("Cancellation note"),
        actor=SourceActor(ActorKind.USER, workspace.owner_user_id),
        expected_object=_expected_object(_receipt(create_salt)),
        client_timestamp=None,
    )
    first = await preflight_harness.store.commit_create(
        create_command,
        compute_request_fingerprint(create_command),
        _receipt(create_salt),
        _diagnostic_context(),
    )

    update_salt = f"cancellation-next-{uuid4()}"
    update_command = UpdateSourceVersion(
        workspace_id=workspace.workspace_id,
        source_id=first.source_id,
        event_id=uuid4(),
        idempotency_key=IdempotencyKey("cancellation-update-1"),
        base_version_id=first.source_version_id,
        actor=SourceActor(ActorKind.USER, workspace.owner_user_id),
        expected_object=_expected_object(_receipt(update_salt)),
        client_timestamp=None,
    )
    update_fingerprint = compute_request_fingerprint(update_command)
    hanging_store = _HangingIntentStore(cancellation_engine)
    counts_before = await preflight_harness.table_row_counts()

    commit_task = asyncio.create_task(
        hanging_store.commit_update(
            update_command,
            update_fingerprint,
            _receipt(update_salt),
            _diagnostic_context(),
        )
    )
    await asyncio.wait_for(hanging_store.hang_reached.wait(), timeout=HANG_REACHED_TIMEOUT_SECONDS)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(commit_task, timeout=CANCEL_TIMEOUT_SECONDS)

    # The cancelled transaction rolled back completely.
    assert await preflight_harness.table_row_counts() == counts_before
    assert await preflight_harness.rejection_audit_rows(workspace_id=workspace.workspace_id) == []
    status = await _await_pool_checked_in(cancellation_engine)
    assert "Checked out connections: 0" in status, status

    # The exact same event and key publish on the released locks and pool.
    retried = await asyncio.wait_for(
        hanging_store.commit_update(
            update_command,
            update_fingerprint,
            _receipt(update_salt),
            _diagnostic_context(),
        ),
        timeout=HANG_REACHED_TIMEOUT_SECONDS,
    )
    assert retried.outcome is PublicationOutcome.PUBLISHED
    assert retried.content_version == 2

    async with cancellation_engine.connect() as connection:
        pointer = (
            await connection.execute(
                sa.select(sources.c.current_version_id).where(
                    sources.c.source_id == first.source_id
                )
            )
        ).scalar_one()
    assert pointer == retried.source_version_id
    assert "Checked out connections: 0" in cancellation_engine.pool.status()
