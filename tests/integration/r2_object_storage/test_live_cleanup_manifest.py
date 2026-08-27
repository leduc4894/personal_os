"""Offline unit contract for the live-harness exact-key cleanup validator.

Design section 16.3 requires cleanup to delete ONLY exact canonical keys the
current run recorded in the dedicated test bucket. These tests prove the
validator and the cleanup driver reject a wrong bucket, a noncanonical key, an
unrecorded key and a wildcard BEFORE any delete call executes: every rejection
case hands a failing fake delete client to the driver and asserts control never
reaches it. No test here touches the network or requires live credentials, so
the module is intentionally NOT marked ``r2_live``.
"""

from __future__ import annotations

import hashlib
import inspect
import tempfile
from collections.abc import AsyncIterator, Callable, Mapping
from pathlib import Path
from typing import cast

import pytest
from tests.integration.r2_object_storage import conftest as live_conftest
from tests.integration.r2_object_storage.cleanup_manifest import (
    REJECTION_BUCKET_MISMATCH,
    REJECTION_NONCANONICAL_KEY,
    REJECTION_UNRECORDED_KEY,
    REJECTION_WILDCARD_KEY,
    CleanupRejection,
    CreatedObjectRecord,
    LiveCleanupManifest,
    run_exact_key_cleanup,
    validate_cleanup_deletions,
)

_TEST_BUCKET = "knowledge-test-objects"
_OTHER_BUCKET = "knowledge-production-objects"


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


_D1 = _digest(b"live-cleanup-recorded-payload-one")
_D2 = _digest(b"live-cleanup-recorded-payload-two")


def _canonical_key(digest_hexadecimal: str) -> str:
    return f"objects/sha256/{digest_hexadecimal[:2]}/{digest_hexadecimal[2:4]}/{digest_hexadecimal}"


def _record(digest_hexadecimal: str, *, size_bytes: int = 8) -> CreatedObjectRecord:
    return CreatedObjectRecord(
        key=_canonical_key(digest_hexadecimal),
        digest_hexadecimal=digest_hexadecimal,
        size_bytes=size_bytes,
        media_type="application/octet-stream",
    )


def _manifest_with(*records: CreatedObjectRecord) -> LiveCleanupManifest:
    # The manifest carries no run identity: payloads are per-run random bytes
    # and cleanup binds only to the recorded exact keys of this run.
    manifest = LiveCleanupManifest(bucket_name=_TEST_BUCKET)
    for record in records:
        manifest.record_created(record)
    return manifest


async def _boom_delete(key: str) -> None:
    raise AssertionError(f"no delete call may run before validation passes, got {key!r}")


class _RecordingDeleteClient:
    """Fake low-level delete client counting exact-key delete calls."""

    def __init__(self) -> None:
        self.deleted_keys: list[str] = []

    async def delete_object(self, key: str) -> None:
        self.deleted_keys.append(key)


# --- Rejections happen before any delete call -------------------------------


def test_wrong_bucket_is_rejected_before_any_delete_call() -> None:
    manifest = _manifest_with(_record(_D1))
    with pytest.raises(CleanupRejection) as rejection:
        validate_cleanup_deletions(
            manifest, bucket_name=_OTHER_BUCKET, keys=manifest.recorded_keys()
        )
    assert rejection.value.reason is REJECTION_BUCKET_MISMATCH


def test_noncanonical_key_is_rejected_before_any_delete_call() -> None:
    for mutated_key in (
        f"test-runs/1234/{_D1}",
        f"objects/sha256/{_D1[:2]}/{_D1[2:4]}/{_D1.upper()}",
        f"objects/sha256/{_D1[2:4]}/{_D1[2:4]}/{_D1}",
        f"objects/sha256/{_D1[:2]}/{_D1[2:4]}/{_D1}extra",
        f"objects/md5/{_D1[:2]}/{_D1[2:4]}/{_D1}",
        f"objects/sha256/{_D1[:2]}/{_D1[2:4]}/{_D1[:-1]}",
    ):
        with pytest.raises(CleanupRejection) as rejection:
            validate_cleanup_deletions(
                _manifest_with(_record(_D1)),
                bucket_name=_TEST_BUCKET,
                keys=(mutated_key,),
            )
        assert rejection.value.reason is REJECTION_NONCANONICAL_KEY, mutated_key


def test_unrecorded_key_is_rejected_before_any_delete_call() -> None:
    unrecorded_key = _canonical_key(_digest(b"never-created-by-this-run"))
    manifest = _manifest_with(_record(_D1))
    with pytest.raises(CleanupRejection) as rejection:
        validate_cleanup_deletions(manifest, bucket_name=_TEST_BUCKET, keys=(unrecorded_key,))
    assert rejection.value.reason is REJECTION_UNRECORDED_KEY


