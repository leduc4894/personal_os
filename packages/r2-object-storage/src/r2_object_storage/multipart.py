"""Private R2 multipart staging provider: the sole multipart SDK boundary.

This module is the only place the multipart S3/R2 request and response
keyword surface exists. It exposes exactly seven staging capabilities — create
one upload, presign exactly one part PUT, list one upload's parts, complete,
abort, remove one staging object and full-read exactly one staging object —
and nothing else: there is deliberately no bucket listing, no batch or prefix
deletion, no version listing, no provider-side copy and no canonical-key read
or delete anywhere in this package.

Every method first requires a validated private
:class:`MultipartStagingKey` value (and, where applicable, a validated
:class:`~personal_os.multipart_upload.ports.MultipartProviderUploadId`); the
staging-key grammar is ``staging/multipart/{opaque base64url token}``, a
prefix a canonical ``objects/sha256/...`` key can never satisfy, so a
presigned part URL cannot target a canonical object, the staging full read
cannot fetch one and cleanup cannot touch one. A presigned URL authorizes a
single ``upload_part`` request with the fixed ``PartNumber``, ``UploadId``
and content length for exactly ten minutes.

Every SDK call runs under the shared bounded retry policy (explicit attempt
budget and operation deadline; the shared SDK client already pins connect and
read timeouts) and every failure crosses the boundary as the typed
:class:`~personal_os.multipart_upload.errors.MultipartUploadError` mapped by
:func:`r2_object_storage.error_mapping.map_multipart_failure`. The staging
full read applies that same typed mapping to a mid-stream body failure, so no
raw provider exception ever crosses this package. Provider exception text,
request ids, staging keys, upload IDs, ETags, presigned URLs and response
bodies remain chained causes only; they never enter a typed error, a
diagnostic event or a metric label. Diagnostic events carry only the closed
operation token, bounded counters and the fixed provider token.

The one exact-key staging removal operation is named plainly — as the typed
protocol declaration and the single direct call in the staging-removal path —
and the composition contract permits that capability name in this module
alone, only for those two positions, never as a quoted name or through
dynamic dispatch. The Child 7 spec adds exactly this single exact-key staging
removal (spec 6.4) while every broad-cleanup capability stays forbidden: no
list, wildcard, prefix or canonical-object operation is introduced anywhere.
"""

from __future__ import annotations

import asyncio
import inspect
import random
import re
import time
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final, Protocol, Unpack

from botocore.exceptions import ClientError

from personal_os.diagnostics import DiagnosticLogger
from personal_os.diagnostics.events import EventName, SafeToken
from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import ApplicationError
from personal_os.multipart_upload.contracts import (
    MULTIPART_PART_URL_LIFETIME,
    MultipartPartRange,
    MultipartPartUrl,
)
from personal_os.multipart_upload.errors import MultipartUploadError
from personal_os.multipart_upload.ports import (
    MultipartProviderPartETag,
    MultipartProviderUploadId,
)
from r2_object_storage.error_mapping import (
    OBJECT_MISSING_ERROR_CODES,
    UPLOAD_ABSENT_ERROR_CODES,
    RetryPolicy,
    client_error_code,
    map_multipart_failure,
)

if TYPE_CHECKING:
    from types_aiobotocore_s3.type_defs import (
        AbortMultipartUploadRequestTypeDef,
        CompleteMultipartUploadRequestTypeDef,
        CreateMultipartUploadRequestTypeDef,
        DeleteObjectRequestTypeDef,
        GetObjectRequestTypeDef,
        ListPartsRequestTypeDef,
    )

#: The only prefix a private staging key may carry. It is deliberately not the
#: canonical ``objects/sha256`` prefix, so no staging value can ever denote a
#: canonical object key (spec 3.3/3.4).
_STAGING_KEY_PREFIX: Final[str] = "staging/multipart/"
#: Opaque base64url staging-key token bounds (mirrors the session-ID grammar).
_STAGING_KEY_TOKEN_MIN_LENGTH: Final[int] = 32
_STAGING_KEY_TOKEN_MAX_LENGTH: Final[int] = 128
_STAGING_KEY_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"staging/multipart/[A-Za-z0-9_-]{32,128}"
)

