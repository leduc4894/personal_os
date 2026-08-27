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
- Payloads are per-run random non-personal bytes.
- Teardown runs in a ``finally``: it validates every recorded key against the
  exact-key contract (wrong bucket, noncanonical key, unrecorded key and
  wildcard are rejected BEFORE any delete call), deletes exactly those keys
  through the harness-local low-level ``delete_object`` — the ONLY deletion
  code in the repository, never exported from ``r2_object_storage`` — then
  proves absence through the adapter's typed verify path. Cleanup failure
  fails the run.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import shutil
import sys
import tempfile
import xml.etree.ElementTree as ElementTree
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import pytest
import pytest_asyncio
from aiobotocore.config import AioConfig
from aiobotocore.session import get_session
from botocore.exceptions import (
    ClientError,
    ConnectionClosedError,
    ConnectTimeoutError,
    ReadTimeoutError,
    ResponseStreamingError,
)
from botocore.exceptions import ConnectionError as BotoCoreConnectionError
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
from personal_os.error_contracts.codes import ErrorCode
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
_SANITIZED_FAILURE_DETAILS: Final[str] = "r2_live_failure_details_redacted"
_ZERO_BYTE_FAILURE_EVENT: Final[str] = "r2_live_zero_byte_failed"
_ZERO_BYTE_STAGES: Final[frozenset[str]] = frozenset({"store", "resolve", "read"})
_ZERO_BYTE_PROVIDER_REASONS: Final[frozenset[str]] = frozenset(
    {
        "provider_client_error",
        "provider_timeout",
        "provider_transport_error",
        "provider_unclassified_error",
        "diagnostic_emission_failed",
    }
)
_ZERO_BYTE_REASONS: Final[frozenset[str]] = (
    frozenset(error_code.value for error_code in ErrorCode) | _ZERO_BYTE_PROVIDER_REASONS
)


@dataclass(frozen=True, slots=True)
class ZeroByteLiveDiagnostic:
    """The only fixed-schema failure record emitted by the live zero-byte case."""

    stage: str
    reason: str

    def to_json(self) -> str:
        """Serialize only the event contract; no exception object is accepted here."""

        if self.stage not in _ZERO_BYTE_STAGES:
            raise ValueError("zero-byte diagnostic stage is not allowed")
        if self.reason not in _ZERO_BYTE_REASONS:
            raise ValueError("zero-byte diagnostic reason is not allowed")
        return json.dumps(
            {
                "event": _ZERO_BYTE_FAILURE_EVENT,
                "stage": self.stage,
                "reason": self.reason,
            },
            separators=(",", ":"),
            sort_keys=True,
        )


def classify_zero_byte_live_failure(failure: BaseException) -> str:
    """Map a body failure to a closed token without reading exception text or causes."""

    if isinstance(failure, ApplicationError):
        return failure.error_code.value
    if isinstance(failure, ClientError):
        return "provider_client_error"
    if isinstance(failure, ConnectTimeoutError | ReadTimeoutError):
        return "provider_timeout"
    if isinstance(
        failure,
        BotoCoreConnectionError | ConnectionClosedError | ResponseStreamingError,
    ):
        return "provider_transport_error"
    return "provider_unclassified_error"


def emit_zero_byte_live_diagnostic(
    stage: str,
    failure: BaseException,
    *,
    emit: Callable[[str], None] = print,
) -> None:
    """Emit a closed diagnostic while preserving the original body failure path."""

    try:
        emit(
            ZeroByteLiveDiagnostic(
                stage=stage,
                reason=classify_zero_byte_live_failure(failure),
            ).to_json()
        )
    except Exception:
        with contextlib.suppress(Exception):
            emit(ZeroByteLiveDiagnostic(stage=stage, reason="diagnostic_emission_failed").to_json())


