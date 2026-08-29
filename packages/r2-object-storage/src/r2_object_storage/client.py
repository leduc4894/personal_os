"""Narrow aiobotocore S3 client wrapper, bounded client manager and request types.

The adapter talks to Cloudflare R2 through this narrow typed surface only.
:class:`AiobotocoreS3Client` keeps every raw SDK keyword argument inside itself;
the adapter and the scripted test fake depend solely on
:class:`S3ClientProtocol`, the frozen request/result values and the
:class:`R2ClientManager` lifecycle owner. None of these provider-package values
are exported from ``personal_os``.

The client is constructed lazily with the bounded configuration the design pins:
SigV4, region ``auto``, TLS verification, four connections, five-second connect
timeout, sixty-second read timeout and SDK retries disabled, with explicit
access/secret values and no ambient AWS credential discovery. The adapter owns
retry so attempt count, the deadline and error mapping stay deterministic.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import IO, TYPE_CHECKING, Final, Protocol

from aiobotocore.config import AioConfig
from aiobotocore.session import AioSession, get_session
from botocore.exceptions import ClientError

from personal_os.error_contracts.codes import ErrorCode
from personal_os.object_storage.errors import ObjectStorageError
from personal_os.object_storage.keys import CanonicalMediaType, CanonicalObjectKey
from r2_object_storage.error_mapping import OBJECT_MISSING_ERROR_CODES
from r2_object_storage.settings import LoadedR2Credentials, ObjectStorageSettings

if TYPE_CHECKING:
    from types_aiobotocore_s3 import S3Client

    from r2_object_storage.multipart import MultipartStagingSdkClient

#: Region Cloudflare R2 accepts for the SigV4-signed S3 API.
_R2_REGION: Final[str] = "auto"
#: Bounded connection pool: one slot per process-wide in-flight operation.
_MAXIMUM_POOL_CONNECTIONS: Final[int] = 4
#: Bound on TCP connect establishment before the adapter owns the retry.
_CONNECT_TIMEOUT_SECONDS: Final[float] = 5.0
#: Bound on a single socket read (the per-operation deadline is five minutes).
_READ_TIMEOUT_SECONDS: Final[float] = 60.0
#: The fixed conditional-create guard enforcing immutable canonical keys.
_CONDITIONAL_IF_NONE_MATCH: Final[str] = "*"

#: Error codes meaning "the probed object is absent" on a HEAD. A real bodiless
#: HEAD error is synthesized by botocore's ``RestXMLParser`` with
#: ``Code == str(status_code)`` — i.e. ``"404"`` for a miss — so the status-only
#: token is recognized alongside the named S3 absence codes for robustness.
_HEAD_OBJECT_ABSENT_CODES: Final[frozenset[str]] = OBJECT_MISSING_ERROR_CODES | frozenset({"404"})

#: Error codes meaning "the probed bucket is absent" on a HEAD. A bodiless
#: ``head_bucket`` miss likewise arrives as ``"404"`` rather than the named
#: ``NoSuchBucket`` code carried by bodied responses.
_HEAD_BUCKET_ABSENT_CODES: Final[frozenset[str]] = frozenset({"404", "NoSuchBucket"})


class StreamingBodyProtocol(Protocol):
    """Async-readable R2 object body.

    The adapter only reads a verified object body once into a fresh spool; this
    protocol captures the async surface it depends on (chunked read and async
    iteration). It carries no bucket, key, endpoint or provider header.
    """

    async def read(self, amt: int | None = None) -> bytes: ...
    def __aiter__(self) -> AsyncIterator[bytes]: ...
    async def __anext__(self) -> bytes: ...


@dataclass(frozen=True, slots=True)
class PutObjectRequest:
    """Conditional single-part upload of an already-spooled canonical object.

    ``spool_path`` is the adapter-local exclusive file holding the verified
    bytes; ``content_md5_base64`` is the S3 ``Content-MD5`` transport guard,
    not the SHA-256 content identity. ``if_none_match`` is fixed to ``"*"`` so a
    PUT can only create, never overwrite, an immutable canonical key.
    """

    object_key: CanonicalObjectKey
    spool_path: Path
    size_bytes: int
    media_type: CanonicalMediaType
    content_md5_base64: str
    if_none_match: str = _CONDITIONAL_IF_NONE_MATCH

    @property
    def content_md5(self) -> str:
        """Base64-encoded binary MD5 digest (the S3 ``Content-MD5`` transport guard)."""

        return self.content_md5_base64


@dataclass(frozen=True, slots=True)
class HeadObjectResult:
    """Exact size, media type and ETag returned by a HEAD object probe."""

    size_bytes: int
    media_type: str
    etag: str


@dataclass(frozen=True, slots=True)
class GetObjectResult:
    """A streamed object body; carries no metadata, key or provider header."""

    body: StreamingBodyProtocol


class S3ClientProtocol(Protocol):
    """The narrow R2 surface the adapter and scripted test fake depend on."""

    async def head_object(self, object_key: CanonicalObjectKey) -> HeadObjectResult | None: ...
    async def put_object(self, request: PutObjectRequest) -> None: ...
    async def get_object(
        self, object_key: CanonicalObjectKey, *, if_match: str
    ) -> GetObjectResult: ...
    async def head_bucket(self) -> None: ...
    async def close(self) -> None: ...


class _S3ClientContext(Protocol):
    """Async context manager that enters a complete SDK S3 client."""

    async def __aenter__(self) -> S3Client: ...
    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None: ...


class AioSessionFactory(Protocol):
    """Structural factory ``R2ClientManager`` uses to create the SDK client.

    The real :class:`AioSession.create_client` is overloaded on a literal
    service name; this protocol captures only the bounded keyword surface the
    manager calls, so a recording session fake can satisfy it deterministically.
    """

    def create_client(
        self,
        service_name: str,
        *,
        region_name: str,
        endpoint_url: str,
        aws_access_key_id: str,
        aws_secret_access_key: str,
        config: AioConfig,
    ) -> _S3ClientContext: ...


class _DefaultAioSessionFactory:
    """Adapter exposing the bounded ``create_client`` surface over a real session.

    Holds one :class:`AioSession` and re-publishes the exact keyword surface the
    manager uses, resolving the typed S3 overload with the literal service name.
    """

    def __init__(self, session: AioSession) -> None:
        self._session = session

    def create_client(
        self,
        service_name: str,
        *,
        region_name: str,
        endpoint_url: str,
        aws_access_key_id: str,
        aws_secret_access_key: str,
        config: AioConfig,
    ) -> _S3ClientContext:
        if service_name != "s3":
            raise ValueError("only the s3 service client is supported")  # pragma: no cover
        context = self._session.create_client(
            "s3",
            region_name=region_name,
            endpoint_url=endpoint_url,
            aws_access_key_id=aws_access_key_id,
            aws_secret_access_key=aws_secret_access_key,
            config=config,
        )
        return context


def _build_config() -> AioConfig:
    """Construct the bounded R2 SDK config: SigV4, region auto, retries off."""

    return AioConfig(
        region_name=_R2_REGION,
        signature_version="s3v4",
        max_pool_connections=_MAXIMUM_POOL_CONNECTIONS,
        connect_timeout=_CONNECT_TIMEOUT_SECONDS,
        read_timeout=_READ_TIMEOUT_SECONDS,
        retries={"total_max_attempts": 1, "mode": "standard"},
    )


class AiobotocoreS3Client:
    """Concrete :class:`S3ClientProtocol` over a real aiobotocore S3 client.

    All raw SDK keyword arguments stay inside this class. ``head_object`` maps
    only an object-level absence to ``None``: the status-only ``"404"`` token a
    real bodiless HEAD miss carries (synthesized by botocore) plus the named
    absence codes. ``head_bucket`` maps its own absence (``"404"`` or
    ``NoSuchBucket``) to terminal ``object_storage_unavailable`` instead of
    ``None``, so a missing bucket is never hidden. Every other failure propagates
    for the retry policy to classify. The wrapper owns the SDK client context;
    :meth:`close` runs it down exactly once.
    """

    def __init__(
        self,
        client: S3Client,
        *,
        bucket: str,
        context: _S3ClientContext,
    ) -> None:
        self._client = client
        self._bucket = bucket
        self._context = context
        self._closed = False

    async def head_object(self, object_key: CanonicalObjectKey) -> HeadObjectResult | None:
        try:
            response = await self._client.head_object(Bucket=self._bucket, Key=str(object_key))
        except ClientError as cause:
            if _error_code(cause) in _HEAD_OBJECT_ABSENT_CODES:
                return None
            raise
        return HeadObjectResult(
            size_bytes=response["ContentLength"],
            media_type=response["ContentType"],
            etag=response["ETag"],
        )

    async def put_object(self, request: PutObjectRequest) -> None:
        body = await asyncio.to_thread(self._open_spool, request.spool_path)
        try:
            await self._client.put_object(
                Bucket=self._bucket,
                Key=str(request.object_key),
                Body=body,
                ContentLength=request.size_bytes,
                ContentMD5=request.content_md5_base64,
                ContentType=str(request.media_type),
                IfNoneMatch=request.if_none_match,
            )
        finally:
            await asyncio.to_thread(body.close)

    async def get_object(self, object_key: CanonicalObjectKey, *, if_match: str) -> GetObjectResult:
        response = await self._client.get_object(
            Bucket=self._bucket, Key=str(object_key), IfMatch=if_match
        )
        return GetObjectResult(body=response["Body"])

    async def head_bucket(self) -> None:
        try:
            await self._client.head_bucket(Bucket=self._bucket)
        except ClientError as cause:
            if _error_code(cause) in _HEAD_BUCKET_ABSENT_CODES:
                raise ObjectStorageError(ErrorCode.OBJECT_STORAGE_UNAVAILABLE) from cause
            raise

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._context.__aexit__(None, None, None)

    @staticmethod
    def _open_spool(spool_path: Path) -> IO[bytes]:
        """Open the spool file for a single read during PUT."""

        return open(spool_path, "rb")


def _error_code(cause: ClientError) -> str | None:
    """Return the safe ``Error.Code`` token from a ``ClientError``, or ``None``.

    ``response["Error"]["Code"]`` is read safely; a malformed error yields
    ``None`` so the caller treats it as an unknown failure and propagates it for
    classification instead of misreading it as an absence.
    """

    response = cause.response
    if not isinstance(response, dict):
        return None
    error = response.get("Error")
    code = error.get("Code") if isinstance(error, dict) else None
    return code if isinstance(code, str) else None


class R2ClientManager:
    """Lazy, concurrency-safe owner of one bounded R2 S3 client per process.

    :meth:`get_client` uses a single lock and publishes only a complete client:
    a cancelled initializer cannot leave a partially-constructed client behind.
    :meth:`close` is idempotent. Explicit access/secret values disable any
    ambient AWS credential discovery; region is fixed to ``auto`` and TLS
    verification cannot be disabled.
    """

    def __init__(
        self,
        settings: ObjectStorageSettings,
        credentials: LoadedR2Credentials,
        *,
        session: AioSessionFactory | None = None,
    ) -> None:
        self._settings = settings
        self._credentials = credentials
        self._session: AioSessionFactory = (
            session if session is not None else _DefaultAioSessionFactory(get_session())
        )
        self._lock = asyncio.Lock()
        self._client: AiobotocoreS3Client | None = None
        self._raw_client: S3Client | None = None

    async def get_client(self) -> S3ClientProtocol:
        async with self._lock:
            if self._client is not None:
                return self._client
            context = self._session.create_client(
                "s3",
                region_name=_R2_REGION,
                endpoint_url=self._settings.r2_endpoint,
                aws_access_key_id=self._credentials.access_key_id.get_secret_value(),
                aws_secret_access_key=self._credentials.secret_access_key.get_secret_value(),
                config=_build_config(),
            )
            raw_client = await context.__aenter__()
            self._raw_client = raw_client
            client = AiobotocoreS3Client(
                raw_client, bucket=self._settings.r2_bucket_name, context=context
            )
            self._client = client
            return client

    async def get_multipart_staging_client(self) -> MultipartStagingSdkClient:
        """Return the process-wide SDK client typed for multipart staging.

        The staging provider keeps its own complete SDK keyword mapping (all
        multipart keywords live in ``multipart.py``), so the manager shares the
        one bounded client — a single connection pool and lifecycle — typed by
        the narrow multipart protocol instead of wrapping it a second time.
        """

        if self._raw_client is None:
            await self.get_client()
        async with self._lock:
            raw_client = self._raw_client
        assert raw_client is not None, "get_client must publish the raw SDK client"
        return raw_client

    async def close(self) -> None:
        async with self._lock:
            client = self._client
            self._client = None
            self._raw_client = None
        if client is not None:
            await client.close()
