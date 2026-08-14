"""Dedicated live R2 harness fixtures: fail-closed setup and exact-key cleanup.

This is the live harness of design section 16.2/16.3. Every ``r2_live`` test
receives one :class:`LiveR2Harness` built against a REAL ``R2S3ObjectStore`` on
one dedicated private test bucket, plus a per-run
:class:`LiveCleanupManifest`. The fixtures contract is exact:

- Setup FAILS (never skips) when any required ``R2_TEST_*`` variable or
  mode-0600 secret file is missing; failure messages carry NAMES only — never
  a bucket, endpoint, key or secret value.
- Configuration is composed onto the exact ``KNOWLEDGE_*`` names the frozen
  settings loader reads (secret FILES beneath a secret root; no plaintext
  secret environment value ever reaches the loader).
- Payloads are per-run random non-personal bytes bound to the manifest nonce.
- Teardown runs in a ``finally``: it validates every recorded key against the
  exact-key contract (wrong bucket, noncanonical key, unrecorded key and
  wildcard are rejected BEFORE any delete call), deletes exactly those keys
  through the harness-local low-level ``delete_object`` — the ONLY deletion
  code in the repository, never exported from ``r2_object_storage`` — then
  proves absence through the adapter's typed verify path. Cleanup failure
  fails the run.
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import tempfile
import uuid
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Final

import pytest
import pytest_asyncio
from aiobotocore.config import AioConfig
from aiobotocore.session import get_session
from tests.integration.r2_object_storage.cleanup_manifest import (
    CleanupRejection,
    CreatedObjectRecord,
    LiveCleanupManifest,
    compose_live_environment,
    run_exact_key_cleanup,
    short_key_prefix,
)
from types_aiobotocore_s3 import S3Client

from personal_os.diagnostics import DiagnosticLogger
from personal_os.error_contracts.exceptions import ApplicationError
from personal_os.object_storage import (
    CanonicalMediaType,
    ContentDigest,
    ExpectedObject,
    VerifiedObjectReceipt,
    derive_canonical_object_key,
)
from r2_object_storage.adapter import R2S3ObjectStore
from r2_object_storage.client import R2ClientManager
from r2_object_storage.error_mapping import RetryPolicy
from r2_object_storage.metrics import InMemoryObjectStorageMetrics
from r2_object_storage.settings import (
    LoadedR2Credentials,
    ObjectStorageSettings,
    load_object_storage_settings,
)
from r2_object_storage.spool import SpoolManager

# Diagnostic events emitted by the store must never render on the console.
_root_logger = logging.getLogger()
if not any(isinstance(handler, logging.NullHandler) for handler in _root_logger.handlers):
    _root_logger.addHandler(logging.NullHandler())

# --- Dedicated test configuration surface (names only, never values) ---------

#: Repository variable carrying the dedicated test bucket's endpoint URL.
_LIVE_ENDPOINT_VARIABLE: Final[str] = "R2_TEST_ENDPOINT"
#: Repository variable carrying the dedicated test bucket's name.
_LIVE_BUCKET_VARIABLE: Final[str] = "R2_TEST_BUCKET_NAME"
#: Directory holding the two dedicated test credential files (mode 0600).
_LIVE_SECRET_ROOT_VARIABLE: Final[str] = "R2_TEST_SECRET_ROOT"
#: Access-key secret file name beneath the secret root.
_ACCESS_KEY_FILE_NAME: Final[str] = "r2_test_access_key_id"
#: Secret-access-key secret file name beneath the secret root.
_SECRET_ACCESS_KEY_FILE_NAME: Final[str] = "r2_test_secret_access_key"

_LIVE_REQUIRED_VARIABLES: Final[tuple[str, ...]] = (
    _LIVE_ENDPOINT_VARIABLE,
    _LIVE_BUCKET_VARIABLE,
    _LIVE_SECRET_ROOT_VARIABLE,
)
_LIVE_REQUIRED_SECRET_FILES: Final[tuple[str, ...]] = (
    _ACCESS_KEY_FILE_NAME,
    _SECRET_ACCESS_KEY_FILE_NAME,
)

_PAYLOAD_CHUNK_SIZE_BYTES: Final[int] = 1_048_576


async def _payload_stream(payload: bytes, chunk_size_bytes: int) -> AsyncIterator[bytes]:
    for offset in range(0, len(payload), chunk_size_bytes):
        yield payload[offset : offset + chunk_size_bytes]


class LiveR2Harness:
    """One dedicated-bucket run: real store, per-run manifest, exact cleanup."""

    def __init__(
        self,
        *,
        store: R2S3ObjectStore,
        manifest: LiveCleanupManifest,
        low_level_client: S3Client,
        bucket_name: str,
    ) -> None:
        self._store = store
        self._manifest = manifest
        self._low_level_client = low_level_client
        self._bucket_name = bucket_name

    @property
    def store(self) -> R2S3ObjectStore:
        """The real adapter against the dedicated test bucket."""

        return self._store

    @property
    def manifest(self) -> LiveCleanupManifest:
        """The current run's exact-key cleanup allowlist."""

        return self._manifest

    async def store_payload(
        self,
        payload: bytes,
        *,
        media_type: str,
        chunk_size_bytes: int = _PAYLOAD_CHUNK_SIZE_BYTES,
    ) -> VerifiedObjectReceipt:
        """Store run payload bytes through the real adapter and record the
        created canonical key for exact cleanup."""

        receipt = await self._store.store_stream(
            _payload_stream(payload, chunk_size_bytes), len(payload), media_type
        )
        self._manifest.record_created(
            CreatedObjectRecord(
                key=str(receipt.object_key),
                digest_hexadecimal=receipt.content_digest.hexadecimal,
                size_bytes=receipt.size_bytes,
                media_type=str(receipt.media_type),
            )
        )
        return receipt

    async def write_object_under_digest(
        self, *, digest_hexadecimal: str, payload: bytes, media_type: str
    ) -> str:
        """Write ``payload`` under the canonical key of ANOTHER digest.

        This is the harness-local corruption primitive for the live
        full-verification case: the object stored under a content-addressed key
        does not hash to that digest, so every read must fail closed. The key is
        recorded for exact cleanup because this run created it.
        """

        object_key = derive_canonical_object_key(ContentDigest.parse(digest_hexadecimal))
        await self._low_level_client.put_object(
            Bucket=self._bucket_name,
            Key=str(object_key),
            Body=payload,
            ContentLength=len(payload),
            ContentType=media_type,
        )
        self._manifest.record_created(
            CreatedObjectRecord(
                key=str(object_key),
                digest_hexadecimal=digest_hexadecimal,
                size_bytes=len(payload),
                media_type=media_type,
            )
        )
        return str(object_key)

    async def delete_exact_object(self, key: str) -> None:
        """Harness-local low-level delete of one exact canonical key.

        Called only by :func:`run_exact_key_cleanup` AFTER validation. This is
        the single deletion call site in the repository.
        """

        await self._low_level_client.delete_object(Bucket=self._bucket_name, Key=key)


