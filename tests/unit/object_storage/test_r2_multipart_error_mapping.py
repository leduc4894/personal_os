"""Multipart staging provider failure mapping, retry wiring and capability text.

These tests prove the R2 multipart staging boundary's error contract: every
classified provider failure maps to the closed Task 1 ``MULTIPART_*`` codes
(transient exhaustion and a missing bucket become the retryable
``multipart_dependency_unavailable``; terminal provider-side failures —
access denial, absent resources outside the idempotent cleanup paths and
malformed responses — become the non-retryable
``multipart_provider_state_invalid``; unknown exceptions cross as the typed
internal error), the bounded retry loop accepts the multipart mapper, the
assembled exact-key staging removal operation name equals the real SDK
operation, and no broad-cleanup capability text (bucket listing, batch delete,
version listing, provider-side copy) exists anywhere in the production adapter
package. Provider messages, request ids, endpoints, staging keys and upload IDs
never enter a mapped error.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from botocore.exceptions import (
    ClientError,
    ConnectionClosedError,
    EndpointConnectionError,
    ReadTimeoutError,
)

from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import InternalApplicationError
from personal_os.multipart_upload.errors import MultipartUploadError
from r2_object_storage import error_mapping
from r2_object_storage.error_mapping import (
    RetryPolicy,
    classify_r2_failure,
    map_multipart_failure,
)
from r2_object_storage.multipart import R2MultipartStagingProvider

_PROVIDER_MESSAGE_SENTINEL = "LEAK-PROVIDER-MESSAGE-5c19"
_PROVIDER_REQUEST_ID_SENTINEL = "LEAK-REQUEST-ID-0e42"
_PROVIDER_ENDPOINT_SENTINEL = "https://leak.example.r2.cloudflarestorage.com"

_REPO_ROOT = Path(__file__).resolve().parents[3]
_ADAPTER_PACKAGE_ROOT = _REPO_ROOT / "packages" / "r2-object-storage" / "src" / "r2_object_storage"


def _client_error(
    code: str,
    status: int,
    *,
    operation: str = "CreateMultipartUpload",
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


def _connection_closed() -> ConnectionClosedError:
    return ConnectionClosedError(endpoint_url=_PROVIDER_ENDPOINT_SENTINEL)


def _endpoint_unreachable() -> EndpointConnectionError:
    return EndpointConnectionError(endpoint_url=_PROVIDER_ENDPOINT_SENTINEL)


def _read_timeout() -> ReadTimeoutError:
    return ReadTimeoutError(endpoint_url=_PROVIDER_ENDPOINT_SENTINEL, proxy_url=None)


async def _no_sleep(_: float) -> None:
    return None


def _zero_jitter(low: float, _high: float) -> float:
    return low


# --- The closed multipart failure mapping --------------------------------------


@pytest.mark.parametrize(
    "cause_factory",
    [
        lambda: _connection_closed(),
        lambda: _endpoint_unreachable(),
        lambda: _read_timeout(),
        lambda: _client_error("SlowDown", 503),
        lambda: _client_error("ServiceUnavailable", 503),
        lambda: _client_error("TooManyRequests", 429),
    ],
)
def test_retryable_and_exhausted_failures_map_to_dependency_unavailable(
    cause_factory: Callable[[], BaseException],
) -> None:
    for exhausted in (False, True):
        error = map_multipart_failure(cause_factory(), exhausted=exhausted)
        assert isinstance(error, MultipartUploadError)
        assert error.error_code is ErrorCode.MULTIPART_DEPENDENCY_UNAVAILABLE
        assert error.is_retryable is True


def test_missing_bucket_maps_to_dependency_unavailable() -> None:
    error = map_multipart_failure(_client_error("NoSuchBucket", 404), exhausted=False)
    assert error.error_code is ErrorCode.MULTIPART_DEPENDENCY_UNAVAILABLE


@pytest.mark.parametrize(
    "cause_factory",
    [
        lambda: _client_error("AccessDenied", 403),
        lambda: _client_error("InvalidAccessKeyId", 403),
        lambda: _client_error("SignatureDoesNotMatch", 403),
        lambda: _client_error("Unauthorized", 401),
        lambda: _client_error("NoSuchUpload", 404),
        lambda: _client_error("NoSuchKey", 404),
        lambda: _client_error("InvalidPart", 400),
        lambda: _client_error("InvalidPartOrder", 400),
        lambda: _client_error("EntityTooSmall", 400),
        lambda: _client_error("PreconditionFailed", 412),
        lambda: ClientError({"Error": {}, "ResponseMetadata": {}}, "ListParts"),
    ],
)
def test_terminal_provider_failures_map_to_provider_state_invalid(
    cause_factory: Callable[[], BaseException],
) -> None:
    error = map_multipart_failure(cause_factory(), exhausted=False)
    assert isinstance(error, MultipartUploadError)
    assert error.error_code is ErrorCode.MULTIPART_PROVIDER_STATE_INVALID
    assert error.is_retryable is False


def test_unknown_exception_maps_to_internal_error() -> None:
    error = map_multipart_failure(RuntimeError("internal-bug-sentinel-90b4"), exhausted=False)
    assert isinstance(error, InternalApplicationError)
    assert error.error_code is ErrorCode.INTERNAL_ERROR
    assert not isinstance(error, MultipartUploadError)


@pytest.mark.parametrize(
    "cause_factory",
    [
        lambda: _client_error("AccessDenied", 403),
        lambda: _client_error("NoSuchUpload", 404),
        lambda: _connection_closed(),
        lambda: ClientError({"Error": {}, "ResponseMetadata": {}}, "ListParts"),
    ],
)
def test_mapped_multipart_errors_carry_no_provider_values(
    cause_factory: Callable[[], BaseException],
) -> None:
    error = map_multipart_failure(cause_factory(), exhausted=False)
    blob = f"{error!r}\n{error}\n{error.to_safe_dict()!r}"
    for sentinel in (
        _PROVIDER_MESSAGE_SENTINEL,
        _PROVIDER_REQUEST_ID_SENTINEL,
        _PROVIDER_ENDPOINT_SENTINEL,
    ):
        assert sentinel not in blob


# --- Retry-policy wiring for the multipart mapper -------------------------------


@pytest.mark.asyncio
async def test_retry_policy_maps_transient_exhaustion_through_multipart_mapper() -> None:
    policy = RetryPolicy(maximum_attempts=2)

    async def operation(_attempt: int) -> str:
        raise _client_error("ServiceUnavailable", 503)

    with pytest.raises(MultipartUploadError) as info:
        await policy.run(
            operation,
            monotonic=lambda: 0.0,
            sleep=_no_sleep,
            jitter=_zero_jitter,
            map_failure=map_multipart_failure,
        )
    assert info.value.error_code is ErrorCode.MULTIPART_DEPENDENCY_UNAVAILABLE


@pytest.mark.asyncio
async def test_retry_policy_maps_unknown_cause_to_internal_error_through_multipart_mapper() -> None:
    policy = RetryPolicy(maximum_attempts=2)

    async def operation(_attempt: int) -> str:
        raise RuntimeError("internal-bug-sentinel-90b4")

    with pytest.raises(InternalApplicationError) as info:
        await policy.run(
            operation,
            monotonic=lambda: 0.0,
            sleep=_no_sleep,
            jitter=_zero_jitter,
            map_failure=map_multipart_failure,
        )
    assert info.value.error_code is ErrorCode.INTERNAL_ERROR
    assert "internal-bug-sentinel-90b4" not in repr(info.value)


@pytest.mark.asyncio
async def test_retry_policy_default_mapper_is_unchanged() -> None:
    from personal_os.object_storage.errors import ObjectStorageError

    policy = RetryPolicy(maximum_attempts=1)

    async def operation(_attempt: int) -> str:
        raise _client_error("AccessDenied", 403)

    with pytest.raises(ObjectStorageError) as info:
        await policy.run(operation, monotonic=lambda: 0.0, sleep=_no_sleep, jitter=_zero_jitter)

    assert info.value.error_code is ErrorCode.OBJECT_STORAGE_ACCESS_DENIED
    assert not isinstance(info.value, MultipartUploadError)


def test_classification_is_shared_with_the_canonical_boundary() -> None:
    # The multipart mapping classifies through the same closed classifier as the
    # canonical adapter: identical inputs must not diverge in retryability.
    slow_down = _client_error("SlowDown", 503)
    denied = _client_error("AccessDenied", 403)
    assert classify_r2_failure(slow_down) is error_mapping.RetryDecision.RETRY
    assert classify_r2_failure(denied) is error_mapping.RetryDecision.TERMINAL


# --- Exact-key staging removal and capability text ------------------------------


def test_staging_removal_operation_matches_the_real_sdk_surface() -> None:
    """The typed protocol operation is the real SDK exact-key removal.

    The capability name the provider declares and calls must exist verbatim on
    the pinned aiobotocore S3 client surface, and it must appear in the module
    exactly as the protocol declaration plus the single direct call — never as
    a quoted name or an assembled/dynamic form a future edit could broaden.
    """

    from types_aiobotocore_s3 import S3Client

    assert hasattr(S3Client, "delete_object"), (
        "the pinned aiobotocore S3 client no longer exposes the exact-key "
        "removal operation the staging provider declares"
    )
    source = (_ADAPTER_PACKAGE_ROOT / "multipart.py").read_text(encoding="utf-8")
    assert source.count("delete_object") == 2
    assert "async def delete_object(" in source
    assert source.count("self._client.delete_object(") == 1
    assert '"delete_object"' not in source
    assert "getattr" not in source


def test_adapter_package_contains_no_broad_cleanup_capability_text() -> None:
    """Every adapter source stays free of broad-cleanup capability text.

    The exact-key staging removal name is permitted only in the multipart
    module, only as the typed protocol declaration plus the single direct call
    (the same scoped contract the composition boundary enforces); every other
    file, and every batch/list/version/copy token anywhere, stays forbidden.
    """

    staging_module = _ADAPTER_PACKAGE_ROOT / "multipart.py"
    package_files = [
        path
        for path in sorted(_ADAPTER_PACKAGE_ROOT.rglob("*.py"))
        if "__pycache__" not in path.parts
    ]
    assert package_files, "the adapter package must contain sources"
    for path in package_files:
        source = path.read_text(encoding="utf-8")
        for forbidden in (
            "list_objects",
            "delete_objects",
            "list_object_versions",
            "copy_object",
        ):
            assert forbidden not in source, f"{path} mentions {forbidden!r}"
        if path == staging_module:
            assert source.count("delete_object") == 2, (
                "multipart.py names the exact-key staging removal operation "
                "outside the protocol declaration and single direct call"
            )
        else:
            assert "delete_object" not in source, f"{path} mentions 'delete_object'"


def test_provider_type_declares_only_the_staging_methods() -> None:
    public = {name for name in dir(R2MultipartStagingProvider) if not name.startswith("_")}
    assert public == {
        "create_upload",
        "presign_part",
        "list_parts",
        "complete_upload",
        "abort_upload",
        "delete_staging_object",
        "open_staging_stream",
    }


# --- The client manager shares one bounded SDK client with the provider --------


class _FakeClientContext:
    """Minimal async context manager publishing one stub SDK client."""

    def __init__(self) -> None:
        self._client = object()

    async def __aenter__(self) -> object:
        return self._client

    async def __aexit__(self, *_exc_info: object) -> None:
        return None


class _RecordingSessionFactory:
    """Records ``create_client`` calls and publishes a stable stub client."""

    def __init__(self) -> None:
        self.create_client_count = 0

    def create_client(self, _service_name: str, **_kwargs: object) -> _FakeClientContext:
        self.create_client_count += 1
        return _FakeClientContext()


@pytest.mark.asyncio
async def test_client_manager_shares_one_sdk_client_for_multipart_staging(
    tmp_path: Path,
) -> None:
    from pydantic import SecretStr

    from r2_object_storage.client import R2ClientManager
    from r2_object_storage.settings import LoadedR2Credentials, ObjectStorageSettings

    settings = ObjectStorageSettings(
        environment="local",
        secret_root=tmp_path / "secrets",
        r2_endpoint="https://abcdef0123456789abcdef0123456789.r2.cloudflarestorage.com",
        r2_bucket_name="knowledge-test",
        r2_access_key_id_file="r2_access_key_id",
        r2_secret_access_key_file="r2_secret_access_key",
        object_storage_spool_root=tmp_path,
    )
    credentials = LoadedR2Credentials(
        access_key_id=SecretStr("test-access-key-id"),
        secret_access_key=SecretStr("test-secret-access-key"),
    )
    session_factory = _RecordingSessionFactory()
    manager = R2ClientManager(
        settings,
        credentials,
        session=session_factory,
    )

    first = await manager.get_multipart_staging_client()
    second = await manager.get_multipart_staging_client()
    assert first is second
    assert session_factory.create_client_count == 1
    await manager.close()
