"""R2 multipart staging provider capability-boundary contract.

These tests prove the private staging-only capability of the R2 adapter
(Child 7 spec 3/6): every staging method first requires a validated private
staging-key value whose grammar can never denote a canonical
``objects/sha256/...`` key, a presigned part URL authorizes exactly one
``upload_part`` request with the fixed ``PartNumber``, ``UploadId``, content
length and the ten-minute ``ExpiresIn``, and cleanup addresses exactly one
persisted staging key or provider upload ID with no list, prefix or wildcard
authority anywhere. The scripted SDK fake records every raw call, so the exact
SDK keyword surface is asserted, and provider messages, request ids, staging
keys, upload IDs and URLs never enter a typed error or a diagnostic event.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from botocore.exceptions import ClientError
from tests.contract.object_storage.scripted_s3 import (
    ScriptedMultipartS3Client,
    scripted_body,
)

from personal_os.diagnostics import DiagnosticLogger
from personal_os.error_contracts.codes import ErrorCode
from personal_os.multipart_upload.contracts import (
    MULTIPART_PART_SIZE_BYTES,
    MULTIPART_PART_URL_LIFETIME,
    MultipartPartRange,
)
from personal_os.multipart_upload.errors import MultipartUploadError
from personal_os.multipart_upload.ports import (
    MultipartProviderPartETag,
    MultipartProviderUploadId,
)
from personal_os.object_storage.keys import ContentDigest, derive_canonical_object_key
from r2_object_storage.error_mapping import RetryPolicy
from r2_object_storage.multipart import (
    MultipartProviderPart,
    MultipartStagingKey,
    MultipartStagingOperation,
    R2MultipartStagingProvider,
)

_BUCKET = "knowledge-test"
_STAGING_KEY_TOKEN = "staging-session-token-00000000000000000001"
_STAGING_KEY = f"staging/multipart/{_STAGING_KEY_TOKEN}"
_UPLOAD_ID = "provider-upload-id-1"
_PRESIGNED_URL = "https://example-account.r2.cloudflarestorage.com/staging/part?signature=abc"
_FIXED_NOW = datetime(2024, 3, 4, 5, 6, 7, tzinfo=UTC)
_PART_RANGE = MultipartPartRange(
    part_number=3,
    offset_bytes=2 * MULTIPART_PART_SIZE_BYTES,
    size_bytes=5 * 1024 * 1024,
)

# Stable sentinels standing in for real provider values: a leakage is
# observable as a string match against rendered errors and diagnostic events.
_PROVIDER_MESSAGE_SENTINEL = "LEAK-PROVIDER-MESSAGE-31dd"
_PROVIDER_REQUEST_ID_SENTINEL = "LEAK-REQUEST-ID-88ab"


def _client_error(code: str, status: int, operation: str = "CreateMultipartUpload") -> ClientError:
    return ClientError(
        {
            "Error": {"Code": code, "Message": _PROVIDER_MESSAGE_SENTINEL},
            "ResponseMetadata": {
                "HTTPStatusCode": status,
                "RequestId": _PROVIDER_REQUEST_ID_SENTINEL,
            },
        },
        operation,
    )


def _canonical_key_string() -> str:
    return str(derive_canonical_object_key(ContentDigest.parse("a" * 64)))


def _canonical_shaped_staging_key() -> Any:
    """A staging-key value whose text is a valid canonical key, unvalidated.

    The provider must reject it defensively even if a caller bypasses
    :meth:`MultipartStagingKey.parse` validation.
    """

    return SimpleNamespace(value=_canonical_key_string())


async def _no_sleep(_: float) -> None:
    return None


def _zero_jitter(low: float, _high: float) -> float:
    return low


def build_provider(
    client: ScriptedMultipartS3Client,
    *,
    logger: DiagnosticLogger | None = None,
    maximum_attempts: int = 3,
) -> R2MultipartStagingProvider:
    """Build a provider with fixed, environment-free wiring over a scripted SDK fake."""

    root_logger = logging.getLogger()
    if not any(isinstance(handler, logging.NullHandler) for handler in root_logger.handlers):
        root_logger.addHandler(logging.NullHandler())
    return R2MultipartStagingProvider(
        client,
        bucket=_BUCKET,
        retry=RetryPolicy(maximum_attempts=maximum_attempts),
        logger=logger
        if logger is not None
        else DiagnosticLogger({"service": "test", "environment": "test"}),
        now_utc=lambda: _FIXED_NOW,
        monotonic=lambda: 0.0,
        sleep=_no_sleep,
        jitter=_zero_jitter,
    )


def _upload_id() -> MultipartProviderUploadId:
    return MultipartProviderUploadId(_UPLOAD_ID)


class _DiagnosticRecordCapture(logging.Handler):
    """Capture diagnostic record dicts emitted through the :class:`DiagnosticLogger`."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.events: list[dict[str, object]] = []

    def emit(self, record: logging.LogRecord) -> None:
        diagnostic = getattr(record, "_diagnostic_schema_record", None)
        if isinstance(diagnostic, dict):
            self.events.append(diagnostic)