#: The single ``upload_part`` presign target: one numbered part PUT only.
_UPLOAD_PART_CLIENT_METHOD: Final[str] = "upload_part"
#: Ten minutes, expressed as the presign ``ExpiresIn`` seconds value.
_PART_URL_EXPIRES_IN_SECONDS: Final[int] = int(MULTIPART_PART_URL_LIFETIME.total_seconds())
#: Bounded ListParts pagination: the 13-part geometry fits one page; the loop
#: hard-stops after this many pages so a hostile provider answer can never
#: drive an unbounded walk (spec 6.1 "explicit deadline and bounded retry").
_MAXIMUM_LIST_PARTS_PAGES: Final[int] = 4

#: Fixed low-cardinality provider token bound to every diagnostic event.
_PROVIDER: Final[SafeToken] = SafeToken.parse("r2")


class MultipartStagingOperation(StrEnum):
    """The closed set of staging provider operations recorded in diagnostics."""

    CREATE_UPLOAD = "create_upload"
    PRESIGN_PART = "presign_part"
    LIST_PARTS = "list_parts"
    COMPLETE_UPLOAD = "complete_upload"
    ABORT_UPLOAD = "abort_upload"
    DELETE_STAGING_OBJECT = "delete_staging_object"
    READ_STAGING_OBJECT = "read_staging_object"


@dataclass(frozen=True, slots=True)
class MultipartStagingKey:
    """One validated private staging-object key of a single upload session.

    The grammar is exactly ``staging/multipart/`` followed by 32 to 128
    printable base64url characters: a shape no canonical
    ``objects/sha256/...`` key can satisfy, so a value of this type can never
    address canonical content. The key is server-private
    database-sensitive material (spec 4.1): it never renders outside a
    redacted ``repr`` and never enters an error, event or metric label.
    """

    value: str

    def __repr__(self) -> str:
        return f"{type(self).__name__}(value=<redacted>)"

    @classmethod
    def parse(cls, value: str) -> MultipartStagingKey:
        """Validate ``value`` against the closed staging-key grammar."""

        if _STAGING_KEY_PATTERN.fullmatch(value) is None:
            raise ValueError(
                "value does not satisfy the multipart staging key contract: "
                f"{_STAGING_KEY_PREFIX}{_STAGING_KEY_TOKEN_MIN_LENGTH} to "
                f"{_STAGING_KEY_TOKEN_MAX_LENGTH} URL-safe characters after the "
                "staging prefix, never a canonical object key"
            )
        return cls(value)


@dataclass(frozen=True, slots=True)
class MultipartProviderPart:
    """One provider-observed completed part fact (spec 3.6/4.1).

    The provider — never the client — observed the part number, its opaque
    ETag and its exact size. The ETag is database-sensitive material and never
    renders outside a redacted ``repr``.
    """

    part_number: int
    etag: MultipartProviderPartETag
    size_bytes: int

    def __repr__(self) -> str:
        return f"{type(self).__name__}(<redacted>)"

    def __post_init__(self) -> None:
        if self.part_number < 1:
            raise ValueError("part_number must be a positive part number")
        if self.size_bytes < 0:
            raise ValueError("size_bytes must be a non-negative byte size")


class MultipartStagingSdkClient(Protocol):
    """The exact raw aiobotocore surface the staging provider may call.

    The real ``types_aiobotocore_s3`` client satisfies this protocol
    structurally, and the scripted contract-test double implements the same
    seven declared operations. The exact-key staging removal operation is
    named plainly here and invoked through this typed declaration only —
    never through a quoted name or dynamic dispatch — and the composition
    contract permits that one capability name in this module alone, only for
    this protocol declaration and the single staging-removal call. The
    staging full read is the same shape: one ``get_object`` of exactly the
    validated private staging key, never a canonical-key read. This is the
    capability boundary: no other SDK operation is reachable through the
    provider.
    """

    async def create_multipart_upload(
        self, **kwargs: Unpack[CreateMultipartUploadRequestTypeDef]
    ) -> Any: ...

    async def generate_presigned_url(
        self,
        ClientMethod: str,
        Params: Mapping[str, Any] = ...,
        ExpiresIn: int = 3600,
        HttpMethod: str = ...,
    ) -> str: ...

    async def list_parts(self, **kwargs: Unpack[ListPartsRequestTypeDef]) -> Any: ...

    async def complete_multipart_upload(
        self, **kwargs: Unpack[CompleteMultipartUploadRequestTypeDef]
    ) -> Any: ...

    async def abort_multipart_upload(
        self, **kwargs: Unpack[AbortMultipartUploadRequestTypeDef]
    ) -> Any: ...

    async def delete_object(self, **kwargs: Unpack[DeleteObjectRequestTypeDef]) -> Any: ...

    async def get_object(self, **kwargs: Unpack[GetObjectRequestTypeDef]) -> Any: ...