def _xml_local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _sanitized_zero_byte_system_out(test_case: ElementTree.Element) -> str | None:
    """Return the sole safe diagnostic for one failed zero-byte testcase."""

    if test_case.get("name") != "test_zero_byte_round_trip":
        return None
    if not any(_xml_local_name(child.tag) in {"failure", "error"} for child in test_case):
        return None
    streams = [child for child in test_case if _xml_local_name(child.tag) == "system-out"]
    if len(streams) != 1 or streams[0].text is None:
        return None
    records: list[object] = []
    for raw_line in streams[0].text.splitlines():
        line = raw_line.strip()
        if not line.startswith("{"):
            continue
        if not line.endswith("}"):
            return None
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError, TypeError:
            return None
    if len(records) != 1:
        return None
    record = records[0]
    if not isinstance(record, dict) or set(record) != {"event", "stage", "reason"}:
        return None
    event = record.get("event")
    stage = record.get("stage")
    reason = record.get("reason")
    if (
        event != _ZERO_BYTE_FAILURE_EVENT
        or not isinstance(stage, str)
        or stage not in _ZERO_BYTE_STAGES
        or not isinstance(reason, str)
        or reason not in _ZERO_BYTE_REASONS
    ):
        return None
    return ZeroByteLiveDiagnostic(stage=stage, reason=reason).to_json()


def sanitize_live_junit_report(raw_report: Path, sanitized_report: Path) -> None:
    """Write a publishable JUnit report with provider failure details removed.

    Test identity, status counts and durations remain useful as gate evidence.
    Failure/error messages and tracebacks, arbitrary streams and properties are
    removed because provider exceptions may contain request or endpoint
    material. The sole exception is a canonicalized fixed-schema zero-byte
    diagnostic in the failed zero-byte testcase. The destination is replaced
    only after a complete XML document is ready, so sanitizer failure cannot
    publish a partial report.
    """

    tree = ElementTree.parse(raw_report)
    root = tree.getroot()
    for parent in root.iter():
        zero_byte_system_out = (
            _sanitized_zero_byte_system_out(parent)
            if _xml_local_name(parent.tag) == "testcase"
            else None
        )
        for child in list(parent):
            local_name = _xml_local_name(child.tag)
            if local_name == "system-out" and zero_byte_system_out is not None:
                child.clear()
                child.text = zero_byte_system_out
            elif local_name in {"properties", "system-out", "system-err"}:
                parent.remove(child)
            elif local_name in {"failure", "error"}:
                child.clear()
                child.tail = None
                child.set("message", _SANITIZED_FAILURE_DETAILS)
                child.text = _SANITIZED_FAILURE_DETAILS

    ElementTree.indent(tree, space="  ")
    descriptor, staging_name = tempfile.mkstemp(
        prefix=".r2-live-junit-",
        suffix=".xml",
        dir=sanitized_report.parent,
    )
    os.close(descriptor)
    staging_report = Path(staging_name)
    try:
        tree.write(staging_report, encoding="utf-8", xml_declaration=True)
        os.replace(staging_report, sanitized_report)
    finally:
        staging_report.unlink(missing_ok=True)


def _run_harness_command(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="r2-live-harness")
    subparsers = parser.add_subparsers(dest="command", required=True)
    sanitizer = subparsers.add_parser("sanitize-junit")
    sanitizer.add_argument("--source", required=True, type=Path)
    sanitizer.add_argument("--destination", required=True, type=Path)
    parsed = parser.parse_args(arguments)
    if parsed.command != "sanitize-junit":  # pragma: no cover - argparse owns choices.
        return 2
    try:
        sanitize_live_junit_report(parsed.source, parsed.destination)
    except Exception:
        print("object_storage_live_junit_sanitization_failed", file=sys.stderr)
        return 1
    return 0


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
    try:
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
            manifest = LiveCleanupManifest(bucket_name=settings.r2_bucket_name)
            client = await client_manager.get_client()
            store = R2S3ObjectStore(
                client,
                spools=SpoolManager(spool_root),
                retry=RetryPolicy(maximum_attempts=3),
                metrics=InMemoryObjectStorageMetrics(),
                logger=DiagnosticLogger(
                    {"service": "object-storage-live-test", "environment": "test"}
                ),
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
    finally:
        shutil.rmtree(spool_root, ignore_errors=True)


if __name__ == "__main__":  # pragma: no cover - exercised by the workflow contract.
    raise SystemExit(_run_harness_command())
