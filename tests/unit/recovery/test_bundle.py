"""Filesystem recovery bundle writer round trips, admission and layout (spec 8.1).

The writer stages into an unguessable sibling directory, writes the manifest
and its sidecar last, and publishes by claiming the final directory with one
exclusive mkdir before moving each staged entry into it — so any pre-existing
final path (file, empty directory or bundle), whenever it appeared, fails
closed without clobbering it. Every failure must surface as a closed-token
``RecoveryError`` — never a path, key or hash.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest

from personal_os.error_contracts.codes import ErrorCode
from personal_os.recovery.bundle import (
    BACKUP_FREE_SPACE_RESERVE_BYTES,
    STAGING_NAME_PREFIX,
    FilesystemRecoveryBundleStore,
    validate_backup_root,
)
from personal_os.recovery.contracts import (
    CANONICAL_COUNT_TABLES,
    POSTGRESQL_SCHEMA_REVISION,
    ManifestDumpEntry,
    ManifestObjectEntry,
    RecoveryEnvironment,
    RecoveryError,
    RecoveryManifest,
)

# A fixed valid UUIDv7 (version nibble 7, RFC 4122 variant), spec 8.1.
_BUNDLE_ID = UUID("018f6b1e-8a2c-7d3e-9f01-2a3b4c5d6e7f")
_CREATED_AT = datetime(2026, 8, 15, 12, 30, 45, 123456)
_DUMP_BYTES = b"canonical-pg-dump-bytes"
_OBJECT_PAYLOADS = (b"first-canonical-object", b"second-canonical-object")


def _object_entry(digest_hexadecimal: str, payload: bytes) -> ManifestObjectEntry:
    object_key = (
        f"objects/sha256/{digest_hexadecimal[0:2]}/{digest_hexadecimal[2:4]}/{digest_hexadecimal}"
    )
    return ManifestObjectEntry(
        content_sha256=digest_hexadecimal,
        object_key=object_key,
        size_bytes=len(payload),
        media_type="application/octet-stream",
        relative_path=object_key,
    )


def _build_manifest(
    dump_bytes: bytes = _DUMP_BYTES,
    payloads: tuple[bytes, ...] = _OBJECT_PAYLOADS,
) -> RecoveryManifest:
    digested = sorted((hashlib.sha256(payload).hexdigest(), payload) for payload in payloads)
    return RecoveryManifest(
        bundle_id=_BUNDLE_ID,
        created_at=_CREATED_AT,
        source_environment=RecoveryEnvironment.LOCAL,
        postgresql_server_version="18.4",
        postgresql_schema_revision=POSTGRESQL_SCHEMA_REVISION,
        postgres_dump=ManifestDumpEntry(
            relative_path="postgres.dump",
            size_bytes=len(dump_bytes),
            sha256=hashlib.sha256(dump_bytes).hexdigest(),
        ),
        canonical_counts={table: index + 1 for index, table in enumerate(CANONICAL_COUNT_TABLES)},
        objects=tuple(_object_entry(digest, payload) for digest, payload in digested),
    )


async def _write_bundle(
    bundle_root: Path,
    *,
    dump_bytes: bytes = _DUMP_BYTES,
    payloads: tuple[bytes, ...] = _OBJECT_PAYLOADS,
) -> RecoveryManifest:
    store = FilesystemRecoveryBundleStore(bundle_root)
    manifest = _build_manifest(dump_bytes, payloads)
    async with store.create_staging(_BUNDLE_ID) as writer:
        staging_writer = cast(Any, writer)
        await staging_writer.write_dump(dump_bytes)
        for payload in payloads:
            await staging_writer.write_object(hashlib.sha256(payload).hexdigest(), payload)
        await staging_writer.finalize(manifest)
    return manifest


@pytest.mark.asyncio
async def test_create_then_verify_round_trip(bundle_root: Path) -> None:
    manifest = await _write_bundle(bundle_root)

    store = FilesystemRecoveryBundleStore(bundle_root)
    assert store.bundle_exists(_BUNDLE_ID) is True

    verified = store.verify_offline(_BUNDLE_ID)
    assert verified.bundle_id == manifest.bundle_id
    assert verified.postgres_dump == manifest.postgres_dump
    assert dict(verified.canonical_counts) == dict(manifest.canonical_counts)
    assert verified.objects == manifest.objects

    async with store.open_verified(_BUNDLE_ID) as bundle:
        assert bundle.manifest.bundle_id == _BUNDLE_ID
        assert bundle.dump_path.read_bytes() == _DUMP_BYTES
        digest = hashlib.sha256(_OBJECT_PAYLOADS[0]).hexdigest()
        expected = (
            bundle_root.resolve()
            / str(_BUNDLE_ID)
            / "objects"
            / "sha256"
            / digest[0:2]
            / digest[2:4]
            / digest
        )
        assert bundle.object_path(digest) == expected
        assert bundle.object_path(digest).read_bytes() == _OBJECT_PAYLOADS[0]


@pytest.mark.asyncio
async def test_creation_fails_when_final_directory_exists(bundle_root: Path) -> None:
    (bundle_root / str(_BUNDLE_ID)).mkdir()

    store = FilesystemRecoveryBundleStore(bundle_root)
    with pytest.raises(RecoveryError) as exc_info:
        async with store.create_staging(_BUNDLE_ID):
            pass

    error = exc_info.value
    assert error.error_code == ErrorCode.CANONICAL_RECOVERY_BUNDLE_EXISTS
    assert error.safe_details["bundle_id"] == _BUNDLE_ID
    assert not [path for path in bundle_root.iterdir() if path.name.startswith(STAGING_NAME_PREFIX)]


@pytest.mark.asyncio
async def test_finalize_renames_staging_away_and_staging_no_longer_exists(
    bundle_root: Path,
) -> None:
    await _write_bundle(bundle_root)

    final = bundle_root / str(_BUNDLE_ID)
    assert final.is_dir()
    assert sorted(path.name for path in bundle_root.iterdir()) == [str(_BUNDLE_ID)]


async def _stage_valid_writer_payloads(staging_writer: Any) -> None:
    await staging_writer.write_dump(_DUMP_BYTES)
    for payload in _OBJECT_PAYLOADS:
        await staging_writer.write_object(hashlib.sha256(payload).hexdigest(), payload)


@pytest.mark.asyncio
async def test_finalize_refuses_an_empty_preexisting_final_directory(
    bundle_root: Path,
) -> None:
    store = FilesystemRecoveryBundleStore(bundle_root)
    final_path = bundle_root / str(_BUNDLE_ID)

    async with store.create_staging(_BUNDLE_ID) as writer:
        staging_writer = cast(Any, writer)
        await _stage_valid_writer_payloads(staging_writer)
        final_path.mkdir()
        with pytest.raises(RecoveryError) as exc_info:
            await staging_writer.finalize(_build_manifest())

    error = exc_info.value
    assert error.error_code == ErrorCode.CANONICAL_RECOVERY_BUNDLE_EXISTS
    assert error.safe_details["bundle_id"] == _BUNDLE_ID
    # Nothing clobbered: the empty directory survives exactly as it was.
    assert final_path.is_dir()
    assert list(final_path.iterdir()) == []
    assert not [path for path in bundle_root.iterdir() if path.name.startswith(STAGING_NAME_PREFIX)]


@pytest.mark.asyncio
async def test_finalize_refuses_a_nonempty_preexisting_final_directory(
    bundle_root: Path,
) -> None:
    store = FilesystemRecoveryBundleStore(bundle_root)
    final_path = bundle_root / str(_BUNDLE_ID)

    async with store.create_staging(_BUNDLE_ID) as writer:
        staging_writer = cast(Any, writer)
        await _stage_valid_writer_payloads(staging_writer)
        final_path.mkdir()
        (final_path / "sentinel.txt").write_text("keep", encoding="utf-8")
        with pytest.raises(RecoveryError) as exc_info:
            await staging_writer.finalize(_build_manifest())

    error = exc_info.value
    assert error.error_code == ErrorCode.CANONICAL_RECOVERY_BUNDLE_EXISTS
    assert error.safe_details["bundle_id"] == _BUNDLE_ID
    # Nothing clobbered: the occupied directory and its content survive.
    assert (final_path / "sentinel.txt").read_text(encoding="utf-8") == "keep"
    assert sorted(path.name for path in final_path.iterdir()) == ["sentinel.txt"]
    assert not [path for path in bundle_root.iterdir() if path.name.startswith(STAGING_NAME_PREFIX)]


@pytest.mark.asyncio
async def test_finalize_refuses_a_preexisting_final_file(bundle_root: Path) -> None:
    store = FilesystemRecoveryBundleStore(bundle_root)
    final_path = bundle_root / str(_BUNDLE_ID)

    async with store.create_staging(_BUNDLE_ID) as writer:
        staging_writer = cast(Any, writer)
        await _stage_valid_writer_payloads(staging_writer)
        final_path.write_text("occupied", encoding="utf-8")
        with pytest.raises(RecoveryError) as exc_info:
            await staging_writer.finalize(_build_manifest())

    error = exc_info.value
    assert error.error_code == ErrorCode.CANONICAL_RECOVERY_BUNDLE_EXISTS
    assert error.safe_details["bundle_id"] == _BUNDLE_ID
    # Nothing clobbered: the occupying file survives untouched.
    assert final_path.read_text(encoding="utf-8") == "occupied"
    assert not [path for path in bundle_root.iterdir() if path.name.startswith(STAGING_NAME_PREFIX)]


@pytest.mark.asyncio
async def test_finalize_fails_closed_when_the_final_path_appears_after_admission(
    bundle_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The publish claim must close the probe-to-publish race (spec 8.1).

    A racer occupying the final path between the admission probe and the
    publish must never be clobbered: with a rename-based publish the empty
    directory silently replaced it on POSIX and surfaced a raw
    ``FileExistsError`` on Windows instead of the closed exists token.
    """

    store = FilesystemRecoveryBundleStore(bundle_root)
    final_path = bundle_root / str(_BUNDLE_ID)
    real_lexists = os.path.lexists

    def lexists_with_racer_winning_the_probe(path: object) -> bool:
        probed = Path(str(path))
        if probed != final_path:
            return real_lexists(path)
        if not final_path.exists():
            final_path.mkdir()  # the racer occupies the final path mid-flight
        return False  # every probe misses the race by construction

    monkeypatch.setattr(os.path, "lexists", lexists_with_racer_winning_the_probe)

    with pytest.raises(RecoveryError) as exc_info:
        async with store.create_staging(_BUNDLE_ID) as writer:
            staging_writer = cast(Any, writer)
            await _stage_valid_writer_payloads(staging_writer)
            await staging_writer.finalize(_build_manifest())

    error = exc_info.value
    assert error.error_code == ErrorCode.CANONICAL_RECOVERY_BUNDLE_EXISTS
    assert error.safe_details["bundle_id"] == _BUNDLE_ID
    # The racer's empty directory survives and the failed writer cleans up.
    assert final_path.is_dir()
    assert list(final_path.iterdir()) == []
    assert not [path for path in bundle_root.iterdir() if path.name.startswith(STAGING_NAME_PREFIX)]


