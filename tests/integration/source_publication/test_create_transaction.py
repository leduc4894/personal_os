"""Atomic create transaction against a disposable PostgreSQL 18.4.

Every case runs against the real migrated baseline through the real async
engine: the create commits exactly one content object, source, version 1 with
null parent, active pointer, create event with null base, two upsert intents
and one ``source.version_published`` audit row; identical bytes across
different sources reuse one global content object with the first
``verified_at`` preserved; an existing hash with diverging receipt metadata
rolls back with ``source_content_object_conflict`` and one standalone
rejection audit; the locked prefix returns an exact replay without mutation;
and an existing global source rejects with ``source_already_exists`` without
disclosing another tenant.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.source_publication.conftest import expected_row_deltas

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
from personal_os.source_locators import NormalizedLocator
from personal_os.sources.actors import ActorKind, SourceActor
from personal_os.sources.commands import (
    CreateSourceVersion,
    IdempotencyKey,
    SourceTitle,
    SourceType,
)
from personal_os.sources.errors import SourcePublicationError
from personal_os.sources.fingerprint import compute_request_fingerprint, compute_safe_diff_hash
from personal_os.sources.results import PublicationOutcome
from postgresql_source_store.engine import create_source_store_engine, dispose_source_store_engine
from postgresql_source_store.tables import (
    audit_events,
    content_objects,
    projection_intents,
    source_locators,
    source_versions,
    sources,
    sync_events,
)

pytestmark = pytest.mark.local_stack

_SOURCE_STATE_ACTIVE = "active"
_AUDIT_ACTION_PUBLISHED = "source.version_published"
_AUDIT_RESULT_SUCCEEDED = "succeeded"


def _diagnostic_context():
    return create_diagnostic_context().context


def _receipt(
    salt: str,
    *,
    size_bytes: int | None = None,
    media_type: str = "text/markdown",
) -> VerifiedObjectReceipt:
    digest = ContentDigest.parse(hashlib.sha256(salt.encode("utf-8")).hexdigest())
    return VerifiedObjectReceipt(
        content_digest=digest,
        object_key=derive_canonical_object_key(digest),
        size_bytes=len(salt) if size_bytes is None else size_bytes,
        media_type=CanonicalMediaType.parse(media_type),
        verified_at=datetime.now(UTC),
        verification_method=VerificationMethod.UPLOADED_FULL_READ,
    )


def _create_command(
    workspace,
    salt: str,
    *,
    actor_kind: str = "user",
    source_id: UUID | None = None,
    event_id: UUID | None = None,
    idempotency_value: str,
    initial_locator: NormalizedLocator | None = None,
) -> tuple[CreateSourceVersion, VerifiedObjectReceipt]:
    receipt = _receipt(salt)
    actor = (
        SourceActor(ActorKind.USER, workspace.owner_user_id)
        if actor_kind == "user"
        else SourceActor(ActorKind.DEVICE, workspace.device_id)
    )
    command = CreateSourceVersion(
        workspace_id=workspace.workspace_id,
        source_id=source_id if source_id is not None else uuid4(),
        event_id=event_id if event_id is not None else uuid4(),
        idempotency_key=IdempotencyKey(idempotency_value),
        source_type=SourceType.MARKDOWN,
        title=SourceTitle("Create transaction note"),
        actor=actor,
        expected_object=ExpectedObject(
            content_digest=receipt.content_digest,
            size_bytes=receipt.size_bytes,
            media_type=receipt.media_type,
        ),
        client_timestamp=None,
        initial_locator=initial_locator,
    )
    return command, receipt


@pytest_asyncio.fixture
async def inspection_engine(source_publication_stack) -> Iterator[AsyncEngine]:
    engine = create_source_store_engine(
        source_publication_stack.settings, source_publication_stack.password
    )
    try:
        yield engine
    finally:
        await dispose_source_store_engine(engine)


async def _fetch_source_row(engine: AsyncEngine, source_id: UUID):
    async with engine.connect() as connection:
        result = await connection.execute(
            sa.select(
                sources.c.workspace_id,
                sources.c.source_type,
                sources.c.title,
                sources.c.sync_state,
                sources.c.current_version_id,
                sources.c.deleted_at,
            ).where(sources.c.source_id == source_id)
        )
        return result.one_or_none()


async def _fetch_version_row(engine: AsyncEngine, source_version_id: UUID):
    async with engine.connect() as connection:
        result = await connection.execute(
            sa.select(
                source_versions.c.content_object_id,
                source_versions.c.content_version,
                source_versions.c.parent_version_id,
                source_versions.c.author_kind,
                source_versions.c.author_id,
                source_versions.c.client_timestamp,
                source_versions.c.committed_at,
            ).where(source_versions.c.source_version_id == source_version_id)
        )
        return result.one_or_none()


async def _fetch_event_row(engine: AsyncEngine, event_id: UUID):
    async with engine.connect() as connection:
        result = await connection.execute(
            sa.select(
                sync_events.c.event_sequence,
                sync_events.c.event_type,
                sync_events.c.base_version_id,
                sync_events.c.committed_version_id,
                sync_events.c.idempotency_key,
                sync_events.c.request_fingerprint,
                sync_events.c.device_id,
                sync_events.c.committed_at,
            ).where(sync_events.c.event_id == event_id)
        )
        return result.one_or_none()


async def _fetch_intent_rows(engine: AsyncEngine, event_id: UUID) -> list:
    async with engine.connect() as connection:
        result = await connection.execute(
            sa.select(
                projection_intents.c.projection_kind,
                projection_intents.c.operation,
                projection_intents.c.status,
                projection_intents.c.attempt_count,
                projection_intents.c.source_version_id,
                projection_intents.c.lease_token,
                projection_intents.c.dispatched_at,
                projection_intents.c.last_error_code,
            )
            .where(projection_intents.c.event_id == event_id)
            .order_by(projection_intents.c.projection_kind)
        )
        return list(result.all())


async def _fetch_success_audit_rows(engine: AsyncEngine, workspace_id: UUID) -> list:
    async with engine.connect() as connection:
        result = await connection.execute(
            sa.select(
                audit_events.c.actor_kind,
                audit_events.c.actor_id,
                audit_events.c.action,
                audit_events.c.target_kind,
                audit_events.c.target_id,
                audit_events.c.request_id,
                audit_events.c.client_request_id,
                audit_events.c.trace_id,
                audit_events.c.result,
                audit_events.c.reason_code,
                audit_events.c.safe_diff_hash,
            ).where(
                audit_events.c.workspace_id == workspace_id,
                audit_events.c.action == _AUDIT_ACTION_PUBLISHED,
            )
        )
        return list(result.all())


async def _fetch_content_object_rows_by_hash(engine: AsyncEngine, content_hash: str) -> list:
    async with engine.connect() as connection:
        result = await connection.execute(
            sa.select(
                content_objects.c.content_object_id,
                content_objects.c.object_key,
                content_objects.c.byte_size,
                content_objects.c.media_type,
                content_objects.c.verified_at,
                content_objects.c.created_at,
            ).where(content_objects.c.content_hash == content_hash)
        )
        return list(result.all())


def _assert_only_one_rejection_audit_was_added(counts_before: dict, counts_after: dict) -> None:
    """A business rejection changes exactly one table: one rejection audit."""
    assert {
        table_name: counts_after[table_name] - counts_before[table_name]
        for table_name in counts_after
    } == expected_row_deltas(audit_events=1)


# --- exact canonical create graph -------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("actor_kind", ["user", "device"])
async def test_create_commits_exact_canonical_graph(
    preflight_harness, inspection_engine, actor_kind: str
) -> None:
    workspace = await preflight_harness.seed_workspace()
    command, receipt = _create_command(
        workspace,
        f"create-graph-{actor_kind}-{uuid4()}",
        actor_kind=actor_kind,
        idempotency_value=f"create-graph-{actor_kind}-1",
    )
    fingerprint = compute_request_fingerprint(command)
    diagnostic_context = _diagnostic_context()
    counts_before = await preflight_harness.table_row_counts()

    result = await preflight_harness.store.commit_create(
        command, fingerprint, receipt, diagnostic_context
    )

    assert result.source_id == command.source_id
    assert result.source_version_id != command.source_id
    assert result.content_version == 1
    assert result.event_id == command.event_id
    assert result.event_sequence >= 1
    assert result.outcome is PublicationOutcome.PUBLISHED
    assert result.content_digest.hexadecimal == receipt.content_digest.hexadecimal
    assert result.committed_at.tzinfo is not None

    counts_after = await preflight_harness.table_row_counts()
    assert {
        table_name: counts_after[table_name] - counts_before[table_name]
        for table_name in counts_after
    } == expected_row_deltas(
        content_objects=1,
        sources=1,
        source_versions=1,
        sync_events=1,
        projection_intents=2,
        audit_events=1,
    )

    source_row = await _fetch_source_row(inspection_engine, command.source_id)
    assert source_row.workspace_id == workspace.workspace_id
    assert source_row.source_type == "markdown"
    assert source_row.title == "Create transaction note"
    assert source_row.sync_state == _SOURCE_STATE_ACTIVE
    assert source_row.current_version_id == result.source_version_id
    assert source_row.deleted_at is None

    version_row = await _fetch_version_row(inspection_engine, result.source_version_id)
    assert version_row.content_version == 1
    assert version_row.parent_version_id is None
    assert version_row.author_kind == actor_kind
    assert version_row.author_id == (
        workspace.owner_user_id if actor_kind == "user" else workspace.device_id
    )
    assert version_row.client_timestamp is None
    assert version_row.committed_at == result.committed_at

    event_row = await _fetch_event_row(inspection_engine, command.event_id)
    assert event_row.event_sequence == result.event_sequence
    assert event_row.event_type == "create"
    assert event_row.base_version_id is None
    assert event_row.committed_version_id == result.source_version_id
    assert event_row.idempotency_key == command.idempotency_key.value
    assert event_row.request_fingerprint == fingerprint.hexadecimal
    assert event_row.device_id == (None if actor_kind == "user" else workspace.device_id)
    assert event_row.committed_at == result.committed_at

    intent_rows = await _fetch_intent_rows(inspection_engine, command.event_id)
    assert [row.projection_kind for row in intent_rows] == ["neo4j", "qdrant"]
    for intent_row in intent_rows:
        assert intent_row.operation == "upsert"
        assert intent_row.status == "pending"
        assert intent_row.attempt_count == 0
        assert intent_row.source_version_id == result.source_version_id
        assert intent_row.lease_token is None
        assert intent_row.dispatched_at is None
        assert intent_row.last_error_code is None

    success_audits = await _fetch_success_audit_rows(inspection_engine, workspace.workspace_id)
    assert len(success_audits) == 1
    audit_row = success_audits[0]
    assert audit_row.action == _AUDIT_ACTION_PUBLISHED
    assert audit_row.result == _AUDIT_RESULT_SUCCEEDED
    assert audit_row.reason_code is None
    assert audit_row.target_kind == "source"
    assert audit_row.target_id == command.source_id
    assert audit_row.actor_kind == actor_kind
    assert audit_row.actor_id == (
        workspace.owner_user_id if actor_kind == "user" else workspace.device_id
    )
    assert audit_row.request_id == diagnostic_context.request_id
    assert audit_row.client_request_id == diagnostic_context.client_request_id
    assert audit_row.trace_id == diagnostic_context.trace.trace_id.value
    assert (
        audit_row.safe_diff_hash
        == compute_safe_diff_hash(command.source_id, None, None, receipt.content_digest).hexadecimal
    )

    assert await preflight_harness.rejection_audit_rows(workspace_id=workspace.workspace_id) == []


# --- content-object deduplication --------------------------------------------------


@pytest.mark.asyncio
async def test_application_receipt_ahead_of_database_clock_commits_deterministically(
    preflight_harness, inspection_engine
) -> None:
    """The receipt instant owns both timestamps on the first object insert."""

    workspace = await preflight_harness.seed_workspace()
    command, receipt = _create_command(
        workspace,
        f"ahead-of-database-clock-{uuid4()}",
        idempotency_value="ahead-of-database-clock-1",
    )
    future_receipt = replace(receipt, verified_at=datetime.now(UTC) + timedelta(seconds=30))

    result = await preflight_harness.store.commit_create(
        command,
        compute_request_fingerprint(command),
        future_receipt,
        _diagnostic_context(),
    )

    assert result.outcome is PublicationOutcome.PUBLISHED
    object_rows = await _fetch_content_object_rows_by_hash(
        inspection_engine, future_receipt.content_digest.hexadecimal
    )
    assert len(object_rows) == 1
    assert object_rows[0].verified_at == future_receipt.verified_at
    assert object_rows[0].created_at == future_receipt.verified_at


@pytest.mark.asyncio
async def test_identical_bytes_across_sources_reuse_one_content_object(
    preflight_harness, inspection_engine
) -> None:
    workspace = await preflight_harness.seed_workspace()
    shared_salt = f"shared-create-bytes-{uuid4()}"
    first_command, first_receipt = _create_command(
        workspace, shared_salt, idempotency_value="shared-create-first-1"
    )
    second_command, second_receipt = _create_command(
        workspace, shared_salt, idempotency_value="shared-create-second-1"
    )

    first_result = await preflight_harness.store.commit_create(
        first_command,
        compute_request_fingerprint(first_command),
        first_receipt,
        _diagnostic_context(),
    )
    second_result = await preflight_harness.store.commit_create(
        second_command,
        compute_request_fingerprint(second_command),
        second_receipt,
        _diagnostic_context(),
    )

    object_rows = await _fetch_content_object_rows_by_hash(
        inspection_engine, first_receipt.content_digest.hexadecimal
    )
    assert len(object_rows) == 1
    object_row = object_rows[0]
    assert object_row.object_key == first_receipt.object_key.value
    assert object_row.byte_size == first_receipt.size_bytes
    assert object_row.media_type == first_receipt.media_type.value
    # The first verified_at and content-object identity survive deduplication.
    assert object_row.verified_at == first_receipt.verified_at

    for result in (first_result, second_result):
        version_row = await _fetch_version_row(inspection_engine, result.source_version_id)
        assert version_row.content_object_id == object_row.content_object_id


@pytest.mark.asyncio
@pytest.mark.parametrize("divergent_dimension", ["byte_size", "media_type"])
async def test_existing_hash_with_diverging_receipt_metadata_rolls_back(
    preflight_harness, inspection_engine, divergent_dimension: str
) -> None:
    workspace = await preflight_harness.seed_workspace()
    salt = f"conflicting-metadata-{uuid4()}"
    first_command, first_receipt = _create_command(
        workspace, salt, idempotency_value="conflict-anchor-1"
    )
    await preflight_harness.store.commit_create(
        first_command,
        compute_request_fingerprint(first_command),
        first_receipt,
        _diagnostic_context(),
    )
    counts_before = await preflight_harness.table_row_counts()

    diverging_kwargs = (
        {"size_bytes": first_receipt.size_bytes + 1}
        if divergent_dimension == "byte_size"
        else {"media_type": "text/plain"}
    )
    second_command, _ = _create_command(workspace, salt, idempotency_value="conflict-contender-1")
    second_receipt = _receipt(salt, **diverging_kwargs)

    with pytest.raises(SourcePublicationError) as captured:
        await preflight_harness.store.commit_create(
            second_command,
            compute_request_fingerprint(second_command),
            second_receipt,
            _diagnostic_context(),
        )

    assert captured.value.error_code is ErrorCode.SOURCE_CONTENT_OBJECT_CONFLICT
    assert dict(captured.value.safe_details) == {"source_id": second_command.source_id}
    _assert_only_one_rejection_audit_was_added(
        counts_before, await preflight_harness.table_row_counts()
    )
    assert await _fetch_source_row(inspection_engine, second_command.source_id) is None

    rejection_audits = await preflight_harness.rejection_audit_rows(
        workspace_id=workspace.workspace_id
    )
    assert len(rejection_audits) == 1
    assert rejection_audits[0].reason_code == "content_object_metadata_conflict"
    assert rejection_audits[0].target_id == second_command.source_id


# --- locked-prefix replay recheck ---------------------------------------------------


@pytest.mark.asyncio
async def test_commit_create_replays_exact_committed_request_without_mutation(
    preflight_harness,
) -> None:
    workspace = await preflight_harness.seed_workspace()
    command, receipt = _create_command(
        workspace, f"replay-recheck-{uuid4()}", idempotency_value="replay-recheck-1"
    )
    fingerprint = compute_request_fingerprint(command)
    committed = await preflight_harness.store.commit_create(
        command, fingerprint, receipt, _diagnostic_context()
    )
    counts_after_commit = await preflight_harness.table_row_counts()

    replayed = await preflight_harness.store.commit_create(
        command, fingerprint, receipt, _diagnostic_context()
    )

    assert replayed == committed
    assert await preflight_harness.table_row_counts() == counts_after_commit
    assert await preflight_harness.rejection_audit_rows(workspace_id=workspace.workspace_id) == []


# --- existing global source ----------------------------------------------------------


@pytest.mark.asyncio
async def test_existing_global_source_rejects_source_already_exists(
    preflight_harness, inspection_engine
) -> None:
    workspace = await preflight_harness.seed_workspace()
    first_command, first_receipt = _create_command(
        workspace, f"existing-source-{uuid4()}", idempotency_value="existing-source-1"
    )
    await preflight_harness.store.commit_create(
        first_command,
        compute_request_fingerprint(first_command),
        first_receipt,
        _diagnostic_context(),
    )
    counts_before = await preflight_harness.table_row_counts()

    contender, contender_receipt = _create_command(
        workspace,
        f"existing-source-contender-{uuid4()}",
        source_id=first_command.source_id,
        idempotency_value="existing-source-contender-1",
    )

    with pytest.raises(SourcePublicationError) as captured:
        await preflight_harness.store.commit_create(
            contender,
            compute_request_fingerprint(contender),
            contender_receipt,
            _diagnostic_context(),
        )

    assert captured.value.error_code is ErrorCode.SOURCE_ALREADY_EXISTS
    assert dict(captured.value.safe_details) == {"source_id": contender.source_id}
    _assert_only_one_rejection_audit_was_added(
        counts_before, await preflight_harness.table_row_counts()
    )
    rejection_audits = await preflight_harness.rejection_audit_rows(
        workspace_id=workspace.workspace_id
    )
    assert len(rejection_audits) == 1
    assert rejection_audits[0].reason_code == "source_already_exists"
    assert rejection_audits[0].target_id == contender.source_id


@pytest.mark.asyncio
async def test_cross_workspace_source_id_reuse_rejects_without_tenant_disclosure(
    preflight_harness,
) -> None:
    owning_workspace = await preflight_harness.seed_workspace()
    requesting_workspace = await preflight_harness.seed_workspace()
    first_command, first_receipt = _create_command(
        owning_workspace, f"cross-tenant-{uuid4()}", idempotency_value="cross-tenant-owning-1"
    )
    await preflight_harness.store.commit_create(
        first_command,
        compute_request_fingerprint(first_command),
        first_receipt,
        _diagnostic_context(),
    )
    counts_before = await preflight_harness.table_row_counts()

    contender, contender_receipt = _create_command(
        requesting_workspace,
        f"cross-tenant-contender-{uuid4()}",
        source_id=first_command.source_id,
        idempotency_value="cross-tenant-contender-1",
    )

    with pytest.raises(SourcePublicationError) as captured:
        await preflight_harness.store.commit_create(
            contender,
            compute_request_fingerprint(contender),
            contender_receipt,
            _diagnostic_context(),
        )

    assert captured.value.error_code is ErrorCode.SOURCE_ALREADY_EXISTS
    assert dict(captured.value.safe_details) == {"source_id": contender.source_id}
    rendered = f"{captured.value} {captured.value!r} {captured.value.to_safe_dict()!r}"
    assert str(owning_workspace.workspace_id) not in rendered
    assert str(owning_workspace.owner_user_id) not in rendered
    assert str(first_command.event_id) not in rendered
    _assert_only_one_rejection_audit_was_added(
        counts_before, await preflight_harness.table_row_counts()
    )
    assert (
        await preflight_harness.rejection_audit_rows(workspace_id=owning_workspace.workspace_id)
        == []
    )
    requesting_audits = await preflight_harness.rejection_audit_rows(
        workspace_id=requesting_workspace.workspace_id
    )
    assert len(requesting_audits) == 1
    assert requesting_audits[0].reason_code == "source_already_exists"


# --- initial locator binding (task 3) ----------------------------------------------


async def _fetch_locator_row(engine: AsyncEngine, source_id: UUID):
    async with engine.connect() as connection:
        result = await connection.execute(
            sa.select(
                source_locators.c.source_locator_id,
                source_locators.c.workspace_id,
                source_locators.c.source_id,
                source_locators.c.normalized_locator,
                source_locators.c.display_locator,
                source_locators.c.opened_event_id,
                source_locators.c.opened_sequence,
                source_locators.c.closed_event_id,
                source_locators.c.closed_sequence,
                source_locators.c.opened_at,
                source_locators.c.closed_at,
            ).where(source_locators.c.source_id == source_id)
        )
        return result.one_or_none()


async def _fetch_locator_count_by_workspace(engine: AsyncEngine, workspace_id: UUID) -> int:
    async with engine.connect() as connection:
        result = await connection.execute(
            sa.select(sa.func.count())
            .select_from(source_locators)
            .where(source_locators.c.workspace_id == workspace_id)
        )
        return int(result.scalar_one())


@pytest.mark.asyncio
async def test_create_with_initial_locator_inserts_locator_row_with_opening_event(
    preflight_harness, inspection_engine
) -> None:
    """The initial source_locators row is created in the same commit as the create."""

    workspace = await preflight_harness.seed_workspace()
    initial_locator = NormalizedLocator("notes/daily/today.md")
    command, receipt = _create_command(
        workspace,
        f"create-with-locator-{uuid4()}",
        idempotency_value=f"create-with-locator-{uuid4()}-1",
        initial_locator=initial_locator,
    )
    counts_before = await preflight_harness.table_row_counts()

    result = await preflight_harness.store.commit_create(
        command,
        compute_request_fingerprint(command),
        receipt,
        _diagnostic_context(),
    )

    counts_after = await preflight_harness.table_row_counts()
    # The create adds one new row to every canonical table plus one new row
    # to source_locators for the bound initial locator.
    deltas = {
        table_name: counts_after[table_name] - counts_before[table_name]
        for table_name in counts_after
    }
    assert deltas["source_locators"] == 1
    assert deltas["content_objects"] == 1
    assert deltas["sources"] == 1
    assert deltas["source_versions"] == 1
    assert deltas["sync_events"] == 1
    assert deltas["projection_intents"] == 2
    assert deltas["audit_events"] == 1

    locator_row = await _fetch_locator_row(inspection_engine, command.source_id)
    assert locator_row is not None
    assert locator_row.workspace_id == workspace.workspace_id
    assert locator_row.source_id == command.source_id
    assert locator_row.normalized_locator == initial_locator.value
    assert locator_row.display_locator == initial_locator.value
    assert locator_row.opened_event_id == command.event_id
    assert locator_row.opened_sequence == result.event_sequence
    assert locator_row.opened_sequence >= 1
    assert locator_row.closed_event_id is None
    assert locator_row.closed_sequence is None
    assert locator_row.closed_at is None
    assert locator_row.opened_at is not None


@pytest.mark.asyncio
async def test_create_without_initial_locator_inserts_no_locator_row(
    preflight_harness, inspection_engine
) -> None:
    """A create without initial_locator leaves the source_locators table untouched."""

    workspace = await preflight_harness.seed_workspace()
    command, receipt = _create_command(
        workspace,
        f"create-without-locator-{uuid4()}",
        idempotency_value=f"create-without-locator-{uuid4()}-1",
    )
    counts_before = await preflight_harness.table_row_counts()

    await preflight_harness.store.commit_create(
        command,
        compute_request_fingerprint(command),
        receipt,
        _diagnostic_context(),
    )

    counts_after = await preflight_harness.table_row_counts()
    assert counts_after["source_locators"] == counts_before["source_locators"]
    locator_row = await _fetch_locator_row(inspection_engine, command.source_id)
    assert locator_row is None


@pytest.mark.asyncio
async def test_rollback_after_locator_insert_leaves_no_partial_locator_graph(
    preflight_harness, inspection_engine
) -> None:
    """A duplicate active locator must roll back every canonical write.

    The unique active locator constraint forces a rejection after the
    initial source_locators row was already inserted. The transaction must
    roll back fully: no source, version, locator, event, intent or audit row
    may survive.
    """

    workspace = await preflight_harness.seed_workspace()
    initial_locator = NormalizedLocator(f"notes/dup-{uuid4()}/shared.md")

    # First create commits successfully: source, version, event, intents, audit
    # and the bound locator all land.
    first_command, first_receipt = _create_command(
        workspace,
        f"locator-rollback-anchor-{uuid4()}",
        idempotency_value=f"locator-rollback-anchor-{uuid4()}-1",
        initial_locator=initial_locator,
    )
    await preflight_harness.store.commit_create(
        first_command,
        compute_request_fingerprint(first_command),
        first_receipt,
        _diagnostic_context(),
    )
    counts_after_first = await preflight_harness.table_row_counts()
    assert await _fetch_locator_count_by_workspace(inspection_engine, workspace.workspace_id) == 1

    # Second create with the same locator must roll back the entire graph.
    second_command, second_receipt = _create_command(
        workspace,
        f"locator-rollback-contender-{uuid4()}",
        idempotency_value=f"locator-rollback-contender-{uuid4()}-1",
        initial_locator=initial_locator,
    )
    counts_before_contender = await preflight_harness.table_row_counts()

    with pytest.raises(SourcePublicationError):
        await preflight_harness.store.commit_create(
            second_command,
            compute_request_fingerprint(second_command),
            second_receipt,
            _diagnostic_context(),
        )

    counts_after_contender = await preflight_harness.table_row_counts()
    # No canonical row may have survived the rejected contender attempt.
    for table_name, expected_delta in (
        ("content_objects", 0),
        ("sources", 0),
        ("source_versions", 0),
        ("sync_events", 0),
        ("projection_intents", 0),
        ("source_locators", 0),
    ):
        delta = counts_after_contender[table_name] - counts_before_contender[table_name]
        assert delta == expected_delta, table_name
    # Only the standalone rejection audit may have been added.
    assert counts_after_contender["audit_events"] - counts_before_contender["audit_events"] == 1
    # The anchored locator still stands: no contender locator was committed.
    assert await _fetch_locator_count_by_workspace(inspection_engine, workspace.workspace_id) == 1
    assert counts_after_first == counts_after_contender or (
        counts_after_first["audit_events"] + 1 == counts_after_contender["audit_events"]
    )
