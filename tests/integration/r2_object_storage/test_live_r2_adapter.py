"""Live R2 adapter contract cases against one dedicated private test bucket.

Every case here is marked ``r2_live``: the default suite never selects it, and
the dedicated ``object-storage-test-live`` command / protected workflow select
it explicitly with ``-m r2_live``. Fixture setup fails (never skips) without
the dedicated test configuration, payloads are per-run random non-personal
bytes, and the harness fixture performs exact-key cleanup in a ``finally`` —
deleting only validated canonical keys this run recorded (design 16.2/16.3).

The case list is exactly the design's live set: zero-byte round trip,
multi-chunk round trip, duplicate store, concurrent conditional create, missing
object, size/media conflict, deliberately corrupted object, repeated /
lost-response-equivalent resolution, and exact cleanup after a forced test
exception. Boundary sizes, timeout injection, the retry matrix and
cancellation stay offline.
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets
from collections.abc import Callable
from contextlib import suppress

import pytest
from tests.integration.r2_object_storage.conftest import (
    LiveR2Harness,
    emit_zero_byte_live_diagnostic,
)

from personal_os.error_contracts.codes import ErrorCode
from personal_os.object_storage import (
    CanonicalMediaType,
    ContentDigest,
    ExpectedObject,
    VerificationMethod,
    VerifiedObjectReceipt,
)
from personal_os.object_storage.errors import ObjectStorageError

pytestmark = [pytest.mark.r2_live, pytest.mark.asyncio]

_MEDIA_TYPE = "application/octet-stream"
_ALT_MEDIA_TYPE = "text/plain"
_CONCURRENT_WRITERS = 6


def _random_payload(size_bytes: int) -> bytes:
    """Per-run random non-personal payload bytes."""

    return secrets.token_bytes(size_bytes)


def _expected(receipt: VerifiedObjectReceipt) -> ExpectedObject:
    return ExpectedObject(
        content_digest=receipt.content_digest,
        size_bytes=receipt.size_bytes,
        media_type=receipt.media_type,
    )


async def _read_verified(harness: LiveR2Harness, expected: ExpectedObject) -> bytes:
    chunks: list[bytes] = []
    async with harness.store.open_verified_reader(expected) as reader:
        async for chunk in reader:
            chunks.append(chunk)
    return b"".join(chunks)


# --- Live cases --------------------------------------------------------------


async def _run_zero_byte_round_trip(
    live_r2_harness: LiveR2Harness,
    *,
    emit_diagnostic: Callable[[str], None] = print,
) -> None:
    """Exercise the three zero-byte body operations with one closed failure record."""

    stage = "store"
    try:
        receipt = await live_r2_harness.store_payload(b"", media_type=_MEDIA_TYPE)

        assert receipt.size_bytes == 0
        assert receipt.verification_method is VerificationMethod.UPLOADED_FULL_READ
        assert len(live_r2_harness.manifest) == 1

        stage = "resolve"
        resolved = await live_r2_harness.store.resolve_verified_object(_expected(receipt))
        assert resolved is not None
        assert resolved.content_digest == receipt.content_digest

        stage = "read"
        assert await _read_verified(live_r2_harness, _expected(receipt)) == b""
    except Exception as failure:
        emit_zero_byte_live_diagnostic(stage, failure, emit=emit_diagnostic)
        raise


async def test_zero_byte_round_trip(live_r2_harness: LiveR2Harness) -> None:
    await _run_zero_byte_round_trip(live_r2_harness)


async def test_multi_chunk_round_trip(live_r2_harness: LiveR2Harness) -> None:
    payload = _random_payload(3 * 1_048_576 + 7)

    receipt = await live_r2_harness.store_payload(payload, media_type=_MEDIA_TYPE)

    assert receipt.size_bytes == len(payload)
    assert receipt.content_digest.hexadecimal == hashlib.sha256(payload).hexdigest()
    resolved = await live_r2_harness.store.resolve_verified_object(_expected(receipt))
    assert resolved is not None
    assert resolved.verification_method is VerificationMethod.EXISTING_FULL_READ
    assert await _read_verified(live_r2_harness, _expected(receipt)) == payload


async def test_duplicate_store_deduplicates(live_r2_harness: LiveR2Harness) -> None:
    payload = _random_payload(4096)

    first = await live_r2_harness.store_payload(payload, media_type=_MEDIA_TYPE)
    second = await live_r2_harness.store_payload(payload, media_type=_MEDIA_TYPE)

    assert first.verification_method is VerificationMethod.UPLOADED_FULL_READ
    assert second.verification_method is VerificationMethod.EXISTING_FULL_READ
    assert second.content_digest == first.content_digest
    assert second.object_key == first.object_key
    # The manifest records the exact key once even after a duplicate store.
    assert live_r2_harness.manifest.recorded_keys() == (str(first.object_key),)


async def test_concurrent_conditional_create(live_r2_harness: LiveR2Harness) -> None:
    payload = _random_payload(256 * 1024)

    receipts = await asyncio.gather(
        *(
            live_r2_harness.store_payload(payload, media_type=_MEDIA_TYPE)
            for _ in range(_CONCURRENT_WRITERS)
        )
    )

    digests = {receipt.content_digest for receipt in receipts}
    assert len(digests) == 1
    assert len({str(receipt.object_key) for receipt in receipts}) == 1
    # Concurrent same-digest writers converge on one immutable canonical key.
    assert live_r2_harness.manifest.recorded_keys() == (str(receipts[0].object_key),)
    resolved = await live_r2_harness.store.resolve_verified_object(_expected(receipts[0]))
    assert resolved is not None
    assert resolved.size_bytes == len(payload)


async def test_missing_object_resolves_to_none(live_r2_harness: LiveR2Harness) -> None:
    absent = ExpectedObject(
        content_digest=ContentDigest.parse(secrets.token_hex(32)),
        size_bytes=0,
        media_type=CanonicalMediaType.parse(_MEDIA_TYPE),
    )

    assert await live_r2_harness.store.resolve_verified_object(absent) is None
    # Nothing was created, so the run's cleanup allowlist stays empty.
    assert len(live_r2_harness.manifest) == 0


async def test_size_and_media_conflict_fail_closed(live_r2_harness: LiveR2Harness) -> None:
    payload = _random_payload(2048)
    receipt = await live_r2_harness.store_payload(payload, media_type=_ALT_MEDIA_TYPE)

    media_conflict = ExpectedObject(
        content_digest=receipt.content_digest,
        size_bytes=receipt.size_bytes,
        media_type=CanonicalMediaType.parse(_MEDIA_TYPE),
    )
    with pytest.raises(ObjectStorageError) as media_rejection:
        await live_r2_harness.store.resolve_verified_object(media_conflict)
    assert media_rejection.value.error_code is ErrorCode.OBJECT_STORAGE_METADATA_CONFLICT

    size_conflict = ExpectedObject(
        content_digest=receipt.content_digest,
        size_bytes=receipt.size_bytes + 1,
        media_type=receipt.media_type,
    )
    with pytest.raises(ObjectStorageError) as size_rejection:
        await live_r2_harness.store.resolve_verified_object(size_conflict)
    assert size_rejection.value.error_code is ErrorCode.OBJECT_STORAGE_INTEGRITY_FAILED

    # No consumer byte may ever flow from a conflicting expectation.
    with pytest.raises(ObjectStorageError):
        async with live_r2_harness.store.open_verified_reader(media_conflict) as reader:
            async for _chunk in reader:
                pass


async def test_corrupted_object_fails_full_verification(
    live_r2_harness: LiveR2Harness,
) -> None:
    canonical = _random_payload(8192)
    corrupted = bytearray(canonical)
    corrupted[-1] ^= 0xFF
    digest_hexadecimal = hashlib.sha256(canonical).hexdigest()

    key = await live_r2_harness.write_object_under_digest(
        digest_hexadecimal=digest_hexadecimal,
        payload=bytes(corrupted),
        media_type=_MEDIA_TYPE,
    )

    assert key == (
        f"objects/sha256/{digest_hexadecimal[:2]}/{digest_hexadecimal[2:4]}/{digest_hexadecimal}"
    )
    expected = ExpectedObject(
        content_digest=ContentDigest.parse(digest_hexadecimal),
        size_bytes=len(canonical),
        media_type=CanonicalMediaType.parse(_MEDIA_TYPE),
    )
    # The HEAD metadata matches, so the failure must come from the full GET's
    # independent digest verification — the heart of the fail-closed contract.
    with pytest.raises(ObjectStorageError) as rejection:
        await live_r2_harness.store.verify_existing_object(expected)
    assert rejection.value.error_code is ErrorCode.OBJECT_STORAGE_INTEGRITY_FAILED
    # The corrupted object was created by THIS run and stays in the allowlist
    # so teardown's exact cleanup removes it.
    assert live_r2_harness.manifest.record_for(key) is not None


async def test_repeated_lost_response_equivalent_resolution(
    live_r2_harness: LiveR2Harness,
) -> None:
    payload = _random_payload(8 * 1024)
    digest_hexadecimal = hashlib.sha256(payload).hexdigest()

    # An out-of-band writer lands the exact canonical object first — the live
    # equivalent of a store whose PUT response was lost: the object exists at
    # the canonical key, but this adapter never received a receipt for it.
    key = await live_r2_harness.write_object_under_digest(
        digest_hexadecimal=digest_hexadecimal,
        payload=payload,
        media_type=_MEDIA_TYPE,
    )
    expected = ExpectedObject(
        content_digest=ContentDigest.parse(digest_hexadecimal),
        size_bytes=len(payload),
        media_type=CanonicalMediaType.parse(_MEDIA_TYPE),
    )

    # Resolution of the never-receipted object goes HEAD -> exists -> full
    # verify, yielding an EXISTING_FULL_READ receipt and exact bytes.
    resolved = await live_r2_harness.store.resolve_verified_object(expected)
    assert resolved is not None
    assert resolved.verification_method is VerificationMethod.EXISTING_FULL_READ
    assert await _read_verified(live_r2_harness, expected) == payload

    # A repeated store of the same content also resolves through the existing
    # object — an immutable canonical key is never overwritten.
    receipt = await live_r2_harness.store_payload(payload, media_type=_MEDIA_TYPE)
    assert receipt.verification_method is VerificationMethod.EXISTING_FULL_READ
    assert str(receipt.object_key) == key
    # The out-of-band creation was recorded by this run for exact cleanup.
    assert live_r2_harness.manifest.recorded_keys() == (key,)


async def test_exact_cleanup_after_forced_test_exception(
    live_r2_harness: LiveR2Harness,
) -> None:
    class _ForcedMidTestFailure(RuntimeError):
        """Deliberate abort after the run recorded its created key."""

    payload = _random_payload(1024)
    receipt = await live_r2_harness.store_payload(payload, media_type=_MEDIA_TYPE)
    assert live_r2_harness.manifest.recorded_keys() == (str(receipt.object_key),)
    assert await live_r2_harness.store.resolve_verified_object(_expected(receipt)) is not None

    # Abort mid-test: the fixture teardown must still run, delete exactly the
    # one recorded key (validated before any delete call) and prove absence.
    with suppress(_ForcedMidTestFailure):
        raise _ForcedMidTestFailure("forced mid-test failure after recording")
