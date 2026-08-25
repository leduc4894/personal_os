"""Verified device content composition: order, mapping and privacy.

These tests pin the runtime download composition without a database or a
provider: the exact-version descriptor resolves first so current-policy
authorization happens before any byte is fetched, exact bytes are yielded
only through the fully verified reader, object absence/corruption and
dependency outages cross the boundary as the closed device codes with the
Task 1 diagnostic reason recorded — never a provider string — and the
descriptor surface never exposes an object key or provider receipt.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from uuid import UUID, uuid4

import pytest
from api_runtime.device_sync_content import (
    VerifiedDeviceContent,
    VerifiedDeviceContentService,
    map_object_storage_failure,
)
from tests.unit.device_sync.fakes import RecordingEventSink, SequenceMonotonic

from personal_os.device_sync.contracts import DeviceContentDescriptor, DeviceSyncContext
from personal_os.device_sync.errors import DeviceSyncError, DeviceSyncErrorCode
from personal_os.device_sync.metrics import (
    DeviceSyncOperation,
    DeviceSyncOutcome,
    InMemoryDeviceSyncMetrics,
)
from personal_os.diagnostics.context import DiagnosticContext, create_diagnostic_context
from personal_os.diagnostics.events import EventName
from personal_os.error_contracts.codes import ErrorCode
from personal_os.exclusion_policy.errors import ExclusionPolicyError
from personal_os.object_storage import CanonicalMediaType, ContentDigest, ExpectedObject
from personal_os.object_storage.errors import ObjectStorageError

_WORKSPACE_ID = uuid4()
_DEVICE_ID = uuid4()
_USER_ID = uuid4()
_SOURCE_ID = uuid4()
_SOURCE_VERSION_ID = uuid4()

_PAYLOAD = b"verified device content payload"
_MEDIA_TYPE = CanonicalMediaType.parse("text/markdown")

_DIAGNOSTIC: DiagnosticContext = create_diagnostic_context().context


def _context() -> DeviceSyncContext:
    return DeviceSyncContext(workspace_id=_WORKSPACE_ID, device_id=_DEVICE_ID, user_id=_USER_ID)


def _descriptor(payload: bytes = _PAYLOAD) -> DeviceContentDescriptor:
    return DeviceContentDescriptor(
        source_id=_SOURCE_ID,
        source_version_id=_SOURCE_VERSION_ID,
        content_digest=ContentDigest.parse(hashlib.sha256(payload).hexdigest()),
        size_bytes=len(payload),
        media_type=_MEDIA_TYPE,
    )


class _VerifiedBufferReader:
    """Bounded async reader over one fixed verified buffer."""

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


class RecordingCatalog:
    """Catalog double resolving one scripted outcome, never fetching bytes."""

    def __init__(
        self,
        *,
        descriptor: DeviceContentDescriptor | None = None,
        error: Exception | None = None,
    ) -> None:
        self._descriptor = descriptor
        self._error = error
        self.calls: list[tuple[UUID, UUID]] = []

    async def resolve_descriptor(
        self,
        context: DeviceSyncContext,
        *,
        source_id: UUID,
        source_version_id: UUID,
        diagnostic_context: DiagnosticContext,
    ) -> DeviceContentDescriptor:
        del context, diagnostic_context
        self.calls.append((source_id, source_version_id))
        if self._error is not None:
            raise self._error
        assert self._descriptor is not None
        return self._descriptor


class RecordingObjectStore:
    """Object-store double opening scripted verified readers, counting opens."""

    def __init__(self, *, error: ObjectStorageError | None = None) -> None:
        self._error = error
        self.open_calls: list[ExpectedObject] = []

    def open_verified_reader(
        self, expected: ExpectedObject
    ) -> AbstractAsyncContextManager[_VerifiedBufferReader]:
        self.open_calls.append(expected)

        @asynccontextmanager
        async def _reader() -> AsyncIterator[_VerifiedBufferReader]:
            if self._error is not None:
                raise self._error
            yield _VerifiedBufferReader(_PAYLOAD)

        return _reader()


class UnusedObjectStore:
    """Object-store double failing the test if any byte path is entered."""

    def open_verified_reader(
        self, expected: ExpectedObject
    ) -> AbstractAsyncContextManager[_VerifiedBufferReader]:
        raise AssertionError("no byte path may run before authorization allows it")


def _download_count(metrics: InMemoryDeviceSyncMetrics) -> int:
    """Total download operations metered across every outcome and reason."""

    return sum(
        metrics.operation_count(operation=DeviceSyncOperation.DOWNLOAD, outcome=outcome)
        for outcome in DeviceSyncOutcome
    )


def _service(
    *,
    catalog: RecordingCatalog,
    objects: UnusedObjectStore | RecordingObjectStore,
    sink: RecordingEventSink | None = None,
) -> tuple[VerifiedDeviceContentService, InMemoryDeviceSyncMetrics]:
    metrics = InMemoryDeviceSyncMetrics()
    service = VerifiedDeviceContentService(
        catalog=catalog,
        objects=objects,
        metrics=metrics,
        diagnostics=sink,
        monotonic=SequenceMonotonic(moments=[10.0, 10.25]),
    )
    return service, metrics


# --- success -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_content_yields_exact_bytes_after_verification() -> None:
    descriptor = _descriptor()
    catalog = RecordingCatalog(descriptor=descriptor)
    store = RecordingObjectStore()
    sink = RecordingEventSink()
    service, metrics = _service(catalog=catalog, objects=store, sink=sink)

    async with service.open_content(
        _context(),
        source_id=_SOURCE_ID,
        source_version_id=_SOURCE_VERSION_ID,
        diagnostic_context=_DIAGNOSTIC,
    ) as content:
        assert isinstance(content, VerifiedDeviceContent)
        assert content.descriptor is descriptor
        assert not hasattr(content.descriptor, "object_key")
        consumed = bytearray()
        async for chunk in content.reader:
            consumed.extend(chunk)
    assert bytes(consumed) == _PAYLOAD
    # The reader was opened with exactly the descriptor's verification
    # request: digest, size and media, never a key.
    assert store.open_calls == [descriptor.expected_object()]
    assert (
        metrics.operation_count(
            operation=DeviceSyncOperation.DOWNLOAD, outcome=DeviceSyncOutcome.SUCCEEDED
        )
        == 1
    )
    assert sink.last_event_name() is EventName.DEVICE_SYNC_OPERATION_COMPLETED
    fields = sink.last_fields()
    assert fields["operation"] is DeviceSyncOperation.DOWNLOAD
    assert isinstance(fields["duration_ms"], int)
    assert "reason" not in fields


# --- authorization precedes any byte -----------------------------------------


@pytest.mark.asyncio
async def test_policy_denial_raises_the_registry_rejection_before_any_bytes() -> None:
    catalog = RecordingCatalog(error=ExclusionPolicyError(ErrorCode.EXCLUSION_POLICY_DENIED))
    store = RecordingObjectStore()
    sink = RecordingEventSink()
    service, metrics = _service(catalog=catalog, objects=store, sink=sink)

    with pytest.raises(ExclusionPolicyError) as raised:
        async with service.open_content(
            _context(),
            source_id=_SOURCE_ID,
            source_version_id=_SOURCE_VERSION_ID,
            diagnostic_context=_DIAGNOSTIC,
        ):
            raise AssertionError("the content context must never be entered")
    assert raised.value.error_code is ErrorCode.EXCLUSION_POLICY_DENIED
    # The authorization boundary rejected before the object store was asked
    # for a single byte: no reader open, no device operation metered.
    assert store.open_calls == []
    assert sink.emitted == []
    assert _download_count(metrics) == 0


# --- membership and integrity mapping ----------------------------------------


@pytest.mark.asyncio
async def test_missing_membership_records_the_closed_rejection() -> None:
    catalog = RecordingCatalog(error=DeviceSyncError(DeviceSyncErrorCode.EVENT_UNAVAILABLE))
    sink = RecordingEventSink()
    service, metrics = _service(catalog=catalog, objects=UnusedObjectStore(), sink=sink)

    with pytest.raises(DeviceSyncError) as raised:
        async with service.open_content(
            _context(),
            source_id=_SOURCE_ID,
            source_version_id=_SOURCE_VERSION_ID,
            diagnostic_context=_DIAGNOSTIC,
        ):
            raise AssertionError("the content context must never be entered")
    assert raised.value.code is DeviceSyncErrorCode.EVENT_UNAVAILABLE
    assert (
        metrics.operation_count(
            operation=DeviceSyncOperation.DOWNLOAD,
            outcome=DeviceSyncOutcome.REJECTED,
            reason=DeviceSyncErrorCode.EVENT_UNAVAILABLE,
        )
        == 1
    )
    assert sink.last_event_name() is EventName.DEVICE_SYNC_OPERATION_REJECTED
    fields = sink.last_fields()
    assert fields["operation"] is DeviceSyncOperation.DOWNLOAD
    assert fields["reason"] is DeviceSyncErrorCode.EVENT_UNAVAILABLE
    assert isinstance(fields["duration_ms"], int)


@pytest.mark.asyncio
async def test_missing_object_is_the_closed_download_integrity_failure() -> None:
    catalog = RecordingCatalog(descriptor=_descriptor())
    store = RecordingObjectStore(error=ObjectStorageError(ErrorCode.OBJECT_STORAGE_OBJECT_MISSING))
    sink = RecordingEventSink()
    service, metrics = _service(catalog=catalog, objects=store, sink=sink)

    with pytest.raises(DeviceSyncError) as raised:
        async with service.open_content(
            _context(),
            source_id=_SOURCE_ID,
            source_version_id=_SOURCE_VERSION_ID,
            diagnostic_context=_DIAGNOSTIC,
        ):
            raise AssertionError("the content context must never be entered")
    assert raised.value.code is DeviceSyncErrorCode.DOWNLOAD_INTEGRITY_FAILED
    rendered = str(raised.value) + repr(raised.value)
    for provider_string in ("r2", "bucket", "etag", "objects/sha256", "endpoint"):
        assert provider_string not in rendered.lower()
    assert (
        metrics.operation_count(
            operation=DeviceSyncOperation.DOWNLOAD,
            outcome=DeviceSyncOutcome.REJECTED,
            reason=DeviceSyncErrorCode.DOWNLOAD_INTEGRITY_FAILED,
        )
        == 1
    )
    assert sink.last_event_name() is EventName.DEVICE_SYNC_OPERATION_REJECTED
    assert sink.last_fields()["reason"] is DeviceSyncErrorCode.DOWNLOAD_INTEGRITY_FAILED


@pytest.mark.asyncio
async def test_corrupt_object_is_the_closed_download_integrity_failure() -> None:
    catalog = RecordingCatalog(descriptor=_descriptor())
    store = RecordingObjectStore(
        error=ObjectStorageError(ErrorCode.OBJECT_STORAGE_INTEGRITY_FAILED)
    )
    service, metrics = _service(catalog=catalog, objects=store)

    with pytest.raises(DeviceSyncError) as raised:
        async with service.open_content(
            _context(),
            source_id=_SOURCE_ID,
            source_version_id=_SOURCE_VERSION_ID,
            diagnostic_context=_DIAGNOSTIC,
        ):
            raise AssertionError("the content context must never be entered")
    assert raised.value.code is DeviceSyncErrorCode.DOWNLOAD_INTEGRITY_FAILED
    assert (
        metrics.operation_count(
            operation=DeviceSyncOperation.DOWNLOAD,
            outcome=DeviceSyncOutcome.REJECTED,
            reason=DeviceSyncErrorCode.DOWNLOAD_INTEGRITY_FAILED,
        )
        == 1
    )


@pytest.mark.asyncio
async def test_object_availability_outage_is_the_retryable_dependency_code() -> None:
    catalog = RecordingCatalog(descriptor=_descriptor())
    store = RecordingObjectStore(error=ObjectStorageError(ErrorCode.OBJECT_STORAGE_UNAVAILABLE))
    sink = RecordingEventSink()
    service, metrics = _service(catalog=catalog, objects=store, sink=sink)

    with pytest.raises(DeviceSyncError) as raised:
        async with service.open_content(
            _context(),
            source_id=_SOURCE_ID,
            source_version_id=_SOURCE_VERSION_ID,
            diagnostic_context=_DIAGNOSTIC,
        ):
            raise AssertionError("the content context must never be entered")
    assert raised.value.code is DeviceSyncErrorCode.DEPENDENCY_UNAVAILABLE
    assert raised.value.is_retryable
    assert (
        metrics.operation_count(
            operation=DeviceSyncOperation.DOWNLOAD,
            outcome=DeviceSyncOutcome.FAILED,
            reason=DeviceSyncErrorCode.DEPENDENCY_UNAVAILABLE,
        )
        == 1
    )
    assert sink.last_event_name() is EventName.DEVICE_SYNC_OPERATION_FAILED


# --- consumer failure and mapping closure ------------------------------------


@pytest.mark.asyncio
async def test_consumer_failure_propagates_unmetered() -> None:
    catalog = RecordingCatalog(descriptor=_descriptor())
    store = RecordingObjectStore()
    sink = RecordingEventSink()
    service, metrics = _service(catalog=catalog, objects=store, sink=sink)

    class _ConsumerAbort(RuntimeError):
        pass

    with pytest.raises(_ConsumerAbort):
        async with service.open_content(
            _context(),
            source_id=_SOURCE_ID,
            source_version_id=_SOURCE_VERSION_ID,
            diagnostic_context=_DIAGNOSTIC,
        ):
            raise _ConsumerAbort("consumer-owned failure")
    # The server-side download completed; the consumer's own failure is not
    # a device operation outcome and is never metered as one.
    assert sink.emitted == []
    assert _download_count(metrics) == 0


def test_object_failure_mapping_is_the_closed_integrity_set() -> None:
    for code in (
        ErrorCode.OBJECT_STORAGE_OBJECT_MISSING,
        ErrorCode.OBJECT_STORAGE_INTEGRITY_FAILED,
        ErrorCode.OBJECT_STORAGE_METADATA_CONFLICT,
        ErrorCode.OBJECT_STORAGE_CONTRACT_INVALID,
    ):
        mapped = map_object_storage_failure(ObjectStorageError(code))
        assert mapped.code is DeviceSyncErrorCode.DOWNLOAD_INTEGRITY_FAILED
        assert not mapped.is_retryable
    for code in (
        ErrorCode.OBJECT_STORAGE_UNAVAILABLE,
        ErrorCode.OBJECT_STORAGE_BUSY,
        ErrorCode.OBJECT_STORAGE_ACCESS_DENIED,
        ErrorCode.OBJECT_STORAGE_CONFIGURATION_INVALID,
    ):
        mapped = map_object_storage_failure(ObjectStorageError(code))
        assert mapped.code is DeviceSyncErrorCode.DEPENDENCY_UNAVAILABLE
        assert mapped.is_retryable


def test_composition_surface_exposes_no_key_or_receipt() -> None:
    # The delivered value object carries only the descriptor and the
    # verified reader: no receipt, key or provider field exists on it.
    content = VerifiedDeviceContent(descriptor=_descriptor(), reader=_VerifiedBufferReader(b""))
    assert not hasattr(content, "object_key")
    assert not hasattr(content, "receipt")
    assert not hasattr(content.descriptor, "object_key")
    assert "<redacted>" in repr(content)