def _require_live_configuration(environment: Mapping[str, str]) -> None:
    """Fail setup (never skip) when a required variable or secret file is absent.

    The failure message lists the missing NAMES only; values, bucket names,
    endpoints and secret contents are never rendered.
    """

    missing_variables = [name for name in _LIVE_REQUIRED_VARIABLES if not environment.get(name)]
    if missing_variables:
        pytest.fail(
            "r2_live harness requires dedicated test configuration; missing "
            f"variable(s): {', '.join(missing_variables)}. Provide "
            f"{_LIVE_ENDPOINT_VARIABLE}, {_LIVE_BUCKET_VARIABLE} and "
            f"{_LIVE_SECRET_ROOT_VARIABLE} pointing at a directory holding the "
            f"mode-0600 secret files {_ACCESS_KEY_FILE_NAME} and "
            f"{_SECRET_ACCESS_KEY_FILE_NAME} (names only are ever rendered).",
            pytrace=False,
        )
    secret_root = Path(environment[_LIVE_SECRET_ROOT_VARIABLE])
    missing_files = [
        name for name in _LIVE_REQUIRED_SECRET_FILES if not (secret_root / name).is_file()
    ]
    if missing_files:
        pytest.fail(
            "r2_live harness requires dedicated test credential files; missing "
            f"file(s) beneath {_LIVE_SECRET_ROOT_VARIABLE}: {', '.join(missing_files)}. "
            "The protected workflow writes them with mode 0600; locally provide "
            "the same files (names only are ever rendered).",
            pytrace=False,
        )


def _load_live_configuration(
    environment: Mapping[str, str], spool_root: Path
) -> tuple[ObjectStorageSettings, LoadedR2Credentials]:
    """Compose the dedicated test variables onto the loader's exact env names."""

    composed = compose_live_environment(
        environment,
        secret_root=environment[_LIVE_SECRET_ROOT_VARIABLE],
        access_key_file_name=_ACCESS_KEY_FILE_NAME,
        secret_access_key_file_name=_SECRET_ACCESS_KEY_FILE_NAME,
        spool_root=str(spool_root),
    )
    try:
        return load_object_storage_settings(environ=composed)
    except ApplicationError as error:
        pytest.fail(
            "r2_live harness configuration was rejected by the frozen settings "
            f"loader: {error.error_code.value} (no value is ever rendered)",
            pytrace=False,
        )
    raise AssertionError("unreachable")  # pragma: no cover