@contextmanager
def capture_diagnostic_events() -> Iterator[_DiagnosticRecordCapture]:
    capture = _DiagnosticRecordCapture()
    root_logger = logging.getLogger()
    original_level = root_logger.level
    root_logger.setLevel(logging.DEBUG)
    root_logger.addHandler(capture)
    try:
        yield capture
    finally:
        root_logger.removeHandler(capture)
        root_logger.setLevel(original_level)


# --- The private staging-key value object -------------------------------------


def test_staging_key_grammar_accepts_only_the_staging_prefix() -> None:
    key = MultipartStagingKey.parse(_STAGING_KEY)
    assert key.value == _STAGING_KEY

    for invalid in (
        _canonical_key_string(),  # a canonical key can never be a staging key
        "objects/sha256/aa/bb/" + "a" * 64,
        "staging/multipart/short-token",
        "staging/multipart/" + "t" * 129,
        "staging/multipart/invalid chars!",
        "staging/other/" + "t" * 40,
        "",
    ):
        with pytest.raises(ValueError):
            MultipartStagingKey.parse(invalid)


def test_staging_key_and_part_values_render_redacted() -> None:
    key = MultipartStagingKey.parse(_STAGING_KEY)
    assert _STAGING_KEY_TOKEN not in repr(key)

    part = MultipartProviderPart(
        part_number=1, etag=MultipartProviderPartETag("provider-etag-1"), size_bytes=128
    )
    assert "provider-etag-1" not in repr(part)


# --- Creation -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_upload_returns_provider_upload_id_for_staging_key() -> None:
    client = ScriptedMultipartS3Client()
    client.enqueue({"UploadId": _UPLOAD_ID})
    provider = build_provider(client)

    upload_id = await provider.create_upload(MultipartStagingKey.parse(_STAGING_KEY))

    assert isinstance(upload_id, MultipartProviderUploadId)
    assert upload_id.value == _UPLOAD_ID
    call = client.single_call("create_multipart_upload")
    assert call.kwargs == {"Bucket": _BUCKET, "Key": _STAGING_KEY}


@pytest.mark.asyncio
async def test_create_upload_rejects_malformed_provider_response() -> None:
    client = ScriptedMultipartS3Client()
    client.enqueue({"Unexpected": "shape"})
    provider = build_provider(client)

    with pytest.raises(MultipartUploadError) as info:
        await provider.create_upload(MultipartStagingKey.parse(_STAGING_KEY))
    assert info.value.error_code is ErrorCode.MULTIPART_PROVIDER_STATE_INVALID


# --- Capability boundary: no canonical target, validated staging key first -----


@pytest.mark.asyncio
async def test_presigned_part_cannot_target_canonical_key() -> None:
    client = ScriptedMultipartS3Client()
    client.enqueue(_PRESIGNED_URL)
    provider = build_provider(client)

    with pytest.raises(MultipartUploadError) as info:
        await provider.presign_part(_canonical_shaped_staging_key(), _upload_id(), _PART_RANGE)
    assert info.value.error_code is ErrorCode.MULTIPART_PROVIDER_STATE_INVALID
    assert client.calls == []


