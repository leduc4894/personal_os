"""Deterministic narrow fake of the adapter's own ``S3ClientProtocol``.

This is a fake of the provider boundary protocol, not an S3/R2 behavioral
emulator: it does not parse keys, compute digests, persist bytes, model eventual
consistency or interpret error codes. It records every call in arrival order
with its arguments and returns or raises the next scripted outcome from a FIFO
queue, so contract tests for the adapter's retry, dedup and verification logic
are fully deterministic and never touch the network.

Provider values never leave the fake except the values a test scripts into it;
nothing here logs, hashes or persists content.
"""

from __future__ import annotations

from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass

from personal_os.object_storage.keys import CanonicalObjectKey
from r2_object_storage.client import (
    GetObjectResult,
    HeadObjectResult,
    PutObjectRequest,
)

#: Default canonical media type and opaque ETag the Task 7 factories script. The
#: test module imports these so the ``ExpectedObject`` it builds matches the HEAD
#: metadata the fake serves, without the fake depending on test-local constants.
DEFAULT_MEDIA_TYPE: str = "application/octet-stream"
DEFAULT_ETAG: str = "etag-1"


@dataclass(frozen=True, slots=True)
class RecordedCall:
    """One recorded ``S3ClientProtocol`` call and its safe arguments.

    Object keys appear only as the canonical key string a test supplied; no
    digest, bucket, endpoint or provider header is ever recorded.
    """

    method: str
    object_key: str | None
    if_match: str | None
    put_size_bytes: int | None
    put_media_type: str | None


