"""Locked update transaction: changed, no-change and stale-base semantics.

Every case runs against the real migrated baseline through the real async
engine. A changed update commits exactly one content object, version
``n+1`` with the current parent, the guarded pointer advance, one update
event, two upsert intents and one ``source.version_published`` audit without
touching type or title. A no-change update writes only one event and one
``source.version_no_change`` audit, preserving the exact source ``updated_at``
and persisting ``base_version_id == committed_version_id``. The base
comparison precedes the content comparison: a stale base with bytes equal to
the current object rejects with ``source_version_conflict``.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from api_runtime.exclusion_policy_crypto import TrustAnchorEd25519Verifier
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

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
from personal_os.sources.fingerprint import compute_request_fingerprint, compute_safe_diff_hash
from personal_os.sources.results import PublicationOutcome
from postgresql_source_store.engine import create_source_store_engine, dispose_source_store_engine
from postgresql_source_store.publication_store import (
    PostgresqlSourcePublicationStore,
    _PendingRejection,
)
from postgresql_source_store.tables import (
    audit_events,
    content_objects,
    projection_intents,
    source_versions,
    sources,
    sync_events,
)

pytestmark = pytest.mark.local_stack

_SOURCE_STATE_ACTIVE = "active"
_AUDIT_ACTION_PUBLISHED = "source.version_published"
_AUDIT_ACTION_NO_CHANGE = "source.version_no_change"
_AUDIT_RESULT_SUCCEEDED = "succeeded"


def _diagnostic_context() -> DiagnosticContext:
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
        verified_at=datetime.now(UTC) - timedelta(seconds=1),
        verification_method=VerificationMethod.UPLOADED_FULL_READ,
    )


def _expected_object(receipt: VerifiedObjectReceipt) -> ExpectedObject:
    return ExpectedObject(
        content_digest=receipt.content_digest,
        size_bytes=receipt.size_bytes,
        media_type=receipt.media_type,
    )


def _create_command(workspace, salt: str, *, idempotency_value: str) -> CreateSourceVersion:
    receipt = _receipt(salt)
    return CreateSourceVersion(
        workspace_id=workspace.workspace_id,
        source_id=uuid4(),
        event_id=uuid4(),
        idempotency_key=IdempotencyKey(idempotency_value),
        source_type=SourceType.MARKDOWN,
        title=SourceTitle("Update transaction note"),
        actor=SourceActor(ActorKind.USER, workspace.owner_user_id),
        expected_object=_expected_object(receipt),
        client_timestamp=None,
    )


def _update_command(
    workspace,
    salt: str,
    *,
    source_id: UUID,
    base_version_id: UUID,
    idempotency_value: str,
    event_id: UUID | None = None,
) -> UpdateSourceVersion:
    return UpdateSourceVersion(
        workspace_id=workspace.workspace_id,
        source_id=source_id,
        event_id=event_id if event_id is not None else uuid4(),
        idempotency_key=IdempotencyKey(idempotency_value),
        base_version_id=base_version_id,
        actor=SourceActor(ActorKind.USER, workspace.owner_user_id),
        expected_object=_expected_object(_receipt(salt)),
        client_timestamp=None,
    )


@pytest_asyncio.fixture
async def update_engine(source_publication_stack) -> Iterator[AsyncEngine]:
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
                sync_events.c.request_fingerprint,
                sync_events.c.committed_at,
            ).where(sync_events.c.event_id == event_id)
        )
        return result.one_or_none()


async def _fetch_intent_count(engine: AsyncEngine, event_id: UUID) -> int:
    async with engine.connect() as connection:
        result = await connection.execute(
            sa.select(sa.func.count())
            .select_from(projection_intents)
            .where(projection_intents.c.event_id == event_id)
        )
        return int(result.scalar_one())


async def _fetch_audit_rows(engine: AsyncEngine, workspace_id: UUID, action: str) -> list:
    async with engine.connect() as connection:
        result = await connection.execute(
            sa.select(
                audit_events.c.action,
                audit_events.c.result,
                audit_events.c.reason_code,
                audit_events.c.target_id,
                audit_events.c.safe_diff_hash,
            ).where(
                audit_events.c.workspace_id == workspace_id,
                audit_events.c.action == action,
            )
        )
        return list(result.all())


async def _fetch_content_object_count_by_hash(engine: AsyncEngine, content_hash: str) -> int:
    async with engine.connect() as connection:
        result = await connection.execute(
            sa.select(sa.func.count())
            .select_from(content_objects)
            .where(content_objects.c.content_hash == content_hash)
        )
        return int(result.scalar_one())


async def _set_source_state(
    engine: AsyncEngine, source_id: UUID, *, sync_state: str, deleted: bool
) -> None:
    async with engine.begin() as connection:
        await _set_source_state_on(connection, source_id, sync_state=sync_state, deleted=deleted)


async def _set_source_state_on(
    connection: AsyncConnection, source_id: UUID, *, sync_state: str, deleted: bool
) -> None:
    values: dict[str, object] = {"sync_state": sync_state}
    if sync_state == "pending":
        values["current_version_id"] = None
    if deleted:
        values["deleted_at"] = sa.text("CURRENT_TIMESTAMP")
    await connection.execute(
        sa.update(sources).values(**values).where(sources.c.source_id == source_id)
    )


def _row_deltas(counts_before: dict, counts_after: dict) -> dict[str, int]:
    return {name: counts_after[name] - counts_before[name] for name in counts_after}


def _assert_only_one_rejection_audit_was_added(counts_before: dict, counts_after: dict) -> None:
    assert _row_deltas(counts_before, counts_after) == {
        "users": 0,
        "workspaces": 0,
        "devices": 0,
        "content_objects": 0,
        "sources": 0,
        "source_versions": 0,
        "sync_events": 0,
        "projection_intents": 0,
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


async def _seed_committed_source(harness, workspace, salt: str):
    """Create a source through the canonical create path and return its result."""
    command = _create_command(workspace, salt, idempotency_value=f"seed-{salt[:24]}-1")
    result = await harness.store.commit_create(
        command, compute_request_fingerprint(command), _receipt(salt), _diagnostic_context()
    )
    return command, result


# --- changed update --------------------------------------------------------------


@pytest.mark.asyncio
async def test_changed_update_commits_next_ordinal_graph(preflight_harness, update_engine) -> None:
    workspace = await preflight_harness.seed_workspace()
    create_salt = f"changed-base-{uuid4()}"
    _, first = await _seed_committed_source(preflight_harness, workspace, create_salt)
    update_salt = f"changed-next-{uuid4()}"
    command = _update_command(
        workspace,
        update_salt,
        source_id=first.source_id,
        base_version_id=first.source_version_id,
        idempotency_value="changed-update-1",
    )
    fingerprint = compute_request_fingerprint(command)
    diagnostic_context = _diagnostic_context()
    counts_before = await preflight_harness.table_row_counts()
    updated_at_before = await preflight_harness.fetch_source_updated_at(first.source_id)

    result = await preflight_harness.store.commit_update(
        command, fingerprint, _receipt(update_salt), diagnostic_context
    )

    assert result.source_id == first.source_id
    assert result.source_version_id != first.source_version_id
    assert result.content_version == first.content_version + 1
    assert result.event_id == command.event_id
    assert result.outcome is PublicationOutcome.PUBLISHED
    assert result.content_digest.hexadecimal == _receipt(update_salt).content_digest.hexadecimal
    assert result.committed_at.tzinfo is not None

    assert _row_deltas(counts_before, await preflight_harness.table_row_counts()) == {
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

    source_row = await _fetch_source_row(update_engine, first.source_id)
    assert source_row.source_type == "markdown"
    assert source_row.title == "Update transaction note"
    assert source_row.sync_state == _SOURCE_STATE_ACTIVE
    assert source_row.current_version_id == result.source_version_id
    assert source_row.deleted_at is None
    updated_at_after = await preflight_harness.fetch_source_updated_at(first.source_id)
    assert updated_at_after > updated_at_before

    version_row = await _fetch_version_row(update_engine, result.source_version_id)
    assert version_row.content_version == 2
    assert version_row.parent_version_id == first.source_version_id
    assert version_row.author_kind == "user"
    assert version_row.author_id == workspace.owner_user_id

    event_row = await _fetch_event_row(update_engine, command.event_id)
    assert event_row.event_sequence == result.event_sequence
    assert event_row.event_type == "update"
    assert event_row.base_version_id == first.source_version_id
    assert event_row.committed_version_id == result.source_version_id
    assert event_row.request_fingerprint == fingerprint.hexadecimal
    assert event_row.committed_at == result.committed_at
    assert await _fetch_intent_count(update_engine, command.event_id) == 2

    base_digest = ContentDigest.parse(
        await preflight_harness.content_digest_for_version(first.source_version_id)
    )
    update_diff_hash = compute_safe_diff_hash(
        first.source_id,
        first.source_version_id,
        base_digest,
        result.content_digest,
    ).hexadecimal
    # The seeded create published too, so the workspace holds two succeeded
    # ``source.version_published`` audits; exactly one carries the update's
    # safe diff hash.
    audits = await _fetch_audit_rows(update_engine, workspace.workspace_id, _AUDIT_ACTION_PUBLISHED)
    assert len(audits) == 2
    matching = [row for row in audits if row.safe_diff_hash == update_diff_hash]
    assert len(matching) == 1
    assert matching[0].result == _AUDIT_RESULT_SUCCEEDED
    assert matching[0].reason_code is None
    assert matching[0].target_id == first.source_id
    assert await preflight_harness.rejection_audit_rows(workspace_id=workspace.workspace_id) == []


@pytest.mark.asyncio
async def test_changed_update_preserves_stored_not_indexed_state_and_writes_intents(
    preflight_harness, update_engine
) -> None:
    workspace = await preflight_harness.seed_workspace()
    create_salt = f"stored-state-{uuid4()}"
    _, first = await _seed_committed_source(preflight_harness, workspace, create_salt)
    await _set_source_state(
        update_engine,
        first.source_id,
        sync_state="stored_not_indexed",
        deleted=False,
    )
    update_salt = f"stored-next-{uuid4()}"
    command = _update_command(
        workspace,
        update_salt,
        source_id=first.source_id,
        base_version_id=first.source_version_id,
        idempotency_value="stored-state-update-1",
    )

    result = await preflight_harness.store.commit_update(
        command, compute_request_fingerprint(command), _receipt(update_salt), _diagnostic_context()
    )

    assert result.outcome is PublicationOutcome.PUBLISHED
    source_row = await _fetch_source_row(update_engine, first.source_id)
    assert source_row.sync_state == "stored_not_indexed"
    assert source_row.current_version_id == result.source_version_id
    assert await _fetch_intent_count(update_engine, command.event_id) == 2


# --- no-change update ------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_change_update_writes_only_event_and_audit(
    preflight_harness, update_engine
) -> None:
    workspace = await preflight_harness.seed_workspace()
    create_salt = f"no-change-{uuid4()}"
    create_command, first = await _seed_committed_source(preflight_harness, workspace, create_salt)
    command = _update_command(
        workspace,
        create_salt,
        source_id=first.source_id,
        base_version_id=first.source_version_id,
        idempotency_value="no-change-update-1",
    )
    fingerprint = compute_request_fingerprint(command)
    counts_before = await preflight_harness.table_row_counts()
    updated_at_before = await preflight_harness.fetch_source_updated_at(first.source_id)

    result = await preflight_harness.store.commit_update(
        command, fingerprint, _receipt(create_salt), _diagnostic_context()
    )

    assert result.outcome is PublicationOutcome.NO_CHANGE
    assert result.source_version_id == first.source_version_id
    assert result.content_version == first.content_version
    assert (
        result.content_digest.hexadecimal
        == create_command.expected_object.content_digest.hexadecimal
    )
    assert result.event_id == command.event_id
    assert result.event_sequence > first.event_sequence
    assert result.committed_at.tzinfo is not None

    assert _row_deltas(counts_before, await preflight_harness.table_row_counts()) == {
        "users": 0,
        "workspaces": 0,
        "devices": 0,
        "content_objects": 0,
        "sources": 0,
        "source_versions": 0,
        "sync_events": 1,
        "projection_intents": 0,
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
    assert await preflight_harness.fetch_source_updated_at(first.source_id) == updated_at_before
    source_row = await _fetch_source_row(update_engine, first.source_id)
    assert source_row.current_version_id == first.source_version_id
    assert source_row.sync_state == _SOURCE_STATE_ACTIVE

    event_row = await _fetch_event_row(update_engine, command.event_id)
    assert event_row.event_type == "update"
    assert event_row.base_version_id == first.source_version_id
    assert event_row.committed_version_id == first.source_version_id
    assert event_row.event_sequence == result.event_sequence
    assert event_row.request_fingerprint == fingerprint.hexadecimal
    assert await _fetch_intent_count(update_engine, command.event_id) == 0

    audits = await _fetch_audit_rows(update_engine, workspace.workspace_id, _AUDIT_ACTION_NO_CHANGE)
    assert len(audits) == 1
    audit_row = audits[0]
    assert audit_row.result == _AUDIT_RESULT_SUCCEEDED
    assert audit_row.reason_code == "content_unchanged"
    assert audit_row.target_id == first.source_id
    assert (
        audit_row.safe_diff_hash
        == compute_safe_diff_hash(
            first.source_id,
            first.source_version_id,
            create_command.expected_object.content_digest,
            create_command.expected_object.content_digest,
        ).hexadecimal
    )
    assert await preflight_harness.rejection_audit_rows(workspace_id=workspace.workspace_id) == []


# --- base comparison precedes content comparison ---------------------------------


@pytest.mark.asyncio
async def test_stale_base_with_equal_bytes_conflicts_before_content_comparison(
    preflight_harness, update_engine
) -> None:
    workspace = await preflight_harness.seed_workspace()
    base_salt = f"stale-base-{uuid4()}"
    _, first = await _seed_committed_source(preflight_harness, workspace, base_salt)
    next_salt = f"stale-next-{uuid4()}"
    second_command = _update_command(
        workspace,
        next_salt,
        source_id=first.source_id,
        base_version_id=first.source_version_id,
        idempotency_value="stale-second-1",
    )
    second = await preflight_harness.store.commit_update(
        second_command,
        compute_request_fingerprint(second_command),
        _receipt(next_salt),
        _diagnostic_context(),
    )
    counts_before = await preflight_harness.table_row_counts()

    stale_command = _update_command(
        workspace,
        next_salt,
        source_id=first.source_id,
        base_version_id=first.source_version_id,
        idempotency_value="stale-third-1",
    )

    with pytest.raises(SourcePublicationError) as captured:
        await preflight_harness.store.commit_update(
            stale_command,
            compute_request_fingerprint(stale_command),
            _receipt(next_salt),
            _diagnostic_context(),
        )

    assert captured.value.error_code is ErrorCode.SOURCE_VERSION_CONFLICT
    assert captured.value.to_safe_dict()["safe_details"] == {
        "source_id": str(first.source_id),
        "current_version_id": str(second.source_version_id),
        "content_version": 2,
    }
    _assert_only_one_rejection_audit_was_added(
        counts_before, await preflight_harness.table_row_counts()
    )
    rejection_audits = await preflight_harness.rejection_audit_rows(
        workspace_id=workspace.workspace_id
    )
    assert len(rejection_audits) == 1
    assert rejection_audits[0].reason_code == "version_conflict"
    assert rejection_audits[0].target_id == first.source_id
    assert (
        await _fetch_content_object_count_by_hash(
            update_engine, _receipt(next_salt).content_digest.hexadecimal
        )
        == 1
    )
    source_row = await _fetch_source_row(update_engine, first.source_id)
    assert source_row.current_version_id == second.source_version_id


# --- update preconditions ----------------------------------------------------------


@pytest.mark.asyncio
async def test_update_of_missing_source_rejects_source_not_found(
    preflight_harness,
) -> None:
    workspace = await preflight_harness.seed_workspace()
    salt = f"missing-source-{uuid4()}"
    command = _update_command(
        workspace,
        salt,
        source_id=uuid4(),
        base_version_id=uuid4(),
        idempotency_value="missing-source-1",
    )
    counts_before = await preflight_harness.table_row_counts()

    with pytest.raises(SourcePublicationError) as captured:
        await preflight_harness.store.commit_update(
            command,
            compute_request_fingerprint(command),
            _receipt(salt),
            _diagnostic_context(),
        )

    assert captured.value.error_code is ErrorCode.SOURCE_NOT_FOUND
    assert dict(captured.value.safe_details) == {"source_id": command.source_id}
    _assert_only_one_rejection_audit_was_added(
        counts_before, await preflight_harness.table_row_counts()
    )
    rejection_audits = await preflight_harness.rejection_audit_rows(
        workspace_id=workspace.workspace_id
    )
    assert len(rejection_audits) == 1
    assert rejection_audits[0].reason_code == "source_not_found"


@pytest.mark.asyncio
@pytest.mark.parametrize("sync_state", ["pending", "deleted"])
async def test_update_of_non_publishable_state_rejects_state_invalid(
    preflight_harness, update_engine, sync_state: str
) -> None:
    workspace = await preflight_harness.seed_workspace()
    create_salt = f"state-invalid-{sync_state}-{uuid4()}"
    _, first = await _seed_committed_source(preflight_harness, workspace, create_salt)
    await _set_source_state(
        update_engine,
        first.source_id,
        sync_state=sync_state,
        deleted=(sync_state == "deleted"),
    )
    update_salt = f"state-invalid-next-{uuid4()}"
    command = _update_command(
        workspace,
        update_salt,
        source_id=first.source_id,
        base_version_id=first.source_version_id,
        idempotency_value=f"state-invalid-{sync_state}-1",
    )
    counts_before = await preflight_harness.table_row_counts()

    with pytest.raises(SourcePublicationError) as captured:
        await preflight_harness.store.commit_update(
            command,
            compute_request_fingerprint(command),
            _receipt(update_salt),
            _diagnostic_context(),
        )

    assert captured.value.error_code is ErrorCode.SOURCE_STATE_INVALID
    assert captured.value.to_safe_dict()["safe_details"] == {
        "source_id": str(first.source_id),
        "source_state": sync_state,
    }
    _assert_only_one_rejection_audit_was_added(
        counts_before, await preflight_harness.table_row_counts()
    )
    rejection_audits = await preflight_harness.rejection_audit_rows(
        workspace_id=workspace.workspace_id
    )
    assert len(rejection_audits) == 1
    assert rejection_audits[0].reason_code == "source_state_invalid"


@pytest.mark.asyncio
async def test_update_from_foreign_workspace_rejects_not_found_without_disclosure(
    preflight_harness,
) -> None:
    owning_workspace = await preflight_harness.seed_workspace()
    create_salt = f"foreign-owner-{uuid4()}"
    _, first = await _seed_committed_source(preflight_harness, owning_workspace, create_salt)
    requesting_workspace = await preflight_harness.seed_workspace()
    update_salt = f"foreign-requester-{uuid4()}"
    command = _update_command(
        requesting_workspace,
        update_salt,
        source_id=first.source_id,
        base_version_id=first.source_version_id,
        idempotency_value="foreign-workspace-1",
    )
    counts_before = await preflight_harness.table_row_counts()

    with pytest.raises(SourcePublicationError) as captured:
        await preflight_harness.store.commit_update(
            command,
            compute_request_fingerprint(command),
            _receipt(update_salt),
            _diagnostic_context(),
        )

    assert captured.value.error_code is ErrorCode.SOURCE_NOT_FOUND
    rendered = f"{captured.value} {captured.value!r} {captured.value.to_safe_dict()!r}"
    assert str(owning_workspace.workspace_id) not in rendered
    assert str(owning_workspace.owner_user_id) not in rendered
    assert str(first.source_version_id) not in rendered
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
    assert requesting_audits[0].reason_code == "source_not_found"


# --- returned rejections after writes --------------------------------------------


class _PointerInvariantUpdateStore(PostgresqlSourcePublicationStore):
    """Store whose guarded update pointer transition returns the invariant rejection.

    The rejection is RETURNED after the content object and version ``n+1`` were
    already written, so the store must roll the whole transaction back — a
    returned post-write rejection can never commit a partial graph.
    """

    async def _advance_current_pointer(
        self,
        connection: AsyncConnection,
        command: UpdateSourceVersion,
        source_version_id: UUID,
    ) -> _PendingRejection | None:
        return self._invariant_rejection(command)


@pytest.mark.asyncio
async def test_returned_pointer_invariant_rejection_after_writes_rolls_back_update(
    preflight_harness, update_engine
) -> None:
    workspace = await preflight_harness.seed_workspace()
    create_salt = f"update-invariant-{uuid4()}"
    _, first = await _seed_committed_source(preflight_harness, workspace, create_salt)
    update_salt = f"update-invariant-next-{uuid4()}"
    command = _update_command(
        workspace,
        update_salt,
        source_id=first.source_id,
        base_version_id=first.source_version_id,
        idempotency_value="update-invariant-1",
    )
    invariant_store = _PointerInvariantUpdateStore(
        update_engine, policy_verifier=TrustAnchorEd25519Verifier()
    )
    counts_before = await preflight_harness.table_row_counts()

    with pytest.raises(SourcePublicationError) as captured:
        await invariant_store.commit_update(
            command,
            compute_request_fingerprint(command),
            _receipt(update_salt),
            _diagnostic_context(),
        )

    assert captured.value.error_code is ErrorCode.SOURCE_CONCURRENCY_INVARIANT_FAILED
    assert dict(captured.value.safe_details) == {"source_id": command.source_id}
    _assert_only_one_rejection_audit_was_added(
        counts_before, await preflight_harness.table_row_counts()
    )
    rejection_audits = await preflight_harness.rejection_audit_rows(
        workspace_id=workspace.workspace_id
    )
    assert len(rejection_audits) == 1
    assert rejection_audits[0].reason_code is None
    source_row = await _fetch_source_row(update_engine, first.source_id)
    assert source_row.current_version_id == first.source_version_id
    assert source_row.sync_state == _SOURCE_STATE_ACTIVE


# --- locked-prefix replay ----------------------------------------------------------


@pytest.mark.asyncio
async def test_commit_update_exact_replay_returns_committed_result_without_mutation(
    preflight_harness,
) -> None:
    workspace = await preflight_harness.seed_workspace()
    create_salt = f"update-replay-{uuid4()}"
    _, first = await _seed_committed_source(preflight_harness, workspace, create_salt)
    update_salt = f"update-replay-next-{uuid4()}"
    command = _update_command(
        workspace,
        update_salt,
        source_id=first.source_id,
        base_version_id=first.source_version_id,
        idempotency_value="update-replay-1",
    )
    fingerprint = compute_request_fingerprint(command)
    receipt = _receipt(update_salt)
    committed = await preflight_harness.store.commit_update(
        command, fingerprint, receipt, _diagnostic_context()
    )
    counts_after_commit = await preflight_harness.table_row_counts()

    replayed = await preflight_harness.store.commit_update(
        command, fingerprint, receipt, _diagnostic_context()
    )

    assert replayed == committed
    assert await preflight_harness.table_row_counts() == counts_after_commit
    assert await preflight_harness.rejection_audit_rows(workspace_id=workspace.workspace_id) == []
