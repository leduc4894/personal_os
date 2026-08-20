"""Offline bundle verification attacks: escapes, links, extras and mutation (spec 8.3, 10).

Every rejection must be a closed-token ``RecoveryError`` reason from
``RecoveryBundleInvalidReason`` — never a path, key, hash or raw content.
POSIX-only scenarios (symlinks, hard links) skip when the platform cannot
create them; the Windows reparse-point predicate is covered with a fake stat
result on every platform.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import stat
import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest

from personal_os.error_contracts.codes import ErrorCode
from personal_os.recovery import bundle as bundle_module
from personal_os.recovery.bundle import STAGING_NAME_PREFIX, FilesystemRecoveryBundleStore
from personal_os.recovery.contracts import (
    CANONICAL_COUNT_TABLES,
    POSTGRESQL_SCHEMA_REVISION,
    ManifestDumpEntry,
    ManifestObjectEntry,
    RecoveryEnvironment,
    RecoveryError,
    RecoveryManifest,
)

_BUNDLE_ID = UUID("018f6b1e-8a2c-7d3e-9f01-2a3b4c5d6e7f")
_CREATED_AT = datetime(2026, 8, 15, 12, 30, 45, 123456)
_DUMP_BYTES = b"canonical-pg-dump-bytes"
_OBJECT_PAYLOADS = (b"first-canonical-object", b"second-canonical-object")


async def _write_valid_bundle(bundle_root: Path) -> Path:
    digested = sorted(
        (hashlib.sha256(payload).hexdigest(), payload) for payload in _OBJECT_PAYLOADS
    )
    objects = tuple(
        ManifestObjectEntry(
            content_sha256=digest,
            object_key=f"objects/sha256/{digest[0:2]}/{digest[2:4]}/{digest}",
            size_bytes=len(payload),
            media_type="application/octet-stream",
            relative_path=f"objects/sha256/{digest[0:2]}/{digest[2:4]}/{digest}",
        )
        for digest, payload in digested
    )
    manifest = RecoveryManifest(
        bundle_id=_BUNDLE_ID,
        created_at=_CREATED_AT,
        source_environment=RecoveryEnvironment.LOCAL,
        postgresql_server_version="18.4",
        postgresql_schema_revision=POSTGRESQL_SCHEMA_REVISION,
        postgres_dump=ManifestDumpEntry(
            relative_path="postgres.dump",
            size_bytes=len(_DUMP_BYTES),
            sha256=hashlib.sha256(_DUMP_BYTES).hexdigest(),
        ),
        canonical_counts={table: index + 1 for index, table in enumerate(CANONICAL_COUNT_TABLES)},
        objects=objects,
    )
    store = FilesystemRecoveryBundleStore(bundle_root)
    async with store.create_staging(_BUNDLE_ID) as writer:
        staging_writer = cast(Any, writer)
        await staging_writer.write_dump(_DUMP_BYTES)
        for payload in _OBJECT_PAYLOADS:
            await staging_writer.write_object(hashlib.sha256(payload).hexdigest(), payload)
        await staging_writer.finalize(manifest)
    return bundle_root / str(_BUNDLE_ID)


def _object_paths(final: Path) -> list[Path]:
    digests = sorted(hashlib.sha256(payload).hexdigest() for payload in _OBJECT_PAYLOADS)
    return [final / "objects" / "sha256" / digest[0:2] / digest[2:4] / digest for digest in digests]


def _assert_verify_rejected(bundle_root: Path, *, reason: str) -> None:
    store = FilesystemRecoveryBundleStore(bundle_root)
    with pytest.raises(RecoveryError) as exc_info:
        store.verify_offline(_BUNDLE_ID)

    error = exc_info.value
    assert error.error_code == ErrorCode.CANONICAL_RECOVERY_BUNDLE_INVALID
    assert error.safe_details["reason"] == reason


def _try_symlink_or_skip(target: Path, link: Path) -> None:
    try:
        os.symlink(target, link)
    except OSError, NotImplementedError:
        pytest.skip("symlink creation is unavailable on this platform")


def test_resolved_child_escaping_root_rejected(bundle_root: Path, tmp_path: Path) -> None:
    final = asyncio.run(_write_valid_bundle(bundle_root))

    outside = tmp_path / "outside"
    outside.mkdir()
    os.rename(final, outside / "stolen-bundle")
    _try_symlink_or_skip(outside / "stolen-bundle", final)

    _assert_verify_rejected(bundle_root, reason="file_tree_mismatch")


@pytest.mark.skipif(os.name != "posix", reason="POSIX-only symlinked object scenario")
def test_symlinked_object_rejected(bundle_root: Path, tmp_path: Path) -> None:
    final = asyncio.run(_write_valid_bundle(bundle_root))

    target = tmp_path / "external-object.bin"
    target.write_bytes(_OBJECT_PAYLOADS[0])
    object_path = _object_paths(final)[0]
    object_path.unlink()
    _try_symlink_or_skip(target, object_path)

    _assert_verify_rejected(bundle_root, reason="file_tree_mismatch")


def test_extra_unregistered_file_rejected(bundle_root: Path) -> None:
    final = asyncio.run(_write_valid_bundle(bundle_root))
    (final / "notes.txt").write_text("unregistered", encoding="utf-8")

    _assert_verify_rejected(bundle_root, reason="file_tree_mismatch")


def test_extra_unregistered_directory_rejected(bundle_root: Path) -> None:
    final = asyncio.run(_write_valid_bundle(bundle_root))
    (final / "extra-empty-dir").mkdir()

    _assert_verify_rejected(bundle_root, reason="file_tree_mismatch")


def test_missing_object_file_rejected(bundle_root: Path) -> None:
    final = asyncio.run(_write_valid_bundle(bundle_root))
    _object_paths(final)[0].unlink()

    _assert_verify_rejected(bundle_root, reason="file_tree_mismatch")


def test_modified_manifest_rejected_by_sidecar(bundle_root: Path) -> None:
    final = asyncio.run(_write_valid_bundle(bundle_root))
    manifest_path = final / "manifest.json"
    manifest_path.write_bytes(manifest_path.read_bytes() + b" ")

    _assert_verify_rejected(bundle_root, reason="checksum_mismatch")


def test_modified_dump_rejected_by_checksum(bundle_root: Path) -> None:
    final = asyncio.run(_write_valid_bundle(bundle_root))
    (final / "postgres.dump").write_bytes(b"x" * len(_DUMP_BYTES))

    _assert_verify_rejected(bundle_root, reason="checksum_mismatch")


def test_modified_object_rejected_by_streaming_checksum(bundle_root: Path) -> None:
    final = asyncio.run(_write_valid_bundle(bundle_root))
    object_path = _object_paths(final)[0]
    original = object_path.read_bytes()
    object_path.write_bytes(b"y" * len(original))

    _assert_verify_rejected(bundle_root, reason="checksum_mismatch")


def test_missing_sidecar_rejected(bundle_root: Path) -> None:
    final = asyncio.run(_write_valid_bundle(bundle_root))
    (final / "manifest.sha256").unlink()

    _assert_verify_rejected(bundle_root, reason="sidecar_missing")


def test_staging_suffix_directory_rejected_by_verify(bundle_root: Path) -> None:
    staging_like = bundle_root / f"{STAGING_NAME_PREFIX}{_BUNDLE_ID}-deadbeefdeadbeef"
    staging_like.mkdir()

    with pytest.raises(RecoveryError) as exc_info:
        bundle_module._require_final_directory(staging_like)

    error = exc_info.value
    assert error.error_code == ErrorCode.CANONICAL_RECOVERY_BUNDLE_INVALID
    assert error.safe_details["reason"] == "file_tree_mismatch"


def test_bundle_directory_of_wrong_type_rejected(bundle_root: Path) -> None:
    (bundle_root / str(_BUNDLE_ID)).write_text("not a directory", encoding="utf-8")

    _assert_verify_rejected(bundle_root, reason="file_tree_mismatch")


def test_directory_bundle_id_must_be_canonical_uuid_string(bundle_root: Path) -> None:
    non_version_7 = uuid.uuid4()

    store = FilesystemRecoveryBundleStore(bundle_root)
    with pytest.raises(RecoveryError) as exc_info:
        store.create_staging(non_version_7)

    error = exc_info.value
    assert error.error_code == ErrorCode.CANONICAL_RECOVERY_BUNDLE_INVALID
    assert error.safe_details["reason"] == "bundle_id_invalid"

    with pytest.raises(RecoveryError) as exc_info:
        store.verify_offline(non_version_7)

    assert exc_info.value.safe_details["reason"] == "bundle_id_invalid"
    assert list(bundle_root.iterdir()) == []


def test_changed_file_during_verification_detected(
    bundle_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    final = asyncio.run(_write_valid_bundle(bundle_root))
    target = final / "manifest.json"

    real_read = os.read
    mutated = False

    def mutating_read(fd: int, size: int) -> bytes:
        nonlocal mutated
        data = real_read(fd, size)
        if data and not mutated:
            mutated = True
            current_mtime_ns = os.stat(target).st_mtime_ns
            os.utime(target, ns=(current_mtime_ns + 1_000_000, current_mtime_ns + 1_000_000))
        return data

    monkeypatch.setattr(os, "read", mutating_read)

    _assert_verify_rejected(bundle_root, reason="file_changed")


def test_hard_linked_object_aliasing_rejected(bundle_root: Path) -> None:
    final = asyncio.run(_write_valid_bundle(bundle_root))
    first, second = _object_paths(final)
    second.unlink()
    try:
        os.link(first, second)
    except OSError:
        pytest.skip("hard-link creation is unavailable on this platform")

    _assert_verify_rejected(bundle_root, reason="file_tree_mismatch")


def test_has_reparse_point_detects_reparse_attribute() -> None:
    reparse = SimpleNamespace(st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
    normal = SimpleNamespace(st_file_attributes=stat.FILE_ATTRIBUTE_NORMAL)
    absent = SimpleNamespace()

    assert bundle_module._has_reparse_point(cast(os.stat_result, reparse)) is True
    assert bundle_module._has_reparse_point(cast(os.stat_result, normal)) is False
    assert bundle_module._has_reparse_point(cast(os.stat_result, absent)) is False