class ScriptedStreamingBody:
    """Minimal mutable :class:`StreamingBodyProtocol` over scripted byte chunks.

    A streaming body is inherently stateful (a single forward pass), so this
    helper tracks its position; the chunks themselves are fixed at construction.
    """

    def __init__(self, chunks: tuple[bytes, ...]) -> None:
        self._chunks = chunks
        self._position = 0

    async def read(self, amt: int | None = None) -> bytes:
        if self._position >= len(self._chunks):
            return b""
        chunk = self._chunks[self._position]
        self._position += 1
        return chunk

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self._iter()

    async def _iter(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk

    async def __anext__(self) -> bytes:
        if self._position >= len(self._chunks):
            raise StopAsyncIteration
        chunk = self._chunks[self._position]
        self._position += 1
        return chunk


@dataclass(frozen=True, slots=True)
class _ReturnOutcome:
    result: HeadObjectResult | GetObjectResult | None


@dataclass(frozen=True, slots=True)
class _RaiseOutcome:
    cause: BaseException


_Outcome = _ReturnOutcome | _RaiseOutcome


def scripted_body(chunks: list[bytes]) -> ScriptedStreamingBody:
    """Build a deterministic streaming body from fixed byte chunks."""

    return ScriptedStreamingBody(chunks=tuple(chunks))


class ScriptedS3Client:
    """Deterministic fake implementing :class:`S3ClientProtocol`.

    Enqueue outcomes in the exact order the adapter is expected to call them.
    Each protocol method records its call then consumes the next outcome: a
    ``HeadObjectResult``/``GetObjectResult``/``None`` is returned, a
    :class:`BaseException` is raised. Calling a method with an empty queue fails
    closed so a missing script is loud rather than flaky.
    """

    def __init__(self) -> None:
        self._outcomes: deque[_Outcome] = deque()
        self.calls: list[RecordedCall] = []

    def enqueue(
        self, outcome: HeadObjectResult | GetObjectResult | BaseException | None
    ) -> ScriptedS3Client:
        """Queue the next return value or exception, FIFO across all methods."""

        if isinstance(outcome, BaseException):
            self._outcomes.append(_RaiseOutcome(outcome))
        else:
            self._outcomes.append(_ReturnOutcome(outcome))
        return self

    # --- Task 7 verification factories --------------------------------------
    #
    # Each factory returns a freshly-built client with the exact ordered outcomes
    # the adapter is expected to consume for one verification. They encode only
    # the narrow scripted behavior; they never hash, parse keys or persist bytes.

    @classmethod
    def matching_get(
        cls,
        payload: bytes,
        *,
        media_type: str = DEFAULT_MEDIA_TYPE,
        etag: str = DEFAULT_ETAG,
        chunks: list[bytes] | None = None,
    ) -> ScriptedS3Client:
        """Script a HEAD whose metadata matches ``payload`` and a GET serving it.

        A zero-byte payload yields an empty body (no chunks); a non-empty payload
        is served as one chunk unless ``chunks`` splits it.
        """

        client = cls()
        client.enqueue(HeadObjectResult(size_bytes=len(payload), media_type=media_type, etag=etag))
        served = chunks if chunks is not None else ([] if len(payload) == 0 else [payload])
        client.enqueue(GetObjectResult(body=scripted_body(served)))
        return client

    @classmethod
    def missing_object(cls) -> ScriptedS3Client:
        """Script an ordinary object absence: HEAD returns ``None``."""

        client = cls()
        client.enqueue(None)
        return client

    @classmethod
    def size_conflict(
        cls,
        payload: bytes,
        wrong_size_bytes: int,
        *,
        media_type: str = DEFAULT_MEDIA_TYPE,
        etag: str = DEFAULT_ETAG,
    ) -> ScriptedS3Client:
        """Script a HEAD whose ``ContentLength`` conflicts with the expected size."""

        client = cls()
        client.enqueue(
            HeadObjectResult(size_bytes=wrong_size_bytes, media_type=media_type, etag=etag)
        )
        return client

    @classmethod
    def media_conflict(
        cls,
        payload: bytes,
        wrong_media_type: str,
        *,
        etag: str = DEFAULT_ETAG,
    ) -> ScriptedS3Client:
        """Script a HEAD whose ``ContentType`` conflicts with the expected media."""

        client = cls()
        client.enqueue(
            HeadObjectResult(size_bytes=len(payload), media_type=wrong_media_type, etag=etag)
        )
        return client

    @classmethod
    def missing_etag(
        cls,
        payload: bytes,
        *,
        media_type: str = DEFAULT_MEDIA_TYPE,
    ) -> ScriptedS3Client:
        """Script a HEAD whose ETag is the empty string."""

        client = cls()
        client.enqueue(HeadObjectResult(size_bytes=len(payload), media_type=media_type, etag=""))
        return client

    @classmethod
    def malformed_etag(
        cls,
        payload: bytes,
        etag: str,
        *,
        media_type: str = DEFAULT_MEDIA_TYPE,
    ) -> ScriptedS3Client:
        """Script a HEAD whose ETag carries whitespace (a malformed opaque token)."""

        client = cls()
        client.enqueue(HeadObjectResult(size_bytes=len(payload), media_type=media_type, etag=etag))
        return client

    @classmethod
    def short_body(
        cls,
        declared_size_bytes: int,
        chunks: list[bytes],
        *,
        media_type: str = DEFAULT_MEDIA_TYPE,
        etag: str = DEFAULT_ETAG,
    ) -> ScriptedS3Client:
        """Script a matching HEAD but a GET body shorter than declared."""

        client = cls()
        client.enqueue(
            HeadObjectResult(size_bytes=declared_size_bytes, media_type=media_type, etag=etag)
        )
        client.enqueue(GetObjectResult(body=scripted_body(chunks)))
        return client

    @classmethod
    def excess_body(
        cls,
        declared_size_bytes: int,
        chunks: list[bytes],
        *,
        media_type: str = DEFAULT_MEDIA_TYPE,
        etag: str = DEFAULT_ETAG,
    ) -> ScriptedS3Client:
        """Script a matching HEAD but a GET body longer than declared."""

        client = cls()
        client.enqueue(
            HeadObjectResult(size_bytes=declared_size_bytes, media_type=media_type, etag=etag)
        )
        client.enqueue(GetObjectResult(body=scripted_body(chunks)))
        return client

    @classmethod
    def corrupt_after_prefix(
        cls,
        prefix: bytes,
        wrong_tail: bytes,
        *,
        media_type: str = DEFAULT_MEDIA_TYPE,
        etag: str = DEFAULT_ETAG,
    ) -> ScriptedS3Client:
        """Script a matching-size HEAD but a GET body with a wrong tail.

        The served body is ``prefix + wrong_tail``: its length matches the HEAD
        ``ContentLength`` so the size check passes, but its SHA-256 cannot match
        the expected digest, proving the fail-closed read.
        """

        client = cls()
        client.enqueue(
            HeadObjectResult(
                size_bytes=len(prefix) + len(wrong_tail), media_type=media_type, etag=etag
            )
        )
        client.enqueue(GetObjectResult(body=scripted_body([prefix, wrong_tail])))
        return client

    @classmethod
    def head_then_get_failure(
        cls,
        payload: bytes,
        get_cause: BaseException,
        *,
        media_type: str = DEFAULT_MEDIA_TYPE,
        etag: str = DEFAULT_ETAG,
    ) -> ScriptedS3Client:
        """Script a matching HEAD then a GET that raises ``get_cause``.

        Used for a changed-ETag conditional GET (a ``412 PreconditionFailed``) and
        for transient GET failures consumed by the retry policy.
        """

        client = cls()
        client.enqueue(HeadObjectResult(size_bytes=len(payload), media_type=media_type, etag=etag))
        client.enqueue(get_cause)
        return client

    @property
    def methods(self) -> list[str]:
        """The recorded method names in call order (close excluded)."""

        return [call.method for call in self.calls]

    @property
    def head_calls(self) -> list[RecordedCall]:
        return [call for call in self.calls if call.method == "head_object"]

    @property
    def put_calls(self) -> list[RecordedCall]:
        return [call for call in self.calls if call.method == "put_object"]

    @property
    def get_calls(self) -> list[RecordedCall]:
        return [call for call in self.calls if call.method == "get_object"]

    async def head_object(self, object_key: CanonicalObjectKey) -> HeadObjectResult | None:
        self.calls.append(
            RecordedCall(
                method="head_object",
                object_key=str(object_key),
                if_match=None,
                put_size_bytes=None,
                put_media_type=None,
            )
        )
        result = self._consume()
        if result is None:
            return None
        if not isinstance(result, HeadObjectResult):
            raise AssertionError("scripted head_object outcome is not a HeadObjectResult")
        return result

    async def put_object(self, request: PutObjectRequest) -> None:
        self.calls.append(
            RecordedCall(
                method="put_object",
                object_key=str(request.object_key),
                if_match=None,
                put_size_bytes=request.size_bytes,
                put_media_type=str(request.media_type),
            )
        )
        result = self._consume()
        if result is not None:
            raise AssertionError("scripted put_object must return None")

    async def get_object(self, object_key: CanonicalObjectKey, *, if_match: str) -> GetObjectResult:
        self.calls.append(
            RecordedCall(
                method="get_object",
                object_key=str(object_key),
                if_match=if_match,
                put_size_bytes=None,
                put_media_type=None,
            )
        )
        result = self._consume()
        if not isinstance(result, GetObjectResult):
            raise AssertionError("scripted get_object outcome is not a GetObjectResult")
        return result

    async def head_bucket(self) -> None:
        self.calls.append(
            RecordedCall(
                method="head_bucket",
                object_key=None,
                if_match=None,
                put_size_bytes=None,
                put_media_type=None,
            )
        )
        result = self._consume()
        if result is not None:
            raise AssertionError("scripted head_bucket must return None")

    async def close(self) -> None:
        # Lifecycle only; not recorded as an object-storage operation.
        return None

    def _consume(self) -> HeadObjectResult | GetObjectResult | None:
        if not self._outcomes:
            raise AssertionError("scripted S3 fake has no outcome queued for the next call")
        outcome = self._outcomes.popleft()
        if isinstance(outcome, _RaiseOutcome):
            raise outcome.cause
        return outcome.result
