"""Durable small-file upload-operation store against the disposable stack.

These integration tests prove the durable operation contract of spec 10.1 and
10.3 end-to-end on the migrated schema: the identity uniqueness of
``(workspace, device, event, idempotency key)``, convergence of concurrent
preflights onto one operation row, rejection of payload substitution under
the same identity, the server-owned reservation of a create UUID without any
``sources`` row, the invalidity of an expired non-terminal operation for
continuation while terminal results survive expiry and a same-identity
re-preflight re-reserves the expired row (fresh token, extended deadline,
still one row and no ``sources`` row), the exact replay of the frozen
terminal result after a lost response, the gated downgrade of the
``20260818_01`` migration back to the exclusion policy head, and the
one-shot ``20260901_03`` data remediation that clears the raw locator on
already-terminal rows (pre-fix historical shape) while keeping the replay
digest and leaving non-terminal rows untouched.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from alembic.util import CommandError
from tests.integration.source_publication.conftest import (
    PreflightHarness,
    SourcePublicationStack,
)

from personal_os.diagnostics.context import DiagnosticContext
from personal_os.diagnostics.trace_context import SpanId, TraceContext, TraceId
from personal_os.error_contracts.codes import ErrorCode
from personal_os.exclusion_policy.enforcement import AllowedPolicyRevisionBinding
from personal_os.object_storage import CanonicalMediaType, ContentDigest
from personal_os.small_file_sync.contracts import (
    NormalizedLocator,
    SmallFileDeviceContext,
    SmallFileIdempotencyKey,
    SmallFileOperation,
    SmallFilePreflight,
    SmallFileTerminalResult,
    SmallFileTerminalResultKind,
    compute_locator_fingerprint,
)
from personal_os.small_file_sync.errors import SmallFileSyncError
from postgresql_source_store.small_file_sync_operations import (
    UPLOAD_OPERATION_EXPIRY_SECONDS,
    PostgresqlSmallFileUploadOperationStore,
    upload_operation_token_hash,
)
from postgresql_source_store.tables import small_file_upload_operations, sources

pytestmark = pytest.mark.local_stack

_WORKTREE_ROOT: Path = Path(__file__).resolve().parents[3]

_TRACE = TraceContext(
    trace_id=TraceId("0123456789abcdef0123456789abcdef"),
    remote_parent_span_id=None,
    local_span_id=SpanId("0123456789abcdef"),
    trace_flags=0,
)

_DIGEST_A = ContentDigest.parse(hashlib.sha256(b"integration-payload-a").hexdigest())
_DIGEST_B = ContentDigest.parse(hashlib.sha256(b"integration-payload-b").hexdigest())
_POLICY_REVISION_NUMBER = 4


def _context() -> DiagnosticContext:
    return DiagnosticContext(request_id=uuid4(), client_request_id=None, trace=_TRACE)


class MutableClock:
    """Injectable aware-UTC clock the tests advance past the expiry deadline.

    The start point is the real current UTC moment so the computed expiry
    stays ahead of the database-owned ``created_at`` default, satisfying the
    migration's timestamp CHECK.
    """

    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, *, seconds: int) -> None:
        self.now = self.now + timedelta(seconds=seconds)


class SmallFileOperationHarness:
    """Harness bound to the disposable stack engine and the mutable clock."""

    def __init__(self, engine: sa.ext.asyncio.AsyncEngine) -> None:
        self.engine = engine
        self.clock = MutableClock()
        self.store = PostgresqlSmallFileUploadOperationStore(engine, clock=self.clock)

    def preflight(
        self,
        *,
        operation: SmallFileOperation = SmallFileOperation.CREATE,
        sha256: ContentDigest = _DIGEST_A,
        size_bytes: int = 128,
        media_type: str = "text/markdown",
        event_id: UUID | None = None,
        idempotency_key: SmallFileIdempotencyKey | None = None,
        source_id: UUID | None = None,
        base_version_id: UUID | None = None,
    ) -> SmallFilePreflight:
        resolved_operation = operation
        return SmallFilePreflight(
            event_id=event_id if event_id is not None else uuid4(),
            idempotency_key=idempotency_key
            if idempotency_key is not None
            else SmallFileIdempotencyKey(str(uuid4())),
            operation=resolved_operation,
            local_file_id=uuid4(),
            source_id=source_id if resolved_operation is SmallFileOperation.UPDATE else None,
            base_version_id=(
                base_version_id if resolved_operation is SmallFileOperation.UPDATE else None
            ),
            normalized_locator=NormalizedLocator("notes/daily/today.md"),
            sha256=sha256,
            size_bytes=size_bytes,
            media_type=CanonicalMediaType.parse(media_type),
            policy_revision_number=_POLICY_REVISION_NUMBER,
        )

    def device_context(self, workspace: object) -> SmallFileDeviceContext:
        return SmallFileDeviceContext(
            device_id=workspace.device_id, workspace_id=workspace.workspace_id
        )

    def policy_binding(
        self, device_context: SmallFileDeviceContext, revision: int = _POLICY_REVISION_NUMBER
    ) -> AllowedPolicyRevisionBinding:
        return AllowedPolicyRevisionBinding(
            workspace_id=device_context.workspace_id, policy_revision_number=revision
        )

    async def operation_row(self, event_id: UUID) -> dict[str, object] | None:
        statement = sa.select(
            small_file_upload_operations.c.operation_id,
            small_file_upload_operations.c.operation_token_hash,
            small_file_upload_operations.c.workspace_id,
            small_file_upload_operations.c.device_id,
            small_file_upload_operations.c.event_id,
            small_file_upload_operations.c.idempotency_key,
            small_file_upload_operations.c.operation_kind,
            small_file_upload_operations.c.declared_sha256,
            small_file_upload_operations.c.declared_size_bytes,
            small_file_upload_operations.c.declared_media_type,
            small_file_upload_operations.c.policy_revision_number,
            small_file_upload_operations.c.reserved_source_id,
            small_file_upload_operations.c.update_source_id,
            small_file_upload_operations.c.update_base_version_id,
            small_file_upload_operations.c.normalized_locator,
            small_file_upload_operations.c.locator_fingerprint,
            small_file_upload_operations.c.state,
            small_file_upload_operations.c.safe_error_code,
            small_file_upload_operations.c.result_kind,
            small_file_upload_operations.c.result_source_id,
            small_file_upload_operations.c.result_source_version_id,
            small_file_upload_operations.c.result_content_version,
            small_file_upload_operations.c.result_committed_at,
            small_file_upload_operations.c.expires_at,
        ).where(small_file_upload_operations.c.event_id == event_id)
        async with self.engine.connect() as connection:
            row = (await connection.execute(statement)).mappings().one_or_none()
        return None if row is None else dict(row)

    async def operation_row_count(self, *event_ids: UUID) -> int:
        """Count operation rows, optionally scoped to the given event ids.

        The module shares one disposable database across its tests, so every
        count assertion is scoped to the events the test itself created.
        """
        statement = sa.select(sa.func.count()).select_from(small_file_upload_operations)
        if event_ids:
            statement = statement.where(small_file_upload_operations.c.event_id.in_(event_ids))
        async with self.engine.connect() as connection:
            count = (await connection.execute(statement)).scalar_one()
        return int(count)

    async def sources_row_count(self, source_id: UUID) -> int:
        async with self.engine.connect() as connection:
            count = (
                await connection.execute(
                    sa.select(sa.func.count())
                    .select_from(sources)
                    .where(sources.c.source_id == source_id)
                )
            ).scalar_one()
        return int(count)


@pytest_asyncio.fixture
async def small_file_harness(
    preflight_harness: PreflightHarness,
) -> SmallFileOperationHarness:
    return SmallFileOperationHarness(preflight_harness.engine)


@pytest_asyncio.fixture
async def seeded_workspace(preflight_harness: PreflightHarness) -> object:
    return await preflight_harness.seed_workspace()


def _terminal_result(
    *,
    source_id: UUID,
    result_kind: SmallFileTerminalResultKind = SmallFileTerminalResultKind.COMMITTED,
) -> SmallFileTerminalResult:
    return SmallFileTerminalResult(
        result_kind=result_kind,
        source_id=source_id,
        source_version_id=uuid4(),
        content_version=1,
        committed_at=datetime.now(UTC),
    )


async def _reserve_created_operation(
    harness: SmallFileOperationHarness, workspace: object
) -> tuple[SmallFilePreflight, object]:
    preflight = harness.preflight()
    operation = await harness.store.reserve_operation(
        preflight,
        harness.device_context(workspace),
        harness.policy_binding(harness.device_context(workspace)),
        _context(),
    )
    return preflight, operation


# --- identity uniqueness and convergence -------------------------------------------


@pytest.mark.asyncio
async def test_same_identity_reservation_converges_on_one_row(
    small_file_harness: SmallFileOperationHarness, seeded_workspace: object
) -> None:
    harness = small_file_harness
    device_context = harness.device_context(seeded_workspace)
    preflight = harness.preflight()
    context = _context()

    first = await harness.store.reserve_operation(
        preflight, device_context, harness.policy_binding(device_context), context
    )
    second = await harness.store.reserve_operation(
        preflight, device_context, harness.policy_binding(device_context), context
    )

    assert await harness.operation_row_count(preflight.event_id) == 1
    assert first.reserved_source_id == second.reserved_source_id
    assert first.reserved_source_id is not None
    assert first.expires_at == second.expires_at
    assert first.operation_token.value != second.operation_token.value

    row = await harness.operation_row(preflight.event_id)
    assert row is not None


@pytest.mark.asyncio
async def test_successful_repreflight_rotates_token_and_rebinds_server_revision(
    small_file_harness: SmallFileOperationHarness, seeded_workspace: object
) -> None:
    harness = small_file_harness
    device_context = harness.device_context(seeded_workspace)
    preflight = harness.preflight()

    first = await harness.store.reserve_operation(
        preflight, device_context, harness.policy_binding(device_context, 4), _context()
    )
    second = await harness.store.reserve_operation(
        preflight, device_context, harness.policy_binding(device_context, 5), _context()
    )

    assert await harness.operation_row_count(preflight.event_id) == 1
    assert first.operation_token.value != second.operation_token.value
    assert first.reserved_source_id == second.reserved_source_id
    row = await harness.operation_row(preflight.event_id)
    assert row is not None
    assert row["policy_revision_number"] == 5
    assert row["state"] == "pending"
    assert row["reserved_source_id"] == first.reserved_source_id


@pytest.mark.asyncio
async def test_server_revision_overrides_plugin_claim_in_row_and_receive_binding(
    small_file_harness: SmallFileOperationHarness, seeded_workspace: object
) -> None:
    harness = small_file_harness
    device_context = harness.device_context(seeded_workspace)
    preflight = harness.preflight()
    server_revision = preflight.policy_revision_number + 37
    assert preflight.policy_revision_number != server_revision

    operation = await harness.store.reserve_operation(
        preflight,
        device_context,
        harness.policy_binding(device_context, server_revision),
        _context(),
    )

    row = await harness.operation_row(preflight.event_id)
    assert row is not None
    assert row["policy_revision_number"] == server_revision
    bound = await harness.store.resolve_bound_operation(
        operation.operation_token,
        device_context,
        _context(),
    )
    assert bound.policy_revision_number == server_revision


@pytest.mark.asyncio
async def test_claimed_exact_token_is_rebound_to_fresh_locator_authority(
    small_file_harness: SmallFileOperationHarness, seeded_workspace: object
) -> None:
    """Re-preflight updates only the claimed row's revision, never its token."""

    harness = small_file_harness
    device_context = harness.device_context(seeded_workspace)
    preflight = harness.preflight()
    operation = await harness.store.reserve_operation(
        preflight, device_context, harness.policy_binding(device_context, 4), _context()
    )
    await harness.store.resolve_bound_operation(
        operation.operation_token,
        device_context,
        _context(),
    )

    with pytest.raises(SmallFileSyncError) as retry_required:
        await harness.store.reserve_operation(
            preflight, device_context, harness.policy_binding(device_context, 5), _context()
        )
    assert retry_required.value.error_code is ErrorCode.SMALL_FILE_UPLOAD_STATE_INVALID

    row = await harness.operation_row(preflight.event_id)
    assert row is not None
    assert row["state"] == "receiving"
    assert row["policy_revision_number"] == 5
    assert row["operation_token_hash"] == upload_operation_token_hash(operation.operation_token)

    rebound = await harness.store.resolve_bound_operation(
        operation.operation_token,
        device_context,
        _context(),
    )
    assert rebound.policy_revision_number == 5


