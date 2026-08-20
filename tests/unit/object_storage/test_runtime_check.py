"""Focused timing behavior for the read-only object-storage runtime check."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from personal_os.diagnostics.logging import reset_diagnostics_for_testing
from personal_os.runtime_configuration.models import ServiceName

if TYPE_CHECKING:
    from r2_object_storage.client import S3ClientProtocol
    from r2_object_storage.runtime_check import R2ClientSource
    from r2_object_storage.settings import LoadedR2Credentials, ObjectStorageSettings
    from r2_object_storage.spool import SpoolCleanupSummary

_ACCOUNT_ID = "abcdef0123456789abcdef0123456789"
_ACCESS_KEY_FILE_NAME = "r2_access_key_id"
_SECRET_ACCESS_KEY_FILE_NAME = "r2_secret_access_key"


class _Clock:
    def __init__(self) -> None:
        self._seconds = 0.0

    def monotonic(self) -> float:
        return self._seconds

    def advance(self, seconds: float) -> None:
        self._seconds += seconds


class _TimedClient:
    def __init__(self, clock: _Clock) -> None:
        self._clock = clock

    async def head_bucket(self) -> None:
        self._clock.advance(0.025)


class _TimedClientSource:
    def __init__(self, clock: _Clock) -> None:
        self._clock = clock
        self._client = cast("S3ClientProtocol", _TimedClient(clock))

    async def get_client(self) -> S3ClientProtocol:
        self._clock.advance(4.0)
        return self._client

    async def close(self) -> None:
        return None


@pytest.fixture(autouse=True)
def _clean_diagnostics() -> Iterator[None]:
    reset_diagnostics_for_testing()
    yield
    reset_diagnostics_for_testing()


def _valid_environ(secret_root: Path, spool_root: Path) -> dict[str, str]:
    return {
        "KNOWLEDGE_ENVIRONMENT": "test",
        "KNOWLEDGE_SECRET_ROOT": str(secret_root),
        "KNOWLEDGE_R2_ENDPOINT": f"https://{_ACCOUNT_ID}.r2.cloudflarestorage.com",
        "KNOWLEDGE_R2_BUCKET_NAME": "knowledge-test",
        "KNOWLEDGE_R2_ACCESS_KEY_ID_FILE": _ACCESS_KEY_FILE_NAME,
        "KNOWLEDGE_R2_SECRET_ACCESS_KEY_FILE": _SECRET_ACCESS_KEY_FILE_NAME,
        "KNOWLEDGE_OBJECT_STORAGE_SPOOL_ROOT": str(spool_root),
    }


@pytest.mark.asyncio
async def test_duration_excludes_client_composition(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """Moving the timer above client composition would add four seconds."""

    secret_root = tmp_path / "secrets"
    secret_root.mkdir()
    (secret_root / _ACCESS_KEY_FILE_NAME).write_text("access-key", encoding="utf-8")
    (secret_root / _SECRET_ACCESS_KEY_FILE_NAME).write_text("secret-key", encoding="utf-8")
    spool_root = tmp_path / "spool"
    spool_root.mkdir()

    clock = _Clock()
    source = _TimedClientSource(clock)

    def _client_source_factory(
        settings: ObjectStorageSettings,
        credentials: LoadedR2Credentials,
    ) -> R2ClientSource:
        del settings, credentials
        return source

    async def _clean_janitor(_spool_root: Path) -> SpoolCleanupSummary:
        from r2_object_storage.spool import SpoolCleanupSummary

        return SpoolCleanupSummary(0, 0, 0, 0)

    async def _no_sleep(_delay_seconds: float) -> None:
        return None

    from r2_object_storage.runtime_check import run_object_storage_runtime_check

    exit_code = await run_object_storage_runtime_check(
        ServiceName.WORKER,
        environ=_valid_environ(secret_root, spool_root),
        client_source_factory=_client_source_factory,
        spool_janitor=_clean_janitor,
        monotonic=clock.monotonic,
        sleep=_no_sleep,
    )

    captured = capsys.readouterr()
    events = [
        json.loads(line)
        for line in (captured.out + captured.err).splitlines()
        if line.startswith("{") and '"diagnostic_schema_version"' in line
    ]
    assert exit_code == 0
    assert len(events) == 1
    assert events[0]["duration_ms"] == 25
