"""Private local immutable filesystem recovery bundle store (spec 8.1, 8.3, 10).

:class:`FilesystemRecoveryBundleStore` stages each bundle in an unguessable
sibling directory beneath the configured backup root, creates every file
exclusively with per-file flush and fsync, writes the manifest and its digest
sidecar last, and publishes the bundle with one atomic same-filesystem rename.
Offline verification follows the spec 10 order exactly: boundary, final
directory type, exact registered tree, sidecar grammar and digest, strict
manifest parse, dump and object streaming digests in bounded 1 MiB chunks, and
totals — with changed-file detection via pre/post ``fstat`` identity and
hard-link aliasing rejected through an inode identity map.

POSIX-only behaviors (private permission bits, directory fsync) are guarded by
``os.name`` checks; Windows rejects reparse traversal through
:data:`stat.FILE_ATTRIBUTE_REPARSE_POINT` instead (spec 8.3). Raised
:class:`~personal_os.recovery.contracts.RecoveryError` details carry only
closed reason tokens — never a path, key, hash or raw content.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import shutil
import stat
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final, NamedTuple, NoReturn
from uuid import UUID

from personal_os.error_contracts.codes import ErrorCode
from personal_os.object_storage.keys import ContentDigest
from personal_os.recovery.contracts import (
    RecoveryBundleInvalidReason,
    RecoveryConfigurationReason,
    RecoveryError,
    RecoveryManifest,
)
from personal_os.recovery.manifest import (
    encode_manifest,
    manifest_digest,
    parse_manifest,
)
from personal_os.recovery.ports import RecoveryBundleWriter, VerifiedRecoveryBundle

#: Free-space reserve the backup root must retain before admission (2 GiB).
BACKUP_FREE_SPACE_RESERVE_BYTES: Final[int] = 2 * 1024**3

#: Prefix of the unguessable sibling staging directory names.
STAGING_NAME_PREFIX: Final[str] = ".staging-"

#: POSIX-only private directory and file permission bits (spec 8.3).
DIRECTORY_PERMISSIONS_POSIX: Final[int] = 0o700
FILE_PERMISSIONS_POSIX: Final[int] = 0o600

#: Every verification read streams in chunks of this size, never whole files.
STREAM_CHUNK_SIZE_BYTES: Final[int] = 1024 * 1024

_MANIFEST_RELATIVE_PATH: Final[str] = "manifest.json"
_SIDECAR_RELATIVE_PATH: Final[str] = "manifest.sha256"
_DUMP_RELATIVE_PATH: Final[str] = "postgres.dump"
_OBJECT_TREE_RELATIVE_PATH: Final[str] = "objects/sha256"
_SIDECAR_DIGEST_HEX_LENGTH: Final[int] = 64
_HEX_LOWER_BYTES: Final[frozenset[int]] = frozenset(b"0123456789abcdef")
_IS_POSIX: Final[bool] = os.name == "posix"


def _reject_bundle_invalid(reason: RecoveryBundleInvalidReason) -> NoReturn:
    raise RecoveryError(
        ErrorCode.CANONICAL_RECOVERY_BUNDLE_INVALID, safe_details={"reason": reason}
    )


def _reject_configuration_invalid(reason: RecoveryConfigurationReason) -> NoReturn:
    raise RecoveryError(
        ErrorCode.CANONICAL_RECOVERY_CONFIGURATION_INVALID, safe_details={"reason": reason}
    )


def _has_reparse_point(stat_result: os.stat_result) -> bool:
    """True when a ``stat`` result carries a Windows reparse-point attribute.

    The attribute exists only on Windows stat results; anywhere else the
    result has no reparse attribute and the predicate is ``False``.
    """

    return bool(getattr(stat_result, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _require_free_space(root: Path) -> None:
    if shutil.disk_usage(root).free < BACKUP_FREE_SPACE_RESERVE_BYTES:
        _reject_configuration_invalid(RecoveryConfigurationReason.FREE_SPACE_RESERVE)


def validate_backup_root(root: Path) -> Path:
    """Validate the configured backup root and return its resolved path.

    The root must be absolute, exist, be a directory and not be a symlink or
    reparse point, and retain the 2 GiB free-space reserve. The closed
    configuration enum has exactly one backup-root token, so every unusable
    root shape maps to ``backup_root_not_absolute`` and a low disk maps to
    ``free_space_reserve``.
    """

    if not root.is_absolute():
        _reject_configuration_invalid(RecoveryConfigurationReason.BACKUP_ROOT_NOT_ABSOLUTE)
    try:
        root_status = os.lstat(root)
    except OSError:
        _reject_configuration_invalid(RecoveryConfigurationReason.BACKUP_ROOT_NOT_ABSOLUTE)
    if (
        stat.S_ISLNK(root_status.st_mode)
        or _has_reparse_point(root_status)
        or not stat.S_ISDIR(root_status.st_mode)
    ):
        _reject_configuration_invalid(RecoveryConfigurationReason.BACKUP_ROOT_NOT_ABSOLUTE)
    _require_free_space(root)
    return root.resolve()


def _canonical_bundle_id_text(bundle_id: UUID) -> str:
    text = str(bundle_id)
    if bundle_id.version != 7 or UUID(text) != bundle_id or text != text.lower():
        _reject_bundle_invalid(RecoveryBundleInvalidReason.BUNDLE_ID_INVALID)
    return text


def _apply_directory_permissions(directory: Path) -> None:
    if _IS_POSIX:
        os.chmod(directory, DIRECTORY_PERMISSIONS_POSIX)


def _create_directories_private(directory: Path) -> None:
    missing_directories: list[Path] = []
    current = directory
    while not current.exists():
        missing_directories.append(current)
        current = current.parent
    for missing_directory in reversed(missing_directories):
        missing_directory.mkdir(mode=DIRECTORY_PERMISSIONS_POSIX)
        _apply_directory_permissions(missing_directory)


def _fsync_directory(directory: Path) -> None:
    if not _IS_POSIX:
        return
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_file_exclusively(path: Path, data: bytes) -> None:
    """Create ``path`` exclusively, write, flush and fsync (spec 8.1)."""

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags, FILE_PERMISSIONS_POSIX)
    try:
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(data)
            stream.flush()
        os.fsync(fd)
    finally:
        os.close(fd)
    if _IS_POSIX:
        os.chmod(path, FILE_PERMISSIONS_POSIX)


class _StreamedFileDigest(NamedTuple):
    """Digest, exact size and inode identity of one streamed verification read."""

    digest_hexadecimal: str
    size_bytes: int
    inode_identity: tuple[int, int]


def _inode_identity(status: os.stat_result) -> tuple[int, int]:
    return (status.st_dev, status.st_ino)


def _assert_file_identity_unchanged(before: os.stat_result, after: os.stat_result) -> None:
    if (
        before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or before.st_ino != after.st_ino
    ):
        _reject_bundle_invalid(RecoveryBundleInvalidReason.FILE_CHANGED)


def _require_regular_unaliased_file(status: os.stat_result) -> None:
    """Re-check type after open and reject hard-link aliasing (spec 8.3)."""

    if not stat.S_ISREG(status.st_mode):
        _reject_bundle_invalid(RecoveryBundleInvalidReason.FILE_TREE_MISMATCH)
    if status.st_nlink > 1:
        _reject_bundle_invalid(RecoveryBundleInvalidReason.FILE_TREE_MISMATCH)


def _open_regular_file_no_follow(path: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        return os.open(path, flags)
    except OSError:
        # A vanished or link-guarded target is a tree mismatch, never a raw
        # operating-system error carrying a path.
        _reject_bundle_invalid(RecoveryBundleInvalidReason.FILE_TREE_MISMATCH)


def _stream_file_digest(path: Path) -> _StreamedFileDigest:
    """Stream SHA-256 in bounded chunks with pre/post identity checking."""

    fd = _open_regular_file_no_follow(path)
    try:
        before = os.fstat(fd)
        _require_regular_unaliased_file(before)
        hasher = hashlib.sha256()
        size_bytes = 0
        while True:
            chunk = os.read(fd, STREAM_CHUNK_SIZE_BYTES)
            if not chunk:
                break
            hasher.update(chunk)
            size_bytes += len(chunk)
        _assert_file_identity_unchanged(before, os.fstat(fd))
        return _StreamedFileDigest(
            digest_hexadecimal=hasher.hexdigest(),
            size_bytes=size_bytes,
            inode_identity=_inode_identity(before),
        )
    finally:
        os.close(fd)


def _stream_file_bytes(path: Path) -> tuple[bytes, tuple[int, int]]:
    fd = _open_regular_file_no_follow(path)
    try:
        before = os.fstat(fd)
        _require_regular_unaliased_file(before)
        chunks: list[bytes] = []
        size_bytes = 0
        while chunk := os.read(fd, STREAM_CHUNK_SIZE_BYTES):
            chunks.append(chunk)
            size_bytes += len(chunk)
        _assert_file_identity_unchanged(before, os.fstat(fd))
        return b"".join(chunks), _inode_identity(before)
    finally:
        os.close(fd)


def _require_regular_tree_entry(path: Path, *, require_directory: bool) -> None:
    status = os.lstat(path)
    if stat.S_ISLNK(status.st_mode) or _has_reparse_point(status):
        _reject_bundle_invalid(RecoveryBundleInvalidReason.FILE_TREE_MISMATCH)
    if require_directory and not stat.S_ISDIR(status.st_mode):
        _reject_bundle_invalid(RecoveryBundleInvalidReason.FILE_TREE_MISMATCH)
    if not require_directory and not stat.S_ISREG(status.st_mode):
        _reject_bundle_invalid(RecoveryBundleInvalidReason.FILE_TREE_MISMATCH)


def _collect_bundle_file_tree(
    bundle_directory: Path,
) -> tuple[set[str], set[str]]:
    """Walk the bundle and collect relative POSIX file and directory paths.

    Symlinks, reparse points, device files and FIFOs anywhere in the tree are
    rejected during the walk itself (spec 8.3).
    """

    file_paths: set[str] = set()
    directory_paths: set[str] = set()
    for directory, dirnames, filenames in os.walk(bundle_directory, followlinks=False):
        for dirname in dirnames:
            path = Path(directory) / dirname
            _require_regular_tree_entry(path, require_directory=True)
            directory_paths.add(path.relative_to(bundle_directory).as_posix())
        for filename in filenames:
            path = Path(directory) / filename
            _require_regular_tree_entry(path, require_directory=False)
            file_paths.add(path.relative_to(bundle_directory).as_posix())
    return file_paths, directory_paths


def _registered_directory_paths(registered_files: set[str]) -> set[str]:
    directories: set[str] = set()
    for relative_path in registered_files:
        parent = PurePosixPath(relative_path).parent
        while parent.as_posix() != ".":
            directories.add(parent.as_posix())
            parent = parent.parent
    return directories


def _verify_sidecar(sidecar_bytes: bytes, manifest_bytes: bytes) -> None:
    """Sidecar grammar (64 lowercase hex + newline) and exact digest (spec 10.4)."""

    if (
        len(sidecar_bytes) != _SIDECAR_DIGEST_HEX_LENGTH + 1
        or not sidecar_bytes.endswith(b"\n")
        or not all(byte in _HEX_LOWER_BYTES for byte in sidecar_bytes[:64])
    ):
        _reject_bundle_invalid(RecoveryBundleInvalidReason.CHECKSUM_MISMATCH)
    if sidecar_bytes[:64].decode("ascii") != manifest_digest(manifest_bytes):
        _reject_bundle_invalid(RecoveryBundleInvalidReason.CHECKSUM_MISMATCH)


def _require_final_directory(bundle_directory: Path) -> None:
    """Final directory type check and staging-suffix rejection (spec 10.2)."""

    if bundle_directory.name.startswith(STAGING_NAME_PREFIX):
        _reject_bundle_invalid(RecoveryBundleInvalidReason.FILE_TREE_MISMATCH)
    try:
        status = os.lstat(bundle_directory)
    except OSError:
        _reject_bundle_invalid(RecoveryBundleInvalidReason.FILE_TREE_MISMATCH)
    if (
        stat.S_ISLNK(status.st_mode)
        or _has_reparse_point(status)
        or not stat.S_ISDIR(status.st_mode)
    ):
        _reject_bundle_invalid(RecoveryBundleInvalidReason.FILE_TREE_MISMATCH)


def _resolve_child_beneath_root(root: Path, child: Path) -> Path:
    """Resolve ``child`` and require it to stay beneath ``root`` (spec 8.3).

    Every existing component is checked with ``lstat`` so a symlink or
    reparse point anywhere on the path is rejected before resolution can
    escape the configured root.
    """

    resolved_child = child.resolve()
    if root not in resolved_child.parents:
        _reject_bundle_invalid(RecoveryBundleInvalidReason.FILE_TREE_MISMATCH)
    current = root
    for component in child.relative_to(root).parts:
        current = current / component
        if os.path.lexists(current):
            component_status = os.lstat(current)
            if stat.S_ISLNK(component_status.st_mode) or _has_reparse_point(component_status):
                _reject_bundle_invalid(RecoveryBundleInvalidReason.FILE_TREE_MISMATCH)
    return resolved_child


def _register_inode_identity(
    seen_identities: set[tuple[int, int]], identity: tuple[int, int]
) -> None:
    """Reject two registered paths sharing one inode (hard-link aliasing)."""

    if identity[1] == 0:
        # The platform cannot report an inode for this file; the per-file
        # link-count check remains the aliasing barrier.
        return
    if identity in seen_identities:
        _reject_bundle_invalid(RecoveryBundleInvalidReason.FILE_TREE_MISMATCH)
    seen_identities.add(identity)


class _StagingBundleWriter:
    """One invocation's staging writer owning exactly its staging directory."""

    def __init__(self, root: Path, bundle_id: UUID, staging_path: Path) -> None:
        self._root = root
        self._bundle_id = bundle_id
        self._staging_path = staging_path
        self._is_finalized = False
        self._is_abandoned = False
        self.dump_path: Path = staging_path / _DUMP_RELATIVE_PATH

    @property
    def is_finalized(self) -> bool:
        return self._is_finalized

    def object_path(self, content_sha256: str) -> Path:
        digest = ContentDigest.parse(content_sha256)
        hexadecimal = digest.hexadecimal
        path = self._staging_path.joinpath(
            _OBJECT_TREE_RELATIVE_PATH, hexadecimal[0:2], hexadecimal[2:4], hexadecimal
        )
        _create_directories_private(path.parent)
        return path

    async def write_dump(self, dump_bytes: bytes) -> None:
        _write_file_exclusively(self.dump_path, dump_bytes)

    async def write_object(self, content_sha256: str, object_bytes: bytes) -> None:
        if hashlib.sha256(object_bytes).hexdigest() != content_sha256:
            raise ValueError("object bytes do not match the requested content digest")
        _write_file_exclusively(self.object_path(content_sha256), object_bytes)

    async def finalize(self, manifest: RecoveryManifest) -> None:
        if self._is_finalized or self._is_abandoned:
            raise RuntimeError("staging writer is already closed")
        if manifest.bundle_id != self._bundle_id:
            _reject_bundle_invalid(RecoveryBundleInvalidReason.BUNDLE_ID_INVALID)
        final_path = self._root / _canonical_bundle_id_text(self._bundle_id)
        try:
            manifest_bytes = encode_manifest(manifest)
            _write_file_exclusively(self._staging_path / _MANIFEST_RELATIVE_PATH, manifest_bytes)
            sidecar_bytes = f"{manifest_digest(manifest_bytes)}\n".encode("ascii")
            _write_file_exclusively(self._staging_path / _SIDECAR_RELATIVE_PATH, sidecar_bytes)
            _fsync_directory(self._staging_path)
            if os.path.lexists(final_path):
                raise RecoveryError(
                    ErrorCode.CANONICAL_RECOVERY_BUNDLE_EXISTS,
                    safe_details={"bundle_id": self._bundle_id},
                )
            # Every file handle was flushed and fsynced when it was written,
            # so the same-volume rename below is safe on Windows as well
            # (spec 8.1); crash consistency beyond that is deployment-owned.
            os.rename(self._staging_path, final_path)
        except BaseException:
            self._remove_staging()
            raise
        _fsync_directory(self._root)
        self._is_finalized = True

    async def abandon(self) -> None:
        self._remove_staging()

    def _remove_staging(self) -> None:
        if self._is_finalized or self._is_abandoned:
            return
        is_owned_staging = (
            self._staging_path.parent == self._root
            and self._staging_path.name.startswith(STAGING_NAME_PREFIX)
            and not self._staging_path.is_symlink()
        )
        if is_owned_staging and self._staging_path.exists():
            shutil.rmtree(self._staging_path, ignore_errors=False)
        self._is_abandoned = True


