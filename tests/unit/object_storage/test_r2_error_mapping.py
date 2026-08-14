"""Bounded R2 client configuration, retry classification and the metrics sink.

These tests prove the provider R2 boundary: the lazily-created SDK client is
constructed with the exact bounded configuration (SigV4, region ``auto``, pool
4, connect 5, read 60, SDK retries disabled, explicit credentials and no ambient
discovery), the closed retry decision matrix maps every documented S3/R2 failure
to ``RETRY``/``CONDITIONAL_CONFLICT``/``TERMINAL``, the bounded retry loop honours
its deadline and backoff, and the in-memory metrics sink records only
low-cardinality operation/result/error/bytes/duration/retry/in-flight/reserved
values. Provider messages, request ids, endpoints, keys and digests never enter a
mapped error, a metric label or a recorded snapshot.
"""

from __future__ import annotations

import asyncio
import io
from collections.abc import Callable
from dataclasses import FrozenInstanceError, dataclass
from pathlib import Path
from typing import Any, cast

import pytest
from aiobotocore.config import AioConfig
from botocore.exceptions import (
    ClientError,
    ConnectionClosedError,
    EndpointConnectionError,
    ReadTimeoutError,
    ResponseStreamingError,
)
from pydantic import SecretStr

from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import InternalApplicationError
from personal_os.object_storage.errors import ObjectStorageError
from personal_os.object_storage.keys import (
    CanonicalMediaType,
    CanonicalObjectKey,
    ContentDigest,
)
from r2_object_storage.client import (
    AiobotocoreS3Client,
    HeadObjectResult,
    PutObjectRequest,
    R2ClientManager,
    S3ClientProtocol,
)
from r2_object_storage.error_mapping import (
    ConditionalCreateConflict,
    RetryDecision,
    RetryPolicy,
    classify_r2_failure,
    map_r2_failure,
)
from r2_object_storage.metrics import (
    InMemoryObjectStorageMetrics,
    ObjectStorageMetrics,
    ObjectStorageOperation,
    ObjectStorageResult,
)
from r2_object_storage.settings import LoadedR2Credentials, ObjectStorageSettings

# Stable sentinels that must never appear in a mapped error, a metric label or a
# recorded snapshot. They stand in for real provider values (messages, request
# ids, endpoints, keys) so a leakage is observable as a string match.
_PROVIDER_MESSAGE_SENTINEL = "LEAK-PROVIDER-MESSAGE-9f3a"
_PROVIDER_REQUEST_ID_SENTINEL = "LEAK-REQUEST-ID-7c2e"
_PROVIDER_ENDPOINT_SENTINEL = "https://leak.example.r2.cloudflarestorage.com"

_VALID_ACCOUNT_ID = "abcdef0123456789abcdef0123456789"
_VALID_ENDPOINT = f"https://{_VALID_ACCOUNT_ID}.r2.cloudflarestorage.com"
_VALID_BUCKET = "knowledge-test"
_ACCESS_KEY_ID = "test-access-key-id"
_SECRET_ACCESS_KEY = "test-secret-access-key"


# --- Client configuration fakes and helpers ---------------------------------


@dataclass
class _FakeClientContext:
    """Minimal async context manager publishing a stub SDK client.

    It records nothing; the recording session records the ``create_client`` call.
    """

    closed = False

    async def __aenter__(self) -> Any:
        return cast("Any", object())

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        self.closed = True
        return None