@pytest.mark.asyncio
async def test_claimed_reauthorization_after_expiry_rejects_stale_terminal_writer(
    small_file_harness: SmallFileOperationHarness,
    preflight_harness: PreflightHarness,
    seeded_workspace: object,
) -> None:
    harness = small_file_harness
    device_context = harness.device_context(seeded_workspace)
    preflight = harness.preflight()

    operation = await harness.store.reserve_operation(
        preflight, device_context, harness.policy_binding(device_context, 4), _context()
    )
    bound = await harness.store.resolve_bound_operation(
        operation.operation_token,
        device_context,
        _context(),
    )

    # Deterministically cross the reservation deadline after the receive has
    # claimed its row, then model a publication result awaiting terminalization.
    harness.clock.advance(seconds=UPLOAD_OPERATION_EXPIRY_SECONDS + 1)
    assert operation.reserved_source_id is not None
    published = await preflight_harness.seed_active_source_with_version_one(
        workspace=seeded_workspace,
        source_id=operation.reserved_source_id,
        title="Claim expiry fence",
    )

    with pytest.raises(SmallFileSyncError) as rejected:
        await harness.store.reserve_operation(
            preflight,
            device_context,
            harness.policy_binding(device_context, 5),
            _context(),
        )
    assert rejected.value.error_code is ErrorCode.SMALL_FILE_UPLOAD_STATE_INVALID

    row = await harness.operation_row(preflight.event_id)
    assert row is not None
    assert row["state"] == "receiving"
    assert row["policy_revision_number"] == 5
    assert row["operation_token_hash"] == upload_operation_token_hash(operation.operation_token)

    resumed = await harness.store.resolve_bound_operation(
        operation.operation_token,
        device_context,
        _context(),
    )
    assert resumed.policy_revision_number == 5

    terminal = SmallFileTerminalResult(
        result_kind=SmallFileTerminalResultKind.COMMITTED,
        source_id=operation.reserved_source_id,
        source_version_id=published.source_version_id,
        content_version=published.content_version,
        committed_at=datetime.now(UTC),
    )
    with pytest.raises(SmallFileSyncError) as stale_writer:
        await harness.store.record_bound_terminal_result(bound, terminal, _context())
    assert stale_writer.value.error_code is ErrorCode.SMALL_FILE_OPERATION_IDENTITY_MISMATCH

    await harness.store.record_bound_terminal_result(resumed, terminal, _context())

    committed = await harness.operation_row(preflight.event_id)
    assert committed is not None
    assert committed["state"] == "committed"
    assert committed["result_kind"] == SmallFileTerminalResultKind.COMMITTED.value
    assert committed["result_source_id"] == operation.reserved_source_id
    assert await harness.sources_row_count(operation.reserved_source_id) == 1