def _low_level_client_config() -> AioConfig:
    """Bounded SDK config for the harness-local delete/corruption client."""

    return AioConfig(
        region_name="auto",
        signature_version="s3v4",
        max_pool_connections=4,
        connect_timeout=5.0,
        read_timeout=60.0,
        retries={"total_max_attempts": 1, "mode": "standard"},
    )


async def _assert_exact_cleanup(harness: LiveR2Harness) -> None:
    """Validate, delete exactly the recorded keys, then prove absence.

    Runs on every exit path (including a forced test exception): the manifest
    records only keys the current run created, validation rejects anything else
    before the first delete call, and each deletion is confirmed through the
    adapter's typed verify path. Any failure here fails the run; per design
    section 16.3 the failure reports only shortened digest prefixes — never a
    full key, bucket, endpoint or provider exception text.
    """

    manifest = harness.manifest
    recorded = manifest.recorded_keys()
    try:
        deleted = await run_exact_key_cleanup(
            manifest,
            bucket_name=manifest.bucket_name,
            keys=recorded,
            delete_one=harness.delete_exact_object,
        )
    except CleanupRejection as rejection:
        pytest.fail(
            f"live cleanup violated the exact-key contract: {rejection.reason.value}",
            pytrace=False,
        )
    except Exception:
        # The provider cause is deliberately not chained or rendered (design
        # section 16.3: only shortened digest prefixes may be reported).
        first_prefix = short_key_prefix(recorded[0]) if recorded else "none"
        pytest.fail(
            "live cleanup delete failed for a provider reason that is not "
            f"rendered; {len(recorded)} recorded key(s), first digest prefix "
            f"{first_prefix}",
            pytrace=False,
        )
    for key in deleted:
        record = manifest.record_for(key)
        assert record is not None, "validated keys always have a record"
        expected = ExpectedObject(
            content_digest=ContentDigest.parse(record.digest_hexadecimal),
            size_bytes=record.size_bytes,
            media_type=CanonicalMediaType.parse(record.media_type),
        )
        remaining = await harness.store.resolve_verified_object(expected)
        if remaining is not None:
            pytest.fail(
                "live cleanup left an object behind under digest prefix "
                f"{short_key_prefix(key)} in the dedicated test bucket",
                pytrace=False,
            )


@pytest_asyncio.fixture
async def live_r2_harness() -> AsyncIterator[LiveR2Harness]:
    """Build the real dedicated-bucket store; clean up exactly what it created."""

    _require_live_configuration(os.environ)
    spool_root = Path(tempfile.mkdtemp(prefix="r2-live-spool-"))
    settings, credentials = _load_live_configuration(os.environ, spool_root)
    client_manager = R2ClientManager(settings, credentials)
    low_level_context = get_session().create_client(
        "s3",
        region_name="auto",
        endpoint_url=settings.r2_endpoint,
        aws_access_key_id=credentials.access_key_id.get_secret_value(),
        aws_secret_access_key=credentials.secret_access_key.get_secret_value(),
        config=_low_level_client_config(),
    )
    low_level_client = await low_level_context.__aenter__()
    try:
        manifest = LiveCleanupManifest(
            bucket_name=settings.r2_bucket_name, run_nonce=uuid.uuid4().hex
        )
        client = await client_manager.get_client()
        store = R2S3ObjectStore(
            client,
            spools=SpoolManager(spool_root),
            retry=RetryPolicy(maximum_attempts=3),
            metrics=InMemoryObjectStorageMetrics(),
            logger=DiagnosticLogger({"service": "object-storage-live-test", "environment": "test"}),
        )
        harness = LiveR2Harness(
            store=store,
            manifest=manifest,
            low_level_client=low_level_client,
            bucket_name=settings.r2_bucket_name,
        )
        try:
            yield harness
        finally:
            # Exact cleanup always runs — on success and on any test failure —
            # while both clients are still open. Cleanup failure fails the run.
            try:
                await _assert_exact_cleanup(harness)
            finally:
                with contextlib.suppress(Exception):
                    await store.close()
                with contextlib.suppress(Exception):
                    await client_manager.close()
    finally:
        with contextlib.suppress(Exception):
            await low_level_context.__aexit__(None, None, None)
        shutil.rmtree(spool_root, ignore_errors=True)
