"""Unit fidelity tests for the canonical-core integration harness fakes.

The local-filesystem object store stands in for the live R2 adapter across
the canonical-core integration suite, so its store contract must mirror the
production adapter's fail-closed behavior: re-storing an existing digest
under a different canonical media type is a metadata conflict and an
identical re-store resolves as a verified dedup receipt — never a silent
pass that the real bucket would reject.
"""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from tests.integration.canonical_core.conftest import LocalFilesystemObjectStore

from personal_os.error_contracts.codes import ErrorCode
from personal_os.object_storage import VerificationMethod
from personal_os.object_storage.errors import ObjectStorageError

_TEST_PAYLOAD: bytes = b"canonical-harness-fidelity-payload"


async def _single_chunk_stream(payload: bytes) -> AsyncIterator[bytes]:
    yield payload


@pytest.mark.asyncio
async def test_fake_store_rejects_same_digest_different_media_restore(
    tmp_path: Path,
) -> None:
    store = LocalFilesystemObjectStore(tmp_path)

    first = await store.store_stream(
        _single_chunk_stream(_TEST_PAYLOAD), len(_TEST_PAYLOAD), "text/markdown"
    )
    assert first.verification_method is VerificationMethod.UPLOADED_FULL_READ

    with pytest.raises(ObjectStorageError) as rejection:
        await store.store_stream(
            _single_chunk_stream(_TEST_PAYLOAD),
            len(_TEST_PAYLOAD),
            "application/octet-stream",
        )
    assert rejection.value.error_code is ErrorCode.OBJECT_STORAGE_METADATA_CONFLICT


@pytest.mark.asyncio
async def test_fake_store_resolves_identical_restore_as_verified_dedup(
    tmp_path: Path,
) -> None:
    store = LocalFilesystemObjectStore(tmp_path)

    await store.store_stream(
        _single_chunk_stream(_TEST_PAYLOAD), len(_TEST_PAYLOAD), "text/markdown"
    )
    duplicate = await store.store_stream(
        _single_chunk_stream(_TEST_PAYLOAD), len(_TEST_PAYLOAD), "text/markdown"
    )

    assert duplicate.verification_method is VerificationMethod.EXISTING_FULL_READ
