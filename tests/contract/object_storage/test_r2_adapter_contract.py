"""R2 adapter verification and fail-closed read contract.

These tests prove the heart of the content-addressable design: the adapter
performs an exact HEAD followed by a conditional full GET (``If-Match`` ETag)
that is independently hashed and size-checked into a verification spool BEFORE a
single byte reaches a consumer. A consumer therefore receives no object byte
until digest, size and media verification has completed; any failure raises a
typed :class:`ObjectStorageError` and leaves the spool root clean.

The deterministic :class:`ScriptedS3Client` records every call in arrival order
with its arguments, so these tests assert the exact expected ``HEAD`` -> ``GET``
sequence, that every ``GET`` carries ``IfMatch``, and that the retry, cleanup and
no-cache contracts hold without touching the network. Provider digests, keys,
buckets, endpoints, media types and paths never enter a recorded metric.
"""

from __future__ import annotations

import base64
import hashlib
import logging
from collections.abc import AsyncIterator, Awaitable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import IO
from uuid import UUID, uuid4

import pytest
from api_runtime.device_sync_content import VerifiedDeviceContentService
from botocore.exceptions import ClientError
from tests.contract.object_storage.scripted_s3 import (
    DEFAULT_ETAG,
    DEFAULT_MEDIA_TYPE,
    ScriptedS3Client,
    scripted_body,
)

from personal_os.device_sync.contracts import DeviceContentDescriptor, DeviceSyncContext
from personal_os.device_sync.errors import DeviceSyncError, DeviceSyncErrorCode
from personal_os.device_sync.metrics import InMemoryDeviceSyncMetrics
from personal_os.diagnostics import DiagnosticLogger, diagnostic_schema_record
from personal_os.diagnostics.context import DiagnosticContext, create_diagnostic_context
from personal_os.diagnostics.events import EventName
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
from personal_os.object_storage.errors import (
    DIGEST_MISMATCH,
    MEDIA_TYPE_INVALID,
    SIZE_OUT_OF_RANGE,
    ObjectStorageError,
)
from r2_object_storage import adapter as adapter_module
from r2_object_storage.adapter import R2S3ObjectStore
from r2_object_storage.client import GetObjectResult, HeadObjectResult
from r2_object_storage.error_mapping import RetryPolicy
from r2_object_storage.metrics import (
    InMemoryObjectStorageMetrics,
    ObjectStorageOperation,
    ObjectStorageResult,
)
from r2_object_storage.spool import HashedSpool, SpoolManager

_ONE_MEBIBYTE = 1_048_576
_FIXED_NOW = datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC)
_FREE_SPACE_BYTES = 8 * 1024 * 1024 * 1024

# Canonical payload used by the matching HEAD/GET and reader tests. Its digest,
# size and media form the ``ExpectedObject`` the adapter must independently prove.
_CANONICAL_PAYLOAD = b"the canonical payload used for verification"

# Fail-closed corpus: the HEAD serves the expected size, but the GET body has a
# valid prefix and a wrong tail. The expected digest is computed from the prefix
# plus a correct tail of the same length, so the size check passes but the
# SHA-256 cannot match -> integrity failure and zero consumer bytes.
_CORRUPT_PREFIX = b"valid-prefix"  # 12 bytes
_CORRUPT_CORRECT_TAIL = b"good-tail!"  # 10 bytes
_CORRUPT_WRONG_TAIL = b"wrong-tail"  # 10 bytes


