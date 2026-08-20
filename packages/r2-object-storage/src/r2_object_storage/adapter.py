"""R2 content-addressable object store: verify-then-read with a fail-closed reader.

The store is the concrete provider adapter over :class:`S3ClientProtocol`. Every
read path performs the same verification sequence before a single byte reaches a
consumer:

1. ``HEAD`` the exact canonical key and require exact size and media type.
2. Capture the returned ETag as an opaque adapter-local token.
3. ``GET`` the complete object with ``If-Match: <etag>``.
4. Stream the response into a fresh bounded verification spool while hashing it.
5. Require exact end-of-stream size and SHA-256.

Only after :meth:`VerificationSpool.copy_and_hash` reaches EOF with an exact hash
and size does a reader context yield. Before that point any failure raises a
typed :class:`ObjectStorageError` and yields no bytes; the verification spool is
removed and its reservation released on every path, including a failure to open
the just-verified spool for reading.

:meth:`R2S3ObjectStore.store_stream` receives and hashes the input into a bounded
spool, derives the canonical key, ``HEAD``s it, and either deduplicates (exists)
or conditionally creates (missing) with ``IfNoneMatch="*"`` before running the
same full verification. An ambiguous PUT retries beginning at ``HEAD``; a PUT
``412`` transitions directly to winner verification. Single-flight concurrency
(Task 9) is layered on top.

Provider exception classes, response bodies, ETags, request ids, endpoints and
object keys remain chained only as internal causes and never enter a typed error,
a metric label or a diagnostic field.
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import random
import time
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable, Coroutine
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Final

from personal_os.diagnostics import DiagnosticLogger
from personal_os.diagnostics.events import EventName, SafeToken
from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import ApplicationError
from personal_os.object_storage import (
    CanonicalMediaType,
    ContentDigest,
    ExpectedObject,
    VerificationMethod,
    VerifiedObjectReader,
    VerifiedObjectReceipt,
    derive_canonical_object_key,
)
from personal_os.object_storage.errors import (
    DIGEST_MISMATCH,
    MEDIA_TYPE_INVALID,
    SIZE_OUT_OF_RANGE,
    ObjectStorageError,
)
from personal_os.object_storage.keys import CanonicalObjectKey
from r2_object_storage.client import (
    GetObjectResult,
    HeadObjectResult,
    PutObjectRequest,
    S3ClientProtocol,
    StreamingBodyProtocol,
)
from r2_object_storage.error_mapping import ConditionalCreateConflict, RetryPolicy
from r2_object_storage.metrics import (
    ObjectStorageMetrics,
    ObjectStorageOperation,
    ObjectStorageResult,
)
from r2_object_storage.spool import HashedSpool, SpoolManager, VerificationSpool

#: Maximum bytes a single verified read may return (matches the reader Protocol).
_MAX_READ_BYTES: Final[int] = 1_048_576
#: Fixed low-cardinality provider token bound to every diagnostic event.
_PROVIDER: Final[SafeToken] = SafeToken.parse("r2")


def _default_now_utc() -> datetime:
    return datetime.now(UTC)


#: The shared single-flight outcome for one content digest: the fully verified
#: receipt plus the verification method its owner resolved.
_SharedStoreOutcome = tuple[VerifiedObjectReceipt, VerificationMethod]


@dataclass(slots=True)
class _SingleFlightEntry:
    """One bounded per-process single-flight entry for a ``ContentDigest``.

    Holds the owner's shared outcome future plus the number of waiters
    currently attached to it. The entry exists only while the owner's shared
    R2 work is in flight; it is removed in a lock-protected ``finally`` and is
    never a verification cache.
    """

    future: asyncio.Future[_SharedStoreOutcome]
    waiter_count: int


async def _run_shielded(cleanup: Coroutine[object, object, None]) -> None:
    """Drive ``cleanup`` to completion even when the caller is cancelled.

    The cleanup runs as a short shielded local task; a ``CancelledError``
    delivered to the caller waits for the cleanup to finish and is then
    re-raised, so lock-protected table removal and waiter detachment can
    never be abandoned half-done.
    """

    task = asyncio.ensure_future(cleanup)
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        with suppress(asyncio.CancelledError):
            await task
        raise


class _AttemptTracker:
    """Mutable holder for the deepest retry attempt a verify operation reached.

    The retry policy invokes its operation callback with the ``attempt`` number;
    recording that number here (without touching the verbatim
    :meth:`RetryPolicy.run`) lets every outcome record the true attempt count.
    """

    __slots__ = ("count",)

    def __init__(self) -> None:
        self.count = 0


def _clone_application_error(error: ApplicationError) -> ApplicationError:
    """Rebuild one typed failure solely from its registered safe surface."""

    error_type: type[ApplicationError] = type(error)
    return error_type(error.error_code, safe_details=error.safe_details)


def require_exact_metadata(head: HeadObjectResult, expected: ExpectedObject) -> None:
    """Require the HEAD size and media to match ``expected`` and a usable ETag.

    A size mismatch at HEAD is integrity failure (design §6.3): under an
    immutable content-addressed key, a stored object whose ``ContentLength``
    differs from the expected size is corruption, not a conflicting write. A
    media-type mismatch is a metadata conflict: the stored canonical object
    carries a different canonical media type than the caller expects. A missing
    or whitespace-bearing ETag violates the opaque-token contract the
    conditional GET depends on, so it fails closed as a contract error before
    any GET.
    """

    if head.size_bytes != expected.size_bytes:
        raise ObjectStorageError(ErrorCode.OBJECT_STORAGE_INTEGRITY_FAILED)
    if head.media_type != str(expected.media_type):
        raise ObjectStorageError(ErrorCode.OBJECT_STORAGE_METADATA_CONFLICT)
    etag = head.etag
    if not etag or any(character.isspace() for character in etag):
        raise ObjectStorageError(ErrorCode.OBJECT_STORAGE_CONTRACT_INVALID)


def _parse_canonical_media_type(media_type: str) -> CanonicalMediaType:
    """Parse ``media_type`` as the canonical MIME grammar or fail at admission.

    A value that does not satisfy the canonical ``type/subtype`` grammar is an
    input failure detected before any byte is received or any R2 call is made.
    """

    try:
        return CanonicalMediaType.parse(media_type)
    except ValueError as cause:
        raise ObjectStorageError(
            ErrorCode.OBJECT_STORAGE_INPUT_INVALID,
            safe_details={"reason": MEDIA_TYPE_INVALID},
        ) from cause


def _parse_claimed_digest(claimed_sha256: str | None) -> ContentDigest | None:
    """Parse the optional claimed digest at admission, or return ``None``.

    A non-``None`` claim that is not exactly 64 lowercase hexadecimal characters
    is a malformed shape: it is rejected at admission before any byte is received
    or any R2 call is made. ``None`` means the caller made no claim.
    """

    if claimed_sha256 is None:
        return None
    try:
        return ContentDigest.parse(claimed_sha256)
    except ValueError as cause:
        raise ObjectStorageError(
            ErrorCode.OBJECT_STORAGE_INPUT_INVALID,
            safe_details={"reason": DIGEST_MISMATCH},
        ) from cause


async def _close_streaming_body(body: StreamingBodyProtocol) -> None:
    """Best-effort close of a GET response body.

    The verification spool already closes the body's async iterator when its drain
    completes; this covers the edge case where the drain never started and never
    raises into the primary error path. It never masks an in-flight result.
    """

    aclose = getattr(body, "aclose", None)
    if aclose is None:
        return
    try:
        outcome = aclose()
        if inspect.isawaitable(outcome):
            await outcome
    except Exception:
        return


def _open_read(path: Path) -> IO[bytes]:
    return open(path, "rb")


def _close_file(file_obj: IO[bytes]) -> None:
    with suppress(OSError):
        file_obj.close()


class _VerifiedObjectReader:
    """Bounded asynchronous reader over one fully verified spool file.

    Constructed only after :meth:`R2S3ObjectStore._verify_to_spool` proved the
    complete object. Reads come solely from the local verified spool; seeking,
    arbitrary ranges and access to the underlying path are unsupported. Closing
    the reader closes the file handle and removes the spool plus its reservation.
    """

    def __init__(self, file_obj: IO[bytes], cleanup: VerificationSpool) -> None:
        self._file = file_obj
        self._cleanup = cleanup
        self._closed = False

    @classmethod
    async def open(cls, hashed: HashedSpool, cleanup: VerificationSpool) -> _VerifiedObjectReader:
        file_obj = await asyncio.to_thread(_open_read, hashed.path)
        return cls(file_obj, cleanup)

    async def read(self, size_bytes: int = _MAX_READ_BYTES) -> bytes:
        if self._closed:
            raise ObjectStorageError(ErrorCode.OBJECT_STORAGE_CONTRACT_INVALID)
        if isinstance(size_bytes, bool) or not isinstance(size_bytes, int):
            raise ObjectStorageError(
                ErrorCode.OBJECT_STORAGE_INPUT_INVALID,
                safe_details={"reason": SIZE_OUT_OF_RANGE},
            )
        if size_bytes < 0 or size_bytes > _MAX_READ_BYTES:
            raise ObjectStorageError(
                ErrorCode.OBJECT_STORAGE_INPUT_INVALID,
                safe_details={"reason": SIZE_OUT_OF_RANGE},
            )
        return await asyncio.to_thread(self._file.read, size_bytes)

    def __aiter__(self) -> _VerifiedObjectReader:
        return self

    async def __anext__(self) -> bytes:
        chunk = await self.read(_MAX_READ_BYTES)
        if not chunk:
            raise StopAsyncIteration
        return chunk

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        # Close the handle before removing the file so the unlink succeeds on
        # platforms that forbid deleting an open file.
        await asyncio.to_thread(_close_file, self._file)
        await self._cleanup.close()


class R2S3ObjectStore:
    """Concrete content-addressable object store over Cloudflare R2.

    Verification is performed fresh on every call (no long-lived cache): each
    :meth:`resolve_verified_object`, :meth:`verify_existing_object` and
    :meth:`open_verified_reader` call runs its own HEAD plus conditional full GET.

    Concurrent :meth:`store_stream` calls for the same digest share the R2
    resolve/create/verify work through a bounded per-process single-flight
    table keyed by :class:`ContentDigest` (design §6.5). Each caller still
    hashes its own input into its own spool before joining; only the shared
    R2 work is deduplicated, and the entry is removed the moment that work
    completes, so the table cannot grow with lifetime object count.
    """

    def __init__(
        self,
        client: S3ClientProtocol,
        *,
        spools: SpoolManager,
        retry: RetryPolicy,
        metrics: ObjectStorageMetrics,
        logger: DiagnosticLogger,
        now_utc: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        jitter: Callable[[float, float], float] | None = None,
    ) -> None:
        self._client = client
        self._spools = spools
        self._retry = retry
        self._metrics = metrics
        self._logger = logger
        self._now_utc: Callable[[], datetime] = now_utc if now_utc is not None else _default_now_utc
        self._monotonic: Callable[[], float] = (
            monotonic if monotonic is not None else time.monotonic
        )
        self._sleep: Callable[[float], Awaitable[None]] = (
            sleep if sleep is not None else asyncio.sleep
        )
        self._jitter: Callable[[float, float], float] = (
            jitter if jitter is not None else random.uniform
        )
        self._single_flight: dict[ContentDigest, _SingleFlightEntry] = {}
        self._single_flight_lock = asyncio.Lock()
        self._closed = False

    @property
    def metrics(self) -> ObjectStorageMetrics:
        return self._metrics

    @property
    def spool_manager(self) -> SpoolManager:
        """The bounded spool manager backing this store (test inspection)."""

        return self._spools

    @property
    def single_flight_entry_count(self) -> int:
        """Single-flight entries currently in flight (bounded test snapshot)."""

        return len(self._single_flight)

    @property
    def single_flight_waiter_count(self) -> int:
        """Waiters currently attached to in-flight single-flight entries."""

        return sum(entry.waiter_count for entry in self._single_flight.values())

    async def resolve_verified_object(
        self, expected: ExpectedObject
    ) -> VerifiedObjectReceipt | None:
        """Verify and return a receipt, or ``None`` for ordinary absence.

        Only the typed ordinary-absence signal maps to ``None``; corrupt, short,
        excess or metadata-conflicting objects raise. Each call performs a fresh
        HEAD plus conditional full GET.
        """

        operation = ObjectStorageOperation.RESOLVE
        started = self._monotonic()
        tracker = _AttemptTracker()
        self._metrics.increment_in_flight(operation=operation)
        try:
            try:
                verification = await self._verify_to_spool(expected, operation, tracker)
            except ApplicationError as cause:
                if cause.error_code is ErrorCode.OBJECT_STORAGE_OBJECT_MISSING:
                    self._record_succeeded(
                        operation, started=started, size_bytes=0, attempt_count=tracker.count
                    )
                    return None
                self._record_failed(
                    operation,
                    cause,
                    started=started,
                    size_bytes=expected.size_bytes,
                    attempt_count=tracker.count,
                )
                raise
            try:
                receipt = self._build_receipt(
                    verification, expected, VerificationMethod.EXISTING_FULL_READ
                )
            finally:
                await verification.close()
            self._record_reserved(operation)
            self._record_succeeded(
                operation,
                started=started,
                size_bytes=receipt.size_bytes,
                attempt_count=tracker.count,
            )
            return receipt
        finally:
            self._metrics.decrement_in_flight(operation=operation)
            self._record_reserved(operation)

    async def verify_existing_object(self, expected: ExpectedObject) -> VerifiedObjectReceipt:
        """Verify an object the caller asserts exists; raise on absence.

        Ordinary absence raises ``object_storage_object_missing``; every other
        failure raises the matching typed code. Each call performs a fresh HEAD
        plus conditional full GET.
        """

        operation = ObjectStorageOperation.VERIFY
        started = self._monotonic()
        tracker = _AttemptTracker()
        self._metrics.increment_in_flight(operation=operation)
        try:
            try:
                verification = await self._verify_to_spool(expected, operation, tracker)
            except ApplicationError as cause:
                self._record_failed(
                    operation,
                    cause,
                    started=started,
                    size_bytes=expected.size_bytes,
                    attempt_count=tracker.count,
                )
                raise
            try:
                receipt = self._build_receipt(
                    verification, expected, VerificationMethod.EXISTING_FULL_READ
                )
            finally:
                await verification.close()
            self._record_reserved(operation)
            self._record_succeeded(
                operation,
                started=started,
                size_bytes=receipt.size_bytes,
                attempt_count=tracker.count,
            )
            return receipt
        finally:
            self._metrics.decrement_in_flight(operation=operation)
            self._record_reserved(operation)

    async def store_stream(
        self,
        stream: AsyncIterable[bytes],
        expected_size_bytes: int,
        media_type: str,
        claimed_sha256: str | None = None,
    ) -> VerifiedObjectReceipt:
        """Store a stream as an immutable content-addressable R2 object.

        The state machine is exact (design §5/§6):

        1. Validate admission: canonical media type, claimed digest shape and the
           declared size (enforced by the spool manager). No R2 call is made
           before admission completes.
        2. Receive and hash the complete input spool under bounded resource
           limits (the spool manager owns the receive deadline and cleanup).
        3. Compare the claimed digest against the backend-computed SHA-256; a
           well-formed claim that disagrees is rejected before any R2 call.
        4. Derive the expected object and canonical key from the computed digest.
        5. ``HEAD`` the canonical key:
           - exists -> full verify -> receipt ``EXISTING_FULL_READ`` (dedup).
           - missing -> one conditional ``PutObject`` with ``IfNoneMatch="*"``:
             success -> full verify -> ``UPLOADED_FULL_READ``;
             ``412`` -> full verify the winner -> ``EXISTING_FULL_READ``;
             ambiguous (transient transport failure) -> retry begins at HEAD.

        The input spool survives through PUT and verification and is removed in
        the receive context's shielded bounded cleanup on every exit path. A
        client digest is only a claim: the receipt is issued only after the
        backend's own full read passes exact digest, size and media verification.

        Same-process single flight (design §6.5): every caller hashes its own
        input into its own spool first; callers that computed the same digest
        then share one owner's R2 resolve/create/verify work and receive that
        owner's verified receipt. The per-digest entry exists only while the
        shared work is in flight and never caches a verification.
        """

        operation = ObjectStorageOperation.STORE
        started = self._monotonic()
        tracker = _AttemptTracker()
        self._metrics.increment_in_flight(operation=operation)
        try:
            canonical_media = _parse_canonical_media_type(media_type)
            claimed_digest = _parse_claimed_digest(claimed_sha256)

            async with self._spools.receive_stream(stream, expected_size_bytes) as hashed:
                self._record_reserved(operation)
                if claimed_digest is not None and hashed.content_digest != claimed_digest:
                    raise ObjectStorageError(
                        ErrorCode.OBJECT_STORAGE_INPUT_INVALID,
                        safe_details={"reason": DIGEST_MISMATCH},
                    )
                expected = ExpectedObject(
                    content_digest=hashed.content_digest,
                    size_bytes=hashed.size_bytes,
                    media_type=canonical_media,
                )
                receipt, method = await self._store_single_flight(
                    expected, hashed, operation, tracker
                )
            self._record_store_outcome(
                operation,
                method,
                started=started,
                size_bytes=receipt.size_bytes,
                attempt_count=tracker.count,
            )
            return receipt
        except ApplicationError as cause:
            self._record_failed(
                operation,
                cause,
                started=started,
                size_bytes=expected_size_bytes,
                attempt_count=tracker.count,
            )
            raise
        finally:
            self._metrics.decrement_in_flight(operation=operation)
            self._record_reserved(operation)

    @asynccontextmanager
    async def open_verified_reader(
        self, expected: ExpectedObject
    ) -> AsyncIterator[VerifiedObjectReader]:
        """Verify the full object, then yield a reader over the verified bytes.

        The context body is entered only after digest, size and media verification
        completed. On any verification failure the body is never entered: a typed
        :class:`ObjectStorageError` propagates from ``__aenter__`` and the
        consumer receives no bytes. A failure to open the just-verified spool for
        reading likewise closes the spool and records the failed outcome before
        re-raising, so no reservation ever leaks. Exiting the context closes and
        removes the verification spool.
        """

        operation = ObjectStorageOperation.READ
        started = self._monotonic()
        tracker = _AttemptTracker()
        self._metrics.increment_in_flight(operation=operation)
        try:
            try:
                verification = await self._verify_to_spool(expected, operation, tracker)
            except ApplicationError as cause:
                self._record_failed(
                    operation,
                    cause,
                    started=started,
                    size_bytes=expected.size_bytes,
                    attempt_count=tracker.count,
                )
                raise
            hashed = verification.hashed
            assert hashed is not None, "verification spool was not hashed"
            try:
                reader = await _VerifiedObjectReader.open(hashed, verification)
            except asyncio.CancelledError:
                # Cancellation re-raises unmapped (and unrecorded) after cleanup.
                await verification.close()
                raise
            except Exception:
                # The verified spool could not be reopened for reading: close the
                # spool (removing the file and releasing the reservation), record
                # the failed outcome, then re-raise the original cause.
                await verification.close()
                self._record_failed(
                    operation,
                    ObjectStorageError(ErrorCode.OBJECT_STORAGE_CONTRACT_INVALID),
                    started=started,
                    size_bytes=hashed.size_bytes,
                    attempt_count=tracker.count,
                )
                raise
            self._record_succeeded(
                operation,
                started=started,
                size_bytes=hashed.size_bytes,
                attempt_count=tracker.count,
            )
            try:
                yield reader
            finally:
                await reader.aclose()
        finally:
            self._metrics.decrement_in_flight(operation=operation)
            self._record_reserved(operation)

    async def close(self) -> None:
        """Close the store's underlying client exactly once."""

        if self._closed:
            return
        self._closed = True
        await self._client.close()

    # --- Verification core -------------------------------------------------

    async def _verify_to_spool(
        self,
        expected: ExpectedObject,
        operation: ObjectStorageOperation,
        tracker: _AttemptTracker,
    ) -> VerificationSpool:
        """HEAD, require exact metadata, then conditional full GET into a spool.

        Returns the hashed verification spool only after the streamed body passed
        exact digest and size verification. Any failure after the reservation is
        granted closes the spool (removing the file and releasing the reservation)
        before re-raising, so no reservation or partial spool ever leaks.
        """

        object_key = derive_canonical_object_key(expected.content_digest)
        head = await self._head_exact(object_key, operation, tracker)
        require_exact_metadata(head, expected)
        verification = await self._spools.reserve_verification(expected.size_bytes)
        self._record_reserved(operation)
        try:
            response = await self._get_with_retry(object_key, head.etag, operation, tracker)
            try:
                await verification.copy_and_hash(response.body, expected)
            finally:
                await _close_streaming_body(response.body)
        except BaseException:
            await verification.close()
            self._record_reserved(operation)
            raise
        return verification

    async def _head_exact(
        self,
        object_key: CanonicalObjectKey,
        operation: ObjectStorageOperation,
        tracker: _AttemptTracker,
    ) -> HeadObjectResult:
        """HEAD ``object_key`` under retry; map ordinary absence to a typed signal.

        ``head_object`` returns ``None`` for ordinary object absence and re-raises
        every other failure for the retry policy to classify. The absence return
        is the only ordinary-absence signal and is mapped to
        ``object_storage_object_missing`` here.
        """

        head = await self._run_with_retry(
            operation,
            tracker,
            lambda _attempt: self._client.head_object(object_key),
        )
        if head is None:
            raise ObjectStorageError(ErrorCode.OBJECT_STORAGE_OBJECT_MISSING)
        return head

    async def _get_with_retry(
        self,
        object_key: CanonicalObjectKey,
        etag: str,
        operation: ObjectStorageOperation,
        tracker: _AttemptTracker,
    ) -> GetObjectResult:
        """Conditional full GET under retry; map a changed ETag to integrity failure.

        A ``412 PreconditionFailed`` means the object changed between HEAD and GET;
        because canonical keys are immutable, a changed ETag is an integrity
        failure (design §7) and the adapter fails closed rather than following a
        moving value. The retry loop's conditional-conflict signal is therefore
        mapped to ``object_storage_integrity_failed`` here. This is the verify
        (GET) path only; Task 8's store path treats a PUT ``412`` as the
        deduplication/winner-verification signal instead.
        """

        try:
            return await self._run_with_retry(
                operation,
                tracker,
                lambda _attempt: self._client.get_object(object_key, if_match=etag),
            )
        except ConditionalCreateConflict as cause:
            raise ObjectStorageError(ErrorCode.OBJECT_STORAGE_INTEGRITY_FAILED) from cause

    async def _run_with_retry[T](
        self,
        operation: ObjectStorageOperation,
        tracker: _AttemptTracker,
        call: Callable[[int], Awaitable[T]],
    ) -> T:
        """Run ``call`` under the retry policy while tracking the true attempts.

        The verbatim :meth:`RetryPolicy.run` invokes its operation callback with
        the ``attempt`` number; this wrapper records that number in ``tracker``
        (so outcome records carry the true attempt count, not a constant) and
        counts one retry in the metrics sink each time an attempt beyond the
        first actually executes.
        """

        async def wrapped(attempt: int) -> T:
            if attempt > 1:
                self._metrics.record_retry(operation=operation)
            if attempt > tracker.count:
                tracker.count = attempt
            return await call(attempt)

        return await self._retry.run(
            wrapped,
            monotonic=self._monotonic,
            sleep=self._sleep,
            jitter=self._jitter,
        )

    def _build_receipt(
        self,
        verification: VerificationSpool,
        expected: ExpectedObject,
        method: VerificationMethod,
    ) -> VerifiedObjectReceipt:
        hashed = verification.hashed
        assert hashed is not None, "verification spool was not hashed"
        return VerifiedObjectReceipt(
            content_digest=hashed.content_digest,
            object_key=derive_canonical_object_key(expected.content_digest),
            size_bytes=hashed.size_bytes,
            media_type=expected.media_type,
            verified_at=self._now_utc(),
            verification_method=method,
        )

    # --- Store core -------------------------------------------------------

    async def _resolve_store_method(
        self,
        expected: ExpectedObject,
        hashed: HashedSpool,
        tracker: _AttemptTracker,
    ) -> VerificationMethod:
        """Resolve the store receipt method: dedup or upload.

        Wraps the ``HEAD`` -> optional conditional ``PUT`` sequence in one retry
        loop so an ambiguous PUT (a transient transport failure whose outcome is
        unknown) re-enters at ``HEAD`` rather than blindly re-uploading. If the
        retry HEAD now shows the object exists, the operation is resolved as a
        dedup; only if it is still missing does a second PUT execute.

        A ``412 PreconditionFailed`` from the conditional PUT is NOT a retry and
        NOT an integrity failure (design §11 line 469): the retry loop raises
        :class:`ConditionalCreateConflict`, caught here to transition directly to
        winner verification as ``EXISTING_FULL_READ``. A terminal PUT failure
        (``BadDigest``/auth) propagates as the mapped typed error. A GET-time
        ``412`` is a different signal mapped in :meth:`_get_with_retry`.

        The client opens the spool file fresh for every ``put_object`` call,
        positioning the body at byte 0 (the rewind before each PUT attempt).
        """

        object_key = derive_canonical_object_key(expected.content_digest)
        content_md5_base64 = base64.b64encode(hashed.md5_digest).decode()

        async def attempt(_attempt: int) -> VerificationMethod:
            head = await self._client.head_object(object_key)
            if head is not None:
                # Exists: dedup path. The subsequent full verify checks exact
                # metadata and fails closed on any mismatch instead of PUTting
                # over the immutable key (no overwrite, no self-repair).
                return VerificationMethod.EXISTING_FULL_READ
            await self._client.put_object(
                PutObjectRequest(
                    object_key=object_key,
                    spool_path=hashed.path,
                    size_bytes=hashed.size_bytes,
                    media_type=expected.media_type,
                    content_md5_base64=content_md5_base64,
                )
            )
            return VerificationMethod.UPLOADED_FULL_READ

        try:
            return await self._run_with_retry(ObjectStorageOperation.STORE, tracker, attempt)
        except ConditionalCreateConflict:
            # The conditional PUT lost the immutable-key race: another writer
            # won and the object now exists. Transition directly to winner
            # verification; do not retry the PUT.
            return VerificationMethod.EXISTING_FULL_READ

    # --- Single flight ----------------------------------------------------

    async def _store_single_flight(
        self,
        expected: ExpectedObject,
        hashed: HashedSpool,
        operation: ObjectStorageOperation,
        tracker: _AttemptTracker,
    ) -> _SharedStoreOutcome:
        """Own or join the per-digest single flight for the shared R2 work.

        The caller has already hashed its own input spool. The first caller
        for a digest becomes the owner and runs the HEAD -> conditional PUT ->
        full verification sequence; later callers with the same digest attach
        as waiters to the owner's outcome future. Waiters await through a
        shield so their own cancellation detaches them without cancelling the
        owner or the other waiters. The owner removes the entry in a
        lock-protected ``finally`` on every exit path, including cancellation,
        so the table is never a cache and never leaks an entry.
        """

        digest = expected.content_digest
        async with self._single_flight_lock:
            entry = self._single_flight.get(digest)
            if entry is None:
                entry = _SingleFlightEntry(
                    future=asyncio.get_running_loop().create_future(), waiter_count=0
                )
                self._single_flight[digest] = entry
                is_owner = True
            else:
                entry.waiter_count += 1
                is_owner = False

        if not is_owner:
            try:
                try:
                    outcome = await asyncio.shield(entry.future)
                except ApplicationError as cause:
                    raise _clone_application_error(cause) from None
            finally:
                await _run_shielded(self._detach_single_flight_waiter(digest))
            receipt, method = outcome
            if receipt.media_type != expected.media_type:
                # The same bytes were shared, but the owner stored a different
                # canonical media type than this caller declared; surface the
                # conflict instead of returning a mismatched receipt.
                raise ObjectStorageError(ErrorCode.OBJECT_STORAGE_METADATA_CONFLICT)
            return receipt, method

        try:
            method = await self._resolve_store_method(expected, hashed, tracker)
            verification = await self._verify_to_spool(expected, operation, tracker)
            try:
                receipt = self._build_receipt(verification, expected, method)
            finally:
                await verification.close()
            self._record_reserved(operation)
        except BaseException as cause:
            await _run_shielded(self._finish_single_flight(digest, cause=cause))
            raise
        await _run_shielded(self._finish_single_flight(digest, outcome=(receipt, method)))
        return receipt, method

    async def _detach_single_flight_waiter(self, digest: ContentDigest) -> None:
        """Detach one waiter from the digest's entry under the lock.

        When the last waiter detaches after the future already failed, the
        exception is marked retrieved so an unobserved failure can never raise
        a stray warning once every waiter is gone.
        """

        async with self._single_flight_lock:
            entry = self._single_flight.get(digest)
            if entry is None:
                return
            if entry.waiter_count > 0:
                entry.waiter_count -= 1
            if entry.waiter_count == 0 and entry.future.done() and not entry.future.cancelled():
                entry.future.exception()

    async def _finish_single_flight(
        self,
        digest: ContentDigest,
        *,
        cause: BaseException | None = None,
        outcome: _SharedStoreOutcome | None = None,
    ) -> None:
        """Complete the shared outcome and remove the digest's entry.

        The owner calls this exactly once from its ``finally`` path while
        holding the lock. A successful outcome resolves the future for every
        waiter; a typed failure is shared with attached waiters; an owner
        cancellation maps to a typed unavailable outcome for attached waiters
        (never a bare ``CancelledError``, which would masquerade as the
        waiter's own cancellation), while an entry with no waiters left is
        simply cancelled.
        """

        async with self._single_flight_lock:
            entry = self._single_flight.pop(digest, None)
            if entry is None or entry.future.done():
                return
            if cause is None:
                assert outcome is not None, "a completed owner must carry its outcome"
                entry.future.set_result(outcome)
            elif entry.waiter_count > 0:
                if isinstance(cause, ApplicationError):
                    entry.future.set_exception(cause)
                else:
                    entry.future.set_exception(
                        ObjectStorageError(ErrorCode.OBJECT_STORAGE_UNAVAILABLE)
                    )
            else:
                entry.future.cancel()

    # --- Metrics and diagnostics ------------------------------------------

    def _record_reserved(self, operation: ObjectStorageOperation) -> None:
        """Sample the process-wide spool reservation into the metrics sink.

        The value is the spool manager's current aggregate reservation (input
        and verification spools together); ``operation`` only identifies which
        operation's admission change triggered the sample. Sampling the
        aggregate keeps the recorded maximum an exact bound of true state.
        """

        self._metrics.record_reserved_bytes(
            operation=operation, size_bytes=self._spools.reserved_size_bytes
        )

    def _duration_ms(self, started: float) -> int:
        return max(0, int((self._monotonic() - started) * 1000))

    def _record_succeeded(
        self,
        operation: ObjectStorageOperation,
        *,
        started: float,
        size_bytes: int,
        attempt_count: int,
    ) -> None:
        duration_ms = self._duration_ms(started)
        self._metrics.record_operation(
            operation=operation,
            result=ObjectStorageResult.SUCCEEDED,
            duration_ms=duration_ms,
            size_bytes=size_bytes,
            attempt_count=attempt_count,
        )
        self._logger.emit(
            EventName.OBJECT_STORAGE_OPERATION_SUCCEEDED,
            {
                "operation": operation,
                "duration_ms": duration_ms,
                "size_bytes": size_bytes,
                "attempt_count": attempt_count,
                "provider": _PROVIDER,
            },
        )

    def _record_store_outcome(
        self,
        operation: ObjectStorageOperation,
        method: VerificationMethod,
        *,
        started: float,
        size_bytes: int,
        attempt_count: int,
    ) -> None:
        """Record a completed store: ``DEDUPLICATED`` for a dedup/412-winner path,
        ``SUCCEEDED`` for a conditional-upload path. Emits the matching registered
        event (:data:`OBJECT_STORAGE_OBJECT_DEDUPLICATED` or
        :data:`OBJECT_STORAGE_OPERATION_SUCCEEDED`)."""

        duration_ms = self._duration_ms(started)
        if method is VerificationMethod.EXISTING_FULL_READ:
            result = ObjectStorageResult.DEDUPLICATED
            event = EventName.OBJECT_STORAGE_OBJECT_DEDUPLICATED
        else:
            result = ObjectStorageResult.SUCCEEDED
            event = EventName.OBJECT_STORAGE_OPERATION_SUCCEEDED
        self._metrics.record_operation(
            operation=operation,
            result=result,
            duration_ms=duration_ms,
            size_bytes=size_bytes,
            attempt_count=attempt_count,
        )
        self._logger.emit(
            event,
            {
                "operation": operation,
                "duration_ms": duration_ms,
                "size_bytes": size_bytes,
                "attempt_count": attempt_count,
                "provider": _PROVIDER,
            },
        )

    def _record_failed(
        self,
        operation: ObjectStorageOperation,
        error: ApplicationError,
        *,
        started: float,
        size_bytes: int,
        attempt_count: int,
    ) -> None:
        duration_ms = self._duration_ms(started)
        self._metrics.record_operation(
            operation=operation,
            result=ObjectStorageResult.FAILED,
            error_code=error.error_code,
            duration_ms=duration_ms,
            size_bytes=size_bytes,
            attempt_count=attempt_count,
        )
        event = (
            EventName.OBJECT_STORAGE_INTEGRITY_FAILED
            if error.error_code is ErrorCode.OBJECT_STORAGE_INTEGRITY_FAILED
            else EventName.OBJECT_STORAGE_OPERATION_FAILED
        )
        self._logger.emit(
            event,
            {
                "operation": operation,
                "duration_ms": duration_ms,
                "attempt_count": attempt_count,
                "provider": _PROVIDER,
                "error_code": error.error_code,
                "error_category": error.category,
                "is_retryable": error.is_retryable,
                "size_bytes": size_bytes,
            },
        )
