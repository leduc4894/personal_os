"""Whole-transaction rollback of the atomic create under injected faults.

A fault-injecting store subclass raises a non-database exception after each
canonical write step of the create transition — content object, pending
source, version 1, active pointer, create event, first intent, second intent
and success audit. Every injected fault must roll the entire transaction
back: no source, version, event, intent, content object or audit row may
survive for the attempted create, and no rejection audit is written for an
internal fault.
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
from personal_os.error_contracts.exceptions import InternalApplicationError
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
)
from personal_os.sources.errors import SourcePublicationError
from personal_os.sources.fingerprint import (
    RequestFingerprint,
    SourceVersionCommand,
    compute_request_fingerprint,
)
from postgresql_source_store.engine import create_source_store_engine, dispose_source_store_engine
from postgresql_source_store.publication_store import (
    ContentObjectLookupRow,
    PostgresqlSourcePublicationStore,
    _PendingRejection,
)
from postgresql_source_store.tables import sources

pytestmark = pytest.mark.local_stack

FAULT_AFTER_CONTENT_OBJECT = "content_object"
FAULT_AFTER_SOURCE = "source"
FAULT_AFTER_VERSION = "version"
FAULT_AFTER_POINTER = "pointer"
FAULT_AFTER_EVENT = "event"
FAULT_AFTER_FIRST_INTENT = "first_intent"
FAULT_AFTER_SECOND_INTENT = "second_intent"
FAULT_AFTER_AUDIT = "audit"

#: Every canonical write step of the create transition, in execution order.
ALL_FAULT_POINTS = (
    FAULT_AFTER_CONTENT_OBJECT,
    FAULT_AFTER_SOURCE,
    FAULT_AFTER_VERSION,
    FAULT_AFTER_POINTER,
    FAULT_AFTER_EVENT,
    FAULT_AFTER_FIRST_INTENT,
    FAULT_AFTER_SECOND_INTENT,
    FAULT_AFTER_AUDIT,
)


class FaultInjectingStore(PostgresqlSourcePublicationStore):
    """Store subclass raising one injected exception after a chosen write step.

    The hook has no production reach: it exists only in this test module and
    wraps the store's own step methods, so every fault fires inside the open
    create transaction and exercises the real rollback path.
    """

    def __init__(self, engine: AsyncEngine, fault_point: str) -> None:
        super().__init__(engine, policy_verifier=TrustAnchorEd25519Verifier())
        self._fault_point = fault_point

    def _maybe_fault(self, completed_step: str) -> None:
        if self._fault_point == completed_step:
            raise RuntimeError(f"injected fault after {completed_step}")

    async def _insert_content_object(
        self,
        connection: AsyncConnection,
        content_object_id: UUID,
        receipt: VerifiedObjectReceipt,
    ) -> ContentObjectLookupRow | None:
        row = await super()._insert_content_object(connection, content_object_id, receipt)
        self._maybe_fault(FAULT_AFTER_CONTENT_OBJECT)
        return row

    async def _insert_pending_source(
        self, connection: AsyncConnection, command: CreateSourceVersion
    ) -> None:
        await super()._insert_pending_source(connection, command)
        self._maybe_fault(FAULT_AFTER_SOURCE)

    async def _insert_version_one(
        self,
        connection: AsyncConnection,
        command: CreateSourceVersion,
        source_version_id: UUID,
        content_object_id: UUID,
    ) -> None:
        await super()._insert_version_one(connection, command, source_version_id, content_object_id)
        self._maybe_fault(FAULT_AFTER_VERSION)

    async def _activate_source_pointer(
        self,
        connection: AsyncConnection,
        command: CreateSourceVersion,
        source_version_id: UUID,
    ) -> _PendingRejection | None:
        rejection = await super()._activate_source_pointer(connection, command, source_version_id)
        self._maybe_fault(FAULT_AFTER_POINTER)
        return rejection

    async def _insert_create_event(
        self,
        connection: AsyncConnection,
        command: CreateSourceVersion,
        request_fingerprint: RequestFingerprint,
        source_version_id: UUID,
    ) -> tuple[int, datetime]:
        event_row = await super()._insert_create_event(
            connection, command, request_fingerprint, source_version_id
        )
        self._maybe_fault(FAULT_AFTER_EVENT)
        return event_row

    async def _insert_projection_intent(
        self,
        connection: AsyncConnection,
        command: SourceVersionCommand,
        source_version_id: UUID,
        projection_intent_id: UUID,
        projection_kind: str,
    ) -> None:
        await super()._insert_projection_intent(
            connection,
            command,
            source_version_id,
            projection_intent_id,
            projection_kind,
        )
        if projection_kind == "qdrant":
            self._maybe_fault(FAULT_AFTER_FIRST_INTENT)
        else:
            self._maybe_fault(FAULT_AFTER_SECOND_INTENT)

    async def _insert_success_audit(
        self,
        connection: AsyncConnection,
        command: CreateSourceVersion,
        receipt: VerifiedObjectReceipt,
        diagnostic_context: DiagnosticContext,
        audit_event_id: UUID,
    ) -> None:
        await super()._insert_success_audit(
            connection, command, receipt, diagnostic_context, audit_event_id
        )
        self._maybe_fault(FAULT_AFTER_AUDIT)


class _PointerInvariantRejectionStore(PostgresqlSourcePublicationStore):
    """Store whose guarded pointer transition returns the invariant rejection.

    Unlike the fault-hook subclass this never raises inside the store: the
    rejection is RETURNED after the content object, pending source and
    version 1 rows were already written. The store must still roll the whole
    transaction back — a returned rejection can never commit the partial
    pending-source graph.
    """

    async def _activate_source_pointer(
        self,
        connection: AsyncConnection,
        command: CreateSourceVersion,
        source_version_id: UUID,
    ) -> _PendingRejection | None:
        return self._invariant_rejection(command)


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


def _create_command(workspace, salt: str) -> CreateSourceVersion:
    receipt = _receipt(salt)
    return CreateSourceVersion(
        workspace_id=workspace.workspace_id,
        source_id=uuid4(),
        event_id=uuid4(),
        idempotency_key=IdempotencyKey("create-rollback-1"),
        source_type=SourceType.MARKDOWN,
        title=SourceTitle("Create rollback note"),
        actor=SourceActor(ActorKind.USER, workspace.owner_user_id),
        expected_object=ExpectedObject(
            content_digest=receipt.content_digest,
            size_bytes=receipt.size_bytes,
            media_type=receipt.media_type,
        ),
        client_timestamp=None,
    )


@pytest_asyncio.fixture
async def fault_engine(source_publication_stack) -> Iterator[AsyncEngine]:
    engine = create_source_store_engine(
        source_publication_stack.settings, source_publication_stack.password
    )
    try:
        yield engine
    finally:
        await dispose_source_store_engine(engine)


async def _source_exists(engine: AsyncEngine, source_id: UUID) -> bool:
    async with engine.connect() as connection:
        result = await connection.execute(
            sa.select(sa.func.count()).select_from(sources).where(sources.c.source_id == source_id)
        )
        return int(result.scalar_one()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("fault_point", ALL_FAULT_POINTS)
async def test_injected_fault_after_each_write_leaves_no_partial_graph(
    preflight_harness, fault_engine, fault_point: str
) -> None:
    workspace = await preflight_harness.seed_workspace()
    salt = f"create-rollback-{fault_point}-{uuid4()}"
    command = _create_command(workspace, salt)
    receipt = _receipt(salt)
    fingerprint = compute_request_fingerprint(command)
    fault_store = FaultInjectingStore(fault_engine, fault_point)
    counts_before = await preflight_harness.table_row_counts()

    with pytest.raises(InternalApplicationError) as captured:
        await fault_store.commit_create(
            command, fingerprint, receipt, create_diagnostic_context().context
        )

    assert captured.value.error_code is ErrorCode.INTERNAL_ERROR
    assert await preflight_harness.table_row_counts() == counts_before
    assert not await _source_exists(fault_engine, command.source_id)
    assert await preflight_harness.rejection_audit_rows(workspace_id=workspace.workspace_id) == []


@pytest.mark.asyncio
async def test_fault_store_without_injected_fault_still_commits_create(
    preflight_harness, fault_engine
) -> None:
    workspace = await preflight_harness.seed_workspace()
    salt = f"create-rollback-unfaulted-{uuid4()}"
    command = _create_command(workspace, salt)
    receipt = _receipt(salt)
    fingerprint = compute_request_fingerprint(command)
    healthy_store = FaultInjectingStore(fault_engine, "none")

    result = await healthy_store.commit_create(
        command, fingerprint, receipt, create_diagnostic_context().context
    )

    assert result.content_version == 1
    assert await _source_exists(fault_engine, command.source_id)


@pytest.mark.asyncio
async def test_returned_invariant_rejection_after_writes_rolls_back_whole_graph(
    preflight_harness, fault_engine
) -> None:
    """A rejection RETURNED after canonical writes must not commit anything."""
    workspace = await preflight_harness.seed_workspace()
    salt = f"create-rollback-invariant-{uuid4()}"
    command = _create_command(workspace, salt)
    receipt = _receipt(salt)
    fingerprint = compute_request_fingerprint(command)
    invariant_store = _PointerInvariantRejectionStore(
        fault_engine, policy_verifier=TrustAnchorEd25519Verifier()
    )
    counts_before = await preflight_harness.table_row_counts()

    with pytest.raises(SourcePublicationError) as captured:
        await invariant_store.commit_create(
            command, fingerprint, receipt, create_diagnostic_context().context
        )

    assert captured.value.error_code is ErrorCode.SOURCE_CONCURRENCY_INVARIANT_FAILED
    assert dict(captured.value.safe_details) == {"source_id": command.source_id}
    counts_after = await preflight_harness.table_row_counts()
    # Only the standalone rejection audit may appear; every canonical table
    # (including the already-inserted content object, pending source and
    # version 1) must be unchanged.
    assert {
        table_name: counts_after[table_name] - counts_before[table_name]
        for table_name in counts_after
    } == {
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
    assert not await _source_exists(fault_engine, command.source_id)
    rejection_audits = await preflight_harness.rejection_audit_rows(
        workspace_id=workspace.workspace_id
    )
    assert len(rejection_audits) == 1
    assert rejection_audits[0].reason_code is None
    assert rejection_audits[0].target_id == command.source_id
