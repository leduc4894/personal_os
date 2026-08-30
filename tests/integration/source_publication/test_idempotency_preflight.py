"""Authorization-aware idempotency preflight against a disposable PostgreSQL 18.4.

Every case runs against the real migrated baseline through the real async
engine: exact key/event/fingerprint replay (create, no-change update and
changed update), a miss, key reuse with a different event, the same event
under another key, a cross-workspace global event collision, invalid and
revoked actors, an unknown workspace before the trust boundary and an
impossible committed event shape. Each mismatch must write exactly one
standalone rejection audit only after a trusted workspace/actor context was
established, and every cross-tenant failure must disclose only the requested
source/event IDs.
"""

from __future__ import annotations

import hashlib
import json
from uuid import uuid4

import pytest

from personal_os.diagnostics.context import create_diagnostic_context
from personal_os.error_contracts.codes import ErrorCode
from personal_os.object_storage import CanonicalMediaType, ContentDigest, ExpectedObject
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

pytestmark = pytest.mark.local_stack

_DIGEST_SEED = "preflight-expected-object-bytes"


def _diagnostic_context():
    return create_diagnostic_context().context


def _expected_object(salt: str = _DIGEST_SEED) -> ExpectedObject:
    return ExpectedObject(
        content_digest=ContentDigest.parse(hashlib.sha256(salt.encode("utf-8")).hexdigest()),
        size_bytes=len(salt),
        media_type=CanonicalMediaType.parse("text/markdown"),
    )


def _create_command(
    workspace, *, event_id=None, idempotency_value: str = "preflight-create-1"
) -> CreateSourceVersion:
    return CreateSourceVersion(
        workspace_id=workspace.workspace_id,
        source_id=uuid4(),
        event_id=event_id if event_id is not None else uuid4(),
        idempotency_key=IdempotencyKey(idempotency_value),
        source_type=SourceType.MARKDOWN,
        title=SourceTitle("Preflight note"),
        actor=SourceActor(ActorKind.USER, workspace.owner_user_id),
        expected_object=_expected_object(),
        client_timestamp=None,
    )


def _update_command(
    workspace,
    *,
    source_id,
    base_version_id,
    event_id=None,
    idempotency_value: str = "preflight-update-1",
    salt: str = _DIGEST_SEED,
) -> UpdateSourceVersion:
    return UpdateSourceVersion(
        workspace_id=workspace.workspace_id,
        source_id=source_id,
        event_id=event_id if event_id is not None else uuid4(),
        idempotency_key=IdempotencyKey(idempotency_value),
        base_version_id=base_version_id,
        actor=SourceActor(ActorKind.USER, workspace.owner_user_id),
        expected_object=_expected_object(salt),
        client_timestamp=None,
    )


def _assert_is_rejection(error: SourcePublicationError, code: ErrorCode) -> None:
    assert isinstance(error, SourcePublicationError)
    assert error.error_code is code


def _safe_render(error: SourcePublicationError) -> str:
    return f"{error} {error!r} {json.dumps(error.to_safe_dict(), default=repr)}"


async def _seed_committed_create(harness, workspace, *, idempotency_value: str):
    source_id = uuid4()
    event_id = uuid4()
    command = CreateSourceVersion(
        workspace_id=workspace.workspace_id,
        source_id=source_id,
        event_id=event_id,
        idempotency_key=IdempotencyKey(idempotency_value),
        source_type=SourceType.MARKDOWN,
        title=SourceTitle("Committed preflight note"),
        actor=SourceActor(ActorKind.USER, workspace.owner_user_id),
        expected_object=_expected_object(f"committed-{event_id}"),
        client_timestamp=None,
    )
    fingerprint = compute_request_fingerprint(command)
    version = await harness.seed_active_source_with_version_one(
        workspace=workspace, source_id=source_id, title="Committed preflight note"
    )
    event_sequence, committed_at = await harness.seed_event(
        workspace_id=workspace.workspace_id,
        source_id=source_id,
        event_id=event_id,
        idempotency_key=idempotency_value,
        request_fingerprint=fingerprint.hexadecimal,
        event_type="create",
        committed_version_id=version.source_version_id,
    )
    return command, fingerprint, version, event_sequence, committed_at