@dataclass(slots=True)
class _VerifiedFilesystemBundle:
    """A verified bundle view whose paths already passed offline verification.

    Not frozen: the port declares writable attributes and structural
    compatibility with ``VerifiedRecoveryBundle`` requires matching members.
    """

    manifest: RecoveryManifest
    dump_path: Path
    bundle_directory: Path

    def object_path(self, content_sha256: str) -> Path:
        digest = ContentDigest.parse(content_sha256)
        hexadecimal = digest.hexadecimal
        return self.bundle_directory.joinpath(
            _OBJECT_TREE_RELATIVE_PATH, hexadecimal[0:2], hexadecimal[2:4], hexadecimal
        )


class FilesystemRecoveryBundleStore:
    """Immutable local bundle store behind the ``RecoveryBundleStore`` port."""

    def __init__(self, root: Path) -> None:
        self._root = validate_backup_root(root)

    def bundle_exists(self, bundle_id: UUID) -> bool:
        """True when the final bundle directory name is occupied (never staging)."""

        return os.path.lexists(self._root / _canonical_bundle_id_text(bundle_id))

    def create_staging(self, bundle_id: UUID) -> AbstractAsyncContextManager[RecoveryBundleWriter]:
        """Admit and stage one bundle in an unguessable sibling directory.

        Admission — canonical UUIDv7, free-space reserve, absent final target —
        happens before anything is created; the staging directory itself is
        created when the context is entered and removed on any exit without a
        successful ``finalize``.
        """

        bundle_text = _canonical_bundle_id_text(bundle_id)
        _require_free_space(self._root)
        if os.path.lexists(self._root / bundle_text):
            raise RecoveryError(
                ErrorCode.CANONICAL_RECOVERY_BUNDLE_EXISTS,
                safe_details={"bundle_id": bundle_id},
            )
        return self._staging_writer_context(bundle_id, bundle_text)

    @asynccontextmanager
    async def _staging_writer_context(
        self, bundle_id: UUID, bundle_text: str
    ) -> AsyncIterator[_StagingBundleWriter]:
        staging_path = self._root / f"{STAGING_NAME_PREFIX}{bundle_text}-{secrets.token_hex(16)}"
        staging_path.mkdir(mode=DIRECTORY_PERMISSIONS_POSIX)
        _apply_directory_permissions(staging_path)
        writer = _StagingBundleWriter(self._root, bundle_id, staging_path)
        try:
            yield writer
        finally:
            if not writer.is_finalized:
                writer._remove_staging()

    def open_verified(self, bundle_id: UUID) -> AbstractAsyncContextManager[VerifiedRecoveryBundle]:
        return self._verified_bundle_context(bundle_id)

    @asynccontextmanager
    async def _verified_bundle_context(
        self, bundle_id: UUID
    ) -> AsyncIterator[_VerifiedFilesystemBundle]:
        manifest = self.verify_offline(bundle_id)
        bundle_directory = self._root / _canonical_bundle_id_text(bundle_id)
        yield _VerifiedFilesystemBundle(
            manifest=manifest,
            dump_path=bundle_directory / _DUMP_RELATIVE_PATH,
            bundle_directory=bundle_directory,
        )

    def verify_offline(self, bundle_id: UUID) -> RecoveryManifest:
        """Verify one bundle offline in the exact spec 10 order.

        No PostgreSQL, R2 or Temporal call happens here. Every failure raises
        ``RecoveryError`` with a closed
        :class:`~personal_os.recovery.contracts.RecoveryBundleInvalidReason`
        token; nothing about paths, keys or digests is disclosed.
        """

        bundle_directory = self._root / _canonical_bundle_id_text(bundle_id)
        _resolve_child_beneath_root(self._root, bundle_directory)
        _require_final_directory(bundle_directory)
        file_tree, directory_tree = _collect_bundle_file_tree(bundle_directory)
        if _MANIFEST_RELATIVE_PATH not in file_tree or _DUMP_RELATIVE_PATH not in file_tree:
            _reject_bundle_invalid(RecoveryBundleInvalidReason.FILE_TREE_MISMATCH)
        if _SIDECAR_RELATIVE_PATH not in file_tree:
            _reject_bundle_invalid(RecoveryBundleInvalidReason.SIDECAR_MISSING)

        manifest_bytes, manifest_identity = _stream_file_bytes(
            bundle_directory / _MANIFEST_RELATIVE_PATH
        )
        sidecar_bytes, sidecar_identity = _stream_file_bytes(
            bundle_directory / _SIDECAR_RELATIVE_PATH
        )
        _verify_sidecar(sidecar_bytes, manifest_bytes)

        manifest = parse_manifest(manifest_bytes)
        if manifest.bundle_id != bundle_id:
            _reject_bundle_invalid(RecoveryBundleInvalidReason.BUNDLE_ID_INVALID)
        registered_files = {
            _MANIFEST_RELATIVE_PATH,
            _SIDECAR_RELATIVE_PATH,
            _DUMP_RELATIVE_PATH,
        } | {entry.relative_path for entry in manifest.objects}
        if file_tree != registered_files:
            _reject_bundle_invalid(RecoveryBundleInvalidReason.FILE_TREE_MISMATCH)
        if directory_tree - _registered_directory_paths(registered_files):
            _reject_bundle_invalid(RecoveryBundleInvalidReason.FILE_TREE_MISMATCH)

        seen_identities: set[tuple[int, int]] = set()
        _register_inode_identity(seen_identities, manifest_identity)
        _register_inode_identity(seen_identities, sidecar_identity)

        dump_result = _stream_file_digest(bundle_directory / _DUMP_RELATIVE_PATH)
        if (
            dump_result.size_bytes != manifest.postgres_dump.size_bytes
            or dump_result.digest_hexadecimal != manifest.postgres_dump.sha256
        ):
            _reject_bundle_invalid(RecoveryBundleInvalidReason.CHECKSUM_MISMATCH)
        _register_inode_identity(seen_identities, dump_result.inode_identity)

        object_count = 0
        object_bytes_total = 0
        for entry in manifest.objects:
            object_path = bundle_directory.joinpath(*PurePosixPath(entry.relative_path).parts)
            object_result = _stream_file_digest(object_path)
            if (
                object_result.size_bytes != entry.size_bytes
                or object_result.digest_hexadecimal != entry.content_sha256
            ):
                _reject_bundle_invalid(RecoveryBundleInvalidReason.CHECKSUM_MISMATCH)
            _register_inode_identity(seen_identities, object_result.inode_identity)
            object_count += 1
            object_bytes_total += object_result.size_bytes

        if object_count != len(manifest.objects) or object_bytes_total != sum(
            entry.size_bytes for entry in manifest.objects
        ):
            _reject_bundle_invalid(RecoveryBundleInvalidReason.CHECKSUM_MISMATCH)
        return manifest
