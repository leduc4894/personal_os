"""Ambiguous-commit recovery through a fresh bounded connection (spec 9.4).

A simulated lost commit acknowledgement — the update transaction committed on
the server, then the caller's connection reports a connection-class failure —
must resolve through the same key/event/fingerprint lookup on a new
connection and return the committed replay, never duplicating the event. When
the outcome lookup itself is unavailable, the store returns the retryable
``source_commit_outcome_unknown``, never claiming a rollback, and a later
replay proves the committed graph survived.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from personal_os.diagnostics.context import DiagnosticContext, create_diagnostic_context
from personal_os.error_contracts.codes import ErrorCode
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
from personal_os.sources.errors import SourcePublicationError
from personal_os.sources.fingerprint import (
    RequestFingerprint,
    compute_request_fingerprint,
)
from postgresql_source_store.engine import create_source_store_engine, dispose_source_store_engine
from postgresql_source_store.publication_store import (
    PostgresqlSourcePublicationStore,
    SourceUpdateIdentities,
)
from postgresql_source_store.tables import sync_events

pytestmark = pytest.mark.local_stack


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


def _create_command(workspace, salt: str, *, idempotency_value: str) -> CreateSourceVersion:
    receipt = _receipt(salt)
    return CreateSourceVersion(
        workspace_id=workspace.workspace_id,
        source_id=uuid4(),
        event_id=uuid4(),
        idempotency_key=IdempotencyKey(idempotency_value),
        source_type=SourceType.MARKDOWN,
        title=SourceTitle("Ambiguous commit note"),
        actor=SourceActor(ActorKind.USER, workspace.owner_user_id),
        expected_object=ExpectedObject(
            content_digest=receipt.content_digest,
            size_bytes=receipt.size_bytes,
            media_type=receipt.media_type,
        ),
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
    receipt = _receipt(salt)
    return UpdateSourceVersion(
        workspace_id=workspace.workspace_id,
        source_id=source_id,
        event_id=uuid4(),
        idempotency_key=IdempotencyKey(idempotency_value),
        base_version_id=base_version_id,
        actor=SourceActor(ActorKind.USER, workspace.owner_user_id),
        expected_object=ExpectedObject(
            content_digest=receipt.content_digest,
            size_bytes=receipt.size_bytes,
            media_type=receipt.media_type,
        ),
        client_timestamp=None,
    )


@pytest_asyncio.fixture
async def ambiguity_engine(source_publication_stack) -> Iterator[AsyncEngine]:
    engine = create_source_store_engine(
        source_publication_stack.settings, source_publication_stack.password
    )
    try:
        yield engine
    finally:
        await dispose_source_store_engine(engine)


class _LostAcknowledgementStore(PostgresqlSourcePublicationStore):
    """Store whose first update commit loses its acknowledgement.

    The super call returns only after the ``async with`` transaction block has
    COMMITTED, so the raised connection-class failure arrives after the
    canonical graph is durable — exactly the "commit succeeded, response lost"
    crash row. The store must resolve the outcome on a new connection.
    """

    def __init__(self, engine: AsyncEngine) -> None:
        super().__init__(engine)
        self._acknowledgement_lost_once = False

    async def _commit_update_once(
        self,
        command: UpdateSourceVersion,
        request_fingerprint: RequestFingerprint,
        receipt: VerifiedObjectReceipt,
        diagnostic_context: DiagnosticContext,
        identities: SourceUpdateIdentities,
    ) -> Any:
        result = await super()._commit_update_once(
            command, request_fingerprint, receipt, diagnostic_context, identities
        )
        if not self._acknowledgement_lost_once:
            self._acknowledgement_lost_once = True
            raise psycopg.InterfaceError("simulated lost commit acknowledgement")
        return result


class _UnavailableRecoveryStore(_LostAcknowledgementStore):
    """Store whose fresh-connection outcome lookup cannot reach PostgreSQL."""

    async def _resolve_committed_once(
        self,
        command: UpdateSourceVersion,
        request_fingerprint: RequestFingerprint,
        diagnostic_context: DiagnosticContext,
    ) -> Any:
        raise psycopg.OperationalError("simulated outcome-lookup outage")


async def _seed_committed_source(harness, workspace, salt: str):
    command = _create_command(workspace, salt, idempotency_value=f"ambiguity-seed-{uuid4()}")
    result = await harness.store.commit_create(
        command, compute_request_fingerprint(command), _receipt(salt), _diagnostic_context()
    )
    return command, result


async def _count_events(engine: AsyncEngine, event_id: UUID) -> int:
    async with engine.connect() as connection:
        result = await connection.execute(
            sa.select(sa.func.count())
            .select_from(sync_events)
            .where(sync_events.c.event_id == event_id)
        )
        return int(result.scalar_one())


@pytest.mark.asyncio
async def test_lost_commit_acknowledgement_resolves_committed_replay_on_new_connection(
    preflight_harness, ambiguity_engine
) -> None:
    workspace = await preflight_harness.seed_workspace()
    create_salt = f"lost-ack-{uuid4()}"
    _, first = await _seed_committed_source(preflight_harness, workspace, create_salt)
    update_salt = f"lost-ack-next-{uuid4()}"
    command = _update_command(
        workspace,
        update_salt,
        source_id=first.source_id,
        base_version_id=first.source_version_id,
        idempotency_value="lost-ack-1",
    )
    fingerprint = compute_request_fingerprint(command)
    receipt = _receipt(update_salt)
    store = _LostAcknowledgementStore(ambiguity_engine)

    result = await store.commit_update(command, fingerprint, receipt, _diagnostic_context())

    assert store._acknowledgement_lost_once is True
    assert result.outcome is not None
    assert result.source_id == command.source_id
    assert result.content_version == 2
    assert result.event_id == command.event_id

    replay = await preflight_harness.store.resolve_committed(
        command, fingerprint, _diagnostic_context()
    )
    assert replay == result
    assert await _count_events(ambiguity_engine, command.event_id) == 1
    assert (
        await preflight_harness.rejection_audit_rows(workspace_id=workspace.workspace_id) == []
    )


@pytest.mark.asyncio
async def test_unavailable_outcome_lookup_returns_retryable_unknown_never_rollback(
    preflight_harness, ambiguity_engine
) -> None:
    workspace = await preflight_harness.seed_workspace()
    create_salt = f"unavailable-lookup-{uuid4()}"
    _, first = await _seed_committed_source(preflight_harness, workspace, create_salt)
    update_salt = f"unavailable-lookup-next-{uuid4()}"
    command = _update_command(
        workspace,
        update_salt,
        source_id=first.source_id,
        base_version_id=first.source_version_id,
        idempotency_value="unavailable-lookup-1",
    )
    fingerprint = compute_request_fingerprint(command)
    store = _UnavailableRecoveryStore(ambiguity_engine)

    with pytest.raises(SourcePublicationError) as captured:
        await store.commit_update(
            command, fingerprint, _receipt(update_salt), _diagnostic_context()
        )

    error = captured.value
    assert error.error_code is ErrorCode.SOURCE_COMMIT_OUTCOME_UNKNOWN
    assert error.is_retryable is True
    assert error.to_safe_dict()["safe_details"] == {"source_id": str(command.source_id)}
    rendered = f"{error} {error!r} {error.to_safe_dict()!r}"
    assert "roll" not in rendered.lower()
    # The commit was durable the whole time: an exact replay returns it, which
    # is exactly the caller's sanctioned retry for the retryable unknown code.
    replay = await preflight_harness.store.resolve_committed(
        command, fingerprint, _diagnostic_context()
    )
    assert replay is not None
    assert replay.content_version == 2
    assert await _count_events(ambiguity_engine, command.event_id) == 1