@pytest.mark.asyncio
async def test_every_staging_method_validates_the_staging_key_before_any_sdk_call() -> None:
    provider = build_provider(ScriptedMultipartS3Client())
    canonical_key = _canonical_shaped_staging_key()

    with pytest.raises(MultipartUploadError):
        await provider.create_upload(canonical_key)
    with pytest.raises(MultipartUploadError):
        await provider.list_parts(canonical_key, _upload_id())
    with pytest.raises(MultipartUploadError):
        await provider.complete_upload(
            canonical_key,
            _upload_id(),
            (
                MultipartProviderPart(
                    part_number=1, etag=MultipartProviderPartETag("etag-1"), size_bytes=8
                ),
            ),
        )
    with pytest.raises(MultipartUploadError):
        await provider.abort_upload(canonical_key, _upload_id())
    with pytest.raises(MultipartUploadError):
        await provider.delete_staging_object(canonical_key)
    with pytest.raises(MultipartUploadError):
        async with provider.open_staging_stream(canonical_key) as _stream:
            pass


@pytest.mark.asyncio
async def test_presign_part_validates_the_upload_id_before_any_sdk_call() -> None:
    client = ScriptedMultipartS3Client()
    client.enqueue(_PRESIGNED_URL)
    provider = build_provider(client)

    with pytest.raises(MultipartUploadError):
        await provider.presign_part(
            MultipartStagingKey.parse(_STAGING_KEY),
            SimpleNamespace(value="not a valid upload id" + "!" * 2000),
            _PART_RANGE,
        )
    assert client.calls == []


# --- Part-URL presign semantics ------------------------------------------------


@pytest.mark.asyncio
async def test_presign_part_signs_single_exact_part_for_ten_minutes() -> None:
    client = ScriptedMultipartS3Client()
    client.enqueue(_PRESIGNED_URL)
    provider = build_provider(client)

    part_url = await provider.presign_part(
        MultipartStagingKey.parse(_STAGING_KEY), _upload_id(), _PART_RANGE
    )

    assert part_url.part_number == _PART_RANGE.part_number
    assert part_url.byte_range == _PART_RANGE
    assert part_url.url == _PRESIGNED_URL
    assert part_url.expires_at == _FIXED_NOW + MULTIPART_PART_URL_LIFETIME
    assert _PRESIGNED_URL not in repr(part_url)

    call = client.single_call("generate_presigned_url")
    assert call.kwargs["ClientMethod"] == "upload_part"
    assert call.kwargs["Params"] == {
        "Bucket": _BUCKET,
        "Key": _STAGING_KEY,
        "UploadId": _UPLOAD_ID,
        "PartNumber": _PART_RANGE.part_number,
        "ContentLength": _PART_RANGE.size_bytes,
    }
    assert call.kwargs["ExpiresIn"] == int(MULTIPART_PART_URL_LIFETIME.total_seconds())


# --- ListParts ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_parts_returns_only_provider_observed_parts() -> None:
    client = ScriptedMultipartS3Client()
    client.enqueue(
        {
            "Parts": [
                {"PartNumber": 1, "ETag": "etag-1", "Size": MULTIPART_PART_SIZE_BYTES},
                {"PartNumber": 3, "ETag": "etag-3", "Size": 5 * 1024 * 1024},
            ],
            "IsTruncated": False,
        }
    )
    provider = build_provider(client)

    parts = await provider.list_parts(MultipartStagingKey.parse(_STAGING_KEY), _upload_id())

    assert parts == (
        MultipartProviderPart(
            part_number=1,
            etag=MultipartProviderPartETag("etag-1"),
            size_bytes=MULTIPART_PART_SIZE_BYTES,
        ),
        MultipartProviderPart(
            part_number=3, etag=MultipartProviderPartETag("etag-3"), size_bytes=5 * 1024 * 1024
        ),
    )
    call = client.single_call("list_parts")
    assert call.kwargs == {"Bucket": _BUCKET, "Key": _STAGING_KEY, "UploadId": _UPLOAD_ID}
    assert client.method_names == ["list_parts"]


