"""Provider-neutral object-storage value objects, reader protocol and store port.

These contracts are transport-neutral, fully typed, immutable and free of any
provider response type. They live in the core ``personal_os`` package; the
concrete R2 adapter implements :class:`CanonicalObjectStore` elsewhere and this
module must not import any infrastructure SDK.
"""

from __future__ import annotations

from collections.abc import AsyncIterable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from personal_os.object_storage.keys import (
    CanonicalMediaType,
    CanonicalObjectKey,
    ContentDigest,
)


class VerificationMethod(StrEnum):
    """How the adapter independently proved the canonical bytes behind a receipt."""

    UPLOADED_FULL_READ = "uploaded_full_read"
    EXISTING_FULL_READ = "existing_full_read"


@dataclass(frozen=True, slots=True)
class ExpectedObject:
    """Verification request describing the bytes a caller expects to read or store.

    ``ExpectedObject`` is a request, not proof: a receipt is issued only after the
    adapter independently verifies exact size, media type and full SHA-256.
    """

    content_digest: ContentDigest
    size_bytes: int
    media_type: CanonicalMediaType


@dataclass(frozen=True, slots=True)
class VerifiedObjectReceipt:
    """Immutable receipt returned only after full size, media and digest verification.

    It deliberately carries no bucket, endpoint, ETag, credential, spool path or
    provider response; those remain adapter-local concerns.
    """

    content_digest: ContentDigest
    object_key: CanonicalObjectKey
    size_bytes: int
    media_type: CanonicalMediaType
    verified_at: datetime
    verification_method: VerificationMethod


class VerifiedObjectReader(Protocol):
    """Bounded asynchronous reader over already-verified canonical object bytes."""

    async def read(self, size_bytes: int = 1_048_576) -> bytes:
        """Return at most ``size_bytes`` bytes.

        Reads of negative size or greater than 1 MiB are rejected at the contract
        boundary before any byte is returned.
        """
        ...

    def __aiter__(self) -> VerifiedObjectReader: ...

    async def __anext__(self) -> bytes: ...


class CanonicalObjectStore(Protocol):
    """Provider-neutral port for verified content-addressable object storage."""

    async def resolve_verified_object(
        self, expected: ExpectedObject
    ) -> VerifiedObjectReceipt | None: ...

    async def store_stream(
        self,
        stream: AsyncIterable[bytes],
        expected_size_bytes: int,
        media_type: str,
        claimed_sha256: str | None = None,
    ) -> VerifiedObjectReceipt: ...

    async def verify_existing_object(self, expected: ExpectedObject) -> VerifiedObjectReceipt: ...

    def open_verified_reader(
        self, expected: ExpectedObject
    ) -> AbstractAsyncContextManager[VerifiedObjectReader]: ...
