"""Bounded secure spooling behavior: hashing, admission, deadlines and cleanup.

These tests prove the provider spool boundary: exact content hashing while
streaming under fixed limits, exclusive owner-only spool files beneath a
resolved root, permit/reservation backpressure with the two distinct timeout
mappings, cancellation and exception cleanup, the bounded counts-only janitor
and the verification-spool reserve/copy/hash mechanics. Time and free space are
controlled through the injectable clock and disk-usage callables; the production
manager exposes no test-only hooks.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from collections.abc import AsyncIterable, AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from personal_os.error_contracts.codes import ErrorCode
from personal_os.object_storage import CanonicalMediaType, ContentDigest, ExpectedObject
from personal_os.object_storage.errors import (
    SIZE_MISMATCH,
    SIZE_OUT_OF_RANGE,
    SPOOL_ADMISSION_WINDOW_EXPIRED,
    SPOOL_FREE_SPACE,
    SPOOL_PERMITS_EXHAUSTED,
    STREAM_INVALID,
    ObjectStorageError,
)
from r2_object_storage import spool as spool_module
from r2_object_storage.spool import SpoolLimits, SpoolManager

_MIB = 1024 * 1024
_MAXIMUM_OBJECT_SIZE_BYTES = 100 * _MIB
_MAXIMUM_RESERVED_SIZE_BYTES = 512 * _MIB
_FREE_SPACE_RESERVE_BYTES = 2 * 1024 * 1024 * 1024
_STALE_AFTER_SECONDS = 86_400
_FIXED_SPOOL_UUID = uuid.UUID("12345678-1234-4123-8123-123456789abc")

# Mutable per-test state: the fake monotonic clock, the fake wall (epoch) clock
# and the stream-started gate. They are rebound by an autouse fixture so every
# test gets fresh values.
fake_now: list[float] = [1_000.0]
fake_wall_now: list[float] = [1_800_000_000.0]
stream_started = asyncio.Event()

requires_posix_permissions = pytest.mark.skipif(
    os.name != "posix", reason="POSIX permission bits are not a Windows contract"
)
requires_posix_symlinks = pytest.mark.skipif(
    os.name != "posix", reason="symlink creation is a privileged Windows operation"
)


@pytest.fixture(autouse=True)
def _fresh_clock_and_stream_gate() -> None:
    global stream_started
    fake_now[0] = 1_000.0
    fake_wall_now[0] = 1_800_000_000.0
    stream_started = asyncio.Event()


def _fake_clock() -> float:
    return fake_now[0]


def _fake_wall_clock() -> float:
    return fake_wall_now[0]


def build_spool_manager(
    spool_root: Path,
    *,
    limits: SpoolLimits | None = None,
    free_space_bytes: int = 8 * 1024 * 1024 * 1024,
) -> SpoolManager:
    """Build a manager with injected monotonic/wall clocks and disk-usage probe."""

    return SpoolManager(
        spool_root,
        limits=limits,
        clock=_fake_clock,
        wall_clock=_fake_wall_clock,
        disk_usage=lambda _root: SimpleNamespace(free=free_space_bytes),
    )


def chunks(*payloads: bytes) -> AsyncIterator[bytes]:
    """Wrap fixed payloads as an asynchronous byte stream."""

    return _chunk_stream(payloads)


async def _chunk_stream(payloads: tuple[bytes, ...]) -> AsyncIterator[bytes]:
    for payload in payloads:
        yield payload


def blocking_stream() -> AsyncIterator[bytes]:
    """Never yield: announce start, then block until cancelled."""

    return _blocking_stream()


async def _blocking_stream() -> AsyncIterator[bytes]:
    stream_started.set()
    await asyncio.Event().wait()
    yield b""


def _spool_file_name(suffix: str = "") -> str:
    return f"cas-spool-{uuid.uuid4()}.part{suffix}"


async def _hold_receive(
    manager: SpoolManager, stream: AsyncIterable[bytes], expected_size_bytes: int
) -> None:
    async with manager.receive_stream(stream, expected_size_bytes):
        pass


async def _cancel_and_wait(task: asyncio.Task[None]) -> None:
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def _expected_object(payload: bytes) -> ExpectedObject:
    return ExpectedObject(
        content_digest=ContentDigest.parse(hashlib.sha256(payload).hexdigest()),
        size_bytes=len(payload),
        media_type=CanonicalMediaType.parse("text/plain"),
    )


def test_spool_limits_defaults_match_the_fixed_contract() -> None:
    assert SpoolLimits() == SpoolLimits(
        maximum_object_size_bytes=_MAXIMUM_OBJECT_SIZE_BYTES,
        chunk_size_bytes=_MIB,
        maximum_in_flight_operations=4,
        maximum_reserved_size_bytes=_MAXIMUM_RESERVED_SIZE_BYTES,
        free_space_reserve_bytes=_FREE_SPACE_RESERVE_BYTES,
        stale_after_seconds=_STALE_AFTER_SECONDS,
        maximum_cleanup_candidates=1_000,
    )


def test_spool_limits_are_immutable() -> None:
    limits = SpoolLimits()
    with pytest.raises(AttributeError):
        limits.maximum_object_size_bytes = 1  # type: ignore[misc]


@pytest.mark.asyncio
async def test_spools_and_hashes_without_buffering_complete_body(tmp_path: Path) -> None:
    manager = build_spool_manager(tmp_path)
    async with manager.receive_stream(chunks(b"abc", b"def"), 6) as spool:
        assert spool.content_digest.hexadecimal == hashlib.sha256(b"abcdef").hexdigest()
        assert spool.size_bytes == 6
        assert spool.path.read_bytes() == b"abcdef"
        assert spool.md5_digest == hashlib.md5(b"abcdef").digest()
    assert list(tmp_path.iterdir()) == []
    assert manager.reserved_size_bytes == 0
    assert manager.in_flight_count == 0


@pytest.mark.asyncio
async def test_zero_byte_stream_is_a_valid_object(tmp_path: Path) -> None:
    manager = build_spool_manager(tmp_path)
    async with manager.receive_stream(chunks(), 0) as spool:
        assert spool.size_bytes == 0
        assert spool.content_digest.hexadecimal == hashlib.sha256(b"").hexdigest()
        assert spool.path.read_bytes() == b""
    assert list(tmp_path.iterdir()) == []
    assert manager.reserved_size_bytes == 0


@pytest.mark.parametrize("declared_size", [-1, _MAXIMUM_OBJECT_SIZE_BYTES + 1])
@pytest.mark.asyncio
async def test_rejects_declared_size_outside_range(tmp_path: Path, declared_size: int) -> None:
    manager = build_spool_manager(tmp_path)
    with pytest.raises(ObjectStorageError) as raised:
        async with manager.receive_stream(chunks(b"abc"), declared_size):
            pass
    assert raised.value.error_code is ErrorCode.OBJECT_STORAGE_INPUT_INVALID
    assert raised.value.safe_details["reason"] is SIZE_OUT_OF_RANGE
    assert not raised.value.is_retryable
    assert list(tmp_path.iterdir()) == []
    assert manager.reserved_size_bytes == 0


@pytest.mark.asyncio
async def test_rejects_boolean_declared_size(tmp_path: Path) -> None:
    manager = build_spool_manager(tmp_path)
    with pytest.raises(ObjectStorageError) as raised:
        async with manager.receive_stream(chunks(b"abc"), True):  # type: ignore[arg-type]
            pass
    assert raised.value.error_code is ErrorCode.OBJECT_STORAGE_INPUT_INVALID
    assert raised.value.safe_details["reason"] is SIZE_OUT_OF_RANGE


@pytest.mark.asyncio
async def test_rejects_stream_shorter_than_declared(tmp_path: Path) -> None:
    manager = build_spool_manager(tmp_path)
    with pytest.raises(ObjectStorageError) as raised:
        async with manager.receive_stream(chunks(b"ab"), 3):
            pass
    assert raised.value.error_code is ErrorCode.OBJECT_STORAGE_INPUT_INVALID
    assert raised.value.safe_details["reason"] is SIZE_MISMATCH
    assert list(tmp_path.iterdir()) == []
    assert manager.reserved_size_bytes == 0
    assert manager.in_flight_count == 0


@pytest.mark.asyncio
async def test_rejects_stream_longer_than_declared_without_writing_overflow(
    tmp_path: Path,
) -> None:
    manager = build_spool_manager(tmp_path)
    with pytest.raises(ObjectStorageError) as raised:
        async with manager.receive_stream(chunks(b"abc", b"de"), 3):
            pass
    assert raised.value.error_code is ErrorCode.OBJECT_STORAGE_INPUT_INVALID
    assert raised.value.safe_details["reason"] is SIZE_MISMATCH
    assert list(tmp_path.iterdir()) == []
    assert manager.reserved_size_bytes == 0


@pytest.mark.asyncio
async def test_rejects_non_bytes_chunk_as_invalid_stream(tmp_path: Path) -> None:
    manager = build_spool_manager(tmp_path)

    async def textual_stream() -> AsyncIterator[bytes]:
        yield "not-bytes"  # type: ignore[misc]

    with pytest.raises(ObjectStorageError) as raised:
        async with manager.receive_stream(textual_stream(), 9):
            pass
    assert raised.value.error_code is ErrorCode.OBJECT_STORAGE_INPUT_INVALID
    assert raised.value.safe_details["reason"] is STREAM_INVALID
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_rejects_empty_chunk_as_invalid_stream(tmp_path: Path) -> None:
    manager = build_spool_manager(tmp_path)
    with pytest.raises(ObjectStorageError) as raised:
        async with manager.receive_stream(chunks(b"abc", b""), 3):
            pass
    assert raised.value.error_code is ErrorCode.OBJECT_STORAGE_INPUT_INVALID
    assert raised.value.safe_details["reason"] is STREAM_INVALID
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_splits_oversized_input_chunks_to_the_chunk_limit(
    tmp_path: Path,
) -> None:
    manager = build_spool_manager(tmp_path)
    oversized = b"x" * (2 * _MIB + 13)
    async with manager.receive_stream(chunks(oversized), len(oversized)) as spool:
        assert spool.size_bytes == len(oversized)
        assert spool.content_digest.hexadecimal == hashlib.sha256(oversized).hexdigest()


@pytest.mark.asyncio
async def test_accepts_exactly_one_hundred_mebibytes(tmp_path: Path) -> None:
    manager = build_spool_manager(tmp_path)
    fixed_chunk = b"a" * _MIB
    expected_hasher = hashlib.sha256()
    for _ in range(100):
        expected_hasher.update(fixed_chunk)
    async with manager.receive_stream(
        chunks(*(100 * [fixed_chunk])), _MAXIMUM_OBJECT_SIZE_BYTES
    ) as spool:
        assert spool.size_bytes == _MAXIMUM_OBJECT_SIZE_BYTES
        assert spool.content_digest.hexadecimal == expected_hasher.hexdigest()
        assert spool.path.stat().st_size == _MAXIMUM_OBJECT_SIZE_BYTES
    assert list(tmp_path.iterdir()) == []
    assert manager.reserved_size_bytes == 0


@pytest.mark.asyncio
async def test_receive_window_timeout_maps_to_non_retryable_stream_invalid(
    tmp_path: Path,
) -> None:
    manager = build_spool_manager(tmp_path)
    resume = asyncio.Event()
    announced = asyncio.Event()

    async def stalled_stream() -> AsyncIterator[bytes]:
        yield b"first"
        announced.set()
        await resume.wait()
        yield b"second"

    async def receive() -> None:
        async with manager.receive_stream(stalled_stream(), 11):
            pass

    task = asyncio.create_task(receive())
    await announced.wait()
    fake_now[0] += 600.0
    resume.set()
    with pytest.raises(ObjectStorageError) as raised:
        await task
    assert raised.value.error_code is ErrorCode.OBJECT_STORAGE_INPUT_INVALID
    assert raised.value.safe_details["reason"] is STREAM_INVALID
    assert not raised.value.is_retryable
    assert list(tmp_path.iterdir()) == []
    assert manager.reserved_size_bytes == 0
    assert manager.in_flight_count == 0


@pytest.mark.asyncio
async def test_receive_completes_inside_the_ten_minute_window(
    tmp_path: Path,
) -> None:
    manager = build_spool_manager(tmp_path)
    resume = asyncio.Event()
    announced = asyncio.Event()

    async def slow_stream() -> AsyncIterator[bytes]:
        yield b"first"
        announced.set()
        await resume.wait()
        yield b"second"

    async def receive() -> None:
        async with manager.receive_stream(slow_stream(), 11) as spool:
            assert spool.size_bytes == 11

    task = asyncio.create_task(receive())
    await announced.wait()
    fake_now[0] += 599.9
    resume.set()
    await task
    assert list(tmp_path.iterdir()) == []
    assert manager.reserved_size_bytes == 0


@pytest.mark.asyncio
async def test_admission_timeout_maps_to_retryable_busy(tmp_path: Path) -> None:
    manager = build_spool_manager(tmp_path)
    started_count = [0]
    all_permits_held = asyncio.Event()

    def held_stream() -> AsyncIterator[bytes]:
        return _held_stream(started_count, all_permits_held)

    async def _held_stream(counter: list[int], gate: asyncio.Event) -> AsyncIterator[bytes]:
        counter[0] += 1
        if counter[0] >= 4:
            gate.set()
        await asyncio.Event().wait()
        yield b""

    holders = [asyncio.create_task(_hold_receive(manager, held_stream(), 10)) for _ in range(4)]
    await all_permits_held.wait()
    assert manager.in_flight_count == 4
    assert manager.reserved_size_bytes == 40

    waiting = asyncio.create_task(_hold_receive(manager, chunks(b"0123456789"), 10))
    await asyncio.sleep(0.05)
    assert not waiting.done()

    fake_now[0] += 600.0
    await _cancel_and_wait(holders[0])
    with pytest.raises(ObjectStorageError) as raised:
        await waiting
    assert raised.value.error_code is ErrorCode.OBJECT_STORAGE_BUSY
    assert raised.value.is_retryable
    assert raised.value.safe_details["reason"] is SPOOL_PERMITS_EXHAUSTED

    for holder in holders[1:]:
        await _cancel_and_wait(holder)
    assert list(tmp_path.iterdir()) == []
    assert manager.reserved_size_bytes == 0
    assert manager.in_flight_count == 0


@pytest.mark.asyncio
async def test_admission_wait_overrunning_the_window_reports_window_expiry(
    tmp_path: Path,
) -> None:
    # The four in-flight permits are held; the fifth acquisition's own
    # receive-window deadline then elapses while it is parked on the admission
    # condition, so the outer wait_for timeout (not the loop's deadline check
    # after a release) maps to retryable busy with the window-expiry reason.
    gated: list[bool] = [False]
    admitted_once_after_gate: list[bool] = [False]

    def _window_crushing_clock() -> float:
        if not gated[0]:
            return 1_000.0
        if not admitted_once_after_gate[0]:
            admitted_once_after_gate[0] = True
            return 1_000.0
        return 1_599.5

    manager = SpoolManager(
        tmp_path,
        clock=_window_crushing_clock,
        wall_clock=_fake_wall_clock,
        disk_usage=lambda _root: SimpleNamespace(free=8 * 1024 * 1024 * 1024),
    )
    started_count = [0]
    all_permits_held = asyncio.Event()

    def held_stream() -> AsyncIterator[bytes]:
        return _held_stream(started_count, all_permits_held)

    async def _held_stream(counter: list[int], gate: asyncio.Event) -> AsyncIterator[bytes]:
        counter[0] += 1
        if counter[0] >= 4:
            gate.set()
        await asyncio.Event().wait()
        yield b""

    holders = [asyncio.create_task(_hold_receive(manager, held_stream(), 10)) for _ in range(4)]
    await all_permits_held.wait()
    gated[0] = True

    waiting = asyncio.create_task(_hold_receive(manager, chunks(b"0123456789"), 10))
    with pytest.raises(ObjectStorageError) as raised:
        await waiting
    assert raised.value.error_code is ErrorCode.OBJECT_STORAGE_BUSY
    assert raised.value.is_retryable
    assert raised.value.safe_details["reason"] is SPOOL_ADMISSION_WINDOW_EXPIRED

    for holder in holders:
        await _cancel_and_wait(holder)
    assert list(tmp_path.iterdir()) == []
    assert manager.reserved_size_bytes == 0
    assert manager.in_flight_count == 0


@pytest.mark.asyncio
async def test_fifth_operation_waits_for_a_permit_then_proceeds(
    tmp_path: Path,
) -> None:
    manager = build_spool_manager(tmp_path)
    started_count = [0]
    all_permits_held = asyncio.Event()

    def held_stream() -> AsyncIterator[bytes]:
        return _held_stream(started_count, all_permits_held)

    async def _held_stream(counter: list[int], gate: asyncio.Event) -> AsyncIterator[bytes]:
        counter[0] += 1
        if counter[0] >= 4:
            gate.set()
        await asyncio.Event().wait()
        yield b""

    holders = [asyncio.create_task(_hold_receive(manager, held_stream(), 10)) for _ in range(4)]
    await all_permits_held.wait()

    waiting = asyncio.create_task(_hold_receive(manager, chunks(b"0123456789"), 10))
    await asyncio.sleep(0.05)
    assert not waiting.done()

    await _cancel_and_wait(holders[0])
    await asyncio.wait_for(waiting, timeout=5)
    assert manager.in_flight_count == 3
    assert manager.reserved_size_bytes == 30

    for holder in holders[1:]:
        await _cancel_and_wait(holder)
    assert list(tmp_path.iterdir()) == []
    assert manager.reserved_size_bytes == 0
    assert manager.in_flight_count == 0


@pytest.mark.asyncio
async def test_insufficient_free_space_rejects_admission_as_busy(
    tmp_path: Path,
) -> None:
    manager = build_spool_manager(tmp_path, free_space_bytes=_FREE_SPACE_RESERVE_BYTES + 10)
    with pytest.raises(ObjectStorageError) as raised:
        async with manager.receive_stream(chunks(b"0123456789"), 11):
            pass
    assert raised.value.error_code is ErrorCode.OBJECT_STORAGE_BUSY
    assert raised.value.is_retryable
    assert raised.value.safe_details["reason"] is SPOOL_FREE_SPACE
    assert list(tmp_path.iterdir()) == []
    assert manager.reserved_size_bytes == 0


@pytest.mark.asyncio
async def test_admission_at_exactly_the_free_space_reserve(tmp_path: Path) -> None:
    manager = build_spool_manager(tmp_path, free_space_bytes=_FREE_SPACE_RESERVE_BYTES + 11)
    async with manager.receive_stream(chunks(b"0123456789!"), 11) as spool:
        assert spool.size_bytes == 11
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_cancellation_releases_reservation_and_file(tmp_path: Path) -> None:
    manager = build_spool_manager(tmp_path)

    async def receive() -> None:
        async with manager.receive_stream(blocking_stream(), 100):
            pass

    task = asyncio.create_task(receive())
    await stream_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert manager.reserved_size_bytes == 0
    assert manager.in_flight_count == 0
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_exception_inside_context_cleans_up_spool_and_reservation(
    tmp_path: Path,
) -> None:
    manager = build_spool_manager(tmp_path)
    with pytest.raises(RuntimeError, match="consumer failed"):
        async with manager.receive_stream(chunks(b"abc"), 3):
            raise RuntimeError("consumer failed")
    assert list(tmp_path.iterdir()) == []
    assert manager.reserved_size_bytes == 0
    assert manager.in_flight_count == 0


@pytest.mark.asyncio
async def test_cancellation_during_source_close_still_closes_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = build_spool_manager(tmp_path)
    closed_descriptors: list[int] = []
    original_close = spool_module.os.close

    def recording_close(file_descriptor: int) -> None:
        closed_descriptors.append(file_descriptor)
        original_close(file_descriptor)

    monkeypatch.setattr(spool_module.os, "close", recording_close)

    class CancellingCloseStream:
        """A stream whose ``aclose`` raises ``CancelledError``."""

        def __init__(self) -> None:
            self._is_finished = False

        def __aiter__(self) -> AsyncIterator[bytes]:
            return self

        async def __anext__(self) -> bytes:
            if self._is_finished:
                raise StopAsyncIteration
            self._is_finished = True
            return b"abc"

        async def aclose(self) -> None:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        async with manager.receive_stream(CancellingCloseStream(), 3):
            pass
    assert len(closed_descriptors) >= 1
    with pytest.raises(OSError):
        spool_module.os.fstat(closed_descriptors[0])
    assert list(tmp_path.iterdir()) == []
    assert manager.reserved_size_bytes == 0
    assert manager.in_flight_count == 0


@requires_posix_permissions
@pytest.mark.asyncio
async def test_spool_file_is_owner_read_write_only(tmp_path: Path) -> None:
    manager = build_spool_manager(tmp_path)
    async with manager.receive_stream(chunks(b"abc"), 3) as spool:
        assert spool.path.stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_rejects_preexisting_non_regular_spool_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = build_spool_manager(tmp_path)
    monkeypatch.setattr(spool_module.uuid, "uuid4", lambda: _FIXED_SPOOL_UUID)
    occupied = tmp_path / f"cas-spool-{_FIXED_SPOOL_UUID}.part"
    occupied.mkdir()
    with pytest.raises(ObjectStorageError) as raised:
        async with manager.receive_stream(chunks(b"abc"), 3):
            pass
    assert raised.value.error_code is ErrorCode.OBJECT_STORAGE_CONFIGURATION_INVALID
    assert list(tmp_path.iterdir()) == [occupied]
    assert manager.reserved_size_bytes == 0


@requires_posix_symlinks
@pytest.mark.asyncio
async def test_rejects_preexisting_symlink_spool_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = build_spool_manager(tmp_path)
    monkeypatch.setattr(spool_module.uuid, "uuid4", lambda: _FIXED_SPOOL_UUID)
    outside_target = tmp_path / "outside.bin"
    outside_target.write_bytes(b"untouched")
    hijacked = tmp_path / f"cas-spool-{_FIXED_SPOOL_UUID}.part"
    hijacked.symlink_to(outside_target)
    with pytest.raises(ObjectStorageError) as raised:
        async with manager.receive_stream(chunks(b"abc"), 3):
            pass
    assert raised.value.error_code is ErrorCode.OBJECT_STORAGE_CONFIGURATION_INVALID
    assert outside_target.read_bytes() == b"untouched"
    assert list(tmp_path.iterdir()) == [outside_target, hijacked]
    assert manager.reserved_size_bytes == 0


@pytest.mark.asyncio
async def test_verification_copy_and_hash_verifies_exact_bytes(
    tmp_path: Path,
) -> None:
    manager = build_spool_manager(tmp_path)
    verification = await manager.reserve_verification(11)
    assert manager.reserved_size_bytes == 11
    hashed = await verification.copy_and_hash(
        chunks(b"0123", b"4567891"), _expected_object(b"01234567891")
    )
    assert hashed.size_bytes == 11
    assert hashed.content_digest.hexadecimal == hashlib.sha256(b"01234567891").hexdigest()
    assert hashed.path.read_bytes() == b"01234567891"
    await verification.close()
    assert list(tmp_path.iterdir()) == []
    assert manager.reserved_size_bytes == 0


@pytest.mark.asyncio
async def test_verification_rejects_short_excess_and_corrupt_bodies(
    tmp_path: Path,
) -> None:
    manager = build_spool_manager(tmp_path)
    expected = _expected_object(b"01234567891")

    short = await manager.reserve_verification(11)
    with pytest.raises(ObjectStorageError) as short_error:
        await short.copy_and_hash(chunks(b"0123"), expected)
    assert short_error.value.error_code is ErrorCode.OBJECT_STORAGE_INTEGRITY_FAILED
    await short.close()

    excess = await manager.reserve_verification(11)
    with pytest.raises(ObjectStorageError) as excess_error:
        await excess.copy_and_hash(chunks(b"01234567891", b"extra"), expected)
    assert excess_error.value.error_code is ErrorCode.OBJECT_STORAGE_INTEGRITY_FAILED
    await excess.close()

    corrupt = await manager.reserve_verification(11)
    with pytest.raises(ObjectStorageError) as corrupt_error:
        await corrupt.copy_and_hash(chunks(b"XXXXXXXXXXX"), expected)
    assert corrupt_error.value.error_code is ErrorCode.OBJECT_STORAGE_INTEGRITY_FAILED
    await corrupt.close()

    assert list(tmp_path.iterdir()) == []
    assert manager.reserved_size_bytes == 0


@pytest.mark.asyncio
async def test_verification_close_is_idempotent_and_rejects_reuse(
    tmp_path: Path,
) -> None:
    manager = build_spool_manager(tmp_path)
    verification = await manager.reserve_verification(11)
    hashed = await verification.copy_and_hash(
        chunks(b"01234567891"), _expected_object(b"01234567891")
    )
    await verification.close()
    await verification.close()
    assert not hashed.path.exists()
    with pytest.raises(ObjectStorageError) as raised:
        await verification.copy_and_hash(chunks(b"01234567891"), _expected_object(b"01234567891"))
    assert raised.value.error_code is ErrorCode.OBJECT_STORAGE_CONTRACT_INVALID
    assert manager.reserved_size_bytes == 0


@pytest.mark.asyncio
async def test_verification_reservation_enforces_the_process_budget(
    tmp_path: Path,
) -> None:
    manager = build_spool_manager(tmp_path)
    started_count = [0]
    all_permits_held = asyncio.Event()

    def held_stream() -> AsyncIterator[bytes]:
        return _held_stream(started_count, all_permits_held)

    async def _held_stream(counter: list[int], gate: asyncio.Event) -> AsyncIterator[bytes]:
        counter[0] += 1
        if counter[0] >= 4:
            gate.set()
        await asyncio.Event().wait()
        yield b""

    holders = [
        asyncio.create_task(_hold_receive(manager, held_stream(), _MAXIMUM_OBJECT_SIZE_BYTES))
        for _ in range(4)
    ]
    await all_permits_held.wait()
    assert manager.reserved_size_bytes == 4 * _MAXIMUM_OBJECT_SIZE_BYTES

    admitted = await manager.reserve_verification(_MAXIMUM_OBJECT_SIZE_BYTES)
    assert (
        manager.reserved_size_bytes == 5 * _MAXIMUM_OBJECT_SIZE_BYTES
    )  # 500 MiB of the 512 MiB budget

    waiting = asyncio.create_task(manager.reserve_verification(_MAXIMUM_OBJECT_SIZE_BYTES))
    await asyncio.sleep(0.05)
    assert not waiting.done()

    fake_now[0] += 600.0
    await admitted.close()
    with pytest.raises(ObjectStorageError) as raised:
        await waiting
    assert raised.value.error_code is ErrorCode.OBJECT_STORAGE_BUSY
    assert raised.value.is_retryable
    assert raised.value.safe_details["reason"] is SPOOL_PERMITS_EXHAUSTED

    for holder in holders:
        await _cancel_and_wait(holder)
    assert list(tmp_path.iterdir()) == []
    assert manager.reserved_size_bytes == 0


@pytest.mark.asyncio
async def test_verification_rejects_declared_size_outside_range(
    tmp_path: Path,
) -> None:
    manager = build_spool_manager(tmp_path)
    with pytest.raises(ObjectStorageError) as raised:
        await manager.reserve_verification(_MAXIMUM_OBJECT_SIZE_BYTES + 1)
    assert raised.value.error_code is ErrorCode.OBJECT_STORAGE_INPUT_INVALID
    assert raised.value.safe_details["reason"] is SIZE_OUT_OF_RANGE
    assert manager.reserved_size_bytes == 0


@pytest.mark.asyncio
async def test_janitor_removes_only_stale_grammar_matching_regular_files(
    tmp_path: Path,
) -> None:
    manager = build_spool_manager(tmp_path)
    stale = tmp_path / _spool_file_name()
    stale.write_bytes(b"stale")
    fresh = tmp_path / _spool_file_name()
    fresh.write_bytes(b"fresh")
    unknown = tmp_path / "cas-spool-not-a-uuid.part"
    unknown.write_bytes(b"unknown")
    directory = tmp_path / _spool_file_name()
    directory.mkdir()
    # Realistic epoch mtimes relative to the injected wall clock; the monotonic
    # fake clock stays at 1000.0, proving ages use the wall clock, not
    # time.monotonic-style values.
    stale_mtime = fake_wall_now[0] - _STALE_AFTER_SECONDS - 1
    fresh_mtime = fake_wall_now[0] - 60
    os.utime(stale, (stale_mtime, stale_mtime))
    os.utime(fresh, (fresh_mtime, fresh_mtime))
    os.utime(unknown, (stale_mtime, stale_mtime))

    summary = await manager.cleanup_stale_spools()

    assert summary.examined_count == 3
    assert summary.removed_count == 1
    assert summary.skipped_count == 2
    assert summary.deferred_count == 0
    assert not stale.exists()
    assert fresh.exists()
    assert unknown.exists()
    assert directory.exists()


@requires_posix_symlinks
@pytest.mark.asyncio
async def test_janitor_never_touches_symlinks(tmp_path: Path) -> None:
    manager = build_spool_manager(tmp_path)
    outside_target = tmp_path / "outside.bin"
    outside_target.write_bytes(b"untouched")
    link = tmp_path / _spool_file_name()
    link.symlink_to(outside_target)
    stale_mtime = fake_wall_now[0] - _STALE_AFTER_SECONDS - 1
    os.utime(link, (stale_mtime, stale_mtime), follow_symlinks=False)

    summary = await manager.cleanup_stale_spools()

    assert summary.examined_count == 1
    assert summary.removed_count == 0
    assert summary.skipped_count == 1
    assert link.is_symlink()
    assert outside_target.read_bytes() == b"untouched"


@pytest.mark.asyncio
async def test_janitor_defers_candidates_beyond_the_cap(tmp_path: Path) -> None:
    manager = build_spool_manager(tmp_path, limits=SpoolLimits(maximum_cleanup_candidates=2))
    stale_files = []
    for _ in range(3):
        stale = tmp_path / _spool_file_name()
        stale.write_bytes(b"stale")
        stale_mtime = fake_wall_now[0] - _STALE_AFTER_SECONDS - 1
        os.utime(stale, (stale_mtime, stale_mtime))
        stale_files.append(stale)

    summary = await manager.cleanup_stale_spools()

    assert summary.examined_count == 2
    assert summary.removed_count == 2
    assert summary.deferred_count == 1
    assert len([path for path in tmp_path.iterdir() if path.is_file()]) == 1