def test_wildcard_key_is_rejected_before_any_delete_call() -> None:
    for wildcard_key in (
        f"objects/sha256/{_D1[:2]}/{_D1[2:4]}/*",
        f"objects/sha256/{_D1[:2]}/{_D1[2:4]}/{_D1}%",
        f"objects/sha256/{_D1[:2]}/{_D1[2:4]}/{_D1[:-1]}?",
        "objects/sha256/*/*/*",
    ):
        with pytest.raises(CleanupRejection) as rejection:
            validate_cleanup_deletions(
                _manifest_with(_record(_D1)),
                bucket_name=_TEST_BUCKET,
                keys=(wildcard_key,),
            )
        assert rejection.value.reason is REJECTION_WILDCARD_KEY, wildcard_key


# --- Acceptance: exactly the recorded keys validate -------------------------


def test_valid_manifest_validates_exactly_the_recorded_keys() -> None:
    manifest = _manifest_with(_record(_D1), _record(_D2, size_bytes=4096))
    validated = validate_cleanup_deletions(
        manifest, bucket_name=_TEST_BUCKET, keys=manifest.recorded_keys()
    )
    assert validated == (_canonical_key(_D1), _canonical_key(_D2))


@pytest.mark.asyncio
async def test_delete_driver_makes_no_call_when_validation_rejects() -> None:
    manifest = _manifest_with(_record(_D1))
    unrecorded_key = _canonical_key(_digest(b"never-created-by-this-run"))
    for bucket_name, keys in (
        (_OTHER_BUCKET, manifest.recorded_keys()),
        (_TEST_BUCKET, (unrecorded_key,)),
        (_TEST_BUCKET, (f"objects/sha256/{_D1[:2]}/{_D1[2:4]}/*",)),
        (_TEST_BUCKET, (f"objects/sha256/{_D1[:2]}/{_D1[2:4]}/{_D1}extra",)),
    ):
        with pytest.raises(CleanupRejection):
            await run_exact_key_cleanup(
                manifest,
                bucket_name=bucket_name,
                keys=keys,
                delete_one=_boom_delete,
            )


@pytest.mark.asyncio
async def test_delete_driver_deletes_each_validated_key_exactly_once() -> None:
    manifest = _manifest_with(_record(_D1), _record(_D2))
    client = _RecordingDeleteClient()
    deleted = await run_exact_key_cleanup(
        manifest,
        bucket_name=_TEST_BUCKET,
        keys=manifest.recorded_keys(),
        delete_one=client.delete_object,
    )
    assert deleted == (_canonical_key(_D1), _canonical_key(_D2))
    assert client.deleted_keys == [_canonical_key(_D1), _canonical_key(_D2)]


def test_validating_a_subset_still_requires_each_key_to_be_recorded() -> None:
    manifest = _manifest_with(_record(_D1), _record(_D2))
    validated = validate_cleanup_deletions(
        manifest, bucket_name=_TEST_BUCKET, keys=(_canonical_key(_D2),)
    )
    assert validated == (_canonical_key(_D2),)


def test_recording_the_same_key_twice_keeps_one_entry() -> None:
    manifest = _manifest_with(_record(_D1), _record(_D1))
    assert manifest.recorded_keys() == (_canonical_key(_D1),)
    assert manifest.record_for(_canonical_key(_D1)) == _record(_D1)


@pytest.mark.asyncio
async def test_fixture_removes_temp_spool_when_settings_loader_rejects(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Moving configuration loading outside the fixture guard leaks this root."""

    spool_root = tmp_path / "loader-rejected-spool"
    spool_root.mkdir()

    monkeypatch.setattr(
        live_conftest,
        "_require_live_configuration",
        lambda _environment: None,
    )
    monkeypatch.setattr(
        tempfile,
        "mkdtemp",
        lambda *, prefix: str(spool_root),
    )

    def _reject_configuration(_environment: Mapping[str, str], _spool_root: Path) -> object:
        raise RuntimeError("settings loader rejected the composed configuration")

    monkeypatch.setattr(live_conftest, "_load_live_configuration", _reject_configuration)

    fixture_function = cast(
        "Callable[[], AsyncIterator[live_conftest.LiveR2Harness]]",
        inspect.unwrap(live_conftest.live_r2_harness),
    )
    fixture_iterator = fixture_function()
    with pytest.raises(RuntimeError, match="settings loader rejected"):
        await anext(fixture_iterator)

    assert not spool_root.exists()