class _RecordingSdkClient:
    """Stub SDK S3 client recording method calls, kwargs and scripted raises.

    This stands in for the real aiobotocore client so the narrow wrapper's own
    kwargs mapping and error mapping can be tested offline.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.responses: dict[str, Any] = {}
        self.raises: dict[str, BaseException] = {}

    def on(self, method: str, *, response: Any = None, cause: BaseException | None = None) -> None:
        if cause is not None:
            self.raises[method] = cause
        else:
            self.responses[method] = response

    async def _call(self, method: str, kwargs: dict[str, Any]) -> Any:
        self.calls.append((method, dict(kwargs)))
        if method in self.raises:
            raise self.raises[method]
        return self.responses.get(method)

    async def head_object(self, **kwargs: Any) -> Any:
        return await self._call("head_object", kwargs)

    async def put_object(self, **kwargs: Any) -> Any:
        return await self._call("put_object", kwargs)

    async def get_object(self, **kwargs: Any) -> Any:
        return await self._call("get_object", kwargs)

    async def head_bucket(self, **kwargs: Any) -> Any:
        return await self._call("head_bucket", kwargs)


def _build_wrapper(sdk_client: _RecordingSdkClient) -> AiobotocoreS3Client:
    context = _FakeClientContext()
    return AiobotocoreS3Client(
        cast("Any", sdk_client), bucket=_VALID_BUCKET, context=cast("Any", context)
    )


class RecordingAioSession:
    """Records the single ``create_client`` call made by ``R2ClientManager``."""

    def __init__(self) -> None:
        self.create_client_calls: list[dict[str, Any]] = []

    def create_client(self, service_name: str, **kwargs: Any) -> _FakeClientContext:
        self.create_client_calls.append({"service_name": service_name, **kwargs})
        return _FakeClientContext()

    @property
    def only_create_client_call(self) -> dict[str, Any]:
        assert len(self.create_client_calls) == 1, "expected exactly one create_client call"
        return self.create_client_calls[0]


def _build_settings(spool_root: Path) -> ObjectStorageSettings:
    return ObjectStorageSettings(
        environment="local",
        secret_root=spool_root / "secrets",
        r2_endpoint=_VALID_ENDPOINT,
        r2_bucket_name=_VALID_BUCKET,
        r2_access_key_id_file="r2_access_key_id",
        r2_secret_access_key_file="r2_secret_access_key",
        object_storage_spool_root=spool_root,
    )


def _build_credentials() -> LoadedR2Credentials:
    return LoadedR2Credentials(
        access_key_id=SecretStr(_ACCESS_KEY_ID),
        secret_access_key=SecretStr(_SECRET_ACCESS_KEY),
    )


def build_client_manager(spool_root: Path, *, session: RecordingAioSession) -> R2ClientManager:
    return R2ClientManager(
        settings=_build_settings(spool_root),
        credentials=_build_credentials(),
        session=session,
    )


# --- S3/R2 failure fixtures --------------------------------------------------


def _client_error(
    code: str,
    status: int,
    *,
    operation: str = "HeadObject",
    message: str = _PROVIDER_MESSAGE_SENTINEL,
) -> ClientError:
    return ClientError(
        {
            "Error": {"Code": code, "Message": message},
            "ResponseMetadata": {
                "HTTPStatusCode": status,
                "RequestId": _PROVIDER_REQUEST_ID_SENTINEL,
            },
        },
        operation,
    )


def _malformed_client_error() -> ClientError:
    # A response missing both the Error code and the HTTP status code: nothing
    # safe can be derived, so the classifier treats it as a malformed response.
    return ClientError({"Error": {}, "ResponseMetadata": {}}, "HeadObject")


def _connection_closed() -> ConnectionClosedError:
    return ConnectionClosedError(endpoint_url=_PROVIDER_ENDPOINT_SENTINEL)


def _endpoint_unreachable() -> EndpointConnectionError:
    return EndpointConnectionError(endpoint_url=_PROVIDER_ENDPOINT_SENTINEL)


def _read_timeout() -> ReadTimeoutError:
    return ReadTimeoutError(endpoint_url=_PROVIDER_ENDPOINT_SENTINEL, proxy_url=None)


# --- Step 1a: the bounded client configuration -------------------------------


@pytest.mark.asyncio
async def test_client_configuration_is_bounded(tmp_path: Path) -> None:
    session = RecordingAioSession()
    manager = build_client_manager(tmp_path, session=session)
    await manager.get_client()
    call = session.only_create_client_call

    assert call["service_name"] == "s3"
    assert call["region_name"] == "auto"
    assert call["endpoint_url"] == _VALID_ENDPOINT
    assert call["aws_access_key_id"] == _ACCESS_KEY_ID
    assert call["aws_secret_access_key"] == _SECRET_ACCESS_KEY

    config = cast(AioConfig, call["config"])
    assert config.max_pool_connections == 4
    assert config.connect_timeout == 5
    assert config.read_timeout == 60
    assert config.retries["total_max_attempts"] == 1
    assert config.retries["mode"] == "standard"
    assert config.region_name == "auto"
    assert config.signature_version == "s3v4"


@pytest.mark.asyncio
async def test_client_manager_reuses_one_client_and_closes_idempotently(
    tmp_path: Path,
) -> None:
    session = RecordingAioSession()
    manager = build_client_manager(tmp_path, session=session)

    first = await manager.get_client()
    second = await manager.get_client()
    assert first is second
    assert len(session.create_client_calls) == 1

    # close() is idempotent and does not raise on repeat calls.
    await manager.close()
    await manager.close()
    assert len(session.create_client_calls) == 1


@pytest.mark.asyncio
async def test_concurrent_get_client_publishes_one_client(tmp_path: Path) -> None:
    session = RecordingAioSession()
    manager = build_client_manager(tmp_path, session=session)

    clients = await asyncio.gather(manager.get_client(), manager.get_client(), manager.get_client())
    assert clients[0] is clients[1] is clients[2]
    assert len(session.create_client_calls) == 1


# --- Step 1a': the narrow wrapper keeps raw SDK kwargs inside itself ---------


@pytest.mark.asyncio
async def test_head_object_returns_exact_size_media_and_etag() -> None:
    sdk_client = _RecordingSdkClient()
    sdk_client.on(
        "head_object",
        response={"ContentLength": 512, "ContentType": "application/json", "ETag": "etag-1"},
    )
    wrapper = _build_wrapper(sdk_client)

    result = await wrapper.head_object(_key())

    assert result == HeadObjectResult(size_bytes=512, media_type="application/json", etag="etag-1")
    method, kwargs = sdk_client.calls[0]
    assert method == "head_object"
    assert kwargs["Bucket"] == _VALID_BUCKET
    assert kwargs["Key"] == str(_key())


@pytest.mark.asyncio
# "404" is the real botocore shape: a bodiless HEAD miss is synthesized by
# RestXMLParser._parse_error_from_http_status with Code == str(status_code).
@pytest.mark.parametrize("absence_code", ["NoSuchKey", "NotFound", "404"])
async def test_head_object_maps_object_absence_to_none(absence_code: str) -> None:
    sdk_client = _RecordingSdkClient()
    sdk_client.on("head_object", cause=_client_error(absence_code, 404))
    wrapper = _build_wrapper(sdk_client)

    assert await wrapper.head_object(_key()) is None


@pytest.mark.asyncio
async def test_head_object_never_hides_missing_bucket() -> None:
    sdk_client = _RecordingSdkClient()
    sdk_client.on("head_object", cause=_client_error("NoSuchBucket", 404))
    wrapper = _build_wrapper(sdk_client)

    with pytest.raises(ClientError):
        await wrapper.head_object(_key())


@pytest.mark.asyncio
async def test_put_object_sends_conditional_create_kwargs(tmp_path: Path) -> None:
    sdk_client = _RecordingSdkClient()
    sdk_client.on("put_object", response={"ETag": "etag-2"})
    wrapper = _build_wrapper(sdk_client)

    spool_path = tmp_path / "cas-spool-test.part"
    spool_path.write_bytes(b"canonical bytes")

    await wrapper.put_object(
        PutObjectRequest(
            object_key=_key(),
            spool_path=spool_path,
            size_bytes=15,
            media_type=CanonicalMediaType.parse("application/json"),
            content_md5_base64="AAAABBBBBBBBBBBBBBBBBBBB==",
        )
    )

    method, kwargs = sdk_client.calls[0]
    assert method == "put_object"
    assert kwargs["Bucket"] == _VALID_BUCKET
    assert kwargs["Key"] == str(_key())
    assert kwargs["IfNoneMatch"] == "*"
    assert kwargs["ContentLength"] == 15
    assert kwargs["ContentMD5"] == "AAAABBBBBBBBBBBBBBBBBBBB=="
    assert kwargs["ContentType"] == "application/json"
    # The spool file is opened as a binary body and closed after the call.
    body = kwargs["Body"]
    assert isinstance(body, io.IOBase)
    assert body.closed


@pytest.mark.asyncio
async def test_get_object_passes_if_match_and_returns_only_body() -> None:
    sdk_client = _RecordingSdkClient()
    sdk_client.on("get_object", response={"Body": b"raw-stream"})
    wrapper = _build_wrapper(sdk_client)

    result = await wrapper.get_object(_key(), if_match="etag-1")

    method, kwargs = sdk_client.calls[0]
    assert method == "get_object"
    assert kwargs["Bucket"] == _VALID_BUCKET
    assert kwargs["Key"] == str(_key())
    assert kwargs["IfMatch"] == "etag-1"
    # GetObjectResult carries only the streaming body.
    assert result.body is not None
    assert not hasattr(result, "etag")
    assert not hasattr(result, "size_bytes")


@pytest.mark.asyncio
async def test_head_bucket_is_a_bare_bucket_probe() -> None:
    sdk_client = _RecordingSdkClient()
    sdk_client.on("head_bucket", response={})
    wrapper = _build_wrapper(sdk_client)

    await wrapper.head_bucket()

    method, kwargs = sdk_client.calls[0]
    assert method == "head_bucket"
    assert kwargs == {"Bucket": _VALID_BUCKET}


@pytest.mark.asyncio
@pytest.mark.parametrize("absence_code", ["404", "NoSuchBucket"])
async def test_head_bucket_absence_maps_to_unavailable(absence_code: str) -> None:
    # A bodiless head_bucket miss arrives as Code == "404" (the botocore
    # status-only synthesis); the named NoSuchBucket code is kept as
    # defense-in-depth. Both map to terminal object_storage_unavailable,
    # never to None.
    sdk_client = _RecordingSdkClient()
    sdk_client.on("head_bucket", cause=_client_error(absence_code, 404))
    wrapper = _build_wrapper(sdk_client)

    with pytest.raises(ObjectStorageError) as info:
        await wrapper.head_bucket()

    assert info.value.error_code is ErrorCode.OBJECT_STORAGE_UNAVAILABLE
    assert info.value.is_retryable is True


@pytest.mark.asyncio
async def test_head_bucket_other_failures_propagate_unmapped() -> None:
    sdk_client = _RecordingSdkClient()
    sdk_client.on("head_bucket", cause=_client_error("AccessDenied", 403))
    wrapper = _build_wrapper(sdk_client)

    with pytest.raises(ClientError):
        await wrapper.head_bucket()


# --- Step 1b: the closed retry decision matrix ------------------------------


@pytest.mark.parametrize(
    ("cause_factory", "expected"),
    [
        # Transient transport/availability conditions -> RETRY.
        (lambda: _connection_closed(), RetryDecision.RETRY),
        (lambda: _endpoint_unreachable(), RetryDecision.RETRY),
        (lambda: _read_timeout(), RetryDecision.RETRY),
        (lambda: ResponseStreamingError(error="reset"), RetryDecision.RETRY),
        (lambda: _client_error("RequestTimeout", 408), RetryDecision.RETRY),
        (lambda: _client_error("TooManyRequests", 429), RetryDecision.RETRY),
        (lambda: _client_error("InternalError", 500), RetryDecision.RETRY),
        (lambda: _client_error("BadGateway", 502), RetryDecision.RETRY),
        (lambda: _client_error("ServiceUnavailable", 503), RetryDecision.RETRY),
        (lambda: _client_error("GatewayTimeout", 504), RetryDecision.RETRY),
        (lambda: _client_error("SlowDown", 503), RetryDecision.RETRY),
        # Design spec §11 pins BadDigest and InvalidDigest as NON-RETRYABLE.
        (lambda: _client_error("BadDigest", 400), RetryDecision.TERMINAL),
        (lambda: _client_error("InvalidDigest", 400), RetryDecision.TERMINAL),
        # Conditional PUT race -> CONDITIONAL_CONFLICT.
        (lambda: _client_error("PreconditionFailed", 412), RetryDecision.CONDITIONAL_CONFLICT),
        # Access denied -> TERMINAL.
        (lambda: _client_error("AccessDenied", 403), RetryDecision.TERMINAL),
        (lambda: _client_error("InvalidAccessKeyId", 403), RetryDecision.TERMINAL),
        (lambda: _client_error("SignatureDoesNotMatch", 403), RetryDecision.TERMINAL),
        (lambda: _client_error("Unauthorized", 401), RetryDecision.TERMINAL),
        # Bucket missing -> TERMINAL (never hidden as an ordinary absence).
        (lambda: _client_error("NoSuchBucket", 404), RetryDecision.TERMINAL),
        # Ordinary object missing -> TERMINAL (the wrapper maps HEAD absence to None).
        (lambda: _client_error("NoSuchKey", 404), RetryDecision.TERMINAL),
        (lambda: _client_error("NotFound", 404), RetryDecision.TERMINAL),
        # Malformed or unsupported responses -> TERMINAL.
        (lambda: _malformed_client_error(), RetryDecision.TERMINAL),
        (lambda: _client_error("InvalidArgument", 400), RetryDecision.TERMINAL),
        (lambda: _client_error("NotImplemented", 501), RetryDecision.TERMINAL),
        # An unknown exception crosses the classifier as a fail-closed TERMINAL.
        (lambda: RuntimeError("unexpected"), RetryDecision.TERMINAL),
    ],
)
def test_retry_decision_matrix(
    cause_factory: Callable[[], BaseException], expected: RetryDecision
) -> None:
    assert classify_r2_failure(cause_factory()) is expected


# --- Step 1c: mapped errors carry no provider values ------------------------


def _leak_sentinels() -> tuple[str, ...]:
    return (
        _PROVIDER_MESSAGE_SENTINEL,
        _PROVIDER_REQUEST_ID_SENTINEL,
        _PROVIDER_ENDPOINT_SENTINEL,
    )


def _assert_no_provider_leak(error: ObjectStorageError) -> None:
    rendered = repr(error)
    serialized = str(error)
    safe = error.to_safe_dict()
    blob = f"{rendered}\n{serialized}\n{safe!r}"
    for sentinel in _leak_sentinels():
        assert sentinel not in blob, f"provider value {sentinel!r} leaked into mapped error"


@pytest.mark.parametrize(
    "cause_factory",
    [
        lambda: _client_error("AccessDenied", 403),
        lambda: _client_error("NoSuchBucket", 404),
        lambda: _client_error("NoSuchKey", 404),
        lambda: _malformed_client_error(),
        lambda: _client_error("InvalidArgument", 400),
    ],
)
def test_mapped_errors_carry_no_provider_values(
    cause_factory: Callable[[], BaseException],
) -> None:
    error = map_r2_failure(cause_factory(), exhausted=False)
    assert isinstance(error, ObjectStorageError)
    _assert_no_provider_leak(error)


def test_exhausted_transient_failure_maps_to_unavailable() -> None:
    error = map_r2_failure(_connection_closed(), exhausted=True)
    assert error.error_code is ErrorCode.OBJECT_STORAGE_UNAVAILABLE
    assert error.is_retryable is True
    _assert_no_provider_leak(error)


def test_mapped_terminal_codes_are_exact() -> None:
    assert (
        map_r2_failure(_client_error("AccessDenied", 403), exhausted=False).error_code
        is ErrorCode.OBJECT_STORAGE_ACCESS_DENIED
    )
    assert (
        map_r2_failure(_client_error("NoSuchBucket", 404), exhausted=False).error_code
        is ErrorCode.OBJECT_STORAGE_UNAVAILABLE
    )
    assert (
        map_r2_failure(_client_error("NoSuchKey", 404), exhausted=False).error_code
        is ErrorCode.OBJECT_STORAGE_OBJECT_MISSING
    )
    assert (
        map_r2_failure(_malformed_client_error(), exhausted=False).error_code
        is ErrorCode.OBJECT_STORAGE_CONTRACT_INVALID
    )
    assert (
        map_r2_failure(_client_error("InvalidArgument", 400), exhausted=False).error_code
        is ErrorCode.OBJECT_STORAGE_CONTRACT_INVALID
    )
    assert (
        map_r2_failure(_client_error("BadDigest", 400), exhausted=False).error_code
        is ErrorCode.OBJECT_STORAGE_CONTRACT_INVALID
    )
    assert (
        map_r2_failure(_client_error("InvalidDigest", 400), exhausted=False).error_code
        is ErrorCode.OBJECT_STORAGE_CONTRACT_INVALID
    )


def test_unknown_exception_maps_to_internal_error_without_leaking_the_cause() -> None:
    """Spec §12: an unknown exception crosses the boundary as internal_error.

    An internal bug (a plain ``RuntimeError`` from a broken client wrapper) is
    never misreported as a provider-integrity failure, and its message never
    enters the mapped error's rendered or safe surface.
    """

    error = map_r2_failure(RuntimeError("internal-bug-sentinel-4d81"), exhausted=False)

    assert isinstance(error, InternalApplicationError)
    assert not isinstance(error, ObjectStorageError)
    assert error.error_code is ErrorCode.INTERNAL_ERROR
    assert error.error_code is not ErrorCode.OBJECT_STORAGE_CONTRACT_INVALID
    assert error.error_code is not ErrorCode.OBJECT_STORAGE_INTEGRITY_FAILED
    assert error.is_retryable is False
    assert error.category == "internal"
    blob = f"{error!r}\n{error}\n{error.to_safe_dict()!r}"
    assert "internal-bug-sentinel-4d81" not in blob


@pytest.mark.asyncio
async def test_retry_policy_maps_unknown_exception_to_internal_error() -> None:
    """The bounded retry loop surfaces an unknown cause as internal_error."""

    async def _operation(_attempt: int) -> str:
        raise RuntimeError("internal-bug-sentinel-4d81")

    async def _sleep(_delay: float) -> None:
        return None

    policy = RetryPolicy(maximum_attempts=3)
    with pytest.raises(InternalApplicationError) as raised:
        await policy.run(_operation, monotonic=lambda: 0.0, sleep=_sleep)

    assert raised.value.error_code is ErrorCode.INTERNAL_ERROR
    assert raised.value.is_retryable is False
    assert "internal-bug-sentinel-4d81" not in repr(raised.value)
    assert "internal-bug-sentinel-4d81" not in str(raised.value)


# --- Step 1d: the bounded retry policy --------------------------------------


def _key() -> CanonicalObjectKey:
    digest = ContentDigest.parse("a" * 64)
    from personal_os.object_storage.keys import derive_canonical_object_key

    return derive_canonical_object_key(digest)


@pytest.mark.asyncio
async def test_retry_policy_succeeds_after_retryable_failures() -> None:
    policy = RetryPolicy(maximum_attempts=3)
    attempts: list[int] = []
    sleep_calls: list[float] = []

    async def operation(attempt: int) -> str:
        attempts.append(attempt)
        if attempt < 3:
            raise _client_error("ServiceUnavailable", 503)
        return "ok"

    def fake_monotonic() -> float:
        return 0.0

    async def fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    def fake_jitter(low: float, high: float) -> float:
        return high  # deterministic: always back off by the full cap.

    result = await policy.run(
        operation,
        monotonic=fake_monotonic,
        sleep=fake_sleep,
        jitter=fake_jitter,
    )
    assert result == "ok"
    assert attempts == [1, 2, 3]
    # Attempt 1 caps at 2**0 = 1.0; attempt 2 caps at 2**1 = 2.0.
    assert sleep_calls == [1.0, 2.0]


@pytest.mark.asyncio
async def test_retry_policy_terminal_failure_raises_immediately() -> None:
    policy = RetryPolicy()

    async def operation(attempt: int) -> str:
        raise _client_error("AccessDenied", 403)

    with pytest.raises(ObjectStorageError) as info:
        await policy.run(operation, monotonic=lambda: 0.0, sleep=_no_sleep, jitter=_zero_jitter)
    assert info.value.error_code is ErrorCode.OBJECT_STORAGE_ACCESS_DENIED


@pytest.mark.asyncio
async def test_retry_policy_exhaustion_maps_to_unavailable() -> None:
    policy = RetryPolicy(maximum_attempts=2)

    async def operation(attempt: int) -> str:
        raise _client_error("ServiceUnavailable", 503)

    with pytest.raises(ObjectStorageError) as info:
        await policy.run(operation, monotonic=lambda: 0.0, sleep=_no_sleep, jitter=_zero_jitter)
    assert info.value.error_code is ErrorCode.OBJECT_STORAGE_UNAVAILABLE


@pytest.mark.asyncio
async def test_retry_policy_deadline_wins_over_attempts() -> None:
    # The clock is fixed at a point already past the deadline.
    times = iter([1000.0, 1400.0])

    def fake_monotonic() -> float:
        return next(times)

    policy = RetryPolicy(maximum_attempts=3, operation_deadline_seconds=300.0)

    async def operation(attempt: int) -> str:
        raise _client_error("ServiceUnavailable", 503)

    with pytest.raises(ObjectStorageError) as info:
        await policy.run(operation, monotonic=fake_monotonic, sleep=_no_sleep, jitter=_zero_jitter)
    assert info.value.error_code is ErrorCode.OBJECT_STORAGE_UNAVAILABLE


@pytest.mark.asyncio
async def test_retry_policy_reraises_cancellation() -> None:
    policy = RetryPolicy()

    async def operation(attempt: int) -> str:
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await policy.run(operation, monotonic=lambda: 0.0, sleep=_no_sleep, jitter=_zero_jitter)


@pytest.mark.asyncio
async def test_retry_policy_conditional_conflict_raises_dedup_signal() -> None:
    policy = RetryPolicy()

    async def operation(attempt: int) -> str:
        raise _client_error("PreconditionFailed", 412)

    with pytest.raises(ConditionalCreateConflict):
        await policy.run(operation, monotonic=lambda: 0.0, sleep=_no_sleep, jitter=_zero_jitter)


async def _no_sleep(_: float) -> None:
    return None


def _zero_jitter(low: float, high: float) -> float:
    return low


# --- Step 1e: the in-memory metrics sink ------------------------------------


def test_in_memory_metrics_records_low_cardinality_values() -> None:
    metrics = InMemoryObjectStorageMetrics()
    assert isinstance(metrics, ObjectStorageMetrics)

    metrics.increment_in_flight(operation=ObjectStorageOperation.STORE)
    metrics.record_reserved_bytes(operation=ObjectStorageOperation.STORE, size_bytes=2048)
    metrics.record_retry(operation=ObjectStorageOperation.STORE)
    metrics.record_operation(
        operation=ObjectStorageOperation.STORE,
        result=ObjectStorageResult.SUCCEEDED,
        duration_ms=42,
        size_bytes=2048,
        attempt_count=2,
    )
    metrics.decrement_in_flight(operation=ObjectStorageOperation.STORE)

    operations = metrics.operations
    assert len(operations) == 1
    record = operations[0]
    assert record.operation is ObjectStorageOperation.STORE
    assert record.result is ObjectStorageResult.SUCCEEDED
    assert record.duration_ms == 42
    assert record.size_bytes == 2048
    assert record.attempt_count == 2
    assert record.error_code is None

    assert metrics.retry_count(operation=ObjectStorageOperation.STORE) == 1
    assert metrics.in_flight_count(operation=ObjectStorageOperation.STORE) == 0
    assert metrics.reserved_bytes(operation=ObjectStorageOperation.STORE) == 2048


def test_in_memory_metrics_records_failure_error_code() -> None:
    metrics = InMemoryObjectStorageMetrics()
    metrics.record_operation(
        operation=ObjectStorageOperation.VERIFY,
        result=ObjectStorageResult.FAILED,
        error_code=ErrorCode.OBJECT_STORAGE_INTEGRITY_FAILED,
        duration_ms=7,
        attempt_count=1,
    )
    record = metrics.operations[0]
    assert record.error_code is ErrorCode.OBJECT_STORAGE_INTEGRITY_FAILED


def test_in_memory_metrics_snapshot_has_no_digest_or_key_labels() -> None:
    metrics = InMemoryObjectStorageMetrics()
    metrics.record_operation(
        operation=ObjectStorageOperation.READ,
        result=ObjectStorageResult.DEDUPLICATED,
        size_bytes=128,
        duration_ms=3,
    )
    snapshot = repr(metrics) + "\n" + repr(metrics.operations)
    assert "objects/sha256" not in snapshot
    assert _PROVIDER_ENDPOINT_SENTINEL not in snapshot
    assert _PROVIDER_REQUEST_ID_SENTINEL not in snapshot


# --- Step 1f: the narrow protocol surface -----------------------------------


def test_request_and_result_types_are_frozen() -> None:
    request = PutObjectRequest(
        object_key=_key(),
        spool_path=Path("/tmp/spool.part"),
        size_bytes=5,
        media_type=CanonicalMediaType.parse("application/json"),
        content_md5_base64="AAAABBBBBBBBBBBBBBBBBBBB==",
    )
    assert request.if_none_match == "*"
    with pytest.raises(FrozenInstanceError):
        request.if_none_match = "weak"  # type: ignore[misc]

    head = HeadObjectResult(size_bytes=5, media_type="application/json", etag="etag-value")
    with pytest.raises(FrozenInstanceError):
        head.etag = "other"  # type: ignore[misc]


def test_s3_client_protocol_is_async() -> None:
    # Structural sanity: the protocol exposes the five async methods the adapter
    # and scripted fake rely on. Static checking is covered by mypy.
    members = {
        "head_object",
        "put_object",
        "get_object",
        "head_bucket",
        "close",
    }
    assert members.issubset(set(dir(S3ClientProtocol)))