@pytest.mark.asyncio
async def test_concurrent_preflights_yield_exactly_one_operation_row(
    small_file_harness: SmallFileOperationHarness, seeded_workspace: object
) -> None:
    harness = small_file_harness
    device_context = harness.device_context(seeded_workspace)
    preflight = harness.preflight()
    context = _context()

    first, second = await asyncio.gather(
        harness.store.reserve_operation(
            preflight, device_context, harness.policy_binding(device_context), context
        ),
        harness.store.reserve_operation(
            preflight, device_context, harness.policy_binding(device_context), context
        ),
    )

    assert await harness.operation_row_count(preflight.event_id) == 1
    assert first.reserved_source_id == second.reserved_source_id


@pytest.mark.asyncio
async def test_distinct_idempotency_keys_allocate_distinct_operations(
    small_file_harness: SmallFileOperationHarness, seeded_workspace: object
) -> None:
    harness = small_file_harness
    device_context = harness.device_context(seeded_workspace)
    first_preflight = harness.preflight()
    second_preflight = harness.preflight()
    await harness.store.reserve_operation(
        first_preflight, device_context, harness.policy_binding(device_context), _context()
    )
    await harness.store.reserve_operation(
        second_preflight, device_context, harness.policy_binding(device_context), _context()
    )
    assert (
        await harness.operation_row_count(first_preflight.event_id, second_preflight.event_id) == 2
    )


@pytest.mark.asyncio
async def test_database_rejects_a_duplicate_identity_row(
    small_file_harness: SmallFileOperationHarness, seeded_workspace: object
) -> None:
    harness = small_file_harness
    device_context = harness.device_context(seeded_workspace)
    preflight = harness.preflight()
    await harness.store.reserve_operation(
        preflight, device_context, harness.policy_binding(device_context), _context()
    )

    with pytest.raises(sa.exc.IntegrityError) as outcome:
        async with harness.engine.begin() as connection:
            await connection.execute(
                sa.insert(small_file_upload_operations).values(
                    operation_id=uuid4(),
                    operation_token_hash=hashlib.sha256(b"duplicate").hexdigest(),
                    workspace_id=device_context.workspace_id,
                    device_id=device_context.device_id,
                    event_id=preflight.event_id,
                    idempotency_key=preflight.idempotency_key.value,
                    operation_kind="create",
                    declared_sha256=preflight.sha256.hexadecimal,
                    declared_size_bytes=preflight.size_bytes,
                    declared_media_type=preflight.media_type.value,
                    policy_revision_number=preflight.policy_revision_number,
                    expires_at=sa.text("CURRENT_TIMESTAMP + interval '15 minutes'"),
                )
            )
    assert outcome.value.orig.diag.constraint_name == "uq_small_file_upload_operations__identity"


# --- payload substitution -----------------------------------------------------------


@pytest.mark.asyncio
async def test_same_identity_with_a_different_payload_is_rejected(
    small_file_harness: SmallFileOperationHarness, seeded_workspace: object
) -> None:
    harness = small_file_harness
    device_context = harness.device_context(seeded_workspace)
    preflight = harness.preflight()
    await harness.store.reserve_operation(
        preflight, device_context, harness.policy_binding(device_context), _context()
    )

    substituted = harness.preflight(
        sha256=_DIGEST_B,
        event_id=preflight.event_id,
        idempotency_key=preflight.idempotency_key,
    )
    with pytest.raises(SmallFileSyncError) as rejected:
        await harness.store.reserve_operation(
            substituted, device_context, harness.policy_binding(device_context), _context()
        )
    assert rejected.value.error_code is ErrorCode.SMALL_FILE_OPERATION_IDENTITY_MISMATCH

    with pytest.raises(SmallFileSyncError) as replay_rejected:
        await harness.store.resolve_terminal_result(substituted, device_context, _context())
    assert replay_rejected.value.error_code is ErrorCode.SMALL_FILE_OPERATION_IDENTITY_MISMATCH


