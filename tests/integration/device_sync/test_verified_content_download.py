"""Disposable PostgreSQL coverage for exact-version verified content reading.

Live coverage of the durable semantics the unit seams cannot prove: the
content catalog resolves only the exact (source, version) pair inside the
credential workspace — a foreign workspace's pair, a mismatched pair and an
unknown version are all indistinguishable from missing through the closed
event-unavailable rejection; current-policy authorization runs inside the
same resolution and a denial (or a workspace with no active revision) fails
closed before the object store is ever asked for a byte; and the composed
verified download yields exact bytes only through the fully verified reader
path, with object absence and corruption mapping onto the closed download
integrity failure without any provider string. The object-store side is a
faithful in-memory double performing the real size/digest verification
before any byte is yielded; the real spool-backed R2 adapter composition is
pinned by the scripted adapter contract suite.
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from api_runtime.device_sync_content import VerifiedDeviceContentService
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.device_sync.conftest import (
    DeviceSyncWorkspace,
    seed_device_sync_workspace,
)
from tests.integration.device_sync.test_cursor_and_manifest_transactions import (
    SeededPolicyRule,
    publish_workspace_policy,
)
from tests.integration.source_publication.conftest import SourcePublicationStack

from personal_os.device_sync.errors import DeviceSyncError, DeviceSyncErrorCode
from personal_os.device_sync.metrics import InMemoryDeviceSyncMetrics
from personal_os.diagnostics.context import DiagnosticContext, TraceContext
from personal_os.diagnostics.trace_context import SpanId, TraceId
from personal_os.error_contracts.codes import ErrorCode
from personal_os.exclusion_policy.errors import ExclusionPolicyError
from personal_os.object_storage import (
    CanonicalMediaType,
    ExpectedObject,
    VerifiedObjectReader,
)
from personal_os.object_storage.errors import ObjectStorageError
from postgresql_source_store.device_content_catalog import PostgresqlDeviceContentCatalog
from postgresql_source_store.engine import (
    create_source_store_engine,
    dispose_source_store_engine,
)
from postgresql_source_store.tables import (
    content_objects,
    source_locators,
    source_versions,
    sources,
    sync_events,
)

pytestmark = pytest.mark.local_stack

_TRACE = TraceContext(
    trace_id=TraceId("0123456789abcdef0123456789abcdef"),
    remote_parent_span_id=None,
    local_span_id=SpanId("0123456789abcdef"),
    trace_flags=0,
)

_MEDIA_TYPE = "text/markdown"


def _diagnostic() -> DiagnosticContext:
    return DiagnosticContext(request_id=uuid4(), client_request_id=None, trace=_TRACE)


class _VerifiedBufferReader:
    """Bounded async reader over one already-verified buffer."""

    def __init__(self, content: bytes) -> None:
        self._remaining = content

    async def read(self, size_bytes: int = 1_048_576) -> bytes:
        chunk = self._remaining[: max(size_bytes, 0)]
        self._remaining = self._remaining[len(chunk) :]
        return chunk

    def __aiter__(self) -> _VerifiedBufferReader:
        return self

    async def __anext__(self) -> bytes:
        if not self._remaining:
            raise StopAsyncIteration
        chunk = self._remaining[:65536]
        self._remaining = self._remaining[len(chunk) :]
        return chunk


class VerifiedMemoryObjectStore:
    """Object-store double performing the real full verification over bytes.

    The reader context verifies the exact size and full SHA-256 of the stored
    bytes BEFORE the context body is entered, mirroring the spool-backed
    adapter's fail-closed contract: absence raises the ordinary missing code,
    a size or digest mismatch raises the integrity failure, and no consumer
    byte can flow before verification completes.
    """

    def __init__(self) -> None:
        self._content_by_digest: dict[str, bytes] = {}
        self.open_calls: list[ExpectedObject] = []

    def store_exact(self, payload: bytes) -> None:
        self._content_by_digest[hashlib.sha256(payload).hexdigest()] = payload

    def store_corrupted(self, digest_hexadecimal: str, payload: bytes) -> None:
        self._content_by_digest[digest_hexadecimal] = payload

    def open_verified_reader(
        self, expected: ExpectedObject
    ) -> AbstractAsyncContextManager[VerifiedObjectReader]:
        self.open_calls.append(expected)

        @asynccontextmanager
        async def _reader() -> AsyncIterator[VerifiedObjectReader]:
            content = self._content_by_digest.get(expected.content_digest.hexadecimal)
            if content is None:
                raise ObjectStorageError(ErrorCode.OBJECT_STORAGE_OBJECT_MISSING)
            if (
                len(content) != expected.size_bytes
                or hashlib.sha256(content).hexdigest() != expected.content_digest.hexadecimal
            ):
                raise ObjectStorageError(ErrorCode.OBJECT_STORAGE_INTEGRITY_FAILED)
            yield _VerifiedBufferReader(content)

        return _reader()


async def seed_exact_version(
    engine: AsyncEngine,
    workspace: DeviceSyncWorkspace,
    payload: bytes,
    *,
    media_type: str = _MEDIA_TYPE,
    locator_text: str | None = None,
) -> tuple[UUID, UUID]:
    """Seed one canonical source/version whose object row names ``payload``.

    The content object row keeps the full canonical shape — including the
    object key the catalog must never surface — while the descriptor the
    catalog resolves derives from the digest, size and media type of the
    exact payload bytes. ``locator_text`` optionally also opens the source's
    active locator row through a create event (the sentinel journeys seed a
    locator-carrying source; the catalog path itself never reads it).
    """

    digest_hexadecimal = hashlib.sha256(payload).hexdigest()
    content_object_id = uuid4()
    source_id = uuid4()
    source_version_id = uuid4()
    create_event_id = uuid4()
    committed_at = datetime.now(UTC)
    async with engine.begin() as connection:
        await connection.execute(
            sa.insert(content_objects).values(
                content_object_id=content_object_id,
                content_hash=digest_hexadecimal,
                object_key=(
                    f"objects/sha256/{digest_hexadecimal[:2]}"
                    f"/{digest_hexadecimal[2:4]}/{digest_hexadecimal}"
                ),
                byte_size=len(payload),
                media_type=media_type,
                verified_at=committed_at,
            )
        )
        await connection.execute(
            sa.insert(sources).values(
                source_id=source_id,
                workspace_id=workspace.workspace_id,
                source_type="markdown",
                title=f"Verified content {uuid4().hex[:12]}",
                sync_state="pending",
                current_version_id=None,
            )
        )
        await connection.execute(
            sa.insert(source_versions).values(
                source_version_id=source_version_id,
                workspace_id=workspace.workspace_id,
                source_id=source_id,
                content_object_id=content_object_id,
                content_version=1,
                author_kind="device",
                author_id=workspace.device_id,
                committed_at=committed_at,
            )
        )
        await connection.execute(
            sa.update(sources)
            .values(
                sync_state="active",
                current_version_id=source_version_id,
                updated_at=sa.text("CURRENT_TIMESTAMP"),
            )
            .where(
                sources.c.workspace_id == workspace.workspace_id,
                sources.c.source_id == source_id,
            )
        )
        if locator_text is not None:
            create_result = await connection.execute(
                sa.insert(sync_events)
                .values(
                    event_id=create_event_id,
                    workspace_id=workspace.workspace_id,
                    source_id=source_id,
                    device_id=workspace.device_id,
                    committed_version_id=source_version_id,
                    base_version_id=None,
                    idempotency_key=f"verified-content-{uuid4().hex}",
                    request_fingerprint=hashlib.sha256(
                        f"verified-content-{source_id.hex}".encode("ascii")
                    ).hexdigest(),
                    event_type="create",
                )
                .returning(sync_events.c.event_sequence)
            )
            create_sequence = int(create_result.scalar_one())
            await connection.execute(
                sa.insert(source_locators).values(
                    source_locator_id=uuid4(),
                    workspace_id=workspace.workspace_id,
                    source_id=source_id,
                    normalized_locator=locator_text,
                    display_locator=locator_text,
                    opened_event_id=create_event_id,
                    opened_sequence=create_sequence,
                )
            )
    return source_id, source_version_id


@pytest_asyncio.fixture
async def content_stack(
    source_publication_stack: SourcePublicationStack,
) -> AsyncIterator[tuple[AsyncEngine, PostgresqlDeviceContentCatalog]]:
    engine = create_source_store_engine(
        source_publication_stack.settings, source_publication_stack.password
    )
    try:
        yield engine, PostgresqlDeviceContentCatalog(engine)
    finally:
        await dispose_source_store_engine(engine)


def _service(
    catalog: PostgresqlDeviceContentCatalog, objects: VerifiedMemoryObjectStore
) -> VerifiedDeviceContentService:
    return VerifiedDeviceContentService(
        catalog=catalog,
        objects=objects,
        metrics=InMemoryDeviceSyncMetrics(),
        diagnostics=None,
    )


# --- exact membership and workspace scope -------------------------------------


@pytest.mark.asyncio
async def test_exact_pair_resolves_the_content_descriptor(
    content_stack: tuple[AsyncEngine, PostgresqlDeviceContentCatalog],
) -> None:
    engine, catalog = content_stack
    workspace = await seed_device_sync_workspace(engine)
    await publish_workspace_policy(engine, workspace)
    payload = secrets.token_bytes(2048)
    source_id, source_version_id = await seed_exact_version(engine, workspace, payload)

    descriptor = await catalog.resolve_descriptor(
        workspace.context(),
        source_id=source_id,
        source_version_id=source_version_id,
        diagnostic_context=_diagnostic(),
    )

    assert descriptor.source_id == source_id
    assert descriptor.source_version_id == source_version_id
    assert descriptor.content_digest.hexadecimal == hashlib.sha256(payload).hexdigest()
    assert descriptor.size_bytes == len(payload)
    assert descriptor.media_type == CanonicalMediaType.parse(_MEDIA_TYPE)
    # The stored object key never crosses into the resolved surface.
    assert not hasattr(descriptor, "object_key")


@pytest.mark.asyncio
async def test_cross_workspace_pair_is_indistinguishable_from_missing(
    content_stack: tuple[AsyncEngine, PostgresqlDeviceContentCatalog],
) -> None:
    engine, catalog = content_stack
    workspace = await seed_device_sync_workspace(engine)
    await publish_workspace_policy(engine, workspace)
    foreign = await seed_device_sync_workspace(engine)
    await publish_workspace_policy(engine, foreign)
    source_b, version_b = await seed_exact_version(engine, foreign, secrets.token_bytes(512))

    with pytest.raises(DeviceSyncError) as raised:
        await catalog.resolve_descriptor(
            workspace.context(),
            source_id=source_b,
            source_version_id=version_b,
            diagnostic_context=_diagnostic(),
        )
    assert raised.value.code is DeviceSyncErrorCode.EVENT_UNAVAILABLE


@pytest.mark.asyncio
async def test_mismatched_and_unknown_pairs_are_unavailable(
    content_stack: tuple[AsyncEngine, PostgresqlDeviceContentCatalog],
) -> None:
    engine, catalog = content_stack
    workspace = await seed_device_sync_workspace(engine)
    await publish_workspace_policy(engine, workspace)
    other = await seed_device_sync_workspace(engine)
    await publish_workspace_policy(engine, other)
    payload = secrets.token_bytes(256)
    source_id, source_version_id = await seed_exact_version(engine, workspace, payload)
    # Distinct bytes: canonical content objects deduplicate globally by hash.
    _, foreign_version_id = await seed_exact_version(engine, other, secrets.token_bytes(256))

    for pair in (
        (source_id, foreign_version_id),  # version belongs to another source
        (uuid4(), source_version_id),  # unknown source
        (source_id, uuid4()),  # unknown version
    ):
        with pytest.raises(DeviceSyncError) as raised:
            await catalog.resolve_descriptor(
                workspace.context(),
                source_id=pair[0],
                source_version_id=pair[1],
                diagnostic_context=_diagnostic(),
            )
        assert raised.value.code is DeviceSyncErrorCode.EVENT_UNAVAILABLE


# --- current-policy authorization before any byte ------------------------------


@pytest.mark.asyncio
async def test_policy_denial_precedes_any_byte_fetch(
    content_stack: tuple[AsyncEngine, PostgresqlDeviceContentCatalog],
) -> None:
    engine, catalog = content_stack
    workspace = await seed_device_sync_workspace(engine)
    await publish_workspace_policy(
        engine,
        workspace,
        rules=(SeededPolicyRule(rule_kind="media_type", text_operand=_MEDIA_TYPE),),
    )
    payload = secrets.token_bytes(1024)
    source_id, source_version_id = await seed_exact_version(engine, workspace, payload)
    objects = VerifiedMemoryObjectStore()
    objects.store_exact(payload)
    service = _service(catalog, objects)

    with pytest.raises(ExclusionPolicyError) as raised:
        async with service.open_content(
            workspace.context(),
            source_id=source_id,
            source_version_id=source_version_id,
            diagnostic_context=_diagnostic(),
        ):
            raise AssertionError("the content context must never be entered")
    assert raised.value.error_code is ErrorCode.EXCLUSION_POLICY_DENIED
    # Authorization failed closed before the object store saw a request.
    assert objects.open_calls == []


@pytest.mark.asyncio
async def test_workspace_without_active_revision_fails_closed_before_bytes(
    content_stack: tuple[AsyncEngine, PostgresqlDeviceContentCatalog],
) -> None:
    engine, catalog = content_stack
    workspace = await seed_device_sync_workspace(engine)
    payload = secrets.token_bytes(1024)
    source_id, source_version_id = await seed_exact_version(engine, workspace, payload)
    objects = VerifiedMemoryObjectStore()
    objects.store_exact(payload)
    service = _service(catalog, objects)

    with pytest.raises(ExclusionPolicyError) as raised:
        async with service.open_content(
            workspace.context(),
            source_id=source_id,
            source_version_id=source_version_id,
            diagnostic_context=_diagnostic(),
        ):
            raise AssertionError("the content context must never be entered")
    assert raised.value.error_code is ErrorCode.EXCLUSION_POLICY_DENIED
    assert objects.open_calls == []


# --- verified download and closed integrity failures ---------------------------


@pytest.mark.asyncio
async def test_verified_download_yields_exact_bytes(
    content_stack: tuple[AsyncEngine, PostgresqlDeviceContentCatalog],
) -> None:
    engine, catalog = content_stack
    workspace = await seed_device_sync_workspace(engine)
    await publish_workspace_policy(engine, workspace)
    payload = secrets.token_bytes(4096)
    source_id, source_version_id = await seed_exact_version(engine, workspace, payload)
    objects = VerifiedMemoryObjectStore()
    objects.store_exact(payload)
    service = _service(catalog, objects)

    async with service.open_content(
        workspace.context(),
        source_id=source_id,
        source_version_id=source_version_id,
        diagnostic_context=_diagnostic(),
    ) as content:
        consumed = bytearray()
        async for chunk in content.reader:
            consumed.extend(chunk)
    assert bytes(consumed) == payload
    assert content.descriptor.content_digest.hexadecimal == (hashlib.sha256(payload).hexdigest())
    # Exactly one verification request for the exact expected bytes.
    assert len(objects.open_calls) == 1
    assert objects.open_calls[0].content_digest == content.descriptor.content_digest
    assert objects.open_calls[0].size_bytes == len(payload)


@pytest.mark.asyncio
async def test_missing_object_is_the_closed_download_integrity_failure(
    content_stack: tuple[AsyncEngine, PostgresqlDeviceContentCatalog],
) -> None:
    engine, catalog = content_stack
    workspace = await seed_device_sync_workspace(engine)
    await publish_workspace_policy(engine, workspace)
    payload = secrets.token_bytes(1024)
    source_id, source_version_id = await seed_exact_version(engine, workspace, payload)
    objects = VerifiedMemoryObjectStore()  # canonical row exists, bytes absent
    service = _service(catalog, objects)

    with pytest.raises(DeviceSyncError) as raised:
        async with service.open_content(
            workspace.context(),
            source_id=source_id,
            source_version_id=source_version_id,
            diagnostic_context=_diagnostic(),
        ):
            raise AssertionError("the content context must never be entered")
    assert raised.value.code is DeviceSyncErrorCode.DOWNLOAD_INTEGRITY_FAILED
    assert "r2" not in (str(raised.value) + repr(raised.value)).lower()


@pytest.mark.asyncio
async def test_corrupt_object_is_the_closed_download_integrity_failure(
    content_stack: tuple[AsyncEngine, PostgresqlDeviceContentCatalog],
) -> None:
    engine, catalog = content_stack
    workspace = await seed_device_sync_workspace(engine)
    await publish_workspace_policy(engine, workspace)
    canonical = secrets.token_bytes(1024)
    corrupted = canonical[:-1] + bytes([canonical[-1] ^ 0xFF])
    source_id, source_version_id = await seed_exact_version(engine, workspace, canonical)
    objects = VerifiedMemoryObjectStore()
    # The stored bytes hash elsewhere: only a full verification can catch it.
    objects.store_corrupted(hashlib.sha256(canonical).hexdigest(), corrupted)
    service = _service(catalog, objects)
    consumed = bytearray()

    with pytest.raises(DeviceSyncError) as raised:
        async with service.open_content(
            workspace.context(),
            source_id=source_id,
            source_version_id=source_version_id,
            diagnostic_context=_diagnostic(),
        ) as content:
            async for chunk in content.reader:
                consumed.extend(chunk)
    assert raised.value.code is DeviceSyncErrorCode.DOWNLOAD_INTEGRITY_FAILED
    # No consumer byte flowed before the full verification failed.
    assert consumed == b""
    rendered = str(raised.value) + repr(raised.value)
    for provider_string in ("r2", "bucket", "etag", "objects/sha256", "endpoint"):
        assert provider_string not in rendered.lower()


# --- the sentinel-laden journey (task 13, step 2) -----------------------------------------


#: Unique sentinels injected through every operand of the verified-download
#: boundary; none may survive into any error rendering, receipt or call
#: evidence the surfaces above produce.
_DEVICE_SYNC_SENTINELS: tuple[str, ...] = (
    "do-not-emit-device-sync-content",
    "do-not-emit-device-sync-locator",
    "do-not-emit-device-sync-path",
    "do-not-emit-device-sync-digest",
    "do-not-emit-device-sync-temp-name",
    "do-not-emit-device-sync-object-key",
    "do-not-emit-device-sync-credential",
    "do-not-emit-device-sync-response-body",
    "do-not-emit-device-sync-provider-exception",
)


class ProviderCrashingObjectStore(VerifiedMemoryObjectStore):
    """The object-store double whose adapter-shaped provider failure carries
    every sentinel: an ``ObjectStorageError`` mapped from a provider
    exception whose text embeds the content, locator, digest, temp name,
    object key, credential, response body and endpoint sentinels."""

    def open_verified_reader(
        self, expected: ExpectedObject
    ) -> AbstractAsyncContextManager[VerifiedObjectReader]:
        self.open_calls.append(expected)

        @asynccontextmanager
        async def _reader() -> AsyncIterator[VerifiedObjectReader]:
            try:
                raise RuntimeError(
                    "provider failed "
                    "content=do-not-emit-device-sync-content "
                    "locator=do-not-emit-device-sync-locator "
                    "path=do-not-emit-device-sync-path "
                    "digest=do-not-emit-device-sync-digest "
                    "temp=do-not-emit-device-sync-temp-name "
                    "key=do-not-emit-device-sync-object-key "
                    "credential=do-not-emit-device-sync-credential "
                    "body=do-not-emit-device-sync-response-body"
                )
            except RuntimeError as cause:
                raise ObjectStorageError(ErrorCode.OBJECT_STORAGE_UNAVAILABLE) from cause
            yield _VerifiedBufferReader(b"")  # pragma: no cover - never reached

        return _reader()


@pytest.mark.asyncio
async def test_sentinel_laden_operands_never_leak_off_the_verified_path(
    content_stack: tuple[AsyncEngine, PostgresqlDeviceContentCatalog],
) -> None:
    """One journey carrying every sentinel through the verified download.

    The canonical bytes embed the content sentinel, the locator text embeds
    the locator/path sentinels, the object key derives from a digest-shaped
    seed and a provider failure embeds the temp-name, object-key, credential
    and response-body sentinels. The resolved descriptor, the mapped typed
    error and every rendering of it may carry none of them: the content
    sentinel may exist ONLY inside the canonical payload bytes the caller
    asked to download, never on any failure surface.
    """

    engine, catalog = content_stack
    workspace = await seed_device_sync_workspace(engine)
    await publish_workspace_policy(engine, workspace)
    payload = (
        b"# note\ncontent do-not-emit-device-sync-content bytes\n"
        b"locator do-not-emit-device-sync-locator\n"
    )
    sentinel_locator = "notes/do-not-emit-device-sync-path.md"
    source_id, source_version_id = await seed_exact_version(
        engine, workspace, payload, locator_text=sentinel_locator
    )
    objects = ProviderCrashingObjectStore()
    service = _service(catalog, objects)

    with pytest.raises(DeviceSyncError) as raised:
        async with service.open_content(
            workspace.context(),
            source_id=source_id,
            source_version_id=source_version_id,
            diagnostic_context=_diagnostic(),
        ):
            raise AssertionError("the content context must never be entered")

    # A provider outage is the retryable dependency reason (only the four
    # verification signals map onto the download-integrity failure).
    assert raised.value.code is DeviceSyncErrorCode.DEPENDENCY_UNAVAILABLE
    rendered_surfaces = str(raised.value) + repr(raised.value)
    assert raised.value.__cause__ is not None
    # The provider failure sits at the deepest cause level: the mapped
    # ObjectStorageError directly chains the sentinel-laden provider
    # exception, so walk the whole chain for the mootness proof — the chain
    # is the source, never a sink; only the rendered surfaces must stay
    # clean.
    provider_cause = raised.value.__cause__
    chain_rendered = ""
    while provider_cause is not None:
        chain_rendered += str(provider_cause) + repr(provider_cause)
        provider_cause = provider_cause.__cause__
    assert "do-not-emit-device-sync-content" in chain_rendered
    for sentinel in _DEVICE_SYNC_SENTINELS:
        assert sentinel not in rendered_surfaces, f"sentinel leaked: {sentinel}"

    # The resolved descriptor surface stays clean of the locator text (the
    # operational wire carries digest/size/media type only) and the store
    # double saw exactly one request shaped by the descriptor's digest.
    descriptor = await catalog.resolve_descriptor(
        workspace.context(),
        source_id=source_id,
        source_version_id=source_version_id,
        diagnostic_context=_diagnostic(),
    )
    descriptor_rendered = repr(descriptor)
    for sentinel in (
        "do-not-emit-device-sync-locator",
        "do-not-emit-device-sync-path",
    ):
        assert sentinel not in descriptor_rendered
    assert len(objects.open_calls) == 1
    assert objects.open_calls[0].content_digest == descriptor.content_digest
