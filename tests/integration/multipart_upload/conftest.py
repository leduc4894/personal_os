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

The live R2 section below adds the Task 12 end-to-end harness: the composed
production service — durable PostgreSQL stores, the real R2 staging provider
and the canonical ``R2S3ObjectStore`` — against one dedicated private R2
TEST bucket. Every staging identity is appended to the per-run exact cleanup
manifest before the first provider mutation that can create it, parts are
PUT through the real presigned URLs, and teardown cleans exactly the
manifest's identities (never a listing, prefix or wildcard). Setup fails —
never skips — when a required ``R2_TEST_*`` variable or credential file is
missing, rendering NAMES only.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import os
import secrets
import shutil
import tempfile
from collections.abc import AsyncIterable, Callable, Iterator
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
import sqlalchemy as sa
from aiobotocore.config import AioConfig
from aiobotocore.session import get_session
from api_runtime.authentication_crypto import (
    AuthenticationKeyring,
    CryptographyAuthenticationCrypto,
)
from api_runtime.exclusion_policy_crypto import TrustAnchorEd25519Verifier
from api_runtime.multipart_upload_composition import (
    KeyringMultipartOperationTokenCodec,
    LazyMultipartStagingSdkClient,
    R2MultipartStagingByteSource,
    RecheckLocatorAwarePolicyEnforcementGuard,
    ValidatedStagingKeyMultipartProvider,
)
from api_runtime.small_file_sync_composition import (
    BoundPolicySmallFilePublicationGateway,
    LazyR2ClientSource,
)
from botocore.exceptions import ClientError
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.r2_object_storage.cleanup_manifest import (
    REJECTION_UNRECORDED_KEY,
    CleanupRejection,
    CreatedObjectRecord,
    LiveCleanupManifest,
    run_exact_key_cleanup,
    run_exact_staging_cleanup,
)
from tests.integration.r2_object_storage.conftest import (
    _load_live_configuration,
    _require_live_configuration,
)
from tests.integration.source_publication.conftest import (
    SourcePublicationStack,
    source_publication_stack,
)
from tools.signed_policy_seed import seed_signed_policy

from personal_os.diagnostics.context import DiagnosticContext, TraceContext
from personal_os.diagnostics.logging import DiagnosticLogger
from personal_os.diagnostics.trace_context import SpanId, TraceId
from personal_os.error_contracts.codes import ErrorCode
from personal_os.exclusion_policy.enforcement import default_utc_clock
from personal_os.multipart_upload.contracts import (
    MultipartCompletionResult,
    MultipartPartRange,
    MultipartPartUrl,
    MultipartSessionState,
    MultipartSessionStatus,
    MultipartUploadPlan,
    MultipartUploadSessionId,
)
from personal_os.multipart_upload.errors import MultipartUploadError
from personal_os.multipart_upload.metrics import InMemoryMultipartUploadMetrics
from personal_os.multipart_upload.ports import (
    MultipartProviderUploadId,
    MultipartSessionRecord,
)
from personal_os.multipart_upload.service import (
    MultipartCleanupBatchOutcome,
    MultipartObservedPart,
    MultipartUploadService,
    derive_staging_key,
)
from personal_os.object_storage import (
    CanonicalMediaType,
    ContentDigest,
    ExpectedObject,
    derive_canonical_object_key,
)
from personal_os.small_file_sync.contracts import (
    NormalizedLocator,
    SmallFileDeviceContext,
    SmallFileIdempotencyKey,
    SmallFileOperation,
    SmallFilePreflight,
    SmallFileUploadOperation,
    compute_locator_fingerprint,
)
from personal_os.sources.metrics import InMemorySourcePublicationMetrics
from postgresql_source_store.canonical_read import PostgresqlCanonicalSourceReadStore
from postgresql_source_store.engine import (
    create_source_store_engine,
    dispose_source_store_engine,
)
from postgresql_source_store.multipart_upload_store import (
    PostgresqlMultipartSessionEvidenceStore,
    PostgresqlMultipartUploadStore,
)
from postgresql_source_store.policy_enforcement import compose_policy_enforcement
from postgresql_source_store.publication_store import PostgresqlSourcePublicationStore
from postgresql_source_store.small_file_sync_operations import (
    UPLOAD_OPERATION_EXPIRY_SECONDS,
    PostgresqlSmallFileUploadOperationStore,
    mint_upload_operation_token,
    upload_operation_token_hash,
)
from postgresql_source_store.tables import (
    devices,
    multipart_uploads,
    small_file_upload_operations,
    source_versions,
    sync_events,
    users,
    workspace_policy_state,
    workspaces,
)
from r2_object_storage.adapter import R2S3ObjectStore
from r2_object_storage.client import R2ClientManager
from r2_object_storage.error_mapping import RetryPolicy, client_error_code
from r2_object_storage.metrics import InMemoryObjectStorageMetrics
from r2_object_storage.multipart import MultipartStagingKey, R2MultipartStagingProvider
from r2_object_storage.spool import SpoolManager