# --- create reservation never inserts a sources row ---------------------------------


@pytest.mark.asyncio
async def test_create_reservation_inserts_no_sources_row(
    small_file_harness: SmallFileOperationHarness, seeded_workspace: object
) -> None:
    harness = small_file_harness
    _, operation = await _reserve_created_operation(harness, seeded_workspace)

    assert operation.reserved_source_id is not None
    assert await harness.sources_row_count(operation.reserved_source_id) == 0


@pytest.mark.asyncio
async def test_create_reservation_persists_initial_locator_evidence(
    small_file_harness: SmallFileOperationHarness, seeded_workspace: object
) -> None:
    """A create binds the preflight locator to the row and its retained digest."""

    harness = small_file_harness
    preflight, operation = await _reserve_created_operation(harness, seeded_workspace)
    digest = compute_locator_fingerprint(preflight.normalized_locator)

    row = await harness.operation_row(preflight.event_id)
    assert row is not None
    assert row["normalized_locator"] == preflight.normalized_locator.value
    assert row["locator_fingerprint"] == digest
    assert operation.reserved_source_id is not None


@pytest.mark.asyncio
async def test_update_reservation_records_no_locator_evidence(
    small_file_harness: SmallFileOperationHarness, seeded_workspace: object
) -> None:
    """Update rows keep the raw locator NULL and persist only its digest.

    An update's locator is preflight policy evidence only: the reservation
    persists just the one-way digest — the declared locator's replay identity —
    and binds the raw column to NULL (the documented update-retry contract in
    ``small_file_sync_operations.py``).
    """

    harness = small_file_harness
    source_id = uuid4()
    base_version_id = uuid4()
    preflight = harness.preflight(
        operation=SmallFileOperation.UPDATE, source_id=source_id, base_version_id=base_version_id
    )
    await harness.store.reserve_operation(
        preflight,
        harness.device_context(seeded_workspace),
        harness.policy_binding(harness.device_context(seeded_workspace)),
        _context(),
    )

    row = await harness.operation_row(preflight.event_id)
    assert row is not None
    assert row["normalized_locator"] is None
    assert row["locator_fingerprint"] == compute_locator_fingerprint(preflight.normalized_locator)


@pytest.mark.asyncio
async def test_terminal_transition_clears_raw_locator_and_keeps_digest(
    small_file_harness: SmallFileOperationHarness, seeded_workspace: object
) -> None:
    """The terminal state retains the digest while nulling the raw locator."""

    harness = small_file_harness
    preflight, operation = await _reserve_created_operation(harness, seeded_workspace)
    assert operation.reserved_source_id is not None
    result = _terminal_result(source_id=operation.reserved_source_id)
    await harness.store.record_terminal_result(operation, result, _context())

    row = await harness.operation_row(preflight.event_id)
    assert row is not None
    assert row["state"] == "committed"
    assert row["normalized_locator"] is None
    assert row["locator_fingerprint"] == compute_locator_fingerprint(preflight.normalized_locator)


@pytest.mark.asyncio
async def test_bound_terminal_transition_clears_raw_locator_and_keeps_digest(
    small_file_harness: SmallFileOperationHarness, seeded_workspace: object
) -> None:
    """A claimed receive's terminal state retains the digest, not the locator."""

    harness = small_file_harness
    device_context = harness.device_context(seeded_workspace)
    preflight, operation = await _reserve_created_operation(harness, seeded_workspace)
    assert operation.reserved_source_id is not None
    bound = await harness.store.resolve_bound_operation(
        operation.operation_token, device_context, _context()
    )
    result = _terminal_result(source_id=operation.reserved_source_id)
    await harness.store.record_bound_terminal_result(bound, result, _context())

    row = await harness.operation_row(preflight.event_id)
    assert row is not None
    assert row["state"] == "committed"
    assert row["normalized_locator"] is None
    assert row["locator_fingerprint"] == compute_locator_fingerprint(preflight.normalized_locator)


@pytest.mark.asyncio
async def test_bound_terminal_failure_clears_raw_locator_and_keeps_digest(
    small_file_harness: SmallFileOperationHarness, seeded_workspace: object
) -> None:
    """A claimed receive's typed terminal failure retains the digest, not the
    raw locator — the failure path clears the note path exactly like the
    success path."""

    harness = small_file_harness
    device_context = harness.device_context(seeded_workspace)
    preflight, operation = await _reserve_created_operation(harness, seeded_workspace)
    bound = await harness.store.resolve_bound_operation(
        operation.operation_token, device_context, _context()
    )
    await harness.store.record_bound_terminal_failure(
        bound, ErrorCode.SOURCE_LOCATOR_CONFLICT, _context()
    )

    row = await harness.operation_row(preflight.event_id)
    assert row is not None
    assert row["state"] == "failed"
    assert row["normalized_locator"] is None
    assert row["locator_fingerprint"] == compute_locator_fingerprint(preflight.normalized_locator)