# --- exact replay --------------------------------------------------------------


@pytest.mark.asyncio
async def test_exact_create_replay_returns_canonical_result_without_mutation(
    preflight_harness,
) -> None:
    workspace = await preflight_harness.seed_workspace()
    command, fingerprint, version, event_sequence, committed_at = await _seed_committed_create(
        preflight_harness, workspace, idempotency_value="exact-create-replay-1"
    )
    counts_before = await preflight_harness.table_row_counts()
    updated_at_before = await preflight_harness.fetch_source_updated_at(command.source_id)

    result = await preflight_harness.store.resolve_committed(
        command, fingerprint, _diagnostic_context()
    )

    assert result is not None
    assert result.source_id == command.source_id
    assert result.source_version_id == version.source_version_id
    assert result.content_version == 1
    assert result.event_id == command.event_id
    assert result.event_sequence == event_sequence
    assert result.outcome is PublicationOutcome.PUBLISHED
    assert result.committed_at == committed_at
    digest = await preflight_harness.content_digest_for_version(version.source_version_id)
    assert result.content_digest.hexadecimal == digest
    assert await preflight_harness.table_row_counts() == counts_before
    assert await preflight_harness.fetch_source_updated_at(command.source_id) == updated_at_before
    assert await preflight_harness.rejection_audit_rows() == []


@pytest.mark.asyncio
async def test_exact_no_change_update_replay_hydrates_no_change_outcome(
    preflight_harness,
) -> None:
    workspace = await preflight_harness.seed_workspace()
    (
        create_command,
        _,
        version_one,
        _,
        _,
    ) = await _seed_committed_create(
        preflight_harness, workspace, idempotency_value="no-change-anchor-1"
    )
    no_change_command = _update_command(
        workspace,
        source_id=create_command.source_id,
        base_version_id=version_one.source_version_id,
        idempotency_value="no-change-replay-1",
    )
    no_change_fingerprint = compute_request_fingerprint(no_change_command)
    event_sequence, committed_at = await preflight_harness.seed_event(
        workspace_id=workspace.workspace_id,
        source_id=create_command.source_id,
        event_id=no_change_command.event_id,
        idempotency_key=no_change_command.idempotency_key.value,
        request_fingerprint=no_change_fingerprint.hexadecimal,
        event_type="update",
        committed_version_id=version_one.source_version_id,
        base_version_id=version_one.source_version_id,
    )
    counts_before = await preflight_harness.table_row_counts()

    result = await preflight_harness.store.resolve_committed(
        no_change_command, no_change_fingerprint, _diagnostic_context()
    )

    assert result is not None
    assert result.outcome is PublicationOutcome.NO_CHANGE
    assert result.source_version_id == version_one.source_version_id
    assert result.content_version == 1
    assert result.event_sequence == event_sequence
    assert result.committed_at == committed_at
    assert await preflight_harness.table_row_counts() == counts_before


@pytest.mark.asyncio
async def test_exact_changed_update_replay_hydrates_published_outcome(
    preflight_harness,
) -> None:
    workspace = await preflight_harness.seed_workspace()
    (
        create_command,
        _,
        version_one,
        _,
        _,
    ) = await _seed_committed_create(
        preflight_harness, workspace, idempotency_value="changed-anchor-1"
    )
    version_two = await preflight_harness.advance_source_version(
        workspace=workspace,
        source_id=create_command.source_id,
        parent=version_one,
    )
    update_command = _update_command(
        workspace,
        source_id=create_command.source_id,
        base_version_id=version_one.source_version_id,
        event_id=uuid4(),
        idempotency_value="changed-replay-1",
        salt=f"changed-{version_two.source_version_id}",
    )
    update_fingerprint = compute_request_fingerprint(update_command)
    event_sequence, committed_at = await preflight_harness.seed_event(
        workspace_id=workspace.workspace_id,
        source_id=create_command.source_id,
        event_id=update_command.event_id,
        idempotency_key=update_command.idempotency_key.value,
        request_fingerprint=update_fingerprint.hexadecimal,
        event_type="update",
        committed_version_id=version_two.source_version_id,
        base_version_id=version_one.source_version_id,
    )
    counts_before = await preflight_harness.table_row_counts()

    result = await preflight_harness.store.resolve_committed(
        update_command, update_fingerprint, _diagnostic_context()
    )

    assert result is not None
    assert result.outcome is PublicationOutcome.PUBLISHED
    assert result.source_version_id == version_two.source_version_id
    assert result.content_version == 2
    assert result.event_sequence == event_sequence
    assert result.committed_at == committed_at
    assert await preflight_harness.table_row_counts() == counts_before


