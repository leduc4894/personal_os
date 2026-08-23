"""Bounded secure content spooling for the R2 object-storage adapter.

The spool manager receives an asynchronous byte stream into a private local
file under fixed resource limits while computing the SHA-256 content identity
and the MD5 transport guard. It owns spool-file creation, admission
(backpressure over permits, the process reservation budget and the filesystem
free-space reserve), the receive deadline and exact cleanup in success,
failure and cancellation paths. Spool paths, digests and content are never
logged, serialized into errors or otherwise disclosed.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shutil
import stat
import time
import uuid
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable, Coroutine
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

from personal_os.error_contracts.codes import ErrorCode
from personal_os.object_storage.contracts import ExpectedObject
from personal_os.object_storage.errors import (
    SIZE_MISMATCH,
    SIZE_OUT_OF_RANGE,
    STREAM_INVALID,
    ObjectStorageError,
)
from personal_os.object_storage.keys import ContentDigest

# The input receive window: admission wait plus stream consumption are each
# bounded to exactly ten minutes of injected monotonic time.
_RECEIVE_WINDOW_SECONDS: Final[float] = 600.0

_SPOOL_FILE_PREFIX: Final[str] = "cas-spool-"
_SPOOL_FILE_SUFFIX: Final[str] = ".part"
_UUID_HEX_GRAMMAR: Final[str] = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_SPOOL_FILE_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(
    f"{re.escape(_SPOOL_FILE_PREFIX)}{_UUID_HEX_GRAMMAR}{re.escape(_SPOOL_FILE_SUFFIX)}"
)
_OWNER_READ_WRITE_MODE: Final[int] = 0o600


class DiskUsageSnapshot(Protocol):
    """Structural free-space view a disk-usage probe must return."""

    @property
    def free(self) -> int: ...


@dataclass(frozen=True, slots=True)
class SpoolLimits:
    """Immutable Phase 1 spool safety limits; these are not settings."""

    maximum_object_size_bytes: int = 104_857_600
    chunk_size_bytes: int = 1_048_576
    maximum_in_flight_operations: int = 4
    maximum_reserved_size_bytes: int = 536_870_912
    free_space_reserve_bytes: int = 2_147_483_648
    stale_after_seconds: int = 86_400
    maximum_cleanup_candidates: int = 1_000


@dataclass(frozen=True, slots=True)
class HashedSpool:
    """A fully received spool with its verified size and content identity.

    ``md5_digest`` is the raw binary MD5 digest used only as the S3
    ``Content-MD5`` transport guard; SHA-256 remains the sole content identity.
    """

    content_digest: ContentDigest
    md5_digest: bytes
    size_bytes: int
    path: Path


@dataclass(frozen=True, slots=True)
class SpoolCleanupSummary:
    """Counts-only result of a bounded stale-spool cleanup; never paths."""

    examined_count: int
    removed_count: int
    skipped_count: int
    deferred_count: int


class _AdmissionWindowExpired(Exception):
    """Internal signal: local admission was not granted inside the window."""


async def _run_shielded_cleanup(cleanup: Coroutine[object, object, None]) -> None:
    """Drive ``cleanup`` to completion even when the caller is cancelled.

    The cleanup runs as a short local task shielded from the caller's
    cancellation; a ``CancelledError`` delivered to the caller waits for the
    cleanup to finish and is then re-raised, so a spool file removal or an
    admission release can never be abandoned half-done.
    """

    task = asyncio.ensure_future(cleanup)
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        with suppress(asyncio.CancelledError):
            await task
        raise


class VerificationSpool:
    """One reserved verification spool for a full stored-object read.

    A reservation is single-use: :meth:`copy_and_hash` streams the response
    body into a fresh exclusive spool file while hashing and verifies the exact
    declared size and digest. :meth:`close` removes the file and releases the
    reservation; it is idempotent and safe on cancellation.
    """

    def __init__(
        self,
        manager: SpoolManager,
        reserved_size_bytes: int,
        reservation_observer: Callable[[int], None] | None,
    ) -> None:
        self._manager = manager
        self._reserved_size_bytes = reserved_size_bytes
        self._reservation_observer = reservation_observer
        self._hashed: HashedSpool | None = None
        self._is_closed = False

    @property
    def hashed(self) -> HashedSpool | None:
        """The hashed spool once :meth:`copy_and_hash` has completed."""

        return self._hashed

    async def copy_and_hash(
        self, response_body: AsyncIterable[bytes], expected: ExpectedObject
    ) -> HashedSpool:
        """Stream ``response_body`` into a fresh spool and verify it."""

        if self._is_closed or self._hashed is not None:
            raise ObjectStorageError(ErrorCode.OBJECT_STORAGE_CONTRACT_INVALID)
        spool_path = self._manager._next_spool_path()
        file_descriptor = await asyncio.to_thread(self._manager._open_exclusive, spool_path)
        try:
            self._hashed = await self._manager._drain_into_spool(
                spool_path,
                file_descriptor,
                response_body,
                expected_size_bytes=expected.size_bytes,
                deadline_monotonic=None,
                timeout_error=None,
                malformed_chunk_error=lambda: ObjectStorageError(
                    ErrorCode.OBJECT_STORAGE_CONTRACT_INVALID
                ),
                size_mismatch_error=lambda: ObjectStorageError(
                    ErrorCode.OBJECT_STORAGE_INTEGRITY_FAILED
                ),
                expected_digest=expected.content_digest,
            )
        except BaseException:
            await self._manager._remove_spool_file(spool_path)
            raise
        return self._hashed

    async def close(self) -> None:
        """Remove the spool file and release the reservation exactly once.

        Both cleanup steps run as short shielded local tasks so a cancellation
        delivered during cleanup still removes the file and releases the
        reservation before the ``CancelledError`` is re-raised.
        """

        if self._is_closed:
            return
        self._is_closed = True
        hashed = self._hashed
        self._hashed = None
        if hashed is not None:
            await _run_shielded_cleanup(self._manager._remove_spool_file(hashed.path))
        await _run_shielded_cleanup(
            self._manager._release_admission(
                self._reserved_size_bytes,
                consume_permit=False,
                reservation_observer=self._reservation_observer,
            )
        )


class SpoolManager:
    """Bounded secure spool owner for input receive and object verification.

    Admission gates four process-wide in-flight receive permits and a shared
    reservation budget through one :class:`asyncio.Condition`; a receive or
    verification reservation waits for local admission inside its ten-minute
    window and maps a wait timeout to retryable ``object_storage_busy``. A
    stream that fails to complete inside its admitted receive window maps to
    non-retryable ``object_storage_input_invalid`` with
    ``reason=stream_invalid``. Every path removes the spool file and releases
    the reservation exactly once. Two clocks are injected: a monotonic clock
    (``time.monotonic``) for receive/admission deadlines and a wall clock
    (``time.time``) for janitor age checks against epoch file mtimes.
    """

    def __init__(
        self,
        spool_root: Path,
        *,
        limits: SpoolLimits | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        disk_usage: Callable[[Path], DiskUsageSnapshot] = shutil.disk_usage,
    ) -> None:
        self._root = spool_root.resolve()
        self._limits = limits if limits is not None else SpoolLimits()
        self._clock = clock
        self._wall_clock = wall_clock
        self._disk_usage = disk_usage
        self._condition = asyncio.Condition()
        self._reserved_size_bytes = 0
        self._in_flight_count = 0

    @property
    def reserved_size_bytes(self) -> int:
        """Bytes currently reserved by input and verification spools."""

        return self._reserved_size_bytes

    @property
    def in_flight_count(self) -> int:
        """Input receive operations currently holding an in-flight permit."""

        return self._in_flight_count

    @asynccontextmanager
    async def receive_stream(
        self,
        stream: AsyncIterable[bytes],
        expected_size_bytes: int,
        *,
        reservation_observer: Callable[[int], None] | None = None,
    ) -> AsyncIterator[HashedSpool]:
        """Receive ``stream`` into a bounded spool while hashing it.

        Yields the hashed spool for inspection; the file is removed when the
        context exits and the reservation is released on every path.
        """

        self._require_declared_size(expected_size_bytes)
        await self._acquire_admission(
            expected_size_bytes,
            consume_permit=True,
            reservation_observer=reservation_observer,
        )
        spool_path = self._next_spool_path()
        try:
            file_descriptor = await asyncio.to_thread(self._open_exclusive, spool_path)
            try:
                try:
                    # The injected clock is checked per chunk; this real-time
                    # backstop also bounds a stream stalled inside a read.
                    async with asyncio.timeout(_RECEIVE_WINDOW_SECONDS):
                        hashed = await self._drain_into_spool(
                            spool_path,
                            file_descriptor,
                            stream,
                            expected_size_bytes=expected_size_bytes,
                            deadline_monotonic=self._clock() + _RECEIVE_WINDOW_SECONDS,
                            timeout_error=lambda: ObjectStorageError(
                                ErrorCode.OBJECT_STORAGE_INPUT_INVALID,
                                safe_details={"reason": STREAM_INVALID},
                            ),
                            malformed_chunk_error=lambda: ObjectStorageError(
                                ErrorCode.OBJECT_STORAGE_INPUT_INVALID,
                                safe_details={"reason": STREAM_INVALID},
                            ),
                            size_mismatch_error=lambda: ObjectStorageError(
                                ErrorCode.OBJECT_STORAGE_INPUT_INVALID,
                                safe_details={"reason": SIZE_MISMATCH},
                            ),
                            expected_digest=None,
                        )
                except TimeoutError:
                    raise ObjectStorageError(
                        ErrorCode.OBJECT_STORAGE_INPUT_INVALID,
                        safe_details={"reason": STREAM_INVALID},
                    ) from None
                try:
                    yield hashed
                finally:
                    await _run_shielded_cleanup(self._remove_spool_file(spool_path))
            finally:
                # Covers drain failure; the unlink above is idempotent.
                await _run_shielded_cleanup(self._remove_spool_file(spool_path))
        finally:
            await _run_shielded_cleanup(
                self._release_admission(
                    expected_size_bytes,
                    consume_permit=True,
                    reservation_observer=reservation_observer,
                )
            )

    async def reserve_verification(
        self,
        size_bytes: int,
        *,
        reservation_observer: Callable[[int], None] | None = None,
    ) -> VerificationSpool:
        """Reserve capacity for one verification spool and wait for admission."""

        self._require_declared_size(size_bytes)
        await self._acquire_admission(
            size_bytes,
            consume_permit=False,
            reservation_observer=reservation_observer,
        )
        return VerificationSpool(self, size_bytes, reservation_observer)

    async def cleanup_stale_spools(self) -> SpoolCleanupSummary:
        """Run the bounded janitor over direct spool-root children.

        Only grammar-matching, regular, non-symlink files older than
        ``stale_after_seconds`` are removed. Spool-file ages are Unix-epoch
        ``st_mtime`` values, so the age term uses the wall clock; at most
        ``maximum_cleanup_candidates`` are examined per run and the summary
        carries counts only.
        """

        now = self._wall_clock()
        return await asyncio.to_thread(self._cleanup_stale_spools_at, now)

    def _cleanup_stale_spools_at(self, now: float) -> SpoolCleanupSummary:
        limits = self._limits
        examined_count = 0
        removed_count = 0
        skipped_count = 0
        deferred_count = 0
        try:
            entries = list(os.scandir(self._root))
        except OSError:
            return SpoolCleanupSummary(0, 0, 0, 0)
        for entry in entries:
            if _SPOOL_FILE_NAME_PATTERN.fullmatch(entry.name) is None:
                continue
            if examined_count >= limits.maximum_cleanup_candidates:
                deferred_count += 1
                continue
            examined_count += 1
            try:
                status = os.lstat(entry.path)
                is_regular_file = not stat.S_ISLNK(status.st_mode) and stat.S_ISREG(status.st_mode)
                is_stale = (now - status.st_mtime) > limits.stale_after_seconds
                if is_regular_file and is_stale:
                    os.unlink(entry.path)
                    removed_count += 1
                else:
                    skipped_count += 1
            except OSError:
                skipped_count += 1
        return SpoolCleanupSummary(examined_count, removed_count, skipped_count, deferred_count)

    def _require_declared_size(self, size_bytes: int) -> None:
        if isinstance(size_bytes, bool) or not isinstance(size_bytes, int):
            raise self._size_out_of_range_error()
        if size_bytes < 0 or size_bytes > self._limits.maximum_object_size_bytes:
            raise self._size_out_of_range_error()

    @staticmethod
    def _size_out_of_range_error() -> ObjectStorageError:
        return ObjectStorageError(
            ErrorCode.OBJECT_STORAGE_INPUT_INVALID,
            safe_details={"reason": SIZE_OUT_OF_RANGE},
        )

    def _has_capacity_locked(self, size_bytes: int, consume_permit: bool) -> bool:
        if consume_permit and self._in_flight_count >= self._limits.maximum_in_flight_operations:
            return False
        return self._reserved_size_bytes + size_bytes <= self._limits.maximum_reserved_size_bytes

    async def _require_free_space_reserve(self, size_bytes: int) -> None:
        disk_usage = await asyncio.to_thread(self._disk_usage, self._root)
        if disk_usage.free - size_bytes < self._limits.free_space_reserve_bytes:
            raise ObjectStorageError(ErrorCode.OBJECT_STORAGE_BUSY)

    def _acquire_admission_locked(
        self,
        size_bytes: int,
        consume_permit: bool,
        reservation_observer: Callable[[int], None] | None,
    ) -> None:
        if consume_permit:
            self._in_flight_count += 1
        self._reserved_size_bytes += size_bytes
        if reservation_observer is not None:
            reservation_observer(self._reserved_size_bytes)

    async def _acquire_admission(
        self,
        size_bytes: int,
        *,
        consume_permit: bool,
        reservation_observer: Callable[[int], None] | None,
    ) -> None:
        deadline = self._clock() + _RECEIVE_WINDOW_SECONDS

        async def acquire_when_available() -> None:
            async with self._condition:
                while True:
                    if self._clock() >= deadline:
                        raise _AdmissionWindowExpired()
                    await self._require_free_space_reserve(size_bytes)
                    if self._has_capacity_locked(size_bytes, consume_permit):
                        self._acquire_admission_locked(
                            size_bytes,
                            consume_permit,
                            reservation_observer,
                        )
                        return
                    await self._condition.wait()

        try:
            await asyncio.wait_for(acquire_when_available(), timeout=deadline - self._clock())
        except (TimeoutError, _AdmissionWindowExpired):
            raise ObjectStorageError(ErrorCode.OBJECT_STORAGE_BUSY) from None

    async def _release_admission(
        self,
        size_bytes: int,
        *,
        consume_permit: bool,
        reservation_observer: Callable[[int], None] | None,
    ) -> None:
        async with self._condition:
            if consume_permit and self._in_flight_count > 0:
                self._in_flight_count -= 1
            if self._reserved_size_bytes > 0:
                self._reserved_size_bytes = max(0, self._reserved_size_bytes - size_bytes)
            if reservation_observer is not None:
                reservation_observer(self._reserved_size_bytes)
            self._condition.notify_all()

    def _next_spool_path(self) -> Path:
        """Return the next internal random spool path beneath the resolved root."""

        return self._root / f"{_SPOOL_FILE_PREFIX}{uuid.uuid4()}{_SPOOL_FILE_SUFFIX}"

    def _open_exclusive(self, spool_path: Path) -> int:
        """Create the spool exclusively and verify it is a safe regular file."""

        if os.path.lexists(spool_path):
            # A pre-existing entry at the internal random name (regular file,
            # directory or symlink) is a spool-root safety violation.
            raise ObjectStorageError(ErrorCode.OBJECT_STORAGE_CONFIGURATION_INVALID)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        try:
            file_descriptor = os.open(spool_path, flags, _OWNER_READ_WRITE_MODE)
        except FileExistsError as error:
            raise ObjectStorageError(ErrorCode.OBJECT_STORAGE_CONFIGURATION_INVALID) from error
        except OSError as error:
            raise ObjectStorageError(ErrorCode.OBJECT_STORAGE_UNAVAILABLE) from error
        try:
            resolved = spool_path.resolve()
            if not resolved.is_relative_to(self._root):
                raise ObjectStorageError(ErrorCode.OBJECT_STORAGE_CONFIGURATION_INVALID)
            if not stat.S_ISREG(os.fstat(file_descriptor).st_mode):
                raise ObjectStorageError(ErrorCode.OBJECT_STORAGE_CONFIGURATION_INVALID)
        except BaseException:
            os.close(file_descriptor)
            raise
        return file_descriptor

    async def _drain_into_spool(
        self,
        spool_path: Path,
        file_descriptor: int,
        body: AsyncIterable[bytes],
        *,
        expected_size_bytes: int,
        deadline_monotonic: float | None,
        timeout_error: Callable[[], ObjectStorageError] | None,
        malformed_chunk_error: Callable[[], ObjectStorageError],
        size_mismatch_error: Callable[[], ObjectStorageError],
        expected_digest: ContentDigest | None,
    ) -> HashedSpool:
        """Consume ``body`` into the spool file while hashing, then close it."""

        chunk_size_bytes = self._limits.chunk_size_bytes
        sha256_hasher = hashlib.sha256()
        md5_hasher = hashlib.md5(usedforsecurity=False)
        size_bytes = 0
        iterator = body.__aiter__()
        try:
            while True:
                try:
                    chunk = await iterator.__anext__()
                except StopAsyncIteration:
                    break
                if deadline_monotonic is not None and self._clock() >= deadline_monotonic:
                    assert timeout_error is not None
                    raise timeout_error()
                if not isinstance(chunk, bytes) or not chunk:
                    raise malformed_chunk_error()
                if size_bytes + len(chunk) > expected_size_bytes:
                    raise size_mismatch_error()
                offset = 0
                while offset < len(chunk):
                    piece = chunk[offset : offset + chunk_size_bytes]
                    await asyncio.to_thread(self._write_all, file_descriptor, piece)
                    sha256_hasher.update(piece)
                    md5_hasher.update(piece)
                    size_bytes += len(piece)
                    offset += len(piece)
        finally:
            # Close the descriptor before the source: a cancellation landing
            # inside the source's ``aclose`` (CancelledError is a
            # BaseException that ``_close_source`` must not suppress) cannot
            # leak the descriptor, and any CancelledError still propagates.
            try:
                await self._close_descriptor(file_descriptor)
            finally:
                await self._close_source(iterator)
        if size_bytes != expected_size_bytes:
            raise size_mismatch_error()
        content_digest = ContentDigest.parse(sha256_hasher.hexdigest())
        if expected_digest is not None and content_digest != expected_digest:
            raise ObjectStorageError(ErrorCode.OBJECT_STORAGE_INTEGRITY_FAILED)
        return HashedSpool(
            content_digest=content_digest,
            md5_digest=md5_hasher.digest(),
            size_bytes=size_bytes,
            path=spool_path,
        )

    @staticmethod
    def _write_all(file_descriptor: int, data: bytes) -> None:
        view = memoryview(data)
        while view:
            written = os.write(file_descriptor, view)
            view = view[written:]

    @staticmethod
    async def _close_source(iterator: AsyncIterator[bytes]) -> None:
        """Close the source generator when supported; best-effort only.

        Ordinary close failures are suppressed; ``CancelledError`` propagates
        (the descriptor is already closed by the caller before this runs).
        """

        aclose: Callable[[], Awaitable[None]] | None = getattr(iterator, "aclose", None)
        if aclose is None:
            return
        # Closing a failed source must never mask the primary error.
        with suppress(Exception):
            await aclose()

    @staticmethod
    async def _close_descriptor(file_descriptor: int) -> None:
        def _close() -> None:
            with suppress(OSError):
                os.close(file_descriptor)

        try:
            await asyncio.to_thread(_close)
        except asyncio.CancelledError:
            _close()
            raise

    @staticmethod
    def _unlink_existing(spool_path: Path) -> None:
        with suppress(OSError):
            os.unlink(spool_path)

    async def _remove_spool_file(self, spool_path: Path) -> None:
        try:
            await asyncio.to_thread(self._unlink_existing, spool_path)
        except asyncio.CancelledError:
            # Cancellation during cleanup still removes the file synchronously.
            self._unlink_existing(spool_path)
            raise