@pytest.mark.asyncio
async def test_pre_migration_null_locator_rows_remain_readable(
    small_file_harness: SmallFileOperationHarness, seeded_workspace: object
) -> None:
    """A pre-migration row with NULL locator columns reads back without error."""

    harness = small_file_harness
    preflight, operation = await _reserve_created_operation(harness, seeded_workspace)
    assert operation.reserved_source_id is not None

    # Null out the locator columns as a pre-migration row would carry them.
    async with harness.engine.begin() as connection:
        await connection.execute(
            sa.update(small_file_upload_operations)
            .values(normalized_locator=None, locator_fingerprint=None)
            .where(small_file_upload_operations.c.event_id == preflight.event_id)
        )

    row = await harness.operation_row(preflight.event_id)
    assert row is not None
    assert row["normalized_locator"] is None
    assert row["locator_fingerprint"] is None

    # The bound operation still resolves: a pre-migration row hydrates without
    # the locator or its digest, so the receive binding carries the canonical
    # post-terminal shape — both locator fields are null, every immutable
    # identity field stays populated so the row stays readable for replay.
    # The durable operation identity lives on the row and the bound operation
    # (``SmallFileUploadOperation`` itself carries only the opaque token).
    bound = await harness.store.resolve_bound_operation(
        operation.operation_token,
        harness.device_context(seeded_workspace),
        _context(),
    )
    assert bound.normalized_locator is None
    assert bound.locator_fingerprint is None
    assert bound.operation_id == row["operation_id"]
    assert bound.workspace_id == harness.device_context(seeded_workspace).workspace_id
    assert bound.device_id == harness.device_context(seeded_workspace).device_id
    assert bound.event_id == preflight.event_id
    assert bound.idempotency_key == SmallFileIdempotencyKey(preflight.idempotency_key.value)
    assert bound.operation is SmallFileOperation.CREATE
    assert bound.declared_sha256 == preflight.sha256
    assert bound.declared_size_bytes == preflight.size_bytes
    assert bound.declared_media_type == preflight.media_type
    assert bound.policy_revision_number == preflight.policy_revision_number
    assert bound.reserved_source_id == operation.reserved_source_id
    assert bound.terminal_result is None


@pytest.mark.asyncio
async def test_update_reservation_records_the_update_base_and_reserves_nothing(
    small_file_harness: SmallFileOperationHarness, seeded_workspace: object
) -> None:
    harness = small_file_harness
    source_id = uuid4()
    base_version_id = uuid4()
    preflight = harness.preflight(
        operation=SmallFileOperation.UPDATE, source_id=source_id, base_version_id=base_version_id
    )
    operation = await harness.store.reserve_operation(
        preflight,
        harness.device_context(seeded_workspace),
        harness.policy_binding(harness.device_context(seeded_workspace)),
        _context(),
    )
    assert operation.reserved_source_id is None

    row = await harness.operation_row(preflight.event_id)
    assert row is not None
    assert row["reserved_source_id"] is None
    assert row["update_source_id"] == source_id
    assert row["update_base_version_id"] == base_version_id


# --- expiry --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expired_non_terminal_operation_cannot_be_continued(
    small_file_harness: SmallFileOperationHarness, seeded_workspace: object
) -> None:
    harness = small_file_harness
    preflight, operation = await _reserve_created_operation(harness, seeded_workspace)
    harness.clock.advance(seconds=UPLOAD_OPERATION_EXPIRY_SECONDS + 1)
    device_context = harness.device_context(seeded_workspace)

    result = await harness.store.resolve_terminal_result(preflight, device_context, _context())
    assert result is None

    # Continuation under the reserved token stays refused while the row is
    # expired: the terminal write must never commit past the deadline.
    with pytest.raises(SmallFileSyncError) as terminal_refused:
        await harness.store.record_terminal_result(
            operation, _terminal_result(source_id=uuid4()), _context()
        )
    assert terminal_refused.value.error_code is ErrorCode.SMALL_FILE_OPERATION_EXPIRED

    with pytest.raises(SmallFileSyncError) as bound_refused:
        await harness.store.resolve_bound_operation(
            operation.operation_token, device_context, _context()
        )
    assert bound_refused.value.error_code is ErrorCode.SMALL_FILE_OPERATION_EXPIRED

    row = await harness.operation_row(preflight.event_id)
    assert row is not None
    assert row["state"] == "pending"


@pytest.mark.asyncio
async def test_expired_pending_re_preflight_re_reserves_the_same_row(
    small_file_harness: SmallFileOperationHarness, seeded_workspace: object
) -> None:
    """Resume after expiry: the wedged identity re-reserves instead of 410ing.

    A preflight succeeds, the device suspends past the fifteen-minute
    deadline before the upload completes, and the resume pass re-preflights
    the exact same identity: the existing row is re-reserved — a fresh token,
    an extended deadline, the reserved create UUID unchanged — so the journal
    event can still complete. Nothing was committed for the non-terminal
    row, so re-reservation cannot double-publish; the old token stays dead.
    """

    harness = small_file_harness
    preflight, expired_operation = await _reserve_created_operation(harness, seeded_workspace)
    harness.clock.advance(seconds=UPLOAD_OPERATION_EXPIRY_SECONDS + 1)
    device_context = harness.device_context(seeded_workspace)
    assert expired_operation.reserved_source_id is not None

    re_reserved = await harness.store.reserve_operation(
        preflight, device_context, harness.policy_binding(device_context), _context()
    )

    assert re_reserved.operation_token.value != expired_operation.operation_token.value
    assert re_reserved.reserved_source_id == expired_operation.reserved_source_id
    expected_deadline = harness.clock() + timedelta(seconds=UPLOAD_OPERATION_EXPIRY_SECONDS)
    assert re_reserved.expires_at == expected_deadline
    assert re_reserved.expires_at > expired_operation.expires_at

    # One identity still owns exactly one row; the re-reservation rotated the
    # stored token hash to the fresh token and re-armed the deadline.
    row = await harness.operation_row(preflight.event_id)
    assert row is not None
    assert await harness.operation_row_count(preflight.event_id) == 1
    assert row["state"] == "pending"
    assert row["reserved_source_id"] == expired_operation.reserved_source_id
    assert row["operation_token_hash"] == upload_operation_token_hash(re_reserved.operation_token)
    assert row["expires_at"] == expected_deadline
    assert await harness.sources_row_count(expired_operation.reserved_source_id) == 0

    # The pre-expiry token is dead: its hash was rotated away, so neither the
    # receive binding nor the terminal write can ever continue it again.
    with pytest.raises(SmallFileSyncError) as bound_dead:
        await harness.store.resolve_bound_operation(
            expired_operation.operation_token, device_context, _context()
        )
    assert bound_dead.value.error_code is ErrorCode.SMALL_FILE_OPERATION_NOT_FOUND

    with pytest.raises(SmallFileSyncError) as terminal_dead:
        await harness.store.record_terminal_result(
            expired_operation, _terminal_result(source_id=uuid4()), _context()
        )
    assert terminal_dead.value.error_code is ErrorCode.SMALL_FILE_OPERATION_NOT_FOUND