@pytest.mark.asyncio
async def test_list_parts_rejects_malformed_provider_part_shape() -> None:
    client = ScriptedMultipartS3Client()
    client.enqueue({"Parts": [{"PartNumber": 1, "Size": 8}], "IsTruncated": False})
    provider = build_provider(client)

    with pytest.raises(MultipartUploadError) as info:
        await provider.list_parts(MultipartStagingKey.parse(_STAGING_KEY), _upload_id())
    assert info.value.error_code is ErrorCode.MULTIPART_PROVIDER_STATE_INVALID


@pytest.mark.asyncio
async def test_list_parts_maps_absent_upload_to_provider_state_invalid() -> None:
    client = ScriptedMultipartS3Client()
    client.enqueue(_client_error("NoSuchUpload", 404, "ListParts"))
    provider = build_provider(client)

    with pytest.raises(MultipartUploadError) as info:
        await provider.list_parts(MultipartStagingKey.parse(_STAGING_KEY), _upload_id())
    assert info.value.error_code is ErrorCode.MULTIPART_PROVIDER_STATE_INVALID


@pytest.mark.asyncio
async def test_list_parts_follows_bounded_pagination_and_never_lists_objects() -> None:
    client = ScriptedMultipartS3Client()
    client.enqueue(
        {
            "Parts": [{"PartNumber": 1, "ETag": "etag-1", "Size": 8}],
            "IsTruncated": True,
            "NextPartNumberMarker": 1,
        }
    )
    client.enqueue(
        {"Parts": [{"PartNumber": 2, "ETag": "etag-2", "Size": 8}], "IsTruncated": False}
    )
    provider = build_provider(client)

    parts = await provider.list_parts(MultipartStagingKey.parse(_STAGING_KEY), _upload_id())

    assert [part.part_number for part in parts] == [1, 2]
    second_call = client.calls[1]
    assert second_call.kwargs["PartNumberMarker"] == 1
    assert all(call.method == "list_parts" for call in client.calls)


# --- Completion -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_complete_upload_sends_provider_observed_metadata_for_exact_upload() -> None:
    client = ScriptedMultipartS3Client()
    client.enqueue({})
    provider = build_provider(client)
    observed = (
        MultipartProviderPart(
            part_number=1, etag=MultipartProviderPartETag("etag-1"), size_bytes=8
        ),
        MultipartProviderPart(
            part_number=2, etag=MultipartProviderPartETag("etag-2"), size_bytes=8
        ),
    )

    await provider.complete_upload(MultipartStagingKey.parse(_STAGING_KEY), _upload_id(), observed)

    call = client.single_call("complete_multipart_upload")
    assert call.kwargs == {
        "Bucket": _BUCKET,
        "Key": _STAGING_KEY,
        "UploadId": _UPLOAD_ID,
        "MultipartUpload": {
            "Parts": [
                {"ETag": "etag-1", "PartNumber": 1},
                {"ETag": "etag-2", "PartNumber": 2},
            ]
        },
    }


# --- Cleanup uses exactly one persisted identity ---------------------------------


@pytest.mark.asyncio
async def test_cleanup_uses_exact_upload_or_key_without_list() -> None:
    client = ScriptedMultipartS3Client()
    client.enqueue({})
    provider = build_provider(client)

    await provider.abort_upload(MultipartStagingKey.parse(_STAGING_KEY), _upload_id())

    assert client.method_names == ["abort_multipart_upload"]
    call = client.single_call("abort_multipart_upload")
    assert call.kwargs == {"Bucket": _BUCKET, "Key": _STAGING_KEY, "UploadId": _UPLOAD_ID}


@pytest.mark.asyncio
async def test_abort_upload_treats_absent_upload_as_idempotent_success() -> None:
    client = ScriptedMultipartS3Client()
    client.enqueue(_client_error("NoSuchUpload", 404, "AbortMultipartUpload"))
    provider = build_provider(client)

    await provider.abort_upload(MultipartStagingKey.parse(_STAGING_KEY), _upload_id())

    assert client.method_names == ["abort_multipart_upload"]


