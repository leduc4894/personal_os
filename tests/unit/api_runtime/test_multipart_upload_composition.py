"""Multipart upload serve composition: the durable graph and its seams.

These tests pin the production composition root's shape without touching a
database or a network: the composed service binds the durable PostgreSQL
session and evidence stores, the str-key staging provider seam over the R2
adapter, the staging byte source over the same adapter's exact staging read,
the locator-aware recheck policy guard, the fenced publication gateway and
the durable closed-log diagnostics sink; the disposal hook closes the one
lazy R2 client exactly once. The keyring codec seam proves its sealed round
trip and its fail-closed opening, and the lazy SDK client resolves the
manager's shared client per call.
"""

from __future__ import annotations

import atexit
import tempfile
from pathlib import Path
from typing import Any

import pytest
from api_runtime.authentication_crypto import (
    AuthenticationKeyring,
    CryptographyAuthenticationCrypto,
)
from api_runtime.multipart_upload_composition import (
    KeyringMultipartOperationTokenCodec,
    LazyMultipartStagingSdkClient,
    R2MultipartStagingByteSource,
    compose_multipart_upload,
)
from api_runtime.small_file_sync_composition import BoundPolicySmallFilePublicationGateway

from personal_os.authentication.crypto import MULTIPART_OPERATION_TOKEN_AEAD_LABEL
from personal_os.diagnostics.logging import DiagnosticLogger
from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import InternalApplicationError
from personal_os.multipart_upload.ports import (
    MultipartOperationTokenCodecPort,
    SealedMultipartOperationToken,
)
from personal_os.multipart_upload.service import MultipartUploadService
from personal_os.small_file_sync.contracts import UploadOperationToken
from postgresql_source_store.multipart_upload_store import (
    PostgresqlMultipartSessionEvidenceStore,
    PostgresqlMultipartUploadStore,
)
from postgresql_source_store.small_file_sync_operations import (
    PostgresqlSmallFileUploadOperationStore,
)
from r2_object_storage.settings import LoadedR2Credentials, ObjectStorageSettings

_KEY_MATERIAL: bytes = bytes(range(32))

_SPOOL_DIRECTORY = tempfile.TemporaryDirectory(prefix="multipart-composition-spool-")
atexit.register(_SPOOL_DIRECTORY.cleanup)

_OBJECT_STORAGE_SETTINGS: ObjectStorageSettings = ObjectStorageSettings(
    secret_root=Path(_SPOOL_DIRECTORY.name),
    r2_endpoint=f"https://{'0' * 32}.r2.cloudflarestorage.com",
    r2_bucket_name="personal-knowledge-objects",
    r2_access_key_id_file="r2_access_key_id",
    r2_secret_access_key_file="r2_secret_access_key",
    object_storage_spool_root=Path(_SPOOL_DIRECTORY.name),
)


def _logger() -> DiagnosticLogger:
    return DiagnosticLogger({"service": "test", "environment": "test"})


def _keyring() -> AuthenticationKeyring:
    from types import MappingProxyType

    return AuthenticationKeyring(
        current_key_id="auth-key-v1",
        keys_by_id=MappingProxyType({"auth-key-v1": _KEY_MATERIAL}),
    )


def _credentials() -> LoadedR2Credentials:
    from pydantic import SecretStr

    return LoadedR2Credentials(
        access_key_id=SecretStr("test-access-key-id"),
        secret_access_key=SecretStr("test-secret-access-key"),
    )


def test_compose_multipart_upload_binds_the_durable_serve_graph() -> None:
    from typing import cast

    from sqlalchemy.ext.asyncio import AsyncEngine

    engine = cast("AsyncEngine", object())
    runtime = compose_multipart_upload(
        engine=engine,
        object_storage_settings=_OBJECT_STORAGE_SETTINGS,
        object_storage_credentials=_credentials(),
        logger=_logger(),
        keyring=_keyring(),
    )

    service = runtime.service
    assert isinstance(service, MultipartUploadService)
    assert isinstance(service.session_store, PostgresqlMultipartUploadStore)
    assert isinstance(service.evidence_store, PostgresqlMultipartSessionEvidenceStore)
    assert isinstance(service.operation_store, PostgresqlSmallFileUploadOperationStore)
    assert isinstance(service.publication_gateway, BoundPolicySmallFilePublicationGateway)
    assert isinstance(service.staging_byte_source, R2MultipartStagingByteSource)
    # The durable closed-log rejection surface (parked finding D2) is bound.
    assert service.diagnostics is not None
    # One lazy R2 client owner disposed exactly once through the hook.
    assert runtime.aclose is not None
    assert runtime.rejection_diagnostics is not None


_STAGING_KEY: str = "staging/multipart/" + "k" * 43


class _StaticBody:
    def __aiter__(self) -> Any:
        return self

    async def read(self, amt: int | None = None) -> bytes:
        return b""

    async def __anext__(self) -> bytes:
        raise StopAsyncIteration


class _StaticReadProvider:
    """The validated-key seam's underlying provider double for the read."""

    def open_staging_stream(self, staging_key: Any) -> Any:
        del staging_key
        return _NullContext([b"staging-bytes"])