@pytest.mark.asyncio
async def test_terminal_results_survive_expiry_for_exact_replay(
    small_file_harness: SmallFileOperationHarness, seeded_workspace: object
) -> None:
    harness = small_file_harness
    device_context = harness.device_context(seeded_workspace)
    preflight, operation = await _reserve_created_operation(harness, seeded_workspace)
    assert operation.reserved_source_id is not None
    result = _terminal_result(source_id=operation.reserved_source_id)
    await harness.store.record_terminal_result(operation, result, _context())

    harness.clock.advance(seconds=UPLOAD_OPERATION_EXPIRY_SECONDS * 10)

    replayed = await harness.store.resolve_terminal_result(preflight, device_context, _context())
    assert replayed is not None
    assert replayed == result


# --- exact replay after a lost response ----------------------------------------------


@pytest.mark.asyncio
async def test_replay_after_commit_returns_the_frozen_terminal_result(
    small_file_harness: SmallFileOperationHarness, seeded_workspace: object
) -> None:
    harness = small_file_harness
    device_context = harness.device_context(seeded_workspace)
    preflight, operation = await _reserve_created_operation(harness, seeded_workspace)
    assert operation.reserved_source_id is not None
    result = _terminal_result(source_id=operation.reserved_source_id)
    await harness.store.record_terminal_result(operation, result, _context())

    replayed = await harness.store.resolve_terminal_result(preflight, device_context, _context())
    assert replayed == result

    # A terminal operation accepts no further reservation or duplicate write.
    with pytest.raises(SmallFileSyncError) as reserved:
        await harness.store.reserve_operation(
            preflight, device_context, harness.policy_binding(device_context), _context()
        )
    assert reserved.value.error_code is ErrorCode.SMALL_FILE_UPLOAD_STATE_INVALID

    await harness.store.record_terminal_result(operation, result, _context())
    row = await harness.operation_row(preflight.event_id)
    assert row is not None
    assert row["state"] == "committed"
    assert row["result_kind"] == SmallFileTerminalResultKind.COMMITTED.value
    assert row["result_source_id"] == operation.reserved_source_id
    assert row["result_content_version"] == 1
    assert await harness.operation_row_count(preflight.event_id) == 1


@pytest.mark.asyncio
async def test_terminal_write_is_atomic_with_the_publication_seam(
    small_file_harness: SmallFileOperationHarness, seeded_workspace: object
) -> None:
    """The transactional seam persists result and terminal state in one commit.

    A failure inside the surrounding publication work must leave the
    operation row exactly as it was: the terminal update and the caller's
    writes commit or roll back together.
    """
    harness = small_file_harness
    preflight, operation = await _reserve_created_operation(harness, seeded_workspace)
    assert operation.reserved_source_id is not None
    result = _terminal_result(source_id=operation.reserved_source_id)

    class DeliberateFailure(Exception):
        pass

    with pytest.raises(DeliberateFailure):
        async with harness.engine.connect() as connection:
            async with connection.begin():
                await harness.store.record_terminal_result_in_transaction(
                    connection, operation, result, _context()
                )
                # Read the terminal state back through the same uncommitted
                # transaction: the seam must have applied it in-transaction.
                in_transaction_state = (
                    await connection.execute(
                        sa.select(small_file_upload_operations.c.state).where(
                            small_file_upload_operations.c.event_id == preflight.event_id
                        )
                    )
                ).scalar_one()
                assert in_transaction_state == "committed"
                raise DeliberateFailure

    row = await harness.operation_row(preflight.event_id)
    assert row is not None
    assert row["state"] == "pending"
    assert row["result_kind"] is None


@pytest.mark.asyncio
async def test_reauthorization_winner_fences_stale_bound_before_canonical_mutation(
    small_file_harness: SmallFileOperationHarness, seeded_workspace: object
) -> None:
    """A fresh locator decision wins before the old publisher can mutate."""

    harness = small_file_harness
    device_context = harness.device_context(seeded_workspace)
    preflight = harness.preflight()
    operation = await harness.store.reserve_operation(
        preflight, device_context, harness.policy_binding(device_context, 4), _context()
    )
    stale_bound = await harness.store.resolve_bound_operation(
        operation.operation_token, device_context, _context()
    )
    with pytest.raises(SmallFileSyncError):
        await harness.store.reserve_operation(
            preflight, device_context, harness.policy_binding(device_context, 5), _context()
        )

    assert operation.reserved_source_id is not None
    with pytest.raises(SmallFileSyncError) as fenced:
        async with harness.engine.begin() as connection:
            await harness.store.acquire_bound_publication_fence_in_transaction(
                connection, stale_bound
            )
            await connection.execute(
                sa.insert(sources).values(
                    source_id=operation.reserved_source_id,
                    workspace_id=device_context.workspace_id,
                    source_type="markdown",
                    title="Stale publisher must not commit",
                )
            )
    assert fenced.value.error_code is ErrorCode.SMALL_FILE_OPERATION_IDENTITY_MISMATCH
    assert await harness.sources_row_count(operation.reserved_source_id) == 0


@pytest.mark.asyncio
async def test_publication_winner_commits_canonical_and_terminal_before_repreflight(
    small_file_harness: SmallFileOperationHarness, seeded_workspace: object
) -> None:
    """The operation fence serializes reauthorization after atomic commit."""

    harness = small_file_harness
    device_context = harness.device_context(seeded_workspace)
    preflight = harness.preflight()
    operation = await harness.store.reserve_operation(
        preflight, device_context, harness.policy_binding(device_context, 4), _context()
    )
    bound = await harness.store.resolve_bound_operation(
        operation.operation_token, device_context, _context()
    )
    assert operation.reserved_source_id is not None
    terminal = _terminal_result(source_id=operation.reserved_source_id)

    async def reauthorize() -> None:
        with pytest.raises(SmallFileSyncError) as blocked:
            await harness.store.reserve_operation(
                preflight, device_context, harness.policy_binding(device_context, 5), _context()
            )
        assert blocked.value.error_code is ErrorCode.SMALL_FILE_UPLOAD_STATE_INVALID

    async with harness.engine.begin() as connection:
        await harness.store.acquire_bound_publication_fence_in_transaction(connection, bound)
        reauthorization = asyncio.create_task(reauthorize())
        await connection.execute(
            sa.insert(sources).values(
                source_id=operation.reserved_source_id,
                workspace_id=device_context.workspace_id,
                source_type="markdown",
                title="Atomic publication winner",
            )
        )
        await harness.store.record_bound_terminal_result_in_transaction(connection, bound, terminal)

    await reauthorization
    assert await harness.sources_row_count(operation.reserved_source_id) == 1
    assert await harness.store.resolve_terminal_result(preflight, device_context, _context()) == (
        terminal
    )
    row = await harness.operation_row(preflight.event_id)
    assert row is not None
    assert row["state"] == "committed"
    assert row["policy_revision_number"] == 4