@pytest.mark.asyncio
async def test_delete_staging_object_targets_exact_staging_key() -> None:
    client = ScriptedMultipartS3Client()
    client.enqueue({})
    provider = build_provider(client)

    await provider.delete_staging_object(MultipartStagingKey.parse(_STAGING_KEY))

    assert client.method_names == ["delete_object"]
    call = client.single_call("delete_object")
    assert call.kwargs == {"Bucket": _BUCKET, "Key": _STAGING_KEY}


@pytest.mark.asyncio
async def test_delete_staging_object_treats_absence_as_idempotent_success() -> None:
    client = ScriptedMultipartS3Client()
    client.enqueue(_client_error("NoSuchKey", 404, "DeleteObject"))
    provider = build_provider(client)

    await provider.delete_staging_object(MultipartStagingKey.parse(_STAGING_KEY))

    assert client.method_names == ["delete_object"]


@pytest.mark.asyncio
async def test_cleanup_failure_surfaces_typed_error_without_provider_identity() -> None:
    client = ScriptedMultipartS3Client()
    client.enqueue(_client_error("AccessDenied", 403, "AbortMultipartUpload"))
    provider = build_provider(client)

    with pytest.raises(MultipartUploadError) as info:
        await provider.abort_upload(MultipartStagingKey.parse(_STAGING_KEY), _upload_id())

    blob = f"{info.value!r}\n{info.value}\n{info.value.to_safe_dict()!r}"
    for sentinel in (_PROVIDER_MESSAGE_SENTINEL, _PROVIDER_REQUEST_ID_SENTINEL, _UPLOAD_ID):
        assert sentinel not in blob


@pytest.mark.asyncio
async def test_retryable_failure_exhausts_to_dependency_unavailable() -> None:
    client = ScriptedMultipartS3Client()
    for _ in range(3):
        client.enqueue(_client_error("SlowDown", 503))
    provider = build_provider(client)

    with pytest.raises(MultipartUploadError) as info:
        await provider.create_upload(MultipartStagingKey.parse(_STAGING_KEY))

    assert info.value.error_code is ErrorCode.MULTIPART_DEPENDENCY_UNAVAILABLE
    assert info.value.is_retryable is True
    assert client.method_names == ["create_multipart_upload"] * 3


# --- Staging read (the verification spool's full-object stream) ------------------


@pytest.mark.asyncio
async def test_open_staging_stream_reads_exactly_one_staging_object() -> None:
    client = ScriptedMultipartS3Client()
    client.enqueue({"Body": scripted_body([b"staging-bytes-1", b"staging-bytes-2"])})
    provider = build_provider(client)

    chunks: list[bytes] = []
    async with provider.open_staging_stream(MultipartStagingKey.parse(_STAGING_KEY)) as stream:
        async for chunk in stream:
            chunks.append(chunk)

    assert chunks == [b"staging-bytes-1", b"staging-bytes-2"]
    call = client.single_call("get_object")
    assert call.kwargs == {"Bucket": _BUCKET, "Key": _STAGING_KEY}
    assert client.method_names == ["get_object"]


@pytest.mark.asyncio
async def test_open_staging_stream_rejects_malformed_provider_response() -> None:
    client = ScriptedMultipartS3Client()
    client.enqueue({"Unexpected": "shape"})
    provider = build_provider(client)

    with pytest.raises(MultipartUploadError) as info:
        async with provider.open_staging_stream(MultipartStagingKey.parse(_STAGING_KEY)) as stream:
            async for _chunk in stream:
                pass
    assert info.value.error_code is ErrorCode.MULTIPART_PROVIDER_STATE_INVALID


@pytest.mark.asyncio
async def test_open_staging_stream_maps_absent_object_to_provider_state_invalid() -> None:
    client = ScriptedMultipartS3Client()
    client.enqueue(_client_error("NoSuchKey", 404, "GetObject"))
    provider = build_provider(client)

    with pytest.raises(MultipartUploadError) as info:
        async with provider.open_staging_stream(MultipartStagingKey.parse(_STAGING_KEY)) as stream:
            async for _chunk in stream:
                pass
    assert info.value.error_code is ErrorCode.MULTIPART_PROVIDER_STATE_INVALID