@pytest.mark.asyncio
async def test_finalize_rolls_back_the_claimed_directory_when_a_move_fails(
    bundle_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed per-entry move leaves neither a partial bundle nor staging."""

    store = FilesystemRecoveryBundleStore(bundle_root)
    final_path = bundle_root / str(_BUNDLE_ID)
    real_rename = os.rename
    move_attempts = 0

    def rename_failing_the_second_move(
        source: str | os.PathLike[str], destination: str | os.PathLike[str]
    ) -> None:
        nonlocal move_attempts
        move_attempts += 1
        if move_attempts == 2:
            raise OSError("simulated move failure")
        real_rename(source, destination)

    monkeypatch.setattr(os, "rename", rename_failing_the_second_move)

    with pytest.raises(OSError, match="simulated move failure"):
        async with store.create_staging(_BUNDLE_ID) as writer:
            staging_writer = cast(Any, writer)
            await _stage_valid_writer_payloads(staging_writer)
            await staging_writer.finalize(_build_manifest())

    assert not final_path.exists()
    assert list(bundle_root.iterdir()) == []


@pytest.mark.asyncio
async def test_abandon_removes_exactly_the_staging_directory(bundle_root: Path) -> None:
    decoy_staging = bundle_root / f"{STAGING_NAME_PREFIX}other-bundle-0000000000000000"
    decoy_staging.mkdir()
    unrelated = bundle_root / "unrelated.txt"
    unrelated.write_text("keep", encoding="utf-8")

    store = FilesystemRecoveryBundleStore(bundle_root)
    async with store.create_staging(_BUNDLE_ID) as writer:
        staging_writer = cast(Any, writer)
        await staging_writer.write_dump(b"partial")
        await staging_writer.abandon()

    assert decoy_staging.is_dir()
    assert unrelated.read_text(encoding="utf-8") == "keep"
    assert not [
        path
        for path in bundle_root.iterdir()
        if path.name.startswith(f"{STAGING_NAME_PREFIX}{_BUNDLE_ID}")
    ]


@pytest.mark.asyncio
async def test_manifest_written_last_and_sidecar_matches_digest(bundle_root: Path) -> None:
    store = FilesystemRecoveryBundleStore(bundle_root)
    manifest = _build_manifest()

    async with store.create_staging(_BUNDLE_ID) as writer:
        staging_writer = cast(Any, writer)
        await staging_writer.write_dump(_DUMP_BYTES)
        for payload in _OBJECT_PAYLOADS:
            await staging_writer.write_object(hashlib.sha256(payload).hexdigest(), payload)
        staging = staging_writer.dump_path.parent
        assert (staging / "manifest.json").exists() is False
        assert (staging / "manifest.sha256").exists() is False
        await staging_writer.finalize(manifest)

    final = bundle_root / str(_BUNDLE_ID)
    manifest_bytes = (final / "manifest.json").read_bytes()
    expected_digest = hashlib.sha256(manifest_bytes + b"\n").hexdigest()
    assert (final / "manifest.sha256").read_bytes() == f"{expected_digest}\n".encode("ascii")


@pytest.mark.asyncio
async def test_object_files_land_under_objects_sha256_first2_next2(bundle_root: Path) -> None:
    await _write_bundle(bundle_root)

    final = bundle_root / str(_BUNDLE_ID)
    for payload in _OBJECT_PAYLOADS:
        digest = hashlib.sha256(payload).hexdigest()
        path = final / "objects" / "sha256" / digest[0:2] / digest[2:4] / digest
        assert path.read_bytes() == payload


def test_validate_backup_root_rejects_relative_path() -> None:
    with pytest.raises(RecoveryError) as exc_info:
        validate_backup_root(Path("relative/backup-root"))

    error = exc_info.value
    assert error.error_code == ErrorCode.CANONICAL_RECOVERY_CONFIGURATION_INVALID
    assert error.safe_details["reason"] == "backup_root_not_absolute"


def test_validate_backup_root_rejects_missing_directory(tmp_path: Path) -> None:
    with pytest.raises(RecoveryError) as exc_info:
        validate_backup_root(tmp_path / "absent-root")

    error = exc_info.value
    assert error.error_code == ErrorCode.CANONICAL_RECOVERY_CONFIGURATION_INVALID
    assert error.safe_details["reason"] == "backup_root_not_absolute"


@pytest.mark.skipif(
    os.name != "posix", reason="symlinked roots are covered by reparse checks elsewhere"
)
def test_validate_backup_root_rejects_symlinked_root(tmp_path: Path) -> None:
    real_root = tmp_path / "real-root"
    real_root.mkdir()
    link = tmp_path / "linked-root"
    os.symlink(real_root, link)

    with pytest.raises(RecoveryError) as exc_info:
        validate_backup_root(link)

    error = exc_info.value
    assert error.error_code == ErrorCode.CANONICAL_RECOVERY_CONFIGURATION_INVALID
    assert error.safe_details["reason"] == "backup_root_not_absolute"


@pytest.mark.asyncio
async def test_admission_checks_free_space_reserve_before_first_copy(
    bundle_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FilesystemRecoveryBundleStore(bundle_root)

    def fake_disk_usage(path: object) -> object:
        del path
        return SimpleNamespace(total=10**12, used=0, free=BACKUP_FREE_SPACE_RESERVE_BYTES - 1)

    monkeypatch.setattr(shutil, "disk_usage", fake_disk_usage)

    with pytest.raises(RecoveryError) as exc_info:
        async with store.create_staging(_BUNDLE_ID):
            pass

    error = exc_info.value
    assert error.error_code == ErrorCode.CANONICAL_RECOVERY_CONFIGURATION_INVALID
    assert error.safe_details["reason"] == "free_space_reserve"
    assert list(bundle_root.iterdir()) == []


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits are not observable elsewhere")
@pytest.mark.asyncio
async def test_posix_bundle_permissions_are_private(bundle_root: Path) -> None:
    await _write_bundle(bundle_root)

    final = bundle_root / str(_BUNDLE_ID)
    assert (final.stat().st_mode & 0o777) == 0o700
    for path in sorted(final.rglob("*")):
        mode = path.stat().st_mode & 0o777
        if path.is_dir():
            assert mode == 0o700
        else:
            assert mode == 0o600