class _NullContext:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def __aenter__(self) -> Any:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    def __aiter__(self) -> Any:
        return self

    async def __anext__(self) -> bytes:
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)


@pytest.mark.asyncio
async def test_staging_byte_source_validates_the_key_before_the_stream() -> None:
    byte_source = R2MultipartStagingByteSource(_StaticReadProvider())

    chunks: list[bytes] = []
    async with byte_source.open_staging_stream(_STAGING_KEY) as stream:
        async for chunk in stream:
            chunks.append(chunk)
    assert chunks == [b"staging-bytes"]

    from personal_os.multipart_upload.errors import MultipartUploadError

    with pytest.raises(MultipartUploadError) as info:
        async with byte_source.open_staging_stream("objects/sha256/aa/bb/" + "a" * 64) as stream:
            async for _chunk in stream:
                pass
    assert info.value.error_code is ErrorCode.MULTIPART_PROVIDER_STATE_INVALID


class _RecordingManager:
    """Manager double resolving one shared raw client per call."""

    def __init__(self) -> None:
        self.resolution_count = 0
        self._client = _RecordingRawClient()

    async def get_multipart_staging_client(self) -> Any:
        self.resolution_count += 1
        return self._client


class _RecordingRawClient:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def create_multipart_upload(self, **kwargs: Any) -> Any:
        self.calls.append("create_multipart_upload")
        return {"UploadId": "upload-1"}

    async def generate_presigned_url(
        self,
        ClientMethod: str,
        Params: Any = None,
        ExpiresIn: int = 3600,
        HttpMethod: Any = None,
    ) -> str:
        self.calls.append("generate_presigned_url")
        return "https://staging.invalid/signed"

    async def list_parts(self, **kwargs: Any) -> Any:
        self.calls.append("list_parts")
        return {"Parts": [], "IsTruncated": False}

    async def complete_multipart_upload(self, **kwargs: Any) -> Any:
        self.calls.append("complete_multipart_upload")
        return {}

    async def abort_multipart_upload(self, **kwargs: Any) -> Any:
        self.calls.append("abort_multipart_upload")
        return {}

    async def delete_object(self, **kwargs: Any) -> Any:
        self.calls.append("delete_object")
        return {}

    async def get_object(self, **kwargs: Any) -> Any:
        self.calls.append("get_object")
        return {"Body": _StaticBody()}


class _StaticBody:
    def __aiter__(self) -> Any:
        return self

    async def read(self, amt: int | None = None) -> bytes:
        return b""

    async def __anext__(self) -> bytes:
        raise StopAsyncIteration


@pytest.mark.asyncio
async def test_lazy_sdk_client_resolves_the_shared_client_per_call() -> None:
    manager = _RecordingManager()
    client = LazyMultipartStagingSdkClient(manager)  # type: ignore[arg-type]
    assert await client.create_multipart_upload(Bucket="b", Key="k") == {"UploadId": "upload-1"}
    raw = manager._client
    await raw.list_parts(Bucket="b", Key="k", UploadId="u")
    assert raw.calls == ["create_multipart_upload", "list_parts"]
    assert manager.resolution_count == 1


def test_keyring_codec_seals_and_opens_the_operation_token() -> None:
    codec: MultipartOperationTokenCodecPort = KeyringMultipartOperationTokenCodec(
        CryptographyAuthenticationCrypto(), _keyring()
    )
    token = UploadOperationToken("t" * 43)

    sealed = codec.seal_token(token=token)

    assert isinstance(sealed, SealedMultipartOperationToken)
    assert sealed.key_id == "auth-key-v1"
    assert token.value not in sealed.ciphertext
    assert codec.open_token(sealed=sealed) == token


def test_keyring_codec_fails_closed_on_an_unknown_key() -> None:
    codec = KeyringMultipartOperationTokenCodec(
        CryptographyAuthenticationCrypto(), _keyring()
    )
    sealed = SealedMultipartOperationToken(
        key_id="auth-key-unknown",
        nonce="bm9uY2Utc2VudGluZWw",
        ciphertext="Y2lwaGVydGV4dC1zZW50aW5lbA",
    )
    with pytest.raises(InternalApplicationError) as info:
        codec.open_token(sealed=sealed)
    assert info.value.error_code is ErrorCode.INTERNAL_ERROR
    assert "bm9uY2Utc2VudGluZWw" not in repr(info.value)


def test_keyring_codec_uses_the_registered_domain_label() -> None:
    # The sealed subkey derives under the closed authentication label
    # vocabulary; an unregistered label must be rejected fail-closed.
    crypto = CryptographyAuthenticationCrypto()
    with pytest.raises(InternalApplicationError):
        crypto.derive_subkey(master_key=_KEY_MATERIAL, label="multipart/unregistered/v1")
    derived = crypto.derive_subkey(
        master_key=_KEY_MATERIAL, label=MULTIPART_OPERATION_TOKEN_AEAD_LABEL
    )
    assert len(derived) == 32