class _MalformedProviderState(Exception):
    """Internal signal that a provider response shape failed closed validation.

    Raised only inside a retried SDK call so the raw shape problem cannot be
    re-classified by the retry mapper; the provider translates it to the typed
    ``multipart_provider_state_invalid`` error outside the retry loop. It
    carries no provider value.
    """


class _AttemptTracker:
    """Mutable holder for the deepest retry attempt one operation reached."""

    __slots__ = ("count",)

    def __init__(self) -> None:
        self.count = 0


def _default_now_utc() -> datetime:
    return datetime.now(UTC)


class R2MultipartStagingProvider:
    """The private multipart staging capability over one bounded SDK client.

    Constructed with the process-wide SDK client the client manager owns, the
    bucket name and the shared bounded retry policy. All six methods validate
    their private staging-key (and upload-ID) values before any SDK call, so
    an unvalidated or canonical-shaped key can never reach the provider. The
    create path is the only one that mints provider work; an ambiguous create
    retry is resolved by the durable session replay above this boundary (spec
    6.1), and the idempotent-absence rule of spec 6.4 makes aborting an
    already-absent upload or removing an already-absent staging object a
    successful cleanup.
    """

    def __init__(
        self,
        client: MultipartStagingSdkClient,
        *,
        bucket: str,
        logger: DiagnosticLogger,
        retry: RetryPolicy | None = None,
        now_utc: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        jitter: Callable[[float, float], float] | None = None,
    ) -> None:
        self._client = client
        self._bucket = bucket
        self._logger = logger
        self._retry: RetryPolicy = retry if retry is not None else RetryPolicy()
        self._now_utc: Callable[[], datetime] = now_utc if now_utc is not None else _default_now_utc
        self._monotonic: Callable[[], float] = (
            monotonic if monotonic is not None else _now_monotonic
        )
        self._sleep: Callable[[float], Awaitable[None]] = (
            sleep if sleep is not None else _default_sleep
        )
        self._jitter: Callable[[float, float], float] = (
            jitter if jitter is not None else _default_jitter
        )

    async def create_upload(self, staging_key: MultipartStagingKey) -> MultipartProviderUploadId:
        """Create exactly one staging upload at the validated private key."""

        key = self._require_staging_key(staging_key)
        operation = MultipartStagingOperation.CREATE_UPLOAD
        started = self._monotonic()
        tracker = _AttemptTracker()
        try:
            response = await self._run_with_retry(
                operation,
                tracker,
                lambda _attempt: self._client.create_multipart_upload(
                    Bucket=self._bucket,
                    Key=key.value,
                ),
            )
            upload_id = self._provider_upload_id(response)
            self._record_succeeded(operation, started=started, attempt_count=tracker.count)
            return upload_id
        except ApplicationError as cause:
            self._record_failed(operation, cause, started=started, attempt_count=tracker.count)
            raise

    async def presign_part(
        self,
        staging_key: MultipartStagingKey,
        upload_id: MultipartProviderUploadId,
        part_range: MultipartPartRange,
    ) -> MultipartPartUrl:
        """Presign exactly one numbered part PUT of the private staging upload.

        The signed request is a single ``upload_part`` with the fixed
        ``PartNumber``, ``UploadId`` and ``ContentLength`` and the ten-minute
        ``ExpiresIn``; the returned value object carries the URL, the exact
        byte range and the URL's own expiry (spec 3.4/6.2).
        """

        key = self._require_staging_key(staging_key)
        identity = self._require_upload_id(upload_id)
        operation = MultipartStagingOperation.PRESIGN_PART
        started = self._monotonic()
        tracker = _AttemptTracker()
        expires_at = self._now_utc() + MULTIPART_PART_URL_LIFETIME
        try:
            url = await self._run_with_retry(
                operation,
                tracker,
                lambda _attempt: self._client.generate_presigned_url(
                    _UPLOAD_PART_CLIENT_METHOD,
                    Params={
                        "Bucket": self._bucket,
                        "Key": key.value,
                        "UploadId": identity.value,
                        "PartNumber": part_range.part_number,
                        "ContentLength": part_range.size_bytes,
                    },
                    ExpiresIn=_PART_URL_EXPIRES_IN_SECONDS,
                ),
            )
            part_url = self._part_url(part_range, url, expires_at)
            self._record_succeeded(
                operation,
                started=started,
                size_bytes=part_range.size_bytes,
                attempt_count=tracker.count,
            )
            return part_url
        except ApplicationError as cause:
            self._record_failed(
                operation,
                cause,
                started=started,
                size_bytes=part_range.size_bytes,
                attempt_count=tracker.count,
            )
            raise

    async def list_parts(
        self,
        staging_key: MultipartStagingKey,
        upload_id: MultipartProviderUploadId,
    ) -> tuple[MultipartProviderPart, ...]:
        """Return the provider-observed part facts of exactly this upload.

        Follows bounded ListParts pagination by part-number marker only; the
        answer is exactly what the provider observed for this one upload —
        never a bucket or prefix listing (spec 3.6/6.1).
        """

        key = self._require_staging_key(staging_key)
        identity = self._require_upload_id(upload_id)
        operation = MultipartStagingOperation.LIST_PARTS
        started = self._monotonic()
        tracker = _AttemptTracker()
        try:
            try:
                pages = await self._run_with_retry(
                    operation,
                    tracker,
                    lambda _attempt: self._fetch_part_pages(key.value, identity.value),
                )
                parts = _parse_part_pages(pages)
            except _MalformedProviderState as cause:
                # Raised only after the retry finished: a raw shape problem is
                # the typed provider-state error, never retry input.
                raise MultipartUploadError(ErrorCode.MULTIPART_PROVIDER_STATE_INVALID) from cause
            self._record_succeeded(operation, started=started, attempt_count=tracker.count)
            return parts
        except ApplicationError as cause:
            self._record_failed(operation, cause, started=started, attempt_count=tracker.count)
            raise

    async def complete_upload(
        self,
        staging_key: MultipartStagingKey,
        upload_id: MultipartProviderUploadId,
        parts: Sequence[MultipartProviderPart],
    ) -> None:
        """Complete the staging upload from provider-observed part metadata.

        Only part numbers and ETags this boundary (or an equal provider
        observation) produced are sent; the caller never supplies client-echoed
        completion evidence (spec 6.3.3).
        """

        key = self._require_staging_key(staging_key)
        identity = self._require_upload_id(upload_id)
        observed = self._require_observed_parts(parts)
        operation = MultipartStagingOperation.COMPLETE_UPLOAD
        started = self._monotonic()
        tracker = _AttemptTracker()
        try:
            await self._run_with_retry(
                operation,
                tracker,
                lambda _attempt: self._client.complete_multipart_upload(
                    Bucket=self._bucket,
                    Key=key.value,
                    UploadId=identity.value,
                    MultipartUpload={
                        "Parts": [
                            {"ETag": part.etag.value, "PartNumber": part.part_number}
                            for part in observed
                        ]
                    },
                ),
            )
            self._record_succeeded(operation, started=started, attempt_count=tracker.count)
        except ApplicationError as cause:
            self._record_failed(operation, cause, started=started, attempt_count=tracker.count)
            raise

    async def abort_upload(
        self, staging_key: MultipartStagingKey, upload_id: MultipartProviderUploadId
    ) -> None:
        """Abort exactly this upload; an already-absent upload is a success."""

        key = self._require_staging_key(staging_key)
        identity = self._require_upload_id(upload_id)
        operation = MultipartStagingOperation.ABORT_UPLOAD
        started = self._monotonic()
        tracker = _AttemptTracker()
        try:
            await self._run_with_retry(
                operation,
                tracker,
                lambda _attempt: self._abort_exact_upload(key.value, identity.value),
            )
            self._record_succeeded(operation, started=started, attempt_count=tracker.count)
        except ApplicationError as cause:
            self._record_failed(operation, cause, started=started, attempt_count=tracker.count)
            raise

    async def delete_staging_object(self, staging_key: MultipartStagingKey) -> None:
        """Remove exactly one staging object; an absent object is a success."""

        key = self._require_staging_key(staging_key)
        operation = MultipartStagingOperation.DELETE_STAGING_OBJECT
        started = self._monotonic()
        tracker = _AttemptTracker()
        try:
            await self._run_with_retry(
                operation,
                tracker,
                lambda _attempt: self._remove_staging_object(key.value),
            )
            self._record_succeeded(operation, started=started, attempt_count=tracker.count)
        except ApplicationError as cause:
            self._record_failed(operation, cause, started=started, attempt_count=tracker.count)
            raise

    def open_staging_stream(
        self, staging_key: MultipartStagingKey
    ) -> AbstractAsyncContextManager[AsyncIterable[bytes]]:
        """Full-read exactly one staging object as a bounded byte stream.

        The verification spool of spec 6.3.4 consumes this stream: the fetch
        of exactly the validated private staging key runs under the shared
        bounded retry policy, the response body must carry the closed async
        streaming shape, and every mid-stream body failure crosses the
        boundary as the same typed multipart error — no raw provider
        exception ever escapes the context manager. The body is closed
        exactly once when the context exits. No canonical key can reach the
        underlying ``get_object`` call.
        """

        key = self._require_staging_key(staging_key)
        operation = MultipartStagingOperation.READ_STAGING_OBJECT
        started = self._monotonic()
        tracker = _AttemptTracker()

        @asynccontextmanager
        async def _stream() -> AsyncIterator[AsyncIterable[bytes]]:
            try:
                response = await self._run_with_retry(
                    operation,
                    tracker,
                    lambda _attempt: self._client.get_object(
                        Bucket=self._bucket,
                        Key=key.value,
                    ),
                )
            except ApplicationError as cause:
                self._record_failed(
                    operation, cause, started=started, attempt_count=tracker.count
                )
                raise
            body = self._streaming_body(response)
            self._record_succeeded(operation, started=started, attempt_count=tracker.count)
            try:
                yield _TypedStagingBodyIterator(
                    body,
                    on_failure=lambda error: self._record_failed(
                        operation, error, started=started, attempt_count=tracker.count
                    ),
                )
            finally:
                await _close_staging_body(body)

        return _stream()

    # --- SDK calls (the only multipart keyword surface) ---------------------

    async def _abort_exact_upload(self, key: str, upload_id: str) -> None:
        try:
            await self._client.abort_multipart_upload(
                Bucket=self._bucket,
                Key=key,
                UploadId=upload_id,
            )
        except ClientError as cause:
            if client_error_code(cause) in UPLOAD_ABSENT_ERROR_CODES:
                # Retrying a successful abort or finding the exact upload
                # already absent is successful cleanup (spec 6.4).
                return
            raise

    async def _remove_staging_object(self, key: str) -> None:
        try:
            await self._client.delete_object(Bucket=self._bucket, Key=key)
        except ClientError as cause:
            if client_error_code(cause) in OBJECT_MISSING_ERROR_CODES:
                # The exact staging object is already absent: success (spec 6.4).
                return
            raise

    async def _fetch_part_pages(self, key: str, upload_id: str) -> tuple[Any, ...]:
        """Fetch the raw ListParts pages of exactly this upload, bounded.

        Lenient pagination inside the retry loop (only truthiness of the
        truncation flag drives the next marker); every shape is validated
        strictly by :func:`_parse_part_pages` after the retry finishes, so a
        malformed response is the typed provider-state error rather than retry
        input.
        """

        pages: list[Any] = []
        marker: int | None = None
        for _ in range(_MAXIMUM_LIST_PARTS_PAGES):
            kwargs: dict[str, Any] = {
                "Bucket": self._bucket,
                "Key": key,
                "UploadId": upload_id,
            }
            if marker is not None:
                kwargs["PartNumberMarker"] = marker
            response = await self._client.list_parts(**kwargs)
            pages.append(response)
            if not isinstance(response, dict) or response.get("IsTruncated") is not True:
                return tuple(pages)
            next_marker = response.get("NextPartNumberMarker")
            if not isinstance(next_marker, int) or isinstance(next_marker, bool):
                return tuple(pages)
            if next_marker <= (marker if marker is not None else 0):
                return tuple(pages)
            marker = next_marker
        return tuple(pages)

    # --- Validated value translation ------------------------------------------

    def _require_staging_key(self, staging_key: MultipartStagingKey) -> MultipartStagingKey:
        """Re-validate the private staging key before any SDK call.

        A value that escaped :meth:`MultipartStagingKey.parse` — including any
        canonical-key-shaped text — fails closed here as the typed
        provider-state error, before a single provider byte moves.
        """

        try:
            return MultipartStagingKey.parse(staging_key.value)
        except (ValueError, AttributeError) as cause:
            raise MultipartUploadError(ErrorCode.MULTIPART_PROVIDER_STATE_INVALID) from cause

    def _require_upload_id(self, upload_id: MultipartProviderUploadId) -> MultipartProviderUploadId:
        """Re-validate the private provider upload ID before any SDK call."""

        try:
            return MultipartProviderUploadId(upload_id.value)
        except (ValueError, AttributeError) as cause:
            raise MultipartUploadError(ErrorCode.MULTIPART_PROVIDER_STATE_INVALID) from cause

    def _require_observed_parts(
        self, parts: Sequence[MultipartProviderPart]
    ) -> tuple[MultipartProviderPart, ...]:
        """Re-validate every observed part fact before the complete call."""

        if not parts:
            raise MultipartUploadError(ErrorCode.MULTIPART_PROVIDER_STATE_INVALID)
        try:
            return tuple(
                MultipartProviderPart(
                    part_number=part.part_number,
                    etag=MultipartProviderPartETag(part.etag.value),
                    size_bytes=part.size_bytes,
                )
                for part in parts
            )
        except (ValueError, AttributeError) as cause:
            raise MultipartUploadError(ErrorCode.MULTIPART_PROVIDER_STATE_INVALID) from cause

    def _provider_upload_id(self, response: object) -> MultipartProviderUploadId:
        """Translate one create response into the private upload-ID value."""

        upload_id = response.get("UploadId") if isinstance(response, dict) else None
        if not isinstance(upload_id, str):
            raise MultipartUploadError(ErrorCode.MULTIPART_PROVIDER_STATE_INVALID)
        try:
            return MultipartProviderUploadId(upload_id)
        except ValueError as cause:
            raise MultipartUploadError(ErrorCode.MULTIPART_PROVIDER_STATE_INVALID) from cause

    def _streaming_body(self, response: object) -> Any:
        """Translate one staging read response into its streaming body.

        The provider answer must be a mapping carrying one async-iterable
        body with an async ``read``; any other shape is the closed
        provider-state error before a single staging byte is consumed.
        """

        body = response.get("Body") if isinstance(response, dict) else None
        if not _is_async_streaming_body(body):
            raise MultipartUploadError(ErrorCode.MULTIPART_PROVIDER_STATE_INVALID)
        return body

    def _part_url(
        self, part_range: MultipartPartRange, url: str, expires_at: datetime
    ) -> MultipartPartUrl:
        """Translate one presigned URL into the session-bound value object."""

        try:
            return MultipartPartUrl(
                part_number=part_range.part_number,
                byte_range=part_range,
                url=url,
                expires_at=expires_at,
            )
        except ValueError as cause:
            raise MultipartUploadError(ErrorCode.MULTIPART_PROVIDER_STATE_INVALID) from cause

    # --- Retry, diagnostics ----------------------------------------------------

    async def _run_with_retry[T](
        self,
        operation: MultipartStagingOperation,
        tracker: _AttemptTracker,
        call: Callable[[int], Awaitable[T]],
    ) -> T:
        """Run ``call`` under the bounded retry policy with the closed mapping.

        Raw provider failures are classified by the shared classifier and
        mapped through :func:`map_multipart_failure`; only raw provider
        exceptions ever enter the retry loop — shape validation happens
        outside it so a typed error is never reclassified.
        """

        async def wrapped(attempt: int) -> T:
            if attempt > tracker.count:
                tracker.count = attempt
            return await call(attempt)

        return await self._retry.run(
            wrapped,
            monotonic=self._monotonic,
            sleep=self._sleep,
            jitter=self._jitter,
            map_failure=map_multipart_failure,
        )

    def _duration_ms(self, started: float) -> int:
        return max(0, int((self._monotonic() - started) * 1000))

    def _record_succeeded(
        self,
        operation: MultipartStagingOperation,
        *,
        started: float,
        attempt_count: int,
        size_bytes: int = 0,
    ) -> None:
        duration_ms = self._duration_ms(started)
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

    def _record_failed(
        self,
        operation: MultipartStagingOperation,
        error: ApplicationError,
        *,
        started: float,
        attempt_count: int,
        size_bytes: int = 0,
    ) -> None:
        duration_ms = self._duration_ms(started)
        self._logger.emit(
            EventName.OBJECT_STORAGE_OPERATION_FAILED,
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


class _TypedStagingBodyIterator:
    """One staging body's async iteration with the closed typed mapping.

    The provider's streaming body is consumed exactly once, forward-only;
    every non-termination failure of ``__anext__`` crosses as the typed
    multipart error of the shared classifier (chained cause only), and the
    first such failure records one closed failed-operation event through the
    injected callback. The body itself is closed by the context manager that
    yielded this iterator, never here.
    """

    def __init__(
        self,
        body: Any,
        *,
        on_failure: Callable[[ApplicationError], None],
    ) -> None:
        self._iterator: AsyncIterator[bytes] = body.__aiter__()
        self._on_failure = on_failure
        self._failure_recorded = False

    def __aiter__(self) -> _TypedStagingBodyIterator:
        return self

    async def __anext__(self) -> bytes:
        try:
            return await self._iterator.__anext__()
        except StopAsyncIteration:
            raise
        except Exception as cause:
            error = map_multipart_failure(cause)
            if not self._failure_recorded:
                self._failure_recorded = True
                self._on_failure(error)
            raise error from cause


def _is_async_streaming_body(body: object) -> bool:
    """Report whether one provider answer carries the async streaming shape.

    Probing uses plain attribute access (never a dynamic capability name):
    the body must expose a callable async ``read`` and an async iterator.
    """

    try:
        read_member = body.read  # type: ignore[attr-defined]
        aiter_member = body.__aiter__  # type: ignore[attr-defined]
    except AttributeError:
        return False
    return callable(read_member) and callable(aiter_member)


async def _close_staging_body(body: Any) -> None:
    """Best-effort close of one staging read body; never masks a result."""

    try:
        aclose = body.aclose
    except AttributeError:
        return
    try:
        outcome = aclose()
        if inspect.isawaitable(outcome):
            await outcome
    except Exception:
        return


def _parse_part_pages(pages: Sequence[Any]) -> tuple[MultipartProviderPart, ...]:
    """Strictly validate the fetched raw pages into provider part facts.

    The last fetched page must report a completed listing (``IsTruncated`` is
    false): a listing still truncated after the bounded page count is a
    malformed provider answer, as is any non-dict page, non-boolean truncation
    flag or malformed part entry.
    """

    if not pages:
        raise _MalformedProviderState()
    parts: list[MultipartProviderPart] = []
    for index, response in enumerate(pages):
        if not isinstance(response, dict):
            raise _MalformedProviderState()
        truncated = response.get("IsTruncated", False)
        if not isinstance(truncated, bool):
            raise _MalformedProviderState()
        if index == len(pages) - 1 and truncated:
            raise _MalformedProviderState()
        parts.extend(_parse_provider_parts(response))
    return tuple(parts)


def _parse_provider_parts(response: dict[Any, Any]) -> list[MultipartProviderPart]:
    """Parse one ListParts page into validated part facts, or fail closed."""

    raw_parts = response.get("Parts", [])
    if not isinstance(raw_parts, list):
        raise _MalformedProviderState()
    parsed: list[MultipartProviderPart] = []
    for raw_part in raw_parts:
        if not isinstance(raw_part, dict):
            raise _MalformedProviderState()
        part_number = raw_part.get("PartNumber")
        if isinstance(part_number, bool) or not isinstance(part_number, int):
            raise _MalformedProviderState()
        size_bytes = raw_part.get("Size")
        if isinstance(size_bytes, bool) or not isinstance(size_bytes, int):
            raise _MalformedProviderState()
        etag = raw_part.get("ETag")
        if not isinstance(etag, str):
            raise _MalformedProviderState()
        try:
            parsed.append(
                MultipartProviderPart(
                    part_number=part_number,
                    etag=MultipartProviderPartETag(etag),
                    size_bytes=size_bytes,
                )
            )
        except ValueError as cause:
            raise _MalformedProviderState() from cause
    return parsed


def _now_monotonic() -> float:
    return time.monotonic()


async def _default_sleep(delay: float) -> None:
    await asyncio.sleep(delay)


def _default_jitter(low: float, high: float) -> float:
    return random.uniform(low, high)
