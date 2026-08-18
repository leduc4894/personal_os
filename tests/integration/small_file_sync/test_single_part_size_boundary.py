"""The 16 MiB single-part ceiling over the real wire (spec 3.1, 10.1, 10.2).

The ceiling is exact: a declared size equal to the server-owned limit is
accepted and a real limit-sized body streams through the bounded content
limiter, the offline object store's full digest verification and the
publication path to a committed receipt; one declared byte more is the
closed size-limit rejection before any reservation, and a body one byte over
the declared limit is cut off by the stream limiter before anything can
publish. The boundary bytes are synthetic — a repeating pattern with the
real SHA-256 computed over the exact payload — and never leave the process.
"""

from __future__ import annotations

from hashlib import sha256
from typing import Any, Final
from uuid import uuid4

from tests.integration.small_file_sync.conftest import SmallFileWireHarness

from personal_os.small_file_sync.contracts import MAX_SINGLE_PART_FILE_SIZE_BYTES

_MEDIA_TYPE: Final[str] = "text/markdown"
_LOCATOR: Final[str] = "notes/boundary-note.md"

#: One chunk of the synthetic boundary payload; the exact body is the chunk
#: repeated to the exact target size so the digest is computed over the real
#: bytes the route receives.
_BOUNDARY_CHUNK: Final[bytes] = b"0123456789abcdef"
_CHUNK_SIZE: Final[int] = len(_BOUNDARY_CHUNK)


def _boundary_content(size_bytes: int) -> bytes:
    chunks, remainder = divmod(size_bytes, _CHUNK_SIZE)
    return _BOUNDARY_CHUNK * chunks + _BOUNDARY_CHUNK[:remainder]


def _create_body(size_bytes: int, sha256_hex: str) -> dict[str, Any]:
    return {
        "event_id": str(uuid4()),
        "idempotency_key": str(uuid4()),
        "operation": "create",
        "local_file_id": str(uuid4()),
        "source_id": None,
        "base_version_id": None,
        "normalized_locator": _LOCATOR,
        "sha256": sha256_hex,
        "size_bytes": size_bytes,
        "media_type": _MEDIA_TYPE,
        "policy_revision": 3,
    }


def _single_part_token(harness: SmallFileWireHarness, body: dict[str, Any]) -> str:
    response = harness.preflight(body)
    assert response.status_code == 200, response.text
    data = dict(response.json()["data"])
    assert data["outcome"] == "single_part_upload", data
    return str(data["operation_id"])


def test_a_file_exactly_at_the_ceiling_commits_end_to_end(
    offline_harness: SmallFileWireHarness,
) -> None:
    harness = offline_harness
    content = _boundary_content(MAX_SINGLE_PART_FILE_SIZE_BYTES)
    body = _create_body(len(content), sha256(content).hexdigest())

    token = _single_part_token(harness, body)
    response = harness.upload(token, content)
    assert response.status_code == 200, response.text
    data = dict(response.json()["data"])
    assert data["result_kind"] == "committed"
    assert harness.sync_state.publication_commits == 1
    assert harness.sync_state.stored_digest_count == 1


def test_one_declared_byte_over_the_ceiling_is_rejected_before_reservation(
    offline_harness: SmallFileWireHarness,
) -> None:
    harness = offline_harness
    body = _create_body(MAX_SINGLE_PART_FILE_SIZE_BYTES + 1, "0" * 64)
    response = harness.preflight(body)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "small_file_size_limit_exceeded"
    assert harness.sync_state.reservation_count == 0
    assert harness.sync_state.publication_commits == 0


def test_streamed_bytes_beyond_the_declared_size_fail_verification(
    offline_harness: SmallFileWireHarness,
) -> None:
    """One streamed byte over the declared size never publishes.

    The content limiter enforces the server-owned 16 MiB ceiling; the exact
    declared-size contract is enforced by the bounded verification path, so
    a body that outruns its declared fingerprint is the closed integrity
    failure with nothing stored and nothing published.
    """

    harness = offline_harness
    content = _boundary_content(1024)
    body = _create_body(len(content), sha256(content).hexdigest())
    token = _single_part_token(harness, body)

    response = harness.upload(token, content + b"x")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "small_file_content_integrity_failed"
    assert harness.sync_state.stored_digest_count == 0
    assert harness.sync_state.publication_commits == 0