@pytest.mark.asyncio
async def test_unused_key_and_event_returns_none(preflight_harness) -> None:
    workspace = await preflight_harness.seed_workspace()
    command = _create_command(workspace, idempotency_value="unused-key-1")
    fingerprint = compute_request_fingerprint(command)

    result = await preflight_harness.store.resolve_committed(
        command, fingerprint, _diagnostic_context()
    )

    assert result is None
    assert await preflight_harness.rejection_audit_rows() == []


# --- key and event identity misuse ---------------------------------------------


@pytest.mark.asyncio
async def test_key_reuse_with_another_event_rejects_and_writes_one_audit(
    preflight_harness,
) -> None:
    workspace = await preflight_harness.seed_workspace()
    committed_command, _, _, _, _ = await _seed_committed_create(
        preflight_harness, workspace, idempotency_value="reused-key-1"
    )
    contender = _create_command(
        workspace,
        event_id=uuid4(),
        idempotency_value="reused-key-1",
    )
    fingerprint = compute_request_fingerprint(contender)

    with pytest.raises(SourcePublicationError) as captured:
        await preflight_harness.store.resolve_committed(
            contender, fingerprint, _diagnostic_context()
        )

    _assert_is_rejection(captured.value, ErrorCode.SOURCE_IDEMPOTENCY_MISMATCH)
    assert dict(captured.value.safe_details) == {"source_id": contender.source_id}
    render = _safe_render(captured.value)
    assert str(committed_command.event_id) not in render
    assert str(committed_command.source_id) not in render

    audits = await preflight_harness.rejection_audit_rows(workspace_id=workspace.workspace_id)
    assert len(audits) == 1
    audit = audits[0]
    assert audit.action == "source.version_publish_rejected"
    assert audit.target_id == contender.source_id
    assert audit.reason_code == "idempotency_mismatch"
    assert audit.workspace_id == workspace.workspace_id
    assert audit.actor_id == workspace.owner_user_id


@pytest.mark.asyncio
async def test_same_event_under_another_key_rejects_with_event_identity_mismatch(
    preflight_harness,
) -> None:
    workspace = await preflight_harness.seed_workspace()
    committed_command, committed_fingerprint, _, _, _ = await _seed_committed_create(
        preflight_harness, workspace, idempotency_value="event-anchor-1"
    )
    contender = CreateSourceVersion(
        workspace_id=workspace.workspace_id,
        source_id=committed_command.source_id,
        event_id=committed_command.event_id,
        idempotency_key=IdempotencyKey("event-contender-1"),
        source_type=committed_command.source_type,
        title=committed_command.title,
        actor=committed_command.actor,
        expected_object=committed_command.expected_object,
        client_timestamp=None,
    )
    fingerprint = compute_request_fingerprint(contender)
    assert fingerprint.hexadecimal == committed_fingerprint.hexadecimal

    with pytest.raises(SourcePublicationError) as captured:
        await preflight_harness.store.resolve_committed(
            contender, fingerprint, _diagnostic_context()
        )

    _assert_is_rejection(captured.value, ErrorCode.SOURCE_EVENT_IDENTITY_MISMATCH)
    assert dict(captured.value.safe_details) == {
        "source_id": contender.source_id,
        "event_id": contender.event_id,
    }
    audits = await preflight_harness.rejection_audit_rows(workspace_id=workspace.workspace_id)
    assert len(audits) == 1
    assert audits[0].reason_code == "event_identity_mismatch"
    assert audits[0].target_id == contender.source_id


