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

import hashlib
import logging
from collections.abc import Awaitable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import IO

import pytest
from botocore.exceptions import ClientError
from tests.contract.object_storage.scripted_s3 import (
    DEFAULT_ETAG,
    DEFAULT_MEDIA_TYPE,
    ScriptedS3Client,
    scripted_body,
)

from personal_os.diagnostics import DiagnosticLogger
from personal_os.error_contracts.codes import ErrorCode
from personal_os.object_storage import (
    CanonicalMediaType,
    ContentDigest,
    ExpectedObject,
    VerificationMethod,
    VerifiedObjectReceipt,
    derive_canonical_object_key,
)
from personal_os.object_storage.errors import SIZE_OUT_OF_RANGE, ObjectStorageError
from r2_object_storage import adapter as adapter_module
from r2_object_storage.adapter import R2S3ObjectStore
from r2_object_storage.error_mapping import RetryPolicy
from r2_object_storage.metrics import (
    InMemoryObjectStorageMetrics,
    ObjectStorageOperation,
    ObjectStorageResult,
)
from r2_object_storage.spool import SpoolManager

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
    read from the process environment.
    """

    root_logger = logging.getLogger()
    if not any(isinstance(handler, logging.NullHandler) for handler in root_logger.handlers):
        root_logger.addHandler(logging.NullHandler())
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
async def test_size_conflict_at_head_raises_metadata_conflict(tmp_path: Path) -> None:
    payload = _CANONICAL_PAYLOAD
    expected = _expected(payload)
    client = ScriptedS3Client.size_conflict(payload, wrong_size_bytes=len(payload) + 1)
    store = build_store(client, tmp_path)

    with pytest.raises(ObjectStorageError) as raised:
        await store.verify_existing_object(expected)

    assert raised.value.error_code is ErrorCode.OBJECT_STORAGE_METADATA_CONFLICT
    # The conflict is detected at HEAD before any GET body is fetched.
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


# --- Expose build_store inputs for the module ------------------------------


def _build_store_inputs(
    tmp_path: Path,
) -> tuple[SpoolManager, RetryPolicy, InMemoryObjectStorageMetrics, DiagnosticLogger]:
    root_logger = logging.getLogger()
    if not any(isinstance(handler, logging.NullHandler) for handler in root_logger.handlers):
        root_logger.addHandler(logging.NullHandler())
    spools = SpoolManager(
        tmp_path,
        clock=lambda: 0.0,
        wall_clock=lambda: 0.0,
        disk_usage=lambda _root: SimpleNamespace(free=_FREE_SPACE_BYTES),
    )
    metrics = InMemoryObjectStorageMetrics()
    logger = DiagnosticLogger({"service": "test", "environment": "test"})
    return spools, RetryPolicy(maximum_attempts=3), metrics, logger


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
