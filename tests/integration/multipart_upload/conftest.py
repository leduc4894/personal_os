"""Disposable PostgreSQL integration fixtures for multipart session state.

The conftest reuses the canonical module-scoped
``source_publication_stack`` fixture: it provisions the disposable
``knowledge-ci-*`` project, applies the real Alembic baseline through the
current head and tears the project down afterwards. On top of it this
module seeds the identity graph and the frozen small-file upload-operation
row every multipart session reservation binds to, and builds the real
async engine plus the durable
:class:`PostgresqlMultipartUploadStore` behind a mutable aware-UTC clock so
lease expiry and the 24-hour deadline are observable without sleeping. No
provider adapter is ever composed here: the session store owns no provider
I/O, and every staging identity is an inert seeded string.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any, Final
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.source_publication.conftest import (
    SourcePublicationStack,
    source_publication_stack,
)

from personal_os.diagnostics.context import DiagnosticContext, TraceContext
from personal_os.diagnostics.trace_context import SpanId, TraceId
from personal_os.multipart_upload.contracts import MultipartUploadSessionId
from personal_os.multipart_upload.ports import (
    MultipartProviderUploadId,
    MultipartSessionRecord,
)
from personal_os.object_storage import CanonicalMediaType, ContentDigest
from personal_os.small_file_sync.contracts import (
    NormalizedLocator,
    SmallFileDeviceContext,
    SmallFileIdempotencyKey,
    SmallFileOperation,
    SmallFilePreflight,
    SmallFileUploadOperation,
    compute_locator_fingerprint,
)
from postgresql_source_store.engine import (
    create_source_store_engine,
    dispose_source_store_engine,
)
from postgresql_source_store.multipart_upload_store import PostgresqlMultipartUploadStore
from postgresql_source_store.small_file_sync_operations import (
    UPLOAD_OPERATION_EXPIRY_SECONDS,
    mint_upload_operation_token,
    upload_operation_token_hash,
)
from postgresql_source_store.tables import (
    devices,
    multipart_uploads,
    small_file_upload_operations,
    users,
    workspaces,
)

__all__ = [
    "MultipartStoreHarness",
    "MutableUtcClock",
    "SeededMultipartOperation",
    "multipart_store_harness",
    "source_publication_stack",
]

#: The seeded multipart transfer geometry: 20 MiB over three parts
#: (8 MiB, 8 MiB, 4 MiB) inside the closed routing range.
SEEDED_MULTIPART_SIZE_BYTES: Final[int] = 20 * 1024 * 1024
SEEDED_MULTIPART_PART_COUNT: Final[int] = 3
SEEDED_MULTIPART_FINAL_PART_BYTES: Final[int] = 4 * 1024 * 1024

_TRACE = TraceContext(
    trace_id=TraceId("0123456789abcdef0123456789abcdef"),
    remote_parent_span_id=None,
    local_span_id=SpanId("0123456789abcdef"),
    trace_flags=0,
)


def diagnostic_context() -> DiagnosticContext:
    """One fresh correlation context for a store call."""

    return DiagnosticContext(request_id=uuid4(), client_request_id=None, trace=_TRACE)


class MutableUtcClock:
    """Injectable aware-UTC clock whose moment tests advance deterministically."""

    def __init__(self, now: datetime | None = None) -> None:
        self.now = now if now is not None else datetime(2026, 8, 28, 12, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now = self.now + delta


@dataclass(frozen=True, slots=True)
class SeededMultipartOperation:
    """One frozen pending upload operation and its domain binding."""

    operation: SmallFileUploadOperation
    staging_key: str
    provider_upload_id: MultipartProviderUploadId


class MultipartStoreHarness:
    """One disposable engine, the durable session store and seeding helpers."""

    def __init__(
        self,
        engine: AsyncEngine,
        store: PostgresqlMultipartUploadStore,
        clock: MutableUtcClock,
    ) -> None:
        self.engine = engine
        self.store = store
        self.clock = clock

    async def seed_device(self) -> SmallFileDeviceContext:
        """Seed one owner user, workspace and active device."""

        owner_user_id = uuid4()
        workspace_id = uuid4()
        device_id = uuid4()
        nonce = uuid4().hex
        async with self.engine.begin() as connection:
            await connection.execute(
                sa.insert(users).values(
                    user_id=owner_user_id,
                    username=f"multipart-{nonce[:16]}",
                    display_name="Multipart Store Owner",
                )
            )
            await connection.execute(
                sa.insert(workspaces).values(
                    workspace_id=workspace_id,
                    owner_user_id=owner_user_id,
                    workspace_key=f"multipart-{nonce[:16]}",
                    display_name="Multipart Store Workspace",
                )
            )
            await connection.execute(
                sa.insert(devices).values(
                    device_id=device_id,
                    workspace_id=workspace_id,
                    user_id=owner_user_id,
                    device_name="Multipart Store Device",
                    device_kind="obsidian",
                )
            )
        return SmallFileDeviceContext(device_id=device_id, workspace_id=workspace_id)

    async def seed_foreign_device(
        self, device_context: SmallFileDeviceContext
    ) -> SmallFileDeviceContext:
        """Seed a second active device inside the same workspace's owner."""

        device_id = uuid4()
        async with self.engine.begin() as connection:
            owner = await connection.execute(
                sa.select(workspaces.c.owner_user_id).where(
                    workspaces.c.workspace_id == device_context.workspace_id
                )
            )
            await connection.execute(
                sa.insert(devices).values(
                    device_id=device_id,
                    workspace_id=device_context.workspace_id,
                    user_id=owner.scalar_one(),
                    device_name="Multipart Foreign Device",
                    device_kind="obsidian",
                )
            )
        return SmallFileDeviceContext(device_id=device_id, workspace_id=device_context.workspace_id)

    async def seed_operation(
        self,
        device_context: SmallFileDeviceContext,
        *,
        now: datetime,
    ) -> SeededMultipartOperation:
        """Seed one frozen pending create-operation in the routing range."""

        nonce = uuid4().hex
        token = mint_upload_operation_token()
        preflight = SmallFilePreflight(
            event_id=uuid4(),
            idempotency_key=SmallFileIdempotencyKey(str(uuid4())),
            operation=SmallFileOperation.CREATE,
            local_file_id=uuid4(),
            source_id=None,
            base_version_id=None,
            normalized_locator=NormalizedLocator(f"notes/multipart/{nonce}.md"),
            sha256=ContentDigest.parse(sha256(nonce.encode("utf-8")).hexdigest()),
            size_bytes=SEEDED_MULTIPART_SIZE_BYTES,
            media_type=CanonicalMediaType.parse("text/markdown"),
            policy_revision_number=1,
        )
        operation_expires_at = now + timedelta(seconds=UPLOAD_OPERATION_EXPIRY_SECONDS)
        async with self.engine.begin() as connection:
            await connection.execute(
                sa.insert(small_file_upload_operations).values(
                    operation_id=uuid4(),
                    operation_token_hash=upload_operation_token_hash(token),
                    workspace_id=device_context.workspace_id,
                    device_id=device_context.device_id,
                    event_id=preflight.event_id,
                    idempotency_key=preflight.idempotency_key.value,
                    operation_kind=preflight.operation.value,
                    declared_sha256=preflight.sha256.hexadecimal,
                    declared_size_bytes=preflight.size_bytes,
                    declared_media_type=preflight.media_type.value,
                    policy_revision_number=preflight.policy_revision_number,
                    reserved_source_id=uuid4(),
                    normalized_locator=preflight.normalized_locator.value,
                    locator_fingerprint=compute_locator_fingerprint(
                        preflight.normalized_locator
                    ),
                    state="pending",
                    expires_at=operation_expires_at,
                )
            )
        return SeededMultipartOperation(
            operation=SmallFileUploadOperation(
                operation_token=token,
                preflight=preflight,
                device_context=device_context,
                reserved_source_id=None,
                expires_at=operation_expires_at,
            ),
            staging_key=f"staging/multipart/{nonce}",
            provider_upload_id=MultipartProviderUploadId(f"provider-upload-{nonce}"),
        )

    async def reserve(
        self,
        seeded: SeededMultipartOperation,
        device_context: SmallFileDeviceContext,
    ) -> MultipartSessionRecord:
        """Reserve the seeded operation's session through the real store."""

        return await self.store.reserve_session(
            operation=seeded.operation,
            staging_key=seeded.staging_key,
            provider_upload_id=seeded.provider_upload_id,
            device_context=device_context,
            diagnostic_context=diagnostic_context(),
        )

    async def session_row(self, session_id: MultipartUploadSessionId) -> dict[str, Any]:
        rows = await self.session_rows(session_id)
        assert len(rows) == 1, "the session lookup must address exactly one row"
        return rows[0]

    async def session_rows(
        self, session_id: MultipartUploadSessionId
    ) -> list[dict[str, Any]]:
        async with self.engine.connect() as connection:
            result = await connection.execute(
                sa.select(
                    multipart_uploads.c.state,
                    multipart_uploads.c.claim_token,
                    multipart_uploads.c.claim_expires_at,
                    multipart_uploads.c.cleanup_state,
                    multipart_uploads.c.cleanup_attempt_count,
                    multipart_uploads.c.cleanup_next_retry_at,
                    multipart_uploads.c.cleanup_reason_code,
                ).where(multipart_uploads.c.session_id == session_id.value)
            )
            return [dict(row) for row in result.mappings()]

    async def session_count(self, workspace_id: UUID) -> int:
        """Count the session rows of exactly one test's seeded workspace."""

        async with self.engine.connect() as connection:
            result = await connection.execute(
                sa.select(sa.func.count())
                .select_from(multipart_uploads)
                .where(multipart_uploads.c.workspace_id == workspace_id)
            )
            return int(result.scalar_one())


@pytest_asyncio.fixture
async def multipart_store_harness(
    source_publication_stack: SourcePublicationStack,
) -> Iterator[MultipartStoreHarness]:
    clock = MutableUtcClock()
    engine = create_source_store_engine(
        source_publication_stack.settings, source_publication_stack.password
    )
    try:
        yield MultipartStoreHarness(
            engine,
            PostgresqlMultipartUploadStore(engine, clock=clock),
            clock,
        )
    finally:
        await dispose_source_store_engine(engine)


def pytest_asyncio_loop_factories(
    config: pytest.Config, item: pytest.Item
) -> dict[str, Callable[[], asyncio.AbstractEventLoop]]:
    """Run every asyncio test and fixture on a selector event loop.

    psycopg async cannot run on the Windows proactor loop, and
    SelectorEventLoop is already the default loop on the Linux CI integration
    runs.
    """
    del config, item
    return {"selector": asyncio.SelectorEventLoop}