__all__ = [
    "LiveR2MultipartHarness",
    "MultipartStoreHarness",
    "MutableUtcClock",
    "SeededMultipartOperation",
    "live_harness",
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
    """Injectable aware-UTC clock whose moment tests advance deterministically.

    The default starts at the real current moment: seeded rows derive their
    deadlines from this clock while the database stamps ``created_at`` with
    its own real now, and the schema's ``expires_at > created_at`` constraint
    rejects any frozen starting moment that has drifted behind real time.
    Tests stay deterministic in every relative assertion (advances and
    deadline offsets compare against ``clock()`` itself).
    """

    def __init__(self, now: datetime | None = None) -> None:
        self.now = now if now is not None else datetime.now(UTC)

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
                    locator_fingerprint=compute_locator_fingerprint(preflight.normalized_locator),
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

    async def reserve_session_only(
        self,
        seeded: SeededMultipartOperation,
        device_context: SmallFileDeviceContext,
    ) -> MultipartSessionRecord:
        """Reserve the seeded operation's session before any provider work."""

        return await self.store.reserve_session(
            operation=seeded.operation,
            device_context=device_context,
            diagnostic_context=diagnostic_context(),
        )

    async def reserve(
        self,
        seeded: SeededMultipartOperation,
        device_context: SmallFileDeviceContext,
    ) -> MultipartSessionRecord:
        """Reserve the session and land its post-create provider identity.

        The spec 6.1 creation order end to end: the durable session row
        first, then the fenced identity write carrying the private staging
        identity the (doubled) provider adapter minted.
        """

        record = await self.reserve_session_only(seeded, device_context)
        return await self.store.record_provider_identity(
            session_id=record.session_id,
            staging_key=seeded.staging_key,
            provider_upload_id=seeded.provider_upload_id,
            device_context=device_context,
            diagnostic_context=diagnostic_context(),
        )

    async def session_row(self, session_id: MultipartUploadSessionId) -> dict[str, Any]:
        rows = await self.session_rows(session_id)
        assert len(rows) == 1, "the session lookup must address exactly one row"
        return rows[0]

    async def session_rows(self, session_id: MultipartUploadSessionId) -> list[dict[str, Any]]:
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


# --- the live R2 multipart journey harness (Task 12, design 9.2) -------------

#: One live transfer's declared size: the smallest multipart-routing size
#: (16 MiB + 1 byte) so the journey runs the exact three-part geometry
#: (8 MiB, 8 MiB, 1 byte) with the least live provider traffic.
LIVE_MULTIPART_SIZE_BYTES: Final[int] = 16 * 1024 * 1024 + 1

#: Bounded timeout of the harness-local presigned part PUT client.
_PART_PUT_TIMEOUT_SECONDS: Final[float] = 180.0

#: The closed provider absence codes of the exact-identity probes.
_PROBE_ABSENCE_CODES: Final[frozenset[str]] = frozenset({"NoSuchKey", "NoSuchUpload"})


class LostCompletionAcknowledgement(Exception):
    """Client-observer sentinel: one completion response never arrived.

    The harness raises this instead of delivering a fully server-committed
    completion result — exactly what a lost acknowledgement is from the
    client side — and the replay that follows proves the server froze one
    source-event result only.
    """

    def __init__(self) -> None:
        super().__init__("multipart completion acknowledgement was lost")


@dataclass(frozen=True, slots=True)
class _LiveTransferIntent:
    """One live transfer's frozen declared identity and its payload bytes."""

    preflight: SmallFilePreflight
    payload: bytes
    digest_hexadecimal: str
    canonical_object_key: str


class ManifestRecordingStagingByteSource:
    """Manifest-gated read seam over the real staging byte source.

    The verification spool's staging read must pass the same exact-identity
    tripwire as every mutating capability: a staging key the run never
    recorded is rejected before the read crosses to the provider, so the
    completion path can never verify bytes at an identity outside the
    cleanup manifest.
    """

    def __init__(
        self,
        source: R2MultipartStagingByteSource,
        *,
        manifest: LiveCleanupManifest,
    ) -> None:
        self._source = source
        self._manifest = manifest

    def open_staging_stream(
        self, staging_key: str
    ) -> AbstractAsyncContextManager[AsyncIterable[bytes]]:
        if self._manifest.staging_record_for(staging_key) is None:
            raise CleanupRejection(REJECTION_UNRECORDED_KEY)
        return self._source.open_staging_stream(staging_key)


class ManifestRecordingStagingProvider:
    """Exact-identity recording wrapper over the real staging provider seam.

    The composed service's str-key seam crosses here first: every mutating
    capability appends the exact identity it is about to touch to the run's
    cleanup manifest — the staging key BEFORE the first provider mutation
    that can create it, the observed provider upload ID the moment it is
    known — and every capability proves the addressed staging key is a
    recorded identity of this run before it crosses to the real provider.
    ``fail_next_delete`` injects exactly one typed failure for the live
    failure/retry case; the real provider call still runs on the retry.
    """

    def __init__(
        self,
        provider: ValidatedStagingKeyMultipartProvider,
        *,
        manifest: LiveCleanupManifest,
    ) -> None:
        self._provider = provider
        self._manifest = manifest
        self._fail_next_delete: MultipartUploadError | None = None

    def _require_recorded(self, staging_key: str) -> None:
        if self._manifest.staging_record_for(staging_key) is None:
            raise CleanupRejection(REJECTION_UNRECORDED_KEY)

    def fail_next_delete(self, error_code: ErrorCode) -> None:
        """Make the next exact staging delete fail once with a typed error."""

        self._fail_next_delete = MultipartUploadError(error_code)

    async def create_upload(self, staging_key: str) -> MultipartProviderUploadId:
        self._manifest.record_staging_key(staging_key)
        upload_id = await self._provider.create_upload(staging_key)
        self._manifest.attach_staging_upload_id(staging_key, upload_id.value)
        return upload_id

    async def presign_part(
        self,
        staging_key: str,
        upload_id: MultipartProviderUploadId,
        part_range: MultipartPartRange,
    ) -> MultipartPartUrl:
        self._require_recorded(staging_key)
        return await self._provider.presign_part(staging_key, upload_id, part_range)

    async def list_parts(
        self, staging_key: str, upload_id: MultipartProviderUploadId
    ) -> tuple[MultipartObservedPart, ...]:
        self._require_recorded(staging_key)
        return await self._provider.list_parts(staging_key, upload_id)

    async def complete_upload(
        self,
        staging_key: str,
        upload_id: MultipartProviderUploadId,
        parts: tuple[MultipartObservedPart, ...],
    ) -> None:
        self._require_recorded(staging_key)
        await self._provider.complete_upload(staging_key, upload_id, parts)

    async def abort_upload(self, staging_key: str, upload_id: MultipartProviderUploadId) -> None:
        self._require_recorded(staging_key)
        await self._provider.abort_upload(staging_key, upload_id)

    async def delete_staging_object(self, staging_key: str) -> None:
        self._require_recorded(staging_key)
        if self._fail_next_delete is not None:
            failure = self._fail_next_delete
            self._fail_next_delete = None
            raise failure
        await self._provider.delete_staging_object(staging_key)


class LiveR2MultipartHarness:
    """One live run: the composed service, real R2 and the exact manifest.

    Drives the production composition — durable PostgreSQL stores, the real
    staging provider and the canonical object store — against the dedicated
    private R2 TEST bucket. The client observer PUTs each part through its
    real presigned URL; nothing ever renders a URL, staging key, upload ID,
    ETag or digest. The fixture's teardown cleans exactly the manifest's
    identities and proves their absence.
    """

    def __init__(
        self,
        *,
        engine: AsyncEngine,
        service: MultipartUploadService,
        manifest: LiveCleanupManifest,
        recording_provider: ManifestRecordingStagingProvider,
        staging_provider: R2MultipartStagingProvider,
        object_store: R2S3ObjectStore,
        low_level_client: Any,
        http_client: httpx.AsyncClient,
        device: SmallFileDeviceContext,
        clock: MutableUtcClock,
    ) -> None:
        self.engine = engine
        self.service = service
        self.manifest = manifest
        self.recording_provider = recording_provider
        self._staging_provider = staging_provider
        self._object_store = object_store
        self._low_level_client = low_level_client
        self._http_client = http_client
        self.device = device
        self.clock = clock
        self._intent: _LiveTransferIntent | None = None
        self._session_id: MultipartUploadSessionId | None = None
        self._dropped_completions = 0
        self._part_put_counts: dict[int, int] = {}

    # --- identity and intent ----------------------------------------------------

    @property
    def preflight(self) -> SmallFilePreflight:
        """The frozen declared identity of this run's transfer."""

        if self._intent is None:
            raise RuntimeError("open the transfer before reading its preflight")
        return self._intent.preflight

    @property
    def session_id(self) -> MultipartUploadSessionId:
        """The opaque session identity of this run's transfer."""

        if self._session_id is None:
            raise RuntimeError("open the transfer before reading its session id")
        return self._session_id

    @property
    def part_count(self) -> int:
        """The exact part count of this run's transfer geometry."""

        return -(-len(self._require_intent().payload) // (8 * 1024 * 1024))

    def diagnostic_context(self) -> DiagnosticContext:
        """One fresh correlation context for a service call."""

        return diagnostic_context()

    def _require_intent(self) -> _LiveTransferIntent:
        if self._intent is None:
            raise RuntimeError("open the transfer first")
        return self._intent

    def advance_clock(self, delta: timedelta) -> None:
        """Advance the store's mutable clock (expiry and backoff observable)."""

        self.clock.advance(delta)

    async def seed_foreign_device(self) -> SmallFileDeviceContext:
        """Seed one second active device of the same workspace's owner."""

        device_id = uuid4()
        async with self.engine.begin() as connection:
            owner = await connection.execute(
                sa.select(workspaces.c.owner_user_id).where(
                    workspaces.c.workspace_id == self.device.workspace_id
                )
            )
            await connection.execute(
                sa.insert(devices).values(
                    device_id=device_id,
                    workspace_id=self.device.workspace_id,
                    user_id=owner.scalar_one(),
                    device_name="Multipart Live Foreign Device",
                    device_kind="obsidian",
                )
            )
        return SmallFileDeviceContext(device_id=device_id, workspace_id=self.device.workspace_id)

    # --- the client observer journey ---------------------------------------------

    async def open_transfer(self) -> MultipartUploadPlan:
        """Create (or exactly replay) this run's one multipart session.

        The per-run payload is random non-personal bytes; the canonical key
        of the declared digest is appended to the cleanup manifest BEFORE
        the first provider mutation of the run, and the staging key is
        appended inside the recording provider before the create call
        crosses to R2.
        """

        if self._intent is None:
            nonce = uuid4().hex
            payload = secrets.token_bytes(LIVE_MULTIPART_SIZE_BYTES)
            digest_hexadecimal = sha256(payload).hexdigest()
            preflight = SmallFilePreflight(
                event_id=uuid4(),
                idempotency_key=SmallFileIdempotencyKey(str(uuid4())),
                operation=SmallFileOperation.CREATE,
                local_file_id=uuid4(),
                source_id=None,
                base_version_id=None,
                normalized_locator=NormalizedLocator(f"notes/multipart-live/{nonce}.md"),
                sha256=ContentDigest.parse(digest_hexadecimal),
                size_bytes=len(payload),
                media_type=CanonicalMediaType.parse("text/markdown"),
                policy_revision_number=1,
            )
            canonical_object_key = str(
                derive_canonical_object_key(ContentDigest.parse(digest_hexadecimal))
            )
            self.manifest.record_created(
                CreatedObjectRecord(
                    key=canonical_object_key,
                    digest_hexadecimal=digest_hexadecimal,
                    size_bytes=len(payload),
                    media_type="text/markdown",
                )
            )
            self._intent = _LiveTransferIntent(
                preflight=preflight,
                payload=payload,
                digest_hexadecimal=digest_hexadecimal,
                canonical_object_key=canonical_object_key,
            )
        plan = await self.service.create_or_resume(
            preflight=self._intent.preflight,
            device_context=self.device,
            diagnostic_context=self.diagnostic_context(),
        )
        if self._session_id is None:
            self._session_id = plan.session_id
        return plan

    def _part_window(self, part_number: int) -> tuple[int, int]:
        """The exact payload window (offset, size) of one part."""

        part_size = 8 * 1024 * 1024
        offset = (part_number - 1) * part_size
        payload = self._require_intent().payload
        size = part_size if offset + part_size <= len(payload) else len(payload) - offset
        return offset, size

    def part_put_count(self, part_number: int) -> int:
        """How many real presigned PUTs this run issued for one part."""

        return self._part_put_counts.get(part_number, 0)

    async def upload_part(self, part_number: int, *, is_corrupt: bool = False) -> None:
        """PUT one part's exact bytes through its real presigned URL."""

        if self._session_id is None:
            await self.open_transfer()
        intent = self._require_intent()
        offset, size = self._part_window(part_number)
        payload_window = intent.payload[offset : offset + size]
        body = bytes(byte ^ 0x5A for byte in payload_window) if is_corrupt else payload_window
        part_url = await self.service.issue_part_url(
            session_id=self.session_id,
            part_number=part_number,
            device_context=self.device,
            diagnostic_context=self.diagnostic_context(),
        )
        response = await self._http_client.put(part_url.url, content=body)
        if response.status_code != 200:
            pytest.fail(
                "live multipart part PUT failed with HTTP status "
                f"{response.status_code} (no URL or provider detail is rendered)",
                pytrace=False,
            )
        self._part_put_counts[part_number] = self.part_put_count(part_number) + 1

    async def upload_all_parts(self) -> None:
        """Open the transfer and PUT every part of its exact geometry."""

        await self.open_transfer()
        for part_number in range(1, self.part_count + 1):
            await self.upload_part(part_number)

    async def upload_remaining_parts(self) -> None:
        """PUT every part not yet uploaded (the post-restart remainder)."""

        for part_number in range(1, self.part_count + 1):
            if self.part_put_count(part_number) == 0:
                await self.upload_part(part_number)

    async def upload_corrupt_part(self) -> None:
        """Upload the full geometry with the first part's bytes corrupted.

        The corrupt window keeps the exact part size, so the provider-side
        geometry observation succeeds and the failure lands where the spec
        puts it: the full-object digest verification during completion.
        """

        await self.open_transfer()
        for part_number in range(1, self.part_count + 1):
            await self.upload_part(part_number, is_corrupt=part_number == 1)

    async def drop_next_complete_response(self) -> None:
        """Lose the acknowledgement of the next completion call.

        The underlying server-side completion still runs fully — the real
        provider complete, the verification spool, the publication and the
        fenced terminal write — and the client observer then sees the
        sentinel instead of the result.
        """

        self._dropped_completions += 1

    async def complete(self) -> MultipartCompletionResult:
        """Claim completion as the client observer, honoring dropped acks."""

        result = await self.service.complete(
            session_id=self.session_id,
            device_context=self.device,
            diagnostic_context=self.diagnostic_context(),
        )
        if self._dropped_completions > 0:
            self._dropped_completions -= 1
            raise LostCompletionAcknowledgement()
        return result

    async def complete_then_replay(self) -> MultipartCompletionResult:
        """Complete with a lost acknowledgement, then replay the frozen result."""

        with pytest.raises(LostCompletionAcknowledgement):
            await self.complete()
        replayed = await self.complete()
        assert replayed.state is MultipartSessionState.COMMITTED
        assert replayed.terminal_result is not None
        return replayed

    async def complete_expect_integrity_failure(self) -> None:
        """Complete and observe the closed integrity refusal."""

        with pytest.raises(MultipartUploadError) as rejection:
            await self.service.complete(
                session_id=self.session_id,
                device_context=self.device,
                diagnostic_context=self.diagnostic_context(),
            )
        assert rejection.value.error_code is ErrorCode.MULTIPART_INTEGRITY_FAILED

    async def abort(self) -> MultipartSessionStatus:
        """Terminalize user cancellation of this run's session."""

        return await self.service.abort(
            session_id=self.session_id,
            device_context=self.device,
            diagnostic_context=self.diagnostic_context(),
        )

    async def run_cleanup(self) -> MultipartCleanupBatchOutcome:
        """Execute one exact-cleanup batch over the durable obligations."""

        return await self.service.run_exact_cleanup(
            batch_limit=10, diagnostic_context=self.diagnostic_context()
        )

    def fail_next_staging_delete(self, error_code: ErrorCode) -> None:
        """Inject exactly one typed staging-delete failure (failure/retry)."""

        self.recording_provider.fail_next_delete(error_code)

    # --- durable and provider observation -----------------------------------------

    async def source_version_count(self) -> int:
        async with self.engine.connect() as connection:
            result = await connection.execute(
                sa.select(sa.func.count())
                .select_from(source_versions)
                .where(source_versions.c.workspace_id == self.device.workspace_id)
            )
            return int(result.scalar_one())

    async def sync_event_count(self) -> int:
        async with self.engine.connect() as connection:
            result = await connection.execute(
                sa.select(sa.func.count())
                .select_from(sync_events)
                .where(
                    sync_events.c.workspace_id == self.device.workspace_id,
                    sync_events.c.event_id == self.preflight.event_id,
                )
            )
            return int(result.scalar_one())

    async def session_count(self) -> int:
        async with self.engine.connect() as connection:
            result = await connection.execute(
                sa.select(sa.func.count())
                .select_from(multipart_uploads)
                .where(multipart_uploads.c.workspace_id == self.device.workspace_id)
            )
            return int(result.scalar_one())

    def staging_key_count(self) -> int:
        """Distinct staging identities the manifest recorded for this run."""

        return len(self.manifest.recorded_staging_resources())

    async def session_row(self) -> dict[str, Any]:
        staging_key = derive_staging_key(self.session_id)
        async with self.engine.connect() as connection:
            result = await connection.execute(
                sa.select(
                    multipart_uploads.c.state,
                    multipart_uploads.c.staging_key,
                    multipart_uploads.c.provider_upload_id,
                    multipart_uploads.c.cleanup_state,
                    multipart_uploads.c.cleanup_attempt_count,
                    multipart_uploads.c.cleanup_next_retry_at,
                    multipart_uploads.c.cleanup_reason_code,
                ).where(multipart_uploads.c.staging_key == staging_key)
            )
            rows = [dict(row) for row in result.mappings()]
        assert len(rows) == 1, "the session's staging key addresses exactly one row"
        return rows[0]

    async def cleanup_manifest_contains_only_session_resources(self) -> bool:
        """The manifest holds exactly this session's staging and canonical keys.

        Every refusal fails through ``pytest.fail`` with ``pytrace=False`` so
        only booleans, counts and closed wording are ever rendered: a failing
        comparison never renders a staging key, provider upload ID, digest or
        URL into pytest or JUnit output.
        """

        staging = self.manifest.recorded_staging_resources()
        if len(staging) != 1:
            pytest.fail(
                f"expected exactly one recorded staging key for this run, got {len(staging)}",
                pytrace=False,
            )
        if staging[0].staging_key != derive_staging_key(self.session_id):
            pytest.fail(
                "the recorded staging key does not address this run's session "
                "(no key value is rendered)",
                pytrace=False,
            )
        row = await self.session_row()
        if row["staging_key"] != staging[0].staging_key:
            pytest.fail(
                "the durable session row and the manifest disagree on the staging "
                "key identity (no key value is rendered)",
                pytrace=False,
            )
        if row["provider_upload_id"] not in staging[0].provider_upload_ids:
            pytest.fail(
                "the durable session row's provider upload ID is not among the "
                f"{len(staging[0].provider_upload_ids)} upload ID(s) recorded for "
                "this run's staging key (no upload ID is rendered)",
                pytrace=False,
            )
        if self.manifest.recorded_keys() != (self._require_intent().canonical_object_key,):
            pytest.fail(
                "the manifest's canonical keys are not exactly this run's one "
                f"declared-digest key ({len(self.manifest.recorded_keys())} recorded; "
                "no digest is rendered)",
                pytrace=False,
            )
        return True

    async def staging_object_exists(self) -> bool:
        """Exact-key probe: does this session's staging object exist in R2?"""

        return await _staging_object_at_key_exists(
            self._low_level_client, self.manifest.bucket_name, derive_staging_key(self.session_id)
        )

    async def upload_in_flight(self) -> bool:
        """Exact-identity probe: is this session's provider upload in flight?"""

        row = await self.session_row()
        upload_id = row["provider_upload_id"]
        if upload_id is None:
            return False
        try:
            response = await self._low_level_client.list_parts(
                Bucket=self.manifest.bucket_name,
                Key=derive_staging_key(self.session_id),
                UploadId=upload_id,
            )
        except ClientError as failure:
            if client_error_code(failure) in _PROBE_ABSENCE_CODES:
                return False
            raise
        await _drain_and_close_body(response)
        return True

    async def canonical_object_exists(self) -> bool:
        """Does the exact canonical object of the declared digest exist?"""

        intent = self._require_intent()
        receipt = await self._object_store.resolve_verified_object(
            ExpectedObject(
                content_digest=ContentDigest.parse(intent.digest_hexadecimal),
                size_bytes=len(intent.payload),
                media_type=CanonicalMediaType.parse("text/markdown"),
            )
        )
        return receipt is not None


def _is_probe_absence(failure: BaseException) -> bool:
    """Classify one exact-probe provider refusal as ordinary absence."""

    return isinstance(failure, ClientError) and (client_error_code(failure) in _PROBE_ABSENCE_CODES)


async def _staging_object_at_key_exists(
    low_level_client: Any, bucket_name: str, staging_key: str
) -> bool:
    """Exact-key existence probe of one already-validated staging key."""

    try:
        response = await low_level_client.get_object(Bucket=bucket_name, Key=staging_key)
    except ClientError as failure:
        if client_error_code(failure) in _PROBE_ABSENCE_CODES:
            return False
        raise
    await _drain_and_close_body(response)
    return True


async def _drain_and_close_body(response: Any) -> None:
    """Drain and close one probe response body without rendering anything."""

    body = response.get("Body") if isinstance(response, dict) else None
    if body is None:
        return
    try:
        await body.read()
    finally:
        close = getattr(body, "close", None)
        if close is not None:
            outcome = close()
            if inspect.isawaitable(outcome):
                await outcome


async def seed_live_workspace(engine: AsyncEngine) -> SmallFileDeviceContext:
    """Seed the identity graph plus a signed empty policy for one live run."""

    owner_user_id = uuid4()
    workspace_id = uuid4()
    device_id = uuid4()
    nonce = uuid4().hex
    async with engine.begin() as connection:
        await connection.execute(
            sa.insert(users).values(
                user_id=owner_user_id,
                username=f"multipart-live-{nonce[:16]}",
                display_name="Multipart Live Owner",
            )
        )
        await connection.execute(
            sa.insert(workspaces).values(
                workspace_id=workspace_id,
                owner_user_id=owner_user_id,
                workspace_key=f"multipart-live-{nonce[:12]}",
                display_name="Multipart Live Workspace",
            )
        )
        await connection.execute(
            sa.insert(devices).values(
                device_id=device_id,
                workspace_id=workspace_id,
                user_id=owner_user_id,
                device_name="Multipart Live Device",
                device_kind="obsidian",
            )
        )
        await connection.execute(
            sa.insert(workspace_policy_state).values(
                workspace_id=workspace_id,
                active_policy_revision_id=None,
                active_revision_number=0,
            )
        )
    await seed_signed_policy(engine, workspace_id=workspace_id, published_by_user_id=owner_user_id)
    return SmallFileDeviceContext(device_id=device_id, workspace_id=workspace_id)


def _live_keyring() -> AuthenticationKeyring:
    """One process-local in-memory keyring for the sealed operation token."""

    key_id = "multipart-live-test-key"
    return AuthenticationKeyring(
        current_key_id=key_id,
        keys_by_id=MappingProxyType({key_id: secrets.token_bytes(32)}),
    )


def _live_low_level_client_config() -> AioConfig:
    """Bounded SDK config for the harness-local probe/delete client."""

    return AioConfig(
        region_name="auto",
        signature_version="s3v4",
        max_pool_connections=4,
        connect_timeout=5.0,
        read_timeout=60.0,
        retries={"total_max_attempts": 1, "mode": "standard"},
    )


async def _run_live_teardown(harness: LiveR2MultipartHarness) -> None:
    """Clean exactly the manifest's identities and prove their absence.

    Order: drain any durable cleanup obligation this test left pending (the
    executor touches only persisted exact identities), abort and remove
    exactly the recorded staging resources through the real typed provider,
    delete exactly the recorded canonical keys through the harness-local
    low-level delete, then prove absence through exact-identity probes and
    the adapter's typed verify path. A manifest rejection or provider
    failure fails the run reporting closed reason tokens and counts only —
    never a key, upload ID, URL or digest.
    """

    with contextlib.suppress(Exception):
        await harness.run_cleanup()
    staging_resources = harness.manifest.recorded_staging_resources()
    try:
        cleaned_staging = await run_exact_staging_cleanup(
            harness.manifest,
            bucket_name=harness.manifest.bucket_name,
            resources=staging_resources,
            abort_one=_provider_abort_one(harness),
            delete_one=_provider_delete_staging_one(harness),
        )
    except CleanupRejection as rejection:
        pytest.fail(
            f"live staging cleanup violated the exact-identity contract: {rejection.reason.value}",
            pytrace=False,
        )
    for staging_key in cleaned_staging:
        if await _staging_object_at_key_exists(
            harness._low_level_client, harness.manifest.bucket_name, staging_key
        ):
            pytest.fail(
                f"live staging cleanup left an object behind ({len(cleaned_staging)} "
                "recorded staging key(s) were cleaned this run)",
                pytrace=False,
            )
    try:
        deleted = await run_exact_key_cleanup(
            harness.manifest,
            bucket_name=harness.manifest.bucket_name,
            keys=harness.manifest.recorded_keys(),
            delete_one=_low_level_delete_canonical_one(harness),
        )
    except CleanupRejection as rejection:
        pytest.fail(
            f"live canonical cleanup violated the exact-key contract: {rejection.reason.value}",
            pytrace=False,
        )
    for key in deleted:
        record = harness.manifest.record_for(key)
        assert record is not None, "validated canonical keys always have a record"
        remaining = await harness._object_store.resolve_verified_object(
            ExpectedObject(
                content_digest=ContentDigest.parse(record.digest_hexadecimal),
                size_bytes=record.size_bytes,
                media_type=CanonicalMediaType.parse(record.media_type),
            )
        )
        if remaining is not None:
            pytest.fail(
                f"live canonical cleanup left an object behind ({len(deleted)} "
                "recorded canonical key(s) were deleted this run)",
                pytrace=False,
            )


def _provider_abort_one(harness: LiveR2MultipartHarness) -> Callable[[str, str], Any]:
    """The typed exact-upload abort the staging cleanup driver calls."""

    async def abort_one(staging_key: str, provider_upload_id: str) -> None:
        await harness._staging_provider.abort_upload(
            MultipartStagingKey.parse(staging_key),
            MultipartProviderUploadId(provider_upload_id),
        )

    return abort_one


def _provider_delete_staging_one(harness: LiveR2MultipartHarness) -> Callable[[str], Any]:
    """The typed exact-key staging delete the cleanup driver calls."""

    async def delete_one(staging_key: str) -> None:
        await harness._staging_provider.delete_staging_object(
            MultipartStagingKey.parse(staging_key)
        )

    return delete_one


def _low_level_delete_canonical_one(harness: LiveR2MultipartHarness) -> Callable[[str], Any]:
    """The harness-local low-level exact canonical-key delete call."""

    async def delete_one(key: str) -> None:
        await harness._low_level_client.delete_object(Bucket=harness.manifest.bucket_name, Key=key)

    return delete_one


@pytest_asyncio.fixture
async def live_harness(
    source_publication_stack: SourcePublicationStack,
) -> Iterator[LiveR2MultipartHarness]:
    """Compose the real service over live R2; clean exactly what it created.

    Setup fails — never skips — when a required ``R2_TEST_*`` variable or
    credential file is missing, rendering NAMES only. The dedicated test
    configuration is composed onto the frozen settings loader's exact
    ``KNOWLEDGE_*`` names (secret FILES; no plaintext secret environment
    value), the engine comes from the disposable stack fixture, and the
    mutable aware-UTC clock drives the durable store so expiry and cleanup
    backoff are observable without sleeping.
    """

    _require_live_configuration(os.environ)
    spool_root = Path(tempfile.mkdtemp(prefix="multipart-live-spool-"))
    engine = create_source_store_engine(
        source_publication_stack.settings, source_publication_stack.password
    )
    low_level_context = None
    http_client = httpx.AsyncClient(timeout=httpx.Timeout(_PART_PUT_TIMEOUT_SECONDS, connect=15.0))
    object_store: R2S3ObjectStore | None = None
    client_manager: R2ClientManager | None = None
    try:
        settings, credentials = _load_live_configuration(os.environ, spool_root)
        client_manager = R2ClientManager(settings, credentials)
        low_level_context = get_session().create_client(
            "s3",
            region_name="auto",
            endpoint_url=settings.r2_endpoint,
            aws_access_key_id=credentials.access_key_id.get_secret_value(),
            aws_secret_access_key=credentials.secret_access_key.get_secret_value(),
            config=_live_low_level_client_config(),
        )
        low_level_client = await low_level_context.__aenter__()
        manifest = LiveCleanupManifest(bucket_name=settings.r2_bucket_name)
        logger = DiagnosticLogger({"service": "multipart-live-test", "environment": "test"})
        verifier = TrustAnchorEd25519Verifier()
        enforcement = compose_policy_enforcement(engine, verifier=verifier)
        object_store = R2S3ObjectStore(
            LazyR2ClientSource(client_manager),
            spools=SpoolManager(spool_root),
            retry=RetryPolicy(maximum_attempts=3),
            metrics=InMemoryObjectStorageMetrics(),
            logger=logger,
        )
        staging_provider = R2MultipartStagingProvider(
            LazyMultipartStagingSdkClient(client_manager),
            bucket=settings.r2_bucket_name,
            logger=logger,
        )
        recording_provider = ManifestRecordingStagingProvider(
            ValidatedStagingKeyMultipartProvider(staging_provider),
            manifest=manifest,
        )
        clock = MutableUtcClock()
        operation_store = PostgresqlSmallFileUploadOperationStore(engine, clock=clock)
        token_codec = KeyringMultipartOperationTokenCodec(
            CryptographyAuthenticationCrypto(), _live_keyring()
        )
        service = MultipartUploadService(
            session_store=PostgresqlMultipartUploadStore(
                engine, clock=clock, token_codec=token_codec
            ),
            evidence_store=PostgresqlMultipartSessionEvidenceStore(engine, token_codec=token_codec),
            operation_store=operation_store,
            policy_guard=RecheckLocatorAwarePolicyEnforcementGuard(enforcement=enforcement),
            current_sources=PostgresqlCanonicalSourceReadStore(engine, policy_verifier=verifier),
            publication_gateway=BoundPolicySmallFilePublicationGateway(
                store=PostgresqlSourcePublicationStore(engine, policy_verifier=verifier),
                object_store=object_store,
                metrics=InMemorySourcePublicationMetrics(),
                clock=default_utc_clock,
                enforcement=enforcement,
                operation_store=operation_store,
                diagnostics=logger,
            ),
            object_store=object_store,
            staging_provider=recording_provider,
            staging_byte_source=ManifestRecordingStagingByteSource(
                R2MultipartStagingByteSource(staging_provider),
                manifest=manifest,
            ),
            metrics=InMemoryMultipartUploadMetrics(),
            clock=clock,
            diagnostics=logger,
        )
        device = await seed_live_workspace(engine)
        harness = LiveR2MultipartHarness(
            engine=engine,
            service=service,
            manifest=manifest,
            recording_provider=recording_provider,
            staging_provider=staging_provider,
            object_store=object_store,
            low_level_client=low_level_client,
            http_client=http_client,
            device=device,
            clock=clock,
        )
        try:
            yield harness
        finally:
            try:
                await _run_live_teardown(harness)
            finally:
                await http_client.aclose()
    finally:
        with contextlib.suppress(Exception):
            if object_store is not None:
                await object_store.close()
        with contextlib.suppress(Exception):
            if client_manager is not None:
                await client_manager.close()
        if low_level_context is not None:
            with contextlib.suppress(Exception):
                await low_level_context.__aexit__(None, None, None)
        await dispose_source_store_engine(engine)
        shutil.rmtree(spool_root, ignore_errors=True)