@pytest.mark.asyncio
async def test_cross_workspace_event_collision_never_discloses_existing_tenant(
    preflight_harness,
) -> None:
    tenant_a = await preflight_harness.seed_workspace()
    tenant_b = await preflight_harness.seed_workspace()
    committed_command, _, _, _, _ = await _seed_committed_create(
        preflight_harness, tenant_a, idempotency_value="tenant-a-key-1"
    )
    contender = _create_command(
        tenant_b,
        event_id=committed_command.event_id,
        idempotency_value="tenant-b-key-1",
    )
    fingerprint = compute_request_fingerprint(contender)

    with pytest.raises(SourcePublicationError) as captured:
        await preflight_harness.store.resolve_committed(
            contender, fingerprint, _diagnostic_context()
        )

    _assert_is_rejection(captured.value, ErrorCode.SOURCE_EVENT_IDENTITY_MISMATCH)
    assert dict(captured.value.safe_details) == {
        "source_id": contender.source_id,
        "event_id": contender.event_id,
    }
    render = _safe_render(captured.value)
    for tenant_a_value in (
        str(tenant_a.workspace_id),
        str(tenant_a.owner_user_id),
        str(committed_command.source_id),
        "tenant-a-key-1",
    ):
        assert tenant_a_value not in render

    assert await preflight_harness.rejection_audit_rows(workspace_id=tenant_a.workspace_id) == []
    tenant_b_audits = await preflight_harness.rejection_audit_rows(
        workspace_id=tenant_b.workspace_id
    )
    assert len(tenant_b_audits) == 1
    assert tenant_b_audits[0].reason_code == "event_identity_mismatch"
    assert tenant_b_audits[0].workspace_id == tenant_b.workspace_id
    assert tenant_b_audits[0].target_id == contender.source_id


# --- actor revalidation ---------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("actor_kind", ["foreign_user", "revoked_device"])
async def test_invalid_actor_rejection_after_trust_writes_actor_invalid_audit(
    preflight_harness, actor_kind: str
) -> None:
    workspace = await preflight_harness.seed_workspace()
    if actor_kind == "foreign_user":
        actor = SourceActor(ActorKind.USER, uuid4())
    else:
        revoked_device_id = await preflight_harness.seed_revoked_device(workspace)
        actor = SourceActor(ActorKind.DEVICE, revoked_device_id)
    command = CreateSourceVersion(
        workspace_id=workspace.workspace_id,
        source_id=uuid4(),
        event_id=uuid4(),
        idempotency_key=IdempotencyKey(f"actor-invalid-{actor_kind}-1"),
        source_type=SourceType.MARKDOWN,
        title=SourceTitle("Actor revalidation note"),
        actor=actor,
        expected_object=_expected_object(),
        client_timestamp=None,
    )
    fingerprint = compute_request_fingerprint(command)

    with pytest.raises(SourcePublicationError) as captured:
        await preflight_harness.store.resolve_committed(command, fingerprint, _diagnostic_context())

    assert captured.value.error_code is ErrorCode.SOURCE_PUBLISH_INPUT_INVALID
    assert str(captured.value.safe_details["reason"]) == "actor_invalid"
    audits = await preflight_harness.rejection_audit_rows(workspace_id=workspace.workspace_id)
    assert len(audits) == 1
    assert audits[0].reason_code == "actor_invalid"
    assert audits[0].actor_kind == actor.actor_kind.value
    assert audits[0].workspace_id == workspace.workspace_id
    assert audits[0].target_id == command.source_id


