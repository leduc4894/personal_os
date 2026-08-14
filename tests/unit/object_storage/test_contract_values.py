"""Canonical object-storage contract values and protocol structural tests."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterable, AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest

from personal_os.object_storage import (
    CanonicalMediaType,
    CanonicalObjectKey,
    CanonicalObjectStore,
    ContentDigest,
    ExpectedObject,
    VerificationMethod,
    VerifiedObjectReader,
    VerifiedObjectReceipt,
    derive_canonical_object_key,
)

EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_derives_only_canonical_key() -> None:
    digest = ContentDigest.parse(EMPTY_SHA256)
    assert derive_canonical_object_key(digest).value == ("objects/sha256/e3/b0/" + EMPTY_SHA256)


@pytest.mark.parametrize(
    "value",
    ["", "SHA256:" + EMPTY_SHA256, EMPTY_SHA256.upper(), "f" * 63, "g" * 64],
)
def test_rejects_noncanonical_digest(value: str) -> None:
    with pytest.raises(ValueError, match="digest"):
        ContentDigest.parse(value)


@pytest.mark.parametrize("value", ["text/plain; charset=utf-8", "TEXT/PLAIN", "*/json"])
def test_rejects_noncanonical_media_type(value: str) -> None:
    with pytest.raises(ValueError, match="media type"):
        CanonicalMediaType.parse(value)


def test_value_objects_are_frozen() -> None:
    digest = ContentDigest.parse(EMPTY_SHA256)
    key = derive_canonical_object_key(digest)
    media = CanonicalMediaType.parse("text/plain")
    with pytest.raises(FrozenInstanceError):
        digest.hexadecimal = "0" * 64  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        key.value = "objects/sha256/00/00/" + "0" * 64  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        media.value = "application/json"  # type: ignore[misc]


def test_canonical_object_store_protocol_is_exercised() -> None:
    asyncio.run(_exercise_canonical_object_store())


async def _empty_chunks() -> AsyncIterator[bytes]:
    return
    yield b""  # pragma: no cover - shapes the async generator


async def _chunks(chunks: tuple[bytes, ...]) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk


class _VerifiedReader:
    """Minimal in-test reader honoring the bounded read contract."""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self._position = 0

    async def read(self, size_bytes: int = 1_048_576) -> bytes:
        if size_bytes < 0 or size_bytes > 1_048_576:
            raise ValueError("read size must be between 0 and 1 MiB")
        chunk = self._payload[self._position : self._position + size_bytes]
        self._position += len(chunk)
        return chunk

    def __aiter__(self) -> _VerifiedReader:
        return self

    async def __anext__(self) -> bytes:
        chunk = await self.read()
        if not chunk:
            raise StopAsyncIteration
        return chunk


class _InMemoryStore:
    """Small in-test CanonicalObjectStore so every signature is type-checkable."""

    def __init__(self) -> None:
        self._objects: dict[ContentDigest, tuple[bytes, CanonicalMediaType]] = {}

    async def resolve_verified_object(
        self, expected: ExpectedObject
    ) -> VerifiedObjectReceipt | None:
        stored = self._objects.get(expected.content_digest)
        if stored is None:
            return None
        payload, media = stored
        return _receipt(
            expected.content_digest, len(payload), media, VerificationMethod.UPLOADED_FULL_READ
        )

    async def store_stream(
        self,
        stream: AsyncIterable[bytes],
        expected_size_bytes: int,
        media_type: str,
        claimed_sha256: str | None = None,
    ) -> VerifiedObjectReceipt:
        del expected_size_bytes, claimed_sha256
        media = CanonicalMediaType.parse(media_type)
        collected = bytearray()
        async for chunk in stream:
            collected.extend(chunk)
        digest = ContentDigest.parse(hashlib.sha256(bytes(collected)).hexdigest())
        self._objects[digest] = (bytes(collected), media)
        return _receipt(digest, len(collected), media, VerificationMethod.UPLOADED_FULL_READ)

    async def verify_existing_object(self, expected: ExpectedObject) -> VerifiedObjectReceipt:
        payload, media = self._objects[expected.content_digest]
        return _receipt(
            expected.content_digest, len(payload), media, VerificationMethod.EXISTING_FULL_READ
        )

    def open_verified_reader(
        self, expected: ExpectedObject
    ) -> AbstractAsyncContextManager[VerifiedObjectReader]:
        payload, _media = self._objects[expected.content_digest]

        @asynccontextmanager
        async def reader() -> AsyncIterator[_VerifiedReader]:
            yield _VerifiedReader(payload)

        return reader()


def _build_store() -> CanonicalObjectStore:
    return _InMemoryStore()


def _receipt(
    digest: ContentDigest,
    size_bytes: int,
    media: CanonicalMediaType,
    method: VerificationMethod,
) -> VerifiedObjectReceipt:
    return VerifiedObjectReceipt(
        content_digest=digest,
        object_key=derive_canonical_object_key(digest),
        size_bytes=size_bytes,
        media_type=media,
        verified_at=datetime.now(UTC),
        verification_method=method,
    )


async def _exercise_canonical_object_store() -> None:
    store = _build_store()
    payload = b"canonical bytes"
    payload_digest_hex = hashlib.sha256(payload).hexdigest()
    expected = ExpectedObject(
        content_digest=ContentDigest.parse(payload_digest_hex),
        size_bytes=len(payload),
        media_type=CanonicalMediaType.parse("application/octet-stream"),
    )

    assert await store.resolve_verified_object(expected) is None

    receipt = await store.store_stream(
        _chunks((b"canon", b"ical bytes")),
        len(payload),
        "application/octet-stream",
    )
    assert receipt.content_digest.hexadecimal == payload_digest_hex
    assert receipt.size_bytes == len(payload)
    assert receipt.media_type.value == "application/octet-stream"
    assert (
        receipt.object_key.value
        == "objects/sha256/"
        + payload_digest_hex[:2]
        + "/"
        + payload_digest_hex[2:4]
        + "/"
        + payload_digest_hex
    )
    assert receipt.verification_method is VerificationMethod.UPLOADED_FULL_READ
    assert receipt.verified_at.tzinfo is not None
    expected_key = (
        f"objects/sha256/{payload_digest_hex[:2]}/{payload_digest_hex[2:4]}/{payload_digest_hex}"
    )
    assert receipt.object_key.value == expected_key

    resolved = await store.resolve_verified_object(expected)
    assert resolved is not None
    assert resolved.content_digest.hexadecimal == payload_digest_hex

    verified = await store.verify_existing_object(expected)
    assert verified.verification_method is VerificationMethod.EXISTING_FULL_READ

    async with store.open_verified_reader(expected) as reader:
        assert await reader.read() == payload
        with pytest.raises(ValueError):
            await reader.read(-1)
        with pytest.raises(ValueError):
            await reader.read(1_048_576 + 1)

    collected: list[bytes] = []
    async with store.open_verified_reader(expected) as iterating_reader:
        async for chunk in iterating_reader:
            collected.append(chunk)
    assert b"".join(collected) == payload

    empty_digest = ContentDigest.parse(EMPTY_SHA256)
    empty_expected = ExpectedObject(
        content_digest=empty_digest,
        size_bytes=0,
        media_type=CanonicalMediaType.parse("application/octet-stream"),
    )
    empty_receipt = await store.store_stream(_empty_chunks(), 0, "application/octet-stream")
    assert empty_receipt.size_bytes == 0
    assert empty_receipt.object_key.value == "objects/sha256/e3/b0/" + EMPTY_SHA256
    assert empty_receipt == VerifiedObjectReceipt(
        content_digest=empty_digest,
        object_key=CanonicalObjectKey("objects/sha256/e3/b0/" + EMPTY_SHA256),
        size_bytes=0,
        media_type=CanonicalMediaType("application/octet-stream"),
        verified_at=empty_receipt.verified_at,
        verification_method=VerificationMethod.UPLOADED_FULL_READ,
    )
    del empty_expected