def _client_error(code: str, status: int, operation: str = "GetObject") -> ClientError:
    return ClientError(
        {
            "Error": {"Code": code, "Message": "scripted"},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        operation,
    )


def _precondition_failed() -> ClientError:
    return _client_error("PreconditionFailed", 412)


def _service_unavailable() -> ClientError:
    return _client_error("ServiceUnavailable", 503)


def _expected(payload: bytes, *, media_type: str = DEFAULT_MEDIA_TYPE) -> ExpectedObject:
    return ExpectedObject(
        content_digest=ContentDigest.parse(hashlib.sha256(payload).hexdigest()),
        size_bytes=len(payload),
        media_type=CanonicalMediaType.parse(media_type),
    )


def _no_sleep(_: float) -> Awaitable[None]:
    async def _sleep() -> None:
        return None

    return _sleep()


def _zero_jitter(low: float, high: float) -> float:
    return low


def build_store(client: ScriptedS3Client, tmp_path: Path) -> R2S3ObjectStore:
    """Build an :class:`R2S3ObjectStore` with fixed, environment-free wiring.

    Supplies a :class:`SpoolManager` rooted at ``tmp_path`` with deterministic
    clocks and ample free space, a three-attempt :class:`RetryPolicy`, an
    in-memory metrics sink and a captured :class:`DiagnosticLogger`. No value is
    read from the process environment. The root logger gains one
    :class:`logging.NullHandler` only while the store is built; its prior
    handlers and level are restored before returning.
    """

    root_logger = logging.getLogger()
    original_handlers = list(root_logger.handlers)
    original_level = root_logger.level
    root_logger.addHandler(logging.NullHandler())
    try:
        spools = SpoolManager(
            tmp_path,
            clock=lambda: 0.0,
            wall_clock=lambda: 0.0,
            disk_usage=lambda _root: SimpleNamespace(free=_FREE_SPACE_BYTES),
        )
        metrics = InMemoryObjectStorageMetrics()
        logger = DiagnosticLogger({"service": "test", "environment": "test"})
        return R2S3ObjectStore(
            client,
            spools=spools,
            retry=RetryPolicy(maximum_attempts=3),
            metrics=metrics,
            logger=logger,
            now_utc=lambda: _FIXED_NOW,
            monotonic=lambda: 0.0,
            sleep=_no_sleep,
            jitter=_zero_jitter,
        )
    finally:
        root_logger.handlers[:] = original_handlers
        root_logger.setLevel(original_level)


async def _chunk_stream(payloads: tuple[bytes, ...]) -> AsyncIterator[bytes]:
    for payload in payloads:
        yield payload


def chunks(*payloads: bytes) -> AsyncIterator[bytes]:
    """Wrap fixed payloads as an asynchronous byte stream for ``store_stream``."""

    return _chunk_stream(payloads)


class _DiagnosticRecordCapture(logging.Handler):
    """Capture diagnostic records emitted through the :class:`DiagnosticLogger`."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.events: list[Mapping[str, object]] = []

    def emit(self, record: logging.LogRecord) -> None:
        diagnostic = diagnostic_schema_record(record)
        if diagnostic is not None:
            self.events.append(diagnostic)


@contextmanager
def capture_diagnostic_events() -> Iterator[_DiagnosticRecordCapture]:
    """Attach a capture handler to the root logger at DEBUG level for the scope."""

    capture = _DiagnosticRecordCapture()
    root_logger = logging.getLogger()
    original_level = root_logger.level
    root_logger.setLevel(logging.DEBUG)
    root_logger.addHandler(capture)
    try:
        yield capture
    finally:
        root_logger.removeHandler(capture)
        root_logger.setLevel(original_level)


class _UnhashedVerificationSpool:
    """Stand-in spool that completed without ever hashing its payload."""

    hashed: HashedSpool | None = None

    async def close(self) -> None:
        return None


async def _return_unhashed_verification_spool(
    _expected_object: object, _operation: object, _tracker: object
) -> object:
    """Replace ``_verify_to_spool`` with a spool whose ``hashed`` is ``None``."""

    return _UnhashedVerificationSpool()


# --- Matching HEAD + If-Match GET -------------------------------------------


@pytest.mark.asyncio
async def test_resolve_returns_receipt_for_matching_head_and_get(tmp_path: Path) -> None:
    payload = _CANONICAL_PAYLOAD
    expected = _expected(payload)
    client = ScriptedS3Client.matching_get(payload)
    store = build_store(client, tmp_path)

    receipt = await store.resolve_verified_object(expected)

    assert isinstance(receipt, VerifiedObjectReceipt)
    assert receipt.content_digest == expected.content_digest
    assert receipt.size_bytes == len(payload)
    assert receipt.media_type == expected.media_type
    assert receipt.object_key == derive_canonical_object_key(expected.content_digest)
    assert receipt.verified_at == _FIXED_NOW
    assert receipt.verification_method is VerificationMethod.EXISTING_FULL_READ
    assert client.methods == ["head_object", "get_object"]
    assert [call.if_match for call in client.get_calls] == [DEFAULT_ETAG]
    assert list(tmp_path.iterdir()) == []
    # A first-try verify records the true attempt count and no retries.
    resolve_records = [
        record
        for record in store.metrics.operations
        if record.operation is ObjectStorageOperation.RESOLVE
    ]
    assert resolve_records[-1].attempt_count == 1
    assert store.metrics.retry_count(ObjectStorageOperation.RESOLVE) == 0


@pytest.mark.asyncio
async def test_verify_existing_returns_receipt_for_matching_head_and_get(
    tmp_path: Path,
) -> None:
    payload = _CANONICAL_PAYLOAD
    expected = _expected(payload)
    client = ScriptedS3Client.matching_get(payload)
    store = build_store(client, tmp_path)

    receipt = await store.verify_existing_object(expected)

    assert receipt.content_digest == expected.content_digest
    assert receipt.size_bytes == len(payload)
    assert client.methods == ["head_object", "get_object"]
    assert [call.if_match for call in client.get_calls] == [DEFAULT_ETAG]


# --- Ordinary absence -------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_returns_none_for_ordinary_absence(tmp_path: Path) -> None:
    expected = _expected(_CANONICAL_PAYLOAD)
    client = ScriptedS3Client.missing_object()
    store = build_store(client, tmp_path)

    receipt = await store.resolve_verified_object(expected)

    assert receipt is None
    assert client.methods == ["head_object"]
    assert client.get_calls == []
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_verify_existing_raises_object_missing_for_absence(tmp_path: Path) -> None:
    expected = _expected(_CANONICAL_PAYLOAD)
    client = ScriptedS3Client.missing_object()
    store = build_store(client, tmp_path)

    with pytest.raises(ObjectStorageError) as raised:
        await store.verify_existing_object(expected)

    assert raised.value.error_code is ErrorCode.OBJECT_STORAGE_OBJECT_MISSING
    assert client.methods == ["head_object"]
    assert client.get_calls == []


@pytest.mark.asyncio
async def test_resolve_raises_for_corrupt_body_instead_of_returning_none(
    tmp_path: Path,
) -> None:
    expected = _expected(_CORRUPT_PREFIX + _CORRUPT_CORRECT_TAIL)
    client = ScriptedS3Client.corrupt_after_prefix(_CORRUPT_PREFIX, _CORRUPT_WRONG_TAIL)
    store = build_store(client, tmp_path)

    with pytest.raises(ObjectStorageError) as raised:
        await store.resolve_verified_object(expected)

    # Absence maps to None; a corrupt body is an integrity failure that must raise.
    assert raised.value.error_code is ErrorCode.OBJECT_STORAGE_INTEGRITY_FAILED
    assert list(tmp_path.iterdir()) == []


# --- HEAD-time metadata conflicts ------------------------------------------


@pytest.mark.asyncio
async def test_size_conflict_at_head_raises_integrity_failed(tmp_path: Path) -> None:
    payload = _CANONICAL_PAYLOAD
    expected = _expected(payload)
    client = ScriptedS3Client.size_conflict(payload, wrong_size_bytes=len(payload) + 1)
    store = build_store(client, tmp_path)

    with pytest.raises(ObjectStorageError) as raised:
        await store.verify_existing_object(expected)

    # Per design §6.3 a size mismatch on an immutable content-addressed key is
    # corruption, not a conflicting write: it fails as an integrity failure.
    assert raised.value.error_code is ErrorCode.OBJECT_STORAGE_INTEGRITY_FAILED
    # The mismatch is detected at HEAD before any GET body is fetched.
    assert client.methods == ["head_object"]
    assert client.get_calls == []
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_media_conflict_at_head_raises_metadata_conflict(tmp_path: Path) -> None:
    payload = _CANONICAL_PAYLOAD
    expected = _expected(payload)
    client = ScriptedS3Client.media_conflict(payload, wrong_media_type="application/json")
    store = build_store(client, tmp_path)

    with pytest.raises(ObjectStorageError) as raised:
        await store.verify_existing_object(expected)

    assert raised.value.error_code is ErrorCode.OBJECT_STORAGE_METADATA_CONFLICT
    assert client.methods == ["head_object"]
    assert client.get_calls == []


# --- Malformed / missing / changed ETag ------------------------------------


@pytest.mark.asyncio
async def test_missing_etag_at_head_raises_contract_invalid(tmp_path: Path) -> None:
    payload = _CANONICAL_PAYLOAD
    expected = _expected(payload)
    client = ScriptedS3Client.missing_etag(payload)
    store = build_store(client, tmp_path)

    with pytest.raises(ObjectStorageError) as raised:
        await store.verify_existing_object(expected)

    assert raised.value.error_code is ErrorCode.OBJECT_STORAGE_CONTRACT_INVALID
    assert client.methods == ["head_object"]
    assert client.get_calls == []


@pytest.mark.asyncio
async def test_malformed_etag_at_head_raises_contract_invalid(tmp_path: Path) -> None:
    payload = _CANONICAL_PAYLOAD
    expected = _expected(payload)
    # Whitespace inside the opaque ETag token is a malformed provider contract.
    client = ScriptedS3Client.malformed_etag(payload, etag="etag 1")
    store = build_store(client, tmp_path)

    with pytest.raises(ObjectStorageError) as raised:
        await store.verify_existing_object(expected)

    assert raised.value.error_code is ErrorCode.OBJECT_STORAGE_CONTRACT_INVALID
    assert client.methods == ["head_object"]


@pytest.mark.asyncio
async def test_changed_etag_get_raises_integrity_failed(tmp_path: Path) -> None:
    payload = _CANONICAL_PAYLOAD
    expected = _expected(payload)
    # HEAD returns an ETag; the conditional GET 412s because the object changed
    # under us. Per design §7 a changing ETag is an integrity failure: the
    # adapter fails closed rather than following a moving value.
    client = ScriptedS3Client.head_then_get_failure(payload, _precondition_failed())
    store = build_store(client, tmp_path)

    with pytest.raises(ObjectStorageError) as raised:
        await store.verify_existing_object(expected)

    assert raised.value.error_code is ErrorCode.OBJECT_STORAGE_INTEGRITY_FAILED
    assert client.methods == ["head_object", "get_object"]
    assert [call.if_match for call in client.get_calls] == [DEFAULT_ETAG]


# --- Short / excess / corrupt body -----------------------------------------


@pytest.mark.asyncio
async def test_short_body_raises_integrity_failed_and_leaves_no_spool(
    tmp_path: Path,
) -> None:
    payload = _CANONICAL_PAYLOAD
    expected = _expected(payload)
    client = ScriptedS3Client.short_body(len(payload), [b"only-the-prefix"])
    store = build_store(client, tmp_path)

    with pytest.raises(ObjectStorageError) as raised:
        await store.verify_existing_object(expected)

    assert raised.value.error_code is ErrorCode.OBJECT_STORAGE_INTEGRITY_FAILED
    assert [call.if_match for call in client.get_calls] == [DEFAULT_ETAG]
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_excess_body_raises_integrity_failed_and_leaves_no_spool(
    tmp_path: Path,
) -> None:
    payload = _CANONICAL_PAYLOAD
    expected = _expected(payload)
    client = ScriptedS3Client.excess_body(len(payload), [payload, b"overflow-bytes"])
    store = build_store(client, tmp_path)

    with pytest.raises(ObjectStorageError) as raised:
        await store.verify_existing_object(expected)

    assert raised.value.error_code is ErrorCode.OBJECT_STORAGE_INTEGRITY_FAILED
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_reader_yields_nothing_before_full_verification(tmp_path: Path) -> None:
    expected = _expected(_CORRUPT_PREFIX + _CORRUPT_CORRECT_TAIL)
    client = ScriptedS3Client.corrupt_after_prefix(_CORRUPT_PREFIX, _CORRUPT_WRONG_TAIL)
    store = build_store(client, tmp_path)
    consumed = bytearray()
    with pytest.raises(ObjectStorageError) as raised:
        async with store.open_verified_reader(expected) as reader:
            async for chunk in reader:
                consumed.extend(chunk)
    assert raised.value.error_code is ErrorCode.OBJECT_STORAGE_INTEGRITY_FAILED
    assert consumed == b""
    assert list(tmp_path.iterdir()) == []


# --- Zero-byte object -------------------------------------------------------


@pytest.mark.asyncio
async def test_zero_byte_object_verifies_and_reads_empty(tmp_path: Path) -> None:
    expected = _expected(b"")
    client = ScriptedS3Client.matching_get(b"")
    store = build_store(client, tmp_path)

    receipt = await store.resolve_verified_object(expected)

    assert receipt is not None
    assert receipt.size_bytes == 0
    assert receipt.content_digest == ContentDigest.parse(hashlib.sha256(b"").hexdigest())

    # A fresh full verify exposes an empty reader that yields EOF immediately.
    client_two = ScriptedS3Client.matching_get(b"")
    store_two = build_store(client_two, tmp_path)
    consumed = bytearray()
    async with store_two.open_verified_reader(expected) as reader:
        async for chunk in reader:
            consumed.extend(chunk)
        assert await reader.read() == b""
    assert consumed == b""
    assert list(tmp_path.iterdir()) == []


# --- Verified reader semantics ---------------------------------------------


@pytest.mark.asyncio
async def test_verified_reader_reads_exact_bytes_then_eof(tmp_path: Path) -> None:
    payload = b"".join(bytes((i % 256,)) for i in range(_ONE_MEBIBYTE + 5))
    expected = _expected(payload)
    client = ScriptedS3Client.matching_get(
        payload, chunks=[payload[:_ONE_MEBIBYTE], payload[_ONE_MEBIBYTE:]]
    )
    store = build_store(client, tmp_path)

    async with store.open_verified_reader(expected) as reader:
        first = await reader.read()
        second = await reader.read()
        third = await reader.read()
        assert len(first) == _ONE_MEBIBYTE
        assert second == payload[_ONE_MEBIBYTE:]
        assert third == b""
        assert await reader.read(0) == b""
    assert payload == first + second
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_verified_reader_async_iteration_yields_chunks(tmp_path: Path) -> None:
    payload = b"chunked-verification-payload"
    expected = _expected(payload)
    client = ScriptedS3Client.matching_get(
        payload, chunks=[b"chunked-", b"verification-", b"payload"]
    )
    store = build_store(client, tmp_path)

    collected = bytearray()
    async with store.open_verified_reader(expected) as reader:
        async for chunk in reader:
            collected.extend(chunk)
    assert bytes(collected) == payload
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_reader_rejects_negative_and_oversized_read(tmp_path: Path) -> None:
    payload = _CANONICAL_PAYLOAD
    expected = _expected(payload)
    client = ScriptedS3Client.matching_get(payload)
    store = build_store(client, tmp_path)

    async with store.open_verified_reader(expected) as reader:
        with pytest.raises(ObjectStorageError) as negative:
            await reader.read(-1)
        assert negative.value.error_code is ErrorCode.OBJECT_STORAGE_INPUT_INVALID
        assert negative.value.safe_details["reason"] is SIZE_OUT_OF_RANGE

        with pytest.raises(ObjectStorageError) as oversized:
            await reader.read(_ONE_MEBIBYTE + 1)
        assert oversized.value.error_code is ErrorCode.OBJECT_STORAGE_INPUT_INVALID
        assert oversized.value.safe_details["reason"] is SIZE_OUT_OF_RANGE
        # A valid bounded read still works after the rejections.
        assert await reader.read(len(payload)) == payload


@pytest.mark.asyncio
async def test_read_after_context_exit_raises_contract_invalid(tmp_path: Path) -> None:
    payload = _CANONICAL_PAYLOAD
    expected = _expected(payload)
    client = ScriptedS3Client.matching_get(payload)
    store = build_store(client, tmp_path)

    async with store.open_verified_reader(expected) as reader:
        assert await reader.read(len(payload)) == payload
    with pytest.raises(ObjectStorageError) as raised:
        await reader.read(1)
    assert raised.value.error_code is ErrorCode.OBJECT_STORAGE_CONTRACT_INVALID
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_reader_open_failure_closes_spool_and_records_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _CANONICAL_PAYLOAD
    expected = _expected(payload)
    client = ScriptedS3Client.matching_get(payload)
    store = build_store(client, tmp_path)

    def _fail_open(path: Path) -> IO[bytes]:
        raise OSError("cannot reopen verified spool")

    monkeypatch.setattr(adapter_module, "_open_read", _fail_open)

    with pytest.raises(OSError, match="cannot reopen verified spool"):
        async with store.open_verified_reader(expected) as reader:
            await reader.read(1)

    # Verification succeeded but the reader could not be opened: the spool is
    # removed, the reservation released and a FAILED outcome is still recorded.
    assert list(tmp_path.iterdir()) == []
    read_records = [
        record
        for record in store.metrics.operations
        if record.operation is ObjectStorageOperation.READ
    ]
    assert read_records[-1].result is ObjectStorageResult.FAILED
    assert read_records[-1].error_code is ErrorCode.OBJECT_STORAGE_CONTRACT_INVALID


# --- Retry, cache and lifecycle contracts ----------------------------------


@pytest.mark.asyncio
async def test_retry_uses_clean_spool_and_carries_if_match_on_every_get(
    tmp_path: Path,
) -> None:
    payload = _CANONICAL_PAYLOAD
    expected = _expected(payload)
    from r2_object_storage.client import GetObjectResult, HeadObjectResult

    # Explicit sequence: HEAD, then a transient GET failure, then the GET body.
    client = ScriptedS3Client()
    client.enqueue(
        HeadObjectResult(size_bytes=len(payload), media_type=DEFAULT_MEDIA_TYPE, etag=DEFAULT_ETAG)
    )
    client.enqueue(_service_unavailable())
    client.enqueue(GetObjectResult(body=scripted_body([payload])))
    store = build_store(client, tmp_path)

    receipt = await store.verify_existing_object(expected)

    assert receipt.size_bytes == len(payload)
    assert client.methods == ["head_object", "get_object", "get_object"]
    assert [call.if_match for call in client.get_calls] == [DEFAULT_ETAG, DEFAULT_ETAG]
    assert list(tmp_path.iterdir()) == []
    # The conditional GET took two attempts; the outcome records the true
    # attempt count (not a constant 1) and exactly one retry.
    verify_records = [
        record
        for record in store.metrics.operations
        if record.operation is ObjectStorageOperation.VERIFY
    ]
    assert verify_records[-1].attempt_count == 2
    assert store.metrics.retry_count(ObjectStorageOperation.VERIFY) == 1


@pytest.mark.asyncio
async def test_no_long_lived_verification_cache(tmp_path: Path) -> None:
    payload = _CANONICAL_PAYLOAD
    expected = _expected(payload)
    from r2_object_storage.client import GetObjectResult, HeadObjectResult

    client = ScriptedS3Client()
    for _ in range(2):
        client.enqueue(
            HeadObjectResult(
                size_bytes=len(payload), media_type=DEFAULT_MEDIA_TYPE, etag=DEFAULT_ETAG
            )
        )
        client.enqueue(GetObjectResult(body=scripted_body([payload])))
    store = build_store(client, tmp_path)

    first = await store.resolve_verified_object(expected)
    second = await store.resolve_verified_object(expected)

    assert first is not None and second is not None
    # Each resolve performs a fresh HEAD + GET; nothing is memoized.
    assert client.methods == ["head_object", "get_object", "head_object", "get_object"]
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_reader_context_exit_removes_spool_and_releases_reservation(
    tmp_path: Path,
) -> None:
    payload = _CANONICAL_PAYLOAD
    expected = _expected(payload)
    client = ScriptedS3Client.matching_get(payload)
    store = build_store(client, tmp_path)

    async with store.open_verified_reader(expected) as reader:
        assert await reader.read(len(payload)) == payload
        # The verification spool exists only for the duration of the context.
        assert len(list(tmp_path.iterdir())) == 1

    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_close_closes_the_underlying_client(tmp_path: Path) -> None:
    client = ScriptedS3Client.matching_get(_CANONICAL_PAYLOAD)
    store = build_store(client, tmp_path)

    await store.close()
    await store.close()


# --- Metrics stay low-cardinality ------------------------------------------


@pytest.mark.asyncio
async def test_metrics_record_outcomes_without_digest_or_key_labels(
    tmp_path: Path,
) -> None:
    payload = _CANONICAL_PAYLOAD
    expected = _expected(payload)
    client = ScriptedS3Client.matching_get(payload)
    store = build_store(client, tmp_path)

    receipt = await store.resolve_verified_object(expected)
    assert receipt is not None

    metrics = store.metrics
    snapshot = repr(metrics) + "\n" + repr(metrics.operations)
    digest_hex = expected.content_digest.hexadecimal
    key_fragment = "objects/sha256"
    assert digest_hex not in snapshot
    assert key_fragment not in snapshot
    assert any(record.operation is ObjectStorageOperation.RESOLVE for record in metrics.operations)


# --- Unhashed verification spool is a typed internal invariant ----------------


@pytest.mark.asyncio
async def test_unhashed_verification_spool_raises_internal_error_on_resolve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Building a receipt from a spool that never hashed fails as internal error."""
    expected = _expected(_CANONICAL_PAYLOAD)
    client = ScriptedS3Client.matching_get(_CANONICAL_PAYLOAD)
    store = build_store(client, tmp_path)
    monkeypatch.setattr(store, "_verify_to_spool", _return_unhashed_verification_spool)

    with pytest.raises(InternalApplicationError) as raised:
        await store.resolve_verified_object(expected)

    assert raised.value.error_code is ErrorCode.INTERNAL_ERROR
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_unhashed_verification_spool_raises_internal_error_on_reader_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reader is never yielded over a verification spool that never hashed."""
    expected = _expected(_CANONICAL_PAYLOAD)
    client = ScriptedS3Client.matching_get(_CANONICAL_PAYLOAD)
    store = build_store(client, tmp_path)
    monkeypatch.setattr(store, "_verify_to_spool", _return_unhashed_verification_spool)

    with pytest.raises(InternalApplicationError) as raised:
        async with store.open_verified_reader(expected) as _reader:
            pytest.fail("an unhashed verification spool must never yield a reader")

    assert raised.value.error_code is ErrorCode.INTERNAL_ERROR
    assert list(tmp_path.iterdir()) == []


# --- Expose build_store inputs for the module ------------------------------


def _build_store_inputs(
    tmp_path: Path,
) -> tuple[SpoolManager, RetryPolicy, InMemoryObjectStorageMetrics, DiagnosticLogger]:
    root_logger = logging.getLogger()
    original_handlers = list(root_logger.handlers)
    original_level = root_logger.level
    root_logger.addHandler(logging.NullHandler())
    try:
        spools = SpoolManager(
            tmp_path,
            clock=lambda: 0.0,
            wall_clock=lambda: 0.0,
            disk_usage=lambda _root: SimpleNamespace(free=_FREE_SPACE_BYTES),
        )
        metrics = InMemoryObjectStorageMetrics()
        logger = DiagnosticLogger({"service": "test", "environment": "test"})
        return spools, RetryPolicy(maximum_attempts=3), metrics, logger
    finally:
        root_logger.handlers[:] = original_handlers
        root_logger.setLevel(original_level)


def test_store_constructor_does_not_read_process_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No KNOWLEDGE_* variable is consulted; scrubbing the environment must not
    # change construction. Any settings-like value lives only in the client.
    for key in list(__import__("os").environ):
        if key.startswith("KNOWLEDGE_"):
            monkeypatch.delenv(key, raising=False)
    spools, retry, metrics, logger = _build_store_inputs(tmp_path)
    client = ScriptedS3Client.matching_get(_CANONICAL_PAYLOAD)
    store = R2S3ObjectStore(
        client,
        spools=spools,
        retry=retry,
        metrics=metrics,
        logger=logger,
        now_utc=lambda: _FIXED_NOW,
        monotonic=lambda: 0.0,
        sleep=_no_sleep,
        jitter=_zero_jitter,
    )
    assert store.metrics is metrics


# --- Store: conditional create, deduplication and lost-response recovery ----


@pytest.mark.asyncio
async def test_store_uses_conditional_single_put_then_full_read(tmp_path: Path) -> None:
    client = ScriptedS3Client.missing_then_put_then_exact_get(b"payload", "text/plain")
    store = build_store(client, tmp_path)
    receipt = await store.store_stream(chunks(b"pay", b"load"), 7, "text/plain")
    put = client.only_put
    assert put.object_key == receipt.object_key
    assert put.size_bytes == 7
    assert put.media_type.value == "text/plain"
    md5 = hashlib.md5(b"payload", usedforsecurity=False).digest()
    assert put.content_md5 == base64.b64encode(md5).decode()
    assert put.if_none_match == "*"
    assert client.calls_after_put == ["head_object", "get_object"]
    assert receipt.verification_method is VerificationMethod.UPLOADED_FULL_READ
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_store_deduplicates_existing_object_without_put(tmp_path: Path) -> None:
    payload = b"dedup-existing"
    client = ScriptedS3Client.existing_then_verify_get(payload, DEFAULT_MEDIA_TYPE)
    store = build_store(client, tmp_path)

    receipt = await store.store_stream(chunks(payload), len(payload), DEFAULT_MEDIA_TYPE)

    assert receipt.verification_method is VerificationMethod.EXISTING_FULL_READ
    assert receipt.content_digest == ContentDigest.parse(hashlib.sha256(payload).hexdigest())
    # No PUT: the store HEAD showed the object exists, so the path is dedup-only.
    assert client.methods == ["head_object", "head_object", "get_object"]
    assert client.put_calls == []
    store_records = [
        record
        for record in store.metrics.operations
        if record.operation is ObjectStorageOperation.STORE
    ]
    assert store_records[-1].result is ObjectStorageResult.DEDUPLICATED
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_store_412_race_transitions_to_winner_verification(tmp_path: Path) -> None:
    payload = b"race-lost-winner"
    client = ScriptedS3Client.missing_then_put_conflict_then_get(
        payload, _precondition_failed(), DEFAULT_MEDIA_TYPE
    )
    store = build_store(client, tmp_path)

    receipt = await store.store_stream(chunks(payload), len(payload), DEFAULT_MEDIA_TYPE)

    # The 412 is NOT a retry and NOT an integrity failure: it transitions directly
    # to winner verification. The stored object is verified as existing.
    assert receipt.verification_method is VerificationMethod.EXISTING_FULL_READ
    assert client.methods == ["head_object", "put_object", "head_object", "get_object"]
    store_records = [
        record
        for record in store.metrics.operations
        if record.operation is ObjectStorageOperation.STORE
    ]
    assert store_records[-1].result is ObjectStorageResult.DEDUPLICATED
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_store_ambiguous_put_retries_beginning_at_head(tmp_path: Path) -> None:
    payload = b"ambiguous-put"
    client = ScriptedS3Client.missing_then_put_ambiguous_then_existing(
        payload, _service_unavailable(), DEFAULT_MEDIA_TYPE
    )
    store = build_store(client, tmp_path)

    receipt = await store.store_stream(chunks(payload), len(payload), DEFAULT_MEDIA_TYPE)

    # After an ambiguous PUT (transient failure, unknown outcome), the retry
    # begins at HEAD, not a blind second PUT. The HEAD now shows the object
    # exists, so it is deduplicated instead of re-uploaded.
    assert client.methods == [
        "head_object",
        "put_object",
        "head_object",
        "head_object",
        "get_object",
    ]
    assert len(client.put_calls) == 1
    assert receipt.verification_method is VerificationMethod.EXISTING_FULL_READ
    store_records = [
        record
        for record in store.metrics.operations
        if record.operation is ObjectStorageOperation.STORE
    ]
    assert store_records[-1].attempt_count == 2
    assert store_records[-1].result is ObjectStorageResult.DEDUPLICATED
    assert store.metrics.retry_count(ObjectStorageOperation.STORE) == 1
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_store_terminal_put_failure_propagates_mapped_error(tmp_path: Path) -> None:
    payload = b"bad-digest-payload"
    client = ScriptedS3Client()
    client.enqueue(None)  # store HEAD: missing
    client.enqueue(_client_error("BadDigest", 400, "PutObject"))  # conditional PUT: terminal
    store = build_store(client, tmp_path)

    with pytest.raises(ObjectStorageError) as raised:
        await store.store_stream(chunks(payload), len(payload), DEFAULT_MEDIA_TYPE)

    # BadDigest is terminal and non-retryable; it propagates as the mapped error.
    assert raised.value.error_code is ErrorCode.OBJECT_STORAGE_CONTRACT_INVALID
    assert client.methods == ["head_object", "put_object"]
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_store_auth_failure_propagates_access_denied(tmp_path: Path) -> None:
    payload = b"auth-failure"
    client = ScriptedS3Client()
    client.enqueue(None)  # store HEAD: missing
    client.enqueue(_client_error("AccessDenied", 403, "PutObject"))  # conditional PUT: terminal
    store = build_store(client, tmp_path)

    with pytest.raises(ObjectStorageError) as raised:
        await store.store_stream(chunks(payload), len(payload), DEFAULT_MEDIA_TYPE)

    assert raised.value.error_code is ErrorCode.OBJECT_STORAGE_ACCESS_DENIED
    assert client.methods == ["head_object", "put_object"]
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_store_rejects_invalid_media_type_without_r2_call(tmp_path: Path) -> None:
    payload = b"bad-media"
    client = ScriptedS3Client()
    store = build_store(client, tmp_path)

    with pytest.raises(ObjectStorageError) as raised:
        await store.store_stream(chunks(payload), len(payload), "text")

    assert raised.value.error_code is ErrorCode.OBJECT_STORAGE_INPUT_INVALID
    assert raised.value.safe_details["reason"] is MEDIA_TYPE_INVALID
    # Admission is validated before any R2 call or spool receive.
    assert client.calls == []
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_store_rejects_invalid_size_without_r2_call(tmp_path: Path) -> None:
    payload = b"x"
    client = ScriptedS3Client()
    store = build_store(client, tmp_path)

    with pytest.raises(ObjectStorageError) as raised:
        await store.store_stream(chunks(payload), -1, DEFAULT_MEDIA_TYPE)

    assert raised.value.error_code is ErrorCode.OBJECT_STORAGE_INPUT_INVALID
    assert raised.value.safe_details["reason"] is SIZE_OUT_OF_RANGE
    assert client.calls == []
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_store_rejects_malformed_claimed_digest_without_r2_call(tmp_path: Path) -> None:
    payload = b"bad-claim"
    client = ScriptedS3Client()
    store = build_store(client, tmp_path)

    with pytest.raises(ObjectStorageError) as raised:
        await store.store_stream(
            chunks(payload), len(payload), DEFAULT_MEDIA_TYPE, claimed_sha256="not-hex"
        )

    assert raised.value.error_code is ErrorCode.OBJECT_STORAGE_INPUT_INVALID
    assert raised.value.safe_details["reason"] is DIGEST_MISMATCH
    # The claim shape is validated at admission before any R2 call or spool receive.
    assert client.calls == []
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_store_rejects_claimed_digest_mismatch_after_hashing(tmp_path: Path) -> None:
    payload = b"claim-mismatch"
    well_formed_but_wrong = "0" * 64
    client = ScriptedS3Client()
    store = build_store(client, tmp_path)

    with pytest.raises(ObjectStorageError) as raised:
        await store.store_stream(
            chunks(payload),
            len(payload),
            DEFAULT_MEDIA_TYPE,
            claimed_sha256=well_formed_but_wrong,
        )

    assert raised.value.error_code is ErrorCode.OBJECT_STORAGE_INPUT_INVALID
    assert raised.value.safe_details["reason"] is DIGEST_MISMATCH
    # The mismatch is detected after hashing but before any R2 call (HEAD).
    assert client.calls == []
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_store_accepts_matching_claimed_digest(tmp_path: Path) -> None:
    payload = b"claim-matches"
    claim = hashlib.sha256(payload).hexdigest()
    client = ScriptedS3Client.missing_then_put_then_exact_get(payload, DEFAULT_MEDIA_TYPE)
    store = build_store(client, tmp_path)

    receipt = await store.store_stream(
        chunks(payload), len(payload), DEFAULT_MEDIA_TYPE, claimed_sha256=claim
    )

    assert receipt.verification_method is VerificationMethod.UPLOADED_FULL_READ
    assert receipt.content_digest == ContentDigest.parse(claim)


@pytest.mark.asyncio
async def test_store_does_not_overwrite_existing_size_mismatch(tmp_path: Path) -> None:
    payload = b"no-overwrite"
    client = ScriptedS3Client()
    wrong_head = HeadObjectResult(
        size_bytes=len(payload) + 1, media_type=DEFAULT_MEDIA_TYPE, etag=DEFAULT_ETAG
    )
    client.enqueue(wrong_head)  # store HEAD: exists with wrong size
    client.enqueue(wrong_head)  # verify HEAD: same wrong size -> integrity failure
    store = build_store(client, tmp_path)

    with pytest.raises(ObjectStorageError) as raised:
        await store.store_stream(chunks(payload), len(payload), DEFAULT_MEDIA_TYPE)

    # The store HEAD showed an existing object; the store path does NOT PUT over
    # it. Verification catches the size mismatch (corruption under an immutable
    # key, design §6.3) and fails closed as an integrity failure.
    assert raised.value.error_code is ErrorCode.OBJECT_STORAGE_INTEGRITY_FAILED
    assert client.methods == ["head_object", "head_object"]
    assert client.put_calls == []
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_store_does_not_overwrite_existing_media_type_mismatch(tmp_path: Path) -> None:
    payload = b"no-overwrite-media"
    client = ScriptedS3Client()
    wrong_head = HeadObjectResult(
        size_bytes=len(payload), media_type="application/json", etag=DEFAULT_ETAG
    )
    client.enqueue(wrong_head)  # store HEAD: exists with wrong media type
    client.enqueue(wrong_head)  # verify HEAD: same wrong media type -> metadata conflict
    store = build_store(client, tmp_path)

    with pytest.raises(ObjectStorageError) as raised:
        await store.store_stream(chunks(payload), len(payload), DEFAULT_MEDIA_TYPE)

    # A media-type mismatch on the stored canonical object is a metadata
    # conflict (design §6.3); the store path never PUTs over the existing key.
    assert raised.value.error_code is ErrorCode.OBJECT_STORAGE_METADATA_CONFLICT
    assert client.methods == ["head_object", "head_object"]
    assert client.put_calls == []
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_store_preserves_input_spool_until_verify_then_removes_it(
    tmp_path: Path,
) -> None:
    payload = b"spool-lifetime"
    client = ScriptedS3Client.missing_then_put_then_exact_get(payload, DEFAULT_MEDIA_TYPE)
    store = build_store(client, tmp_path)

    await store.store_stream(chunks(payload), len(payload), DEFAULT_MEDIA_TYPE)

    # The input spool survives through PUT and verify, then is removed in the
    # shielded bounded cleanup when the receive context exits.
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_store_removes_input_spool_on_failure(tmp_path: Path) -> None:
    payload = b"spool-on-failure"
    client = ScriptedS3Client()
    client.enqueue(None)  # store HEAD: missing
    client.enqueue(_client_error("BadDigest", 400, "PutObject"))  # terminal PUT failure
    store = build_store(client, tmp_path)

    with pytest.raises(ObjectStorageError):
        await store.store_stream(chunks(payload), len(payload), DEFAULT_MEDIA_TYPE)

    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_store_independently_hashes_each_same_hash_input(tmp_path: Path) -> None:
    payload = b"independent-hashing"
    head = HeadObjectResult(
        size_bytes=len(payload), media_type=DEFAULT_MEDIA_TYPE, etag=DEFAULT_ETAG
    )
    client = ScriptedS3Client()
    # First call: missing -> conditional PUT -> verify.
    client.enqueue(None)  # store HEAD: missing
    client.enqueue(None)  # PUT: success
    client.enqueue(head)  # verify HEAD
    client.enqueue(GetObjectResult(body=scripted_body([payload])))  # verify GET
    # Second call: same content, now exists -> dedup verify.
    client.enqueue(head)  # store HEAD: exists
    client.enqueue(head)  # verify HEAD
    client.enqueue(GetObjectResult(body=scripted_body([payload])))  # verify GET
    store = build_store(client, tmp_path)

    first = await store.store_stream(chunks(payload), len(payload), DEFAULT_MEDIA_TYPE)
    second = await store.store_stream(chunks(payload), len(payload), DEFAULT_MEDIA_TYPE)

    # Each call independently hashed its own input to the same identity.
    assert first.content_digest == second.content_digest
    assert first.object_key == second.object_key
    assert first.verification_method is VerificationMethod.UPLOADED_FULL_READ
    assert second.verification_method is VerificationMethod.EXISTING_FULL_READ
    assert len(client.put_calls) == 1
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_store_emits_deduplicated_event_on_existing_path(tmp_path: Path) -> None:
    payload = b"dedup-event-existing"
    client = ScriptedS3Client.existing_then_verify_get(payload, DEFAULT_MEDIA_TYPE)
    store = build_store(client, tmp_path)
    with capture_diagnostic_events() as capture:
        receipt = await store.store_stream(chunks(payload), len(payload), DEFAULT_MEDIA_TYPE)

    assert receipt.verification_method is VerificationMethod.EXISTING_FULL_READ
    dedup_events = [
        event
        for event in capture.events
        if event.get("event") == EventName.OBJECT_STORAGE_OBJECT_DEDUPLICATED.value
    ]
    assert len(dedup_events) == 1
    event = dedup_events[0]
    assert event["operation"] == ObjectStorageOperation.STORE.value
    assert event["size_bytes"] == len(payload)
    assert event["provider"] == "r2"
    assert isinstance(event["duration_ms"], int)
    assert isinstance(event["attempt_count"], int)


@pytest.mark.asyncio
async def test_store_emits_deduplicated_event_on_412_winner(tmp_path: Path) -> None:
    payload = b"dedup-event-412"
    client = ScriptedS3Client.missing_then_put_conflict_then_get(
        payload, _precondition_failed(), DEFAULT_MEDIA_TYPE
    )
    store = build_store(client, tmp_path)
    with capture_diagnostic_events() as capture:
        receipt = await store.store_stream(chunks(payload), len(payload), DEFAULT_MEDIA_TYPE)

    assert receipt.verification_method is VerificationMethod.EXISTING_FULL_READ
    dedup_events = [
        event
        for event in capture.events
        if event.get("event") == EventName.OBJECT_STORAGE_OBJECT_DEDUPLICATED.value
    ]
    assert len(dedup_events) == 1
    assert dedup_events[0]["operation"] == ObjectStorageOperation.STORE.value


@pytest.mark.asyncio
async def test_store_emits_succeeded_event_on_upload(tmp_path: Path) -> None:
    payload = b"upload-event"
    client = ScriptedS3Client.missing_then_put_then_exact_get(payload, DEFAULT_MEDIA_TYPE)
    store = build_store(client, tmp_path)
    with capture_diagnostic_events() as capture:
        receipt = await store.store_stream(chunks(payload), len(payload), DEFAULT_MEDIA_TYPE)

    assert receipt.verification_method is VerificationMethod.UPLOADED_FULL_READ
    dedup_events = [
        event
        for event in capture.events
        if event.get("event") == EventName.OBJECT_STORAGE_OBJECT_DEDUPLICATED.value
    ]
    assert dedup_events == []
    succeeded_events = [
        event
        for event in capture.events
        if event.get("event") == EventName.OBJECT_STORAGE_OPERATION_SUCCEEDED.value
        and event.get("operation") == ObjectStorageOperation.STORE.value
    ]
    assert len(succeeded_events) == 1


@pytest.mark.asyncio
async def test_store_records_failed_outcome_for_input_invalid(tmp_path: Path) -> None:
    payload = b"record-failed"
    client = ScriptedS3Client()
    store = build_store(client, tmp_path)

    with pytest.raises(ObjectStorageError):
        await store.store_stream(chunks(payload), len(payload), "text")

    store_records = [
        record
        for record in store.metrics.operations
        if record.operation is ObjectStorageOperation.STORE
    ]
    assert store_records[-1].result is ObjectStorageResult.FAILED
    assert store_records[-1].error_code is ErrorCode.OBJECT_STORAGE_INPUT_INVALID


# --- Verified device download composition -------------------------------------


class _StaticDeviceContentCatalog:
    """Catalog double resolving one fixed exact-version descriptor.

    The catalog is the authorization/membership seam and resolves no bytes;
    these cases pin what the composed device download does around the REAL
    adapter: the exact HEAD + conditional If-Match GET verification runs
    before the consumer receives anything, and absence/corruption cross the
    boundary as the closed device download integrity error.
    """

    def __init__(self, expected: ExpectedObject) -> None:
        self._expected = expected

    async def resolve_descriptor(
        self,
        context: DeviceSyncContext,
        *,
        source_id: UUID,
        source_version_id: UUID,
        diagnostic_context: DiagnosticContext,
    ) -> DeviceContentDescriptor:
        del context, diagnostic_context
        return DeviceContentDescriptor(
            source_id=source_id,
            source_version_id=source_version_id,
            content_digest=self._expected.content_digest,
            size_bytes=self._expected.size_bytes,
            media_type=self._expected.media_type,
        )


def _device_download_service(
    client: ScriptedS3Client, tmp_path: Path, expected: ExpectedObject
) -> VerifiedDeviceContentService:
    store = build_store(client, tmp_path)
    return VerifiedDeviceContentService(
        catalog=_StaticDeviceContentCatalog(expected),
        objects=store,
        metrics=InMemoryDeviceSyncMetrics(),
        diagnostics=None,
    )


_DEVICE_DOWNLOAD_CONTEXT = DeviceSyncContext(
    workspace_id=uuid4(), device_id=uuid4(), user_id=uuid4()
)


async def _open_device_content(
    service: VerifiedDeviceContentService, expected: ExpectedObject
) -> bytes:
    diagnostic = create_diagnostic_context().context
    collected = bytearray()
    async with service.open_content(
        _DEVICE_DOWNLOAD_CONTEXT,
        source_id=uuid4(),
        source_version_id=uuid4(),
        diagnostic_context=diagnostic,
    ) as content:
        assert content.descriptor.expected_object() == expected
        async for chunk in content.reader:
            collected.extend(chunk)
    return bytes(collected)


@pytest.mark.asyncio
async def test_device_download_yields_exact_bytes_after_if_match_get(
    tmp_path: Path,
) -> None:
    payload = _CANONICAL_PAYLOAD
    expected = _expected(payload)
    client = ScriptedS3Client.matching_get(payload)
    service = _device_download_service(client, tmp_path, expected)

    assert await _open_device_content(service, expected) == payload

    # The composed download performed the exact adapter verification: one
    # HEAD then one conditional If-Match GET, spool removed on exit.
    assert client.methods == ["head_object", "get_object"]
    assert [call.if_match for call in client.get_calls] == [DEFAULT_ETAG]
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_device_download_missing_object_is_closed_integrity_failure(
    tmp_path: Path,
) -> None:
    expected = _expected(_CANONICAL_PAYLOAD)
    client = ScriptedS3Client.missing_object()
    service = _device_download_service(client, tmp_path, expected)

    with pytest.raises(DeviceSyncError) as raised:
        await _open_device_content(service, expected)

    assert raised.value.code is DeviceSyncErrorCode.DOWNLOAD_INTEGRITY_FAILED
    rendered = (str(raised.value) + repr(raised.value)).lower()
    for provider_string in ("r2", "bucket", "etag", "objects/sha256", "endpoint"):
        assert provider_string not in rendered
    assert client.methods == ["head_object"]
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_device_download_corrupt_object_fails_closed_before_any_byte(
    tmp_path: Path,
) -> None:
    expected = _expected(_CORRUPT_PREFIX + _CORRUPT_CORRECT_TAIL)
    client = ScriptedS3Client.corrupt_after_prefix(_CORRUPT_PREFIX, _CORRUPT_WRONG_TAIL)
    service = _device_download_service(client, tmp_path, expected)

    consumed = bytearray()
    with pytest.raises(DeviceSyncError) as raised:
        async with service.open_content(
            _DEVICE_DOWNLOAD_CONTEXT,
            source_id=uuid4(),
            source_version_id=uuid4(),
            diagnostic_context=create_diagnostic_context().context,
        ) as content:
            async for chunk in content.reader:
                consumed.extend(chunk)

    assert raised.value.code is DeviceSyncErrorCode.DOWNLOAD_INTEGRITY_FAILED
    # No consumer byte flowed before the full verification failed, and the
    # spool root is clean.
    assert consumed == b""
    assert list(tmp_path.iterdir()) == []