# --- migration downgrade ---------------------------------------------------------------


def _sanitized_environment(stack: SourcePublicationStack) -> dict[str, str]:
    environment = dict(os.environ)
    for inherited_key in [key for key in environment if key.startswith("KNOWLEDGE_")]:
        del environment[inherited_key]
    environment.update(
        {
            "KNOWLEDGE_ENVIRONMENT": "test",
            "KNOWLEDGE_SECRET_ROOT": str((_WORKTREE_ROOT / ".local" / "stack-secrets").resolve()),
            "KNOWLEDGE_DATABASE_HOST": "127.0.0.1",
            "KNOWLEDGE_DATABASE_PORT": str(stack.port),
            "KNOWLEDGE_DATABASE_NAME": "knowledge",
            "KNOWLEDGE_DATABASE_USER": "knowledge_app",
            "KNOWLEDGE_DATABASE_PASSWORD_FILE": "postgres_application_password",
            "KNOWLEDGE_DATABASE_SSL_MODE": "disable",
        }
    )
    return environment


def _run_guarded_alembic(
    stack: SourcePublicationStack, *arguments: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["uv", "run", "alembic", *arguments],
        cwd=str(_WORKTREE_ROOT),
        env=_sanitized_environment(stack),
        capture_output=True,
        text=True,
        check=False,
    )


def run_inprocess_alembic_downgrade(
    stack: SourcePublicationStack, *, destructive: bool, target: str = "20260817_01"
) -> None:
    """Run ``alembic downgrade`` in-process against the disposable stack.

    The in-process command path leaves ``Config.cmd_opts`` unset unless
    ``destructive`` is requested, so ``migrations/env.py`` skips its own CLI
    downgrade gate and the small-file migration's own row-level gate decides.
    ``target`` names the revision the walk stops at.
    """
    from argparse import Namespace

    from alembic import command
    from alembic.config import Config

    configuration = Config(str(_WORKTREE_ROOT / "alembic.ini"))
    configuration.set_main_option("script_location", str(_WORKTREE_ROOT / "migrations"))
    if destructive:
        configuration.cmd_opts = Namespace(x=["allow_destructive=true"])  # type: ignore[assignment]
    environment = _sanitized_environment(stack)
    saved: dict[str, str | None] = {}
    try:
        for key, value in environment.items():
            saved[key] = os.environ.get(key)
            os.environ[key] = value
        command.downgrade(configuration, target)
    finally:
        for key, original in saved.items():
            if original is None:
                del os.environ[key]
            else:
                os.environ[key] = original


async def _schema_head(harness: SmallFileOperationHarness) -> str:
    async with harness.engine.connect() as connection:
        head = (
            await connection.execute(sa.text("SELECT version_num FROM public.alembic_version"))
        ).scalar_one()
    return str(head)