@pytest.mark.asyncio
async def test_unknown_workspace_rejects_without_writing_any_audit(
    preflight_harness,
) -> None:
    class _UnknownWorkspace:
        workspace_id = uuid4()
        owner_user_id = uuid4()
        device_id = uuid4()

    command = _create_command(_UnknownWorkspace(), idempotency_value="unknown-workspace-1")
    fingerprint = compute_request_fingerprint(command)
    audit_counts_before = await preflight_harness.table_row_counts()

    with pytest.raises(SourcePublicationError) as captured:
        await preflight_harness.store.resolve_committed(command, fingerprint, _diagnostic_context())

    assert captured.value.error_code is ErrorCode.SOURCE_PUBLISH_INPUT_INVALID
    assert await preflight_harness.table_row_counts() == audit_counts_before


@pytest.mark.asyncio
async def test_active_device_actor_revalidates_and_can_replay(preflight_harness) -> None:
    workspace = await preflight_harness.seed_workspace()
    source_id = uuid4()
    event_id = uuid4()
    command = CreateSourceVersion(
        workspace_id=workspace.workspace_id,
        source_id=source_id,
        event_id=event_id,
        idempotency_key=IdempotencyKey("device-replay-1"),
        source_type=SourceType.MARKDOWN,
        title=SourceTitle("Device replay note"),
        actor=SourceActor(ActorKind.DEVICE, workspace.device_id),
        expected_object=_expected_object(),
        client_timestamp=None,
    )
    fingerprint = compute_request_fingerprint(command)
    version = await preflight_harness.seed_active_source_with_version_one(
        workspace=workspace, source_id=source_id, title="Device replay note"
    )
    await preflight_harness.seed_event(
        workspace_id=workspace.workspace_id,
        source_id=source_id,
        event_id=event_id,
        idempotency_key="device-replay-1",
        request_fingerprint=fingerprint.hexadecimal,
        event_type="create",
        committed_version_id=version.source_version_id,
        device_id=workspace.device_id,
    )

    result = await preflight_harness.store.resolve_committed(
        command, fingerprint, _diagnostic_context()
    )

    assert result is not None
    assert result.source_version_id == version.source_version_id
    assert await preflight_harness.rejection_audit_rows(workspace_id=workspace.workspace_id) == []


# --- impossible committed event shape -------------------------------------------


@pytest.mark.asyncio
async def test_impossible_event_shape_rejects_with_invariant_failure_and_audits(
    preflight_harness,
) -> None:
    workspace = await preflight_harness.seed_workspace()
    source_id = uuid4()
    event_id = uuid4()
    command = CreateSourceVersion(
        workspace_id=workspace.workspace_id,
        source_id=source_id,
        event_id=event_id,
        idempotency_key=IdempotencyKey("impossible-shape-1"),
        source_type=SourceType.MARKDOWN,
        title=SourceTitle("Impossible shape note"),
        actor=SourceActor(ActorKind.USER, workspace.owner_user_id),
        expected_object=_expected_object(),
        client_timestamp=None,
    )
    fingerprint = compute_request_fingerprint(command)
    version = await preflight_harness.seed_active_source_with_version_one(
        workspace=workspace, source_id=source_id, title="Impossible shape note"
    )
    # A create event that also carries a base version is an impossible shape.
    await preflight_harness.seed_event(
        workspace_id=workspace.workspace_id,
        source_id=source_id,
        event_id=event_id,
        idempotency_key="impossible-shape-1",
        request_fingerprint=fingerprint.hexadecimal,
        event_type="create",
        committed_version_id=version.source_version_id,
        base_version_id=version.source_version_id,
    )

    with pytest.raises(SourcePublicationError) as captured:
        await preflight_harness.store.resolve_committed(command, fingerprint, _diagnostic_context())

    _assert_is_rejection(captured.value, ErrorCode.SOURCE_CONCURRENCY_INVARIANT_FAILED)
    audits = await preflight_harness.rejection_audit_rows(workspace_id=workspace.workspace_id)
    assert len(audits) == 1
    assert audits[0].reason_code is None
    assert audits[0].target_id == command.source_id