@pytest.mark.asyncio
async def test_open_staging_stream_maps_mid_stream_failure_to_typed_error() -> None:
    client = ScriptedMultipartS3Client()
    client.enqueue(
        {"Body": scripted_body([b"first-chunk", b"never-delivered"], fail_after_first=True)}
    )
    provider = build_provider(client)

    with pytest.raises(MultipartUploadError) as info:
        async with provider.open_staging_stream(MultipartStagingKey.parse(_STAGING_KEY)) as stream:
            async for _chunk in stream:
                pass

    assert info.value.error_code is ErrorCode.MULTIPART_DEPENDENCY_UNAVAILABLE
    blob = f"{info.value!r}\n{info.value}\n{info.value.to_safe_dict()!r}"
    for sentinel in (_PROVIDER_MESSAGE_SENTINEL, _PROVIDER_REQUEST_ID_SENTINEL):
        assert sentinel not in blob


@pytest.mark.asyncio
async def test_open_staging_stream_closes_the_body_on_exit() -> None:
    client = ScriptedMultipartS3Client()
    body = scripted_body([b"chunk"])
    client.enqueue({"Body": body})
    provider = build_provider(client)

    async with provider.open_staging_stream(MultipartStagingKey.parse(_STAGING_KEY)) as stream:
        async for _chunk in stream:
            pass

    assert body.close_count == 1


@pytest.mark.asyncio
async def test_staging_stream_diagnostics_carry_only_closed_fields() -> None:
    client = ScriptedMultipartS3Client()
    client.enqueue(
        {"Body": scripted_body([b"first", b"second"], fail_after_first=True)}
    )
    with capture_diagnostic_events() as capture:
        provider = build_provider(client)
        with pytest.raises(MultipartUploadError):
            async with provider.open_staging_stream(
                MultipartStagingKey.parse(_STAGING_KEY)
            ) as stream:
                async for _chunk in stream:
                    pass

    by_name = {
        event.get("event"): event
        for event in capture.events
        if event.get("event")
        in {"object_storage_operation_succeeded", "object_storage_operation_failed"}
    }
    succeeded = by_name["object_storage_operation_succeeded"]
    assert succeeded["operation"] == MultipartStagingOperation.READ_STAGING_OBJECT.value
    failed = by_name["object_storage_operation_failed"]
    assert failed["operation"] == MultipartStagingOperation.READ_STAGING_OBJECT.value
    assert failed["error_code"] == ErrorCode.MULTIPART_DEPENDENCY_UNAVAILABLE.value
    rendered = repr(capture.events)
    for sensitive in (_STAGING_KEY_TOKEN, _UPLOAD_ID):
        assert sensitive not in rendered


# --- Capability surface and closed diagnostics ----------------------------------


def test_provider_exposes_exactly_the_staging_capability() -> None:
    provider = build_provider(ScriptedMultipartS3Client())
    exposed = {name for name in dir(provider) if not name.startswith("_")}
    assert exposed == {
        "create_upload",
        "presign_part",
        "list_parts",
        "complete_upload",
        "abort_upload",
        "delete_staging_object",
        "open_staging_stream",
    }
    for forbidden in ("list_objects", "delete_objects", "copy_object", "list_object_versions"):
        assert not hasattr(provider, forbidden)


@pytest.mark.asyncio
async def test_diagnostics_events_carry_only_closed_fields() -> None:
    client = ScriptedMultipartS3Client()
    client.enqueue(_PRESIGNED_URL)
    with capture_diagnostic_events() as capture:
        provider = build_provider(client)
        await provider.presign_part(
            MultipartStagingKey.parse(_STAGING_KEY), _upload_id(), _PART_RANGE
        )

    succeeded = [
        event
        for event in capture.events
        if event.get("event") == "object_storage_operation_succeeded"
    ]
    assert len(succeeded) == 1
    record = succeeded[0]
    assert record["operation"] == MultipartStagingOperation.PRESIGN_PART.value
    assert record["attempt_count"] == 1
    rendered = repr(record)
    for sensitive in (_STAGING_KEY_TOKEN, _UPLOAD_ID, _PRESIGNED_URL):
        assert sensitive not in rendered