def _chain_head() -> str:
    """The current single Alembic graph head (the re-upgrade target)."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(Config(str(_WORKTREE_ROOT / "alembic.ini")))
    heads = script.get_heads()
    assert len(heads) == 1, heads
    return str(heads[0])


#: The locator-lifecycle revision: the highest revision whose downgrade
#: discards small-file evidence (it drops the operation table's locator
#: columns). A downgrade refused by the evidence gate must stop here, with
#: that revision's destructive work never applied — never half-way through
#: it — so the operation rows and their locator columns stay intact.
_SMALL_FILE_HEAD = "20260820_01"

#: The remediation revision ``20260901_03``: the highest revision before the
#: one-shot terminal-locator data remediation, and the walk target its
#: downgrade/re-upgrade cycle stops at.
_PRE_REMEDIATION_HEAD = "20260901_02"


def _terminal_locator_clear_sql() -> str:
    """The exact guarded UPDATE the remediation revision ships.

    Read straight from the migration module (its filename starts with a
    digit, so it cannot be imported by name) so the idempotency proof
    replays the shipped statement, not a restated copy of it.
    """
    import importlib.util

    migration_path = (
        _WORKTREE_ROOT
        / "migrations"
        / "versions"
        / ("20260901_03_clear_terminal_small_file_locators.py")
    )
    assert migration_path.is_file(), f"missing remediation migration: {migration_path.name}"
    spec = importlib.util.spec_from_file_location("terminal_locator_remediation", migration_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return str(module.TERMINAL_LOCATOR_CLEAR_SQL)


async def _seed_pre_fix_locator_rows(
    harness: SmallFileOperationHarness, workspace: object
) -> tuple[dict[str, UUID], str, str]:
    """Seed one operation row per state, every one still carrying the raw locator.

    The rows emulate the historical pre-fix terminal shape — ``committed``
    and ``failed`` rows that reached their terminal state before the
    clear-on-terminal writers landed and so still hold the note path next to
    the retained digest. The runtime can no longer produce that shape, so
    the rows are written directly.
    """
    device_context = harness.device_context(workspace)
    locator = "notes/remediation/before-terminal-clear-fix.md"
    fingerprint = compute_locator_fingerprint(NormalizedLocator(locator))
    now = datetime.now(UTC)
    event_ids = {state: uuid4() for state in ("pending", "receiving", "committed", "failed")}
    rows: list[dict[str, object]] = []
    for state, event_id in event_ids.items():
        # One uniform key set across every row: SQLAlchemy compiles the
        # executemany INSERT from the first row's keys, so a key carried only
        # by the committed row would be silently dropped and the row would
        # violate the terminal-shape CHECK.
        frozen_result = (
            {
                "result_kind": "committed",
                "result_source_id": uuid4(),
                "result_source_version_id": uuid4(),
                "result_content_version": 1,
                "result_committed_at": now,
            }
            if state == "committed"
            else {
                "result_kind": None,
                "result_source_id": None,
                "result_source_version_id": None,
                "result_content_version": None,
                "result_committed_at": None,
            }
        )
        rows.append(
            {
                "operation_id": uuid4(),
                "operation_token_hash": hashlib.sha256(
                    f"terminal-locator-remediation-{state}".encode("ascii")
                ).hexdigest(),
                "workspace_id": device_context.workspace_id,
                "device_id": device_context.device_id,
                "event_id": event_id,
                "idempotency_key": str(uuid4()),
                "operation_kind": "create",
                "declared_sha256": hashlib.sha256(f"declared-{state}".encode("ascii")).hexdigest(),
                "declared_size_bytes": 128,
                "declared_media_type": "text/markdown",
                "policy_revision_number": _POLICY_REVISION_NUMBER,
                "state": state,
                "safe_error_code": "source_locator_conflict" if state == "failed" else None,
                "normalized_locator": locator,
                "locator_fingerprint": fingerprint,
                "expires_at": now + timedelta(hours=1),
                **frozen_result,
            }
        )
    async with harness.engine.begin() as connection:
        await connection.execute(sa.insert(small_file_upload_operations), rows)
    return event_ids, locator, fingerprint


@pytest.mark.asyncio
async def test_terminal_locator_remediation_clears_historical_terminal_locators(
    small_file_harness: SmallFileOperationHarness,
    seeded_workspace: object,
    source_publication_stack: SourcePublicationStack,
) -> None:
    """The one-shot data remediation brings pre-fix terminal rows to the
    now-standard NULL-locator shape while keeping the replay digest."""

    harness = small_file_harness
    event_ids, locator, fingerprint = await _seed_pre_fix_locator_rows(harness, seeded_workspace)

    # Walk exactly the remediation revision down: its downgrade is the
    # documented privacy no-op, so every seeded row must survive it with the
    # raw locator still in place — the walk proves the no-op instead of
    # assuming it.
    run_inprocess_alembic_downgrade(
        source_publication_stack, destructive=False, target=_PRE_REMEDIATION_HEAD
    )
    assert await _schema_head(harness) == _PRE_REMEDIATION_HEAD
    for state, event_id in event_ids.items():
        row = await harness.operation_row(event_id)
        assert row is not None, state
        assert row["state"] == state
        assert row["normalized_locator"] == locator, state
        assert row["locator_fingerprint"] == fingerprint, state

    # Re-apply only the remediation revision through the guarded CLI path.
    reupgrade = _run_guarded_alembic(source_publication_stack, "upgrade", "head")
    assert reupgrade.returncode == 0, reupgrade.stderr
    assert await _schema_head(harness) == _chain_head()

    # Terminal rows lose the raw note path and keep the digest; non-terminal
    # rows keep both — the migration must not touch live negotiations.
    expectations = {
        "pending": locator,
        "receiving": locator,
        "committed": None,
        "failed": None,
    }
    for state, expected_locator in expectations.items():
        row = await harness.operation_row(event_ids[state])
        assert row is not None, state
        assert row["state"] == state
        assert row["normalized_locator"] == expected_locator, state
        assert row["locator_fingerprint"] == fingerprint, state

    # Idempotency: the shipped guarded UPDATE matches zero rows once the
    # terminal locators are already cleared, so a replayed remediation is a
    # no-op rather than an error or a repeated mutation.
    async with harness.engine.begin() as connection:
        replay = await connection.execute(sa.text(_terminal_locator_clear_sql()))
    assert replay.rowcount == 0
    for state, expected_locator in expectations.items():
        row = await harness.operation_row(event_ids[state])
        assert row is not None, state
        assert row["normalized_locator"] == expected_locator, state
        assert row["locator_fingerprint"] == fingerprint, state


@pytest.mark.asyncio
async def test_downgrade_refuses_to_discard_operation_evidence(
    small_file_harness: SmallFileOperationHarness,
    seeded_workspace: object,
    source_publication_stack: SourcePublicationStack,
) -> None:
    preflight, _ = await _reserve_created_operation(small_file_harness, seeded_workspace)

    # In-process downgrade leaves the environment-level CLI gate aside, so
    # only the migration's own row-level gate decides: operation rows exist
    # and no destructive argument is present, so the downgrade must refuse.
    with pytest.raises(CommandError):
        run_inprocess_alembic_downgrade(source_publication_stack, destructive=False)

    # A refused downgrade must leave NO half-applied small-file evidence
    # schema behind: the walk stops at 20260820_01 — the revision whose
    # downgrade drops the locator columns — with those columns and the
    # operation rows still intact (BACKLOG 2026-08-30, migrations/small-file).
    assert await _schema_head(small_file_harness) == _SMALL_FILE_HEAD
    async with small_file_harness.engine.connect() as connection:
        columns = (
            (
                await connection.execute(
                    sa.text(
                        "SELECT column_name FROM information_schema.columns"
                        " WHERE table_schema = 'knowledge'"
                        " AND table_name = 'small_file_upload_operations'"
                        " AND column_name IN ('normalized_locator', 'locator_fingerprint')"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert set(columns) == {"normalized_locator", "locator_fingerprint"}
    assert await small_file_harness.operation_row_count(preflight.event_id) == 1


@pytest.mark.asyncio
async def test_gated_downgrade_drops_the_operation_table_and_reapplies_head(
    small_file_harness: SmallFileOperationHarness,
    seeded_workspace: object,
    source_publication_stack: SourcePublicationStack,
) -> None:
    """Prove the deterministic destructive downgrade under the open gate.

    This test intentionally runs last: under the explicit destructive gate the
    migration drops the ``small_file_upload_operations`` table and returns the
    schema exactly to the exclusion policy head, then re-applies the full
    chain head so the stack teardown observes the latest schema.
    """
    stack = source_publication_stack
    await _reserve_created_operation(small_file_harness, seeded_workspace)

    run_inprocess_alembic_downgrade(stack, destructive=True)

    assert await _schema_head(small_file_harness) == "20260817_01"
    async with small_file_harness.engine.connect() as connection:
        exists = (
            await connection.execute(
                sa.text(
                    "SELECT count(*) FROM information_schema.tables"
                    " WHERE table_schema = 'knowledge'"
                    " AND table_name = 'small_file_upload_operations'"
                )
            )
        ).scalar_one()
    assert int(exists) == 0

    reupgrade = _run_guarded_alembic(stack, "upgrade", "head")
    assert reupgrade.returncode == 0, reupgrade.stderr
    # ``upgrade head`` reaches the current graph head (revisions stack on the
    # small-file head), not the small-file revision itself.
    assert await _schema_head(small_file_harness) == _chain_head()
