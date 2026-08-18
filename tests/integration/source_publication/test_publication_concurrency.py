"""Deterministic concurrent publication outcomes against a disposable stack.

Every scenario from design section 9.2 runs concurrently against the real
database through one bounded engine: 100 exact concurrent replays of one
update commit exactly one canonical event and return equivalent results; two
different-key updates from one base yield one publish and one
``source_version_conflict``; two creates for one source yield one source and
one ``source_already_exists`` rejection; and distinct sources publishing
identical bytes share exactly one global content object. Every gather is
bounded by a finite task-group timeout.
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
from sqlalchemy.ext.asyncio import AsyncEngine

from personal_os.diagnostics.context import create_diagnostic_context
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
from personal_os.sources.fingerprint import compute_request_fingerprint
from personal_os.sources.results import PublicationOutcome
from postgresql_source_store.engine import create_source_store_engine, dispose_source_store_engine
from postgresql_source_store.tables import (
    content_objects,
    source_versions,
    sources,
    sync_events,
)

pytestmark = pytest.mark.local_stack

#: Finite bound for every concurrent gather: no unbounded wait exists here.
CONCURRENCY_TIMEOUT_SECONDS: float = 90.0
EXACT_REPLAY_COUNT = 100


def _diagnostic_context():
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


def _create_command(
    workspace, salt: str, *, idempotency_value: str, source_id: UUID | None = None
) -> CreateSourceVersion:
    return CreateSourceVersion(
        workspace_id=workspace.workspace_id,
        source_id=source_id if source_id is not None else uuid4(),
        event_id=uuid4(),
        idempotency_key=IdempotencyKey(idempotency_value),
        source_type=SourceType.MARKDOWN,
        title=SourceTitle("Concurrency note"),
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
async def concurrency_engine(source_publication_stack) -> Iterator[AsyncEngine]:
    engine = create_source_store_engine(
        source_publication_stack.settings, source_publication_stack.password
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


async def _seed_committed_source(harness, workspace, salt: str):
    command = _create_command(workspace, salt, idempotency_value=f"concurrency-seed-{uuid4()}")
    result = await harness.store.commit_create(
        command, compute_request_fingerprint(command), _receipt(salt), _diagnostic_context()
    )
    return command, result


async def _count_rows(engine: AsyncEngine, table, *conditions) -> int:
    async with engine.connect() as connection:
        statement = sa.select(sa.func.count()).select_from(table)
        for condition in conditions:
            statement = statement.where(condition)
        return int((await connection.execute(statement)).scalar_one())


# --- 100 exact concurrent replays --------------------------------------------------


@pytest.mark.asyncio
async def test_hundred_exact_concurrent_replays_commit_one_event(
    preflight_harness, concurrency_engine
) -> None:
    workspace = await preflight_harness.seed_workspace()
    create_salt = f"replay-hundred-{uuid4()}"
    _, first = await _seed_committed_source(preflight_harness, workspace, create_salt)
    update_salt = f"replay-hundred-next-{uuid4()}"
    command = _update_command(
        workspace,
        update_salt,
        source_id=first.source_id,
        base_version_id=first.source_version_id,
        idempotency_value="replay-hundred-1",
    )
    fingerprint = compute_request_fingerprint(command)
    receipt = _receipt(update_salt)
    counts_before = await preflight_harness.table_row_counts()

    async with asyncio.timeout(CONCURRENCY_TIMEOUT_SECONDS):
        outcomes = await asyncio.gather(
            *(
                _capture(
                    preflight_harness.store.commit_update(
                        command, fingerprint, receipt, _diagnostic_context()
                    )
                )
                for _ in range(EXACT_REPLAY_COUNT)
            )
        )

    assert not any(isinstance(outcome, BaseException) for outcome in outcomes)
    results = outcomes
    assert all(result == results[0] for result in results)
    assert results[0].outcome is PublicationOutcome.PUBLISHED
    assert results[0].content_version == 2

    assert {
        name: after - counts_before[name]
        for name, after in (await preflight_harness.table_row_counts()).items()
    } == {
        "users": 0,
        "workspaces": 0,
        "devices": 0,
        "content_objects": 1,
        "sources": 0,
        "source_versions": 1,
        "sync_events": 1,
        "projection_intents": 2,
        "audit_events": 1,
        "user_credentials": 0,
        "web_sessions": 0,
        "totp_credentials": 0,
        "totp_recovery_codes": 0,
        "device_token_families": 0,
        "device_tokens": 0,
        "device_authorization_grants": 0,
        "authentication_throttle_buckets": 0,
        "workspace_policy_state": 0,
        "policy_drafts": 0,
        "policy_draft_rules": 0,
        "source_policies": 0,
        "policy_rules": 0,
        "policy_previews": 0,
        "policy_preview_results": 0,
        "policy_evaluations": 0,
        "policy_signing_keys": 0,
        "policy_keysets": 0,
        "policy_keyset_signatures": 0,
        "policy_reconciliation_intents": 0,
        "small_file_upload_operations": 0,
    }
    assert (
        await _count_rows(
            concurrency_engine, sync_events, sync_events.c.event_id == command.event_id
        )
        == 1
    )
    assert await preflight_harness.rejection_audit_rows(workspace_id=workspace.workspace_id) == []


# --- two different-key updates from one base ---------------------------------------


@pytest.mark.asyncio
async def test_two_updates_from_one_base_yield_one_publish_and_one_conflict(
    preflight_harness, concurrency_engine
) -> None:
    workspace = await preflight_harness.seed_workspace()
    create_salt = f"two-updates-{uuid4()}"
    _, first = await _seed_committed_source(preflight_harness, workspace, create_salt)
    winner_salt = f"two-updates-winner-{uuid4()}"
    loser_salt = f"two-updates-loser-{uuid4()}"
    winner = _update_command(
        workspace,
        winner_salt,
        source_id=first.source_id,
        base_version_id=first.source_version_id,
        idempotency_value="two-updates-winner-1",
    )
    loser = _update_command(
        workspace,
        loser_salt,
        source_id=first.source_id,
        base_version_id=first.source_version_id,
        idempotency_value="two-updates-loser-1",
    )
    counts_before = await preflight_harness.table_row_counts()

    async with asyncio.timeout(CONCURRENCY_TIMEOUT_SECONDS):
        winner_outcome, loser_outcome = await asyncio.gather(
            _capture(
                preflight_harness.store.commit_update(
                    winner,
                    compute_request_fingerprint(winner),
                    _receipt(winner_salt),
                    _diagnostic_context(),
                )
            ),
            _capture(
                preflight_harness.store.commit_update(
                    loser,
                    compute_request_fingerprint(loser),
                    _receipt(loser_salt),
                    _diagnostic_context(),
                )
            ),
        )

    by_error = {
        outcome.error_code if isinstance(outcome, SourcePublicationError) else None: outcome
        for outcome in (winner_outcome, loser_outcome)
    }
    assert set(by_error) == {None, ErrorCode.SOURCE_VERSION_CONFLICT}
    published = by_error[None]
    conflicted = by_error[ErrorCode.SOURCE_VERSION_CONFLICT]
    assert published.outcome is PublicationOutcome.PUBLISHED
    assert published.content_version == 2
    assert conflicted.to_safe_dict()["safe_details"]["source_id"] == str(first.source_id)

    assert {
        name: after - counts_before[name]
        for name, after in (await preflight_harness.table_row_counts()).items()
    } == {
        "users": 0,
        "workspaces": 0,
        "devices": 0,
        "content_objects": 1,
        "sources": 0,
        "source_versions": 1,
        "sync_events": 1,
        "projection_intents": 2,
        # One in-transaction success audit plus the loser's standalone
        # rejection audit.
        "audit_events": 2,
        "user_credentials": 0,
        "web_sessions": 0,
        "totp_credentials": 0,
        "totp_recovery_codes": 0,
        "device_token_families": 0,
        "device_tokens": 0,
        "device_authorization_grants": 0,
        "authentication_throttle_buckets": 0,
        "workspace_policy_state": 0,
        "policy_drafts": 0,
        "policy_draft_rules": 0,
        "source_policies": 0,
        "policy_rules": 0,
        "policy_previews": 0,
        "policy_preview_results": 0,
        "policy_evaluations": 0,
        "policy_signing_keys": 0,
        "policy_keysets": 0,
        "policy_keyset_signatures": 0,
        "policy_reconciliation_intents": 0,
        "small_file_upload_operations": 0,
    }
    rejection_audits = await preflight_harness.rejection_audit_rows(
        workspace_id=workspace.workspace_id
    )
    assert len(rejection_audits) == 1
    assert rejection_audits[0].reason_code == "version_conflict"
    assert (
        await _count_rows(
            concurrency_engine, source_versions, source_versions.c.source_id == first.source_id
        )
        == 2
    )
    source_row_current = await _current_pointer(concurrency_engine, first.source_id)
    assert source_row_current == published.source_version_id


async def _current_pointer(engine: AsyncEngine, source_id: UUID) -> UUID:
    async with engine.connect() as connection:
        result = await connection.execute(
            sa.select(sources.c.current_version_id).where(sources.c.source_id == source_id)
        )
        return result.scalar_one()


# --- two concurrent creates for one source ------------------------------------------


@pytest.mark.asyncio
async def test_two_concurrent_creates_for_one_source_yield_one_source_and_one_rejection(
    preflight_harness, concurrency_engine
) -> None:
    workspace = await preflight_harness.seed_workspace()
    source_id = uuid4()
    winner_salt = f"create-race-winner-{uuid4()}"
    loser_salt = f"create-race-loser-{uuid4()}"
    winner = _create_command(
        workspace,
        winner_salt,
        idempotency_value="create-race-winner-1",
        source_id=source_id,
    )
    loser = _create_command(
        workspace,
        loser_salt,
        idempotency_value="create-race-loser-1",
        source_id=source_id,
    )
    counts_before = await preflight_harness.table_row_counts()

    async with asyncio.timeout(CONCURRENCY_TIMEOUT_SECONDS):
        outcomes = await asyncio.gather(
            _capture(
                preflight_harness.store.commit_create(
                    winner,
                    compute_request_fingerprint(winner),
                    _receipt(winner_salt),
                    _diagnostic_context(),
                )
            ),
            _capture(
                preflight_harness.store.commit_create(
                    loser,
                    compute_request_fingerprint(loser),
                    _receipt(loser_salt),
                    _diagnostic_context(),
                )
            ),
        )

    by_error = {
        outcome.error_code if isinstance(outcome, SourcePublicationError) else None: outcome
        for outcome in outcomes
    }
    assert set(by_error) == {None, ErrorCode.SOURCE_ALREADY_EXISTS}
    assert by_error[None].outcome is PublicationOutcome.PUBLISHED
    assert by_error[ErrorCode.SOURCE_ALREADY_EXISTS].to_safe_dict()["safe_details"] == {
        "source_id": str(source_id)
    }
    assert {
        name: after - counts_before[name]
        for name, after in (await preflight_harness.table_row_counts()).items()
    } == {
        "users": 0,
        "workspaces": 0,
        "devices": 0,
        # The losing create rolls back entirely, so only the winner's bytes
        # leave one content object.
        "content_objects": 1,
        "sources": 1,
        "source_versions": 1,
        "sync_events": 1,
        "projection_intents": 2,
        # One in-transaction success audit plus the loser's standalone
        # rejection audit.
        "audit_events": 2,
        "user_credentials": 0,
        "web_sessions": 0,
        "totp_credentials": 0,
        "totp_recovery_codes": 0,
        "device_token_families": 0,
        "device_tokens": 0,
        "device_authorization_grants": 0,
        "authentication_throttle_buckets": 0,
        "workspace_policy_state": 0,
        "policy_drafts": 0,
        "policy_draft_rules": 0,
        "source_policies": 0,
        "policy_rules": 0,
        "policy_previews": 0,
        "policy_preview_results": 0,
        "policy_evaluations": 0,
        "policy_signing_keys": 0,
        "policy_keysets": 0,
        "policy_keyset_signatures": 0,
        "policy_reconciliation_intents": 0,
        "small_file_upload_operations": 0,
    }
    rejection_audits = await preflight_harness.rejection_audit_rows(
        workspace_id=workspace.workspace_id
    )
    assert len(rejection_audits) == 1
    assert rejection_audits[0].reason_code == "source_already_exists"
    assert await _count_rows(concurrency_engine, sources, sources.c.source_id == source_id) == 1


# --- distinct sources with identical bytes ------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_distinct_sources_with_identical_bytes_share_one_content_object(
    preflight_harness, concurrency_engine
) -> None:
    workspace = await preflight_harness.seed_workspace()
    shared_salt = f"shared-concurrency-{uuid4()}"
    commands = [
        _create_command(workspace, shared_salt, idempotency_value=f"shared-concurrency-{index}")
        for index in range(6)
    ]

    async with asyncio.timeout(CONCURRENCY_TIMEOUT_SECONDS):
        outcomes = await asyncio.gather(
            *(
                _capture(
                    preflight_harness.store.commit_create(
                        command,
                        compute_request_fingerprint(command),
                        _receipt(shared_salt),
                        _diagnostic_context(),
                    )
                )
                for command in commands
            )
        )

    assert not any(isinstance(outcome, BaseException) for outcome in outcomes)
    for result in outcomes:
        assert result.outcome is PublicationOutcome.PUBLISHED
    content_hash = _receipt(shared_salt).content_digest.hexadecimal
    assert (
        await _count_rows(
            concurrency_engine, content_objects, content_objects.c.content_hash == content_hash
        )
        == 1
    )
