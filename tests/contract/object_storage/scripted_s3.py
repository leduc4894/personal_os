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
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any

from botocore.exceptions import ResponseStreamingError

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
    ``fail_after_first`` scripts one mid-stream transport failure after the
    first chunk, and ``close_count`` observes the adapter's body disposal.
    """

    def __init__(self, chunks: tuple[bytes, ...], *, fail_after_first: bool = False) -> None:
        self._chunks = chunks
        self._position = 0
        self._fail_after_first = fail_after_first
        self.close_count = 0

    async def read(self, amt: int | None = None) -> bytes:
        if self._fail_after_first and self._position > 0:
            raise ResponseStreamingError(error="scripted mid-stream transport failure")
        if self._position >= len(self._chunks):
            return b""
        chunk = self._chunks[self._position]
        self._position += 1
        return chunk

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self._iter()

    async def _iter(self) -> AsyncIterator[bytes]:
        for index, chunk in enumerate(self._chunks):
            if self._fail_after_first and index > 0:
                raise ResponseStreamingError(error="scripted mid-stream transport failure")
            yield chunk

    async def __anext__(self) -> bytes:
        if self._fail_after_first and self._position > 0:
            raise ResponseStreamingError(error="scripted mid-stream transport failure")
        if self._position >= len(self._chunks):
            raise StopAsyncIteration
        chunk = self._chunks[self._position]
        self._position += 1
        return chunk

    async def aclose(self) -> None:
        self.close_count += 1


@dataclass(frozen=True, slots=True)
class _ReturnOutcome:
    result: HeadObjectResult | GetObjectResult | None


@dataclass(frozen=True, slots=True)
class _RaiseOutcome:
    cause: BaseException


_Outcome = _ReturnOutcome | _RaiseOutcome


def scripted_body(chunks: list[bytes], *, fail_after_first: bool = False) -> ScriptedStreamingBody:
    """Build a deterministic streaming body from fixed byte chunks."""

    return ScriptedStreamingBody(chunks=tuple(chunks), fail_after_first=fail_after_first)


def _payload_chunks(payload: bytes) -> list[bytes]:
    """Return the single-chunk body for ``payload`` (empty list for zero bytes)."""

    return [] if len(payload) == 0 else [payload]


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
        self.put_requests: list[PutObjectRequest] = []

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

    # --- Task 8 store factories ---------------------------------------------
    #
    # Each factory scripts the ordered outcomes the store path consumes: a
    # store-level HEAD, optionally a conditional PUT, then the full verification
    # HEAD+GET. They never hash, parse keys or persist bytes.

    @classmethod
    def missing_then_put_then_exact_get(
        cls,
        payload: bytes,
        media_type: str = DEFAULT_MEDIA_TYPE,
        *,
        etag: str = DEFAULT_ETAG,
    ) -> ScriptedS3Client:
        """Script a missing store HEAD, a successful conditional PUT, then the
        exact verification HEAD+GET that proves the stored object."""

        client = cls()
        client.enqueue(None)  # store HEAD: missing
        client.enqueue(None)  # conditional PUT: success
        client.enqueue(HeadObjectResult(size_bytes=len(payload), media_type=media_type, etag=etag))
        client.enqueue(GetObjectResult(body=scripted_body(_payload_chunks(payload))))
        return client

    @classmethod
    def existing_then_verify_get(
        cls,
        payload: bytes,
        media_type: str = DEFAULT_MEDIA_TYPE,
        *,
        etag: str = DEFAULT_ETAG,
    ) -> ScriptedS3Client:
        """Script a store-level HEAD showing an exact existing object, then the
        verification HEAD+GET that proves it. No PUT: the object is deduplicated."""

        client = cls()
        head = HeadObjectResult(size_bytes=len(payload), media_type=media_type, etag=etag)
        client.enqueue(head)  # store HEAD: exists
        client.enqueue(head)  # verification HEAD
        client.enqueue(GetObjectResult(body=scripted_body(_payload_chunks(payload))))
        return client

    @classmethod
    def missing_then_put_conflict_then_get(
        cls,
        payload: bytes,
        put_cause: BaseException,
        media_type: str = DEFAULT_MEDIA_TYPE,
        *,
        etag: str = DEFAULT_ETAG,
    ) -> ScriptedS3Client:
        """Script a missing store HEAD, a conditional PUT that raises ``put_cause``
        (a ``412 PreconditionFailed`` race loss), then the winner verification
        HEAD+GET. The 412 transitions directly to winner verification (no re-HEAD
        between the failed PUT and the winner verify)."""

        client = cls()
        client.enqueue(None)  # store HEAD: missing
        client.enqueue(put_cause)  # conditional PUT: 412 race loss
        client.enqueue(HeadObjectResult(size_bytes=len(payload), media_type=media_type, etag=etag))
        client.enqueue(GetObjectResult(body=scripted_body(_payload_chunks(payload))))
        return client

    @classmethod
    def missing_then_put_ambiguous_then_existing(
        cls,
        payload: bytes,
        put_cause: BaseException,
        media_type: str = DEFAULT_MEDIA_TYPE,
        *,
        etag: str = DEFAULT_ETAG,
    ) -> ScriptedS3Client:
        """Script a missing store HEAD, a conditional PUT that raises an ambiguous
        (transient) ``put_cause``, then a store HEAD showing the object now exists
        (the retry begins at HEAD, not a blind re-PUT), then the verification
        HEAD+GET."""

        client = cls()
        head = HeadObjectResult(size_bytes=len(payload), media_type=media_type, etag=etag)
        client.enqueue(None)  # attempt 1 store HEAD: missing
        client.enqueue(put_cause)  # attempt 1 PUT: ambiguous transient failure
        client.enqueue(head)  # attempt 2 store HEAD: exists (retry begins HEAD)
        client.enqueue(head)  # verification HEAD
        client.enqueue(GetObjectResult(body=scripted_body(_payload_chunks(payload))))
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
    def only_put(self) -> PutObjectRequest:
        """Return the single recorded PUT request; fail closed unless exactly one."""

        if len(self.put_requests) != 1:
            raise AssertionError(
                f"expected exactly one put_object call, found {len(self.put_requests)}"
            )
        return self.put_requests[0]

    @property
    def calls_after_put(self) -> list[str]:
        """Method names of every call after the first ``put_object``."""

        for index, call in enumerate(self.calls):
            if call.method == "put_object":
                return [entry.method for entry in self.calls[index + 1 :]]
        raise AssertionError("no put_object call recorded")

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
        self.put_requests.append(request)
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


@dataclass(frozen=True, slots=True)
class RecordedSdkCall:
    """One recorded raw multipart SDK call and its exact keyword arguments.

    The keywords appear exactly as the provider passed them (the staging key,
    upload ID, part number and expiry the provider bound), so contract tests
    assert the precise SDK surface. No provider header, endpoint or request id
    is ever recorded.
    """

    method: str
    kwargs: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class _MultipartReturnOutcome:
    result: Any


@dataclass(frozen=True, slots=True)
class _MultipartRaiseOutcome:
    cause: BaseException


_MultipartOutcome = _MultipartReturnOutcome | _MultipartRaiseOutcome


class ScriptedMultipartS3Client:
    """Deterministic fake of the raw multipart staging SDK surface.

    A fake of the provider boundary's SDK protocol, not an S3/R2 behavioral
    emulator: it records every raw SDK call in arrival order with the exact
    keyword arguments and returns or raises the next scripted outcome from a
    FIFO queue shared across all methods, so the staging provider's keyword
    mapping, retry and error translation are fully deterministic and never
    touch the network. Calling a method with an empty queue fails closed so a
    missing script is loud rather than flaky. Nothing here logs, hashes or
    persists content.
    """

    def __init__(self) -> None:
        self._outcomes: deque[_MultipartOutcome] = deque()
        self.calls: list[RecordedSdkCall] = []

    def enqueue(self, outcome: Any) -> ScriptedMultipartS3Client:
        """Queue the next raw return value or exception, FIFO across methods."""

        if isinstance(outcome, BaseException):
            self._outcomes.append(_MultipartRaiseOutcome(outcome))
        else:
            self._outcomes.append(_MultipartReturnOutcome(outcome))
        return self

    @property
    def method_names(self) -> list[str]:
        """The recorded raw SDK method names in call order."""

        return [call.method for call in self.calls]

    def single_call(self, method: str) -> RecordedSdkCall:
        """Return the single recorded call of ``method``; fail closed otherwise."""

        matches = [call for call in self.calls if call.method == method]
        if len(matches) != 1:
            raise AssertionError(f"expected exactly one {method} call, found {len(matches)}")
        return matches[0]

    async def create_multipart_upload(self, **kwargs: Any) -> Any:
        return self._consume("create_multipart_upload", kwargs)

    async def generate_presigned_url(
        self,
        ClientMethod: str,
        Params: Mapping[str, Any] | None = None,
        ExpiresIn: int = 3600,
        HttpMethod: Any = None,
    ) -> str:
        recorded: dict[str, Any] = {
            "ClientMethod": ClientMethod,
            "Params": dict(Params) if Params is not None else {},
            "ExpiresIn": ExpiresIn,
        }
        if HttpMethod is not None:
            recorded["HttpMethod"] = HttpMethod
        return self._consume("generate_presigned_url", recorded)

    async def list_parts(self, **kwargs: Any) -> Any:
        return self._consume("list_parts", kwargs)

    async def complete_multipart_upload(self, **kwargs: Any) -> Any:
        return self._consume("complete_multipart_upload", kwargs)

    async def abort_multipart_upload(self, **kwargs: Any) -> Any:
        return self._consume("abort_multipart_upload", kwargs)

    async def delete_object(self, **kwargs: Any) -> Any:
        return self._consume("delete_object", kwargs)

    async def get_object(self, **kwargs: Any) -> Any:
        return self._consume("get_object", kwargs)

    def _consume(self, method: str, kwargs: Mapping[str, Any]) -> Any:
        self.calls.append(RecordedSdkCall(method=method, kwargs=dict(kwargs)))
        if not self._outcomes:
            raise AssertionError("scripted multipart fake has no outcome queued for the next call")
        outcome = self._outcomes.popleft()
        if isinstance(outcome, _MultipartRaiseOutcome):
            raise outcome.cause
        return outcome.result
