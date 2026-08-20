"""R2 adapter concurrency, cancellation and bounded-resource contract.

These tests prove the Task 9 resource contract with real asyncio concurrency,
real cancellation and real spool files under ``tmp_path``: the spool manager
admits at most four in-flight receives while a fifth waits, the aggregate
reservation never exceeds the configured budget while at most one maximum
verification spool runs alongside the retained input spools, same-digest
stores share exactly one R2 resolve/create/verify sequence through the bounded
single-flight table, one waiter's cancellation never cancels the shared owner
or the other waiters, an owner cancellation cleans up its entry without
hanging its waiters, client shutdown runs exactly once, and 10,000 completed
small items leave zero table entries, reservations, permits or spool files.

``build_repeating_store`` is a deterministic scripted client/store/metrics
fixture over an in-memory content-addressed map: it never reads the
environment and never contacts the network. ``run_bounded`` is an async
producer that keeps at most four submitted tasks. Both are local to this file.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import AsyncIterator, Callable, Coroutine, Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError
from tests.contract.object_storage.scripted_s3 import scripted_body

from personal_os.diagnostics import DiagnosticLogger
from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import InternalApplicationError
from personal_os.object_storage import VerificationMethod, VerifiedObjectReceipt
from personal_os.object_storage.errors import STREAM_INVALID, ObjectStorageError
from personal_os.object_storage.keys import CanonicalObjectKey
from r2_object_storage import spool as spool_module
from r2_object_storage.adapter import R2S3ObjectStore
from r2_object_storage.client import GetObjectResult, HeadObjectResult, PutObjectRequest
from r2_object_storage.error_mapping import RetryPolicy
from r2_object_storage.metrics import (
    InMemoryObjectStorageMetrics,
    ObjectStorageOperation,
    ObjectStorageResult,
)
from r2_object_storage.spool import SpoolLimits, SpoolManager

#: One mebibyte, the spool chunk size and the scaled test object size.
_ONE_MEBIBYTE = 1_048_576
_FIXED_NOW = datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC)
_FREE_SPACE_BYTES = 8 * 1024 * 1024 * 1024


def _no_sleep(_: float) -> Coroutine[None, None, None]:
    async def _sleep() -> None:
        return None

    return _sleep()


def _zero_jitter(low: float, high: float) -> float:
    return low


async def _chunk_stream(payloads: tuple[bytes, ...]) -> AsyncIterator[bytes]:
    for payload in payloads:
        yield payload


def chunks(*payloads: bytes) -> AsyncIterator[bytes]:
    """Wrap fixed payloads as an asynchronous byte stream for ``store_stream``."""

    return _chunk_stream(payloads)


class _ConcurrentBodyTracker:
    """Counts-only tracker of verification bodies consumed concurrently."""

    def __init__(self) -> None:
        self.active_count = 0
        self.maximum_active_count = 0

    def entered(self) -> None:
        self.active_count += 1
        self.maximum_active_count = max(self.maximum_active_count, self.active_count)

    def exited(self) -> None:
        self.active_count = max(0, self.active_count - 1)


class _CountingBody:
    """A scripted streaming body that tracks concurrent consumption."""

    def __init__(self, payload: bytes, tracker: _ConcurrentBodyTracker) -> None:
        self._iterator = scripted_body([payload] if payload else []).__aiter__()
        self._tracker = tracker
        self._is_active = False
        self._is_finished = False

    def __aiter__(self) -> _CountingBody:
        return self

    async def __anext__(self) -> bytes:
        if not self._is_active:
            self._is_active = True
            self._tracker.entered()
        try:
            chunk = await self._iterator.__anext__()
        except StopAsyncIteration:
            self._finish()
            raise
        return chunk

    async def aclose(self) -> None:
        self._finish()

    def _finish(self) -> None:
        if self._is_active and not self._is_finished:
            self._is_finished = True
            self._tracker.exited()


class RepeatingStoreClient:
    """Deterministic in-memory content-addressed fake of ``S3ClientProtocol``.

    Repeatedly serves store traffic for any number of generated digests:
    ``head_object`` answers from the uploaded map, ``put_object`` reads the
    caller's spool file once and records its bytes under the canonical key,
    and ``get_object`` serves those exact bytes behind the served ETag with a
    body that tracks concurrent verification reads. An optional ``head_gate``
    holds every HEAD until set so tests can pin owner/waiter ordering
    deterministically. Every call samples the aggregate spool reservation the
    adapter's spool manager currently holds. Nothing here reads the
    environment, hashes keys, contacts the network or logs content.
    """

    def __init__(
        self,
        spools: SpoolManager | None = None,
        *,
        head_gate: asyncio.Event | None = None,
        head_failure: BaseException | None = None,
        get_failure: BaseException | None = None,
    ) -> None:
        self._objects: dict[str, bytes] = {}
        self._etags: dict[str, str] = {}
        self._spools = spools
        self._head_gate = head_gate
        self._head_failure = head_failure
        self._get_failure = get_failure
        self._bodies = _ConcurrentBodyTracker()
        self.calls: list[str] = []
        self.close_count = 0
        self.maximum_observed_reserved_bytes = 0
        self.first_put_observed_reserved_bytes: int | None = None

    @property
    def object_count(self) -> int:
        return len(self._objects)

    @property
    def maximum_concurrent_bodies(self) -> int:
        return self._bodies.maximum_active_count

    def _observe_reserved(self) -> None:
        if self._spools is not None:
            self.maximum_observed_reserved_bytes = max(
                self.maximum_observed_reserved_bytes, self._spools.reserved_size_bytes
            )

    async def head_object(self, object_key: CanonicalObjectKey) -> HeadObjectResult | None:
        self.calls.append("head_object")
        if self._head_gate is not None:
            await self._head_gate.wait()
        self._observe_reserved()
        if self._head_failure is not None:
            raise self._head_failure
        payload = self._objects.get(str(object_key))
        if payload is None:
            return None
        return HeadObjectResult(
            size_bytes=len(payload),
            media_type="application/octet-stream",
            etag=self._etags[str(object_key)],
        )

    async def put_object(self, request: PutObjectRequest) -> None:
        self.calls.append("put_object")
        self._observe_reserved()
        if self.first_put_observed_reserved_bytes is None and self._spools is not None:
            self.first_put_observed_reserved_bytes = self._spools.reserved_size_bytes
        key = str(request.object_key)
        self._objects[key] = request.spool_path.read_bytes()
        self._etags[key] = f"etag-{key[:12]}"

    async def get_object(self, object_key: CanonicalObjectKey, *, if_match: str) -> GetObjectResult:
        self.calls.append("get_object")
        self._observe_reserved()
        if self._get_failure is not None:
            raise self._get_failure
        key = str(object_key)
        if if_match != self._etags.get(key):
            raise AssertionError("conditional GET did not carry the served ETag")
        return GetObjectResult(body=_CountingBody(self._objects[key], self._bodies))

    async def head_bucket(self) -> None:
        self.calls.append("head_bucket")
        return None

    async def close(self) -> None:
        self.close_count += 1


def build_repeating_store(
    tmp_path: Path,
    *,
    limits: SpoolLimits | None = None,
    head_gate: asyncio.Event | None = None,
    head_failure: BaseException | None = None,
    get_failure: BaseException | None = None,
    metrics: InMemoryObjectStorageMetrics | None = None,
) -> tuple[R2S3ObjectStore, RepeatingStoreClient, InMemoryObjectStorageMetrics]:
    """Build a deterministic scripted client/store/metrics fixture.

    Supplies a :class:`SpoolManager` rooted at ``tmp_path`` with fixed
    environment-free wiring: deterministic clocks, ample injected free space
    and optional scaled limits, a three-attempt :class:`RetryPolicy`, an
    in-memory metrics sink and a captured :class:`DiagnosticLogger`. The
    scripted client repeatedly serves store traffic for generated digests
    without contacting the network.
    """

    root_logger = logging.getLogger()
    if not any(isinstance(handler, logging.NullHandler) for handler in root_logger.handlers):
        root_logger.addHandler(logging.NullHandler())
    spools = SpoolManager(
        tmp_path,
        limits=limits,
        clock=lambda: 0.0,
        wall_clock=lambda: 0.0,
        disk_usage=lambda _root: SimpleNamespace(free=_FREE_SPACE_BYTES),
    )
    client = RepeatingStoreClient(
        spools,
        head_gate=head_gate,
        head_failure=head_failure,
        get_failure=get_failure,
    )
    metrics = metrics if metrics is not None else InMemoryObjectStorageMetrics()
    logger = DiagnosticLogger({"service": "test", "environment": "test"})
    store = R2S3ObjectStore(
        client,
        spools=spools,
        retry=RetryPolicy(maximum_attempts=3),
        metrics=metrics,
        logger=logger,
        now_utc=lambda: _FIXED_NOW,
        monotonic=lambda: 0.0,
        sleep=_no_sleep,
        jitter=_zero_jitter,
    )
    return store, client, metrics


def _client_error(code: str, status: int) -> ClientError:
    return ClientError(
        {
            "Error": {"Code": code, "Message": "provider-message-must-remain-private"},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        "HeadObject",
    )


class _ReservationRecordingMetrics(InMemoryObjectStorageMetrics):
    """Test sink retaining every gauge emission in arrival order."""

    def __init__(self) -> None:
        super().__init__()
        self.reservation_samples: list[int] = []

    def record_reserved_bytes(self, *, operation: ObjectStorageOperation, size_bytes: int) -> None:
        super().record_reserved_bytes(operation=operation, size_bytes=size_bytes)
        self.reservation_samples.append(size_bytes)


async def run_bounded(
    stores: Iterator[Coroutine[object, object, VerifiedObjectReceipt]],
) -> None:
    """Run ``stores`` as an async producer keeping at most four submitted tasks.

    A new task is created only while fewer than four submitted tasks remain
    pending; every completion is re-raised immediately so a failure never
    hides behind later items.
    """

    submitted: set[asyncio.Task[VerifiedObjectReceipt]] = set()
    for store_coroutine in stores:
        while len(submitted) >= 4:
            done, pending = await asyncio.wait(submitted, return_when=asyncio.FIRST_COMPLETED)
            submitted = set(pending)
            for task in done:
                task.result()
        submitted.add(asyncio.create_task(store_coroutine))
    await asyncio.gather(*submitted)


async def _wait_until(condition: Callable[[], bool], *, description: str) -> None:
    """Yield to the loop until ``condition`` holds; fail loud if it never does.

    Each iteration first yields without delay and then sleeps briefly, so the
    2000-iteration budget spans real wall time: waiting on waiter tasks that
    are still hashing their spool in executor threads cannot exhaust the loop
    in a few milliseconds when the machine is under full-suite load.
    """

    for attempt in range(2000):
        if condition():
            return
        await asyncio.sleep(0 if attempt % 10 == 0 else 0.001)
    raise AssertionError(f"condition not reached: {description}")


def _assert_no_residual_state(store: R2S3ObjectStore, tmp_path: Path) -> None:
    assert store.single_flight_entry_count == 0
    assert store.single_flight_waiter_count == 0
    assert store.spool_manager.reserved_size_bytes == 0
    assert store.spool_manager.in_flight_count == 0
    assert list(tmp_path.iterdir()) == []


# --- Admission and receive backstops --------------------------------------


@pytest.mark.asyncio
async def test_free_space_probe_does_not_block_independent_async_work(tmp_path: Path) -> None:
    """A blocking filesystem probe yields the event loop while admission is locked."""

    probe_started = threading.Event()
    independent_task_completed = threading.Event()
    allow_probe_return = threading.Event()
    independent_completed_before_probe_return = [False]
    releaser_failures: list[BaseException] = []

    def blocking_disk_usage(_root: Path) -> SimpleNamespace:
        probe_started.set()
        allow_probe_return.wait()
        return SimpleNamespace(free=_FREE_SPACE_BYTES)

    def release_probe_after_loop_progress() -> None:
        try:
            assert probe_started.wait(timeout=1)
            independent_completed_before_probe_return[0] = independent_task_completed.wait(
                timeout=1
            )
        except BaseException as error:
            releaser_failures.append(error)
        finally:
            allow_probe_return.set()

    releaser = threading.Thread(target=release_probe_after_loop_progress)
    releaser.start()
    manager = SpoolManager(tmp_path, disk_usage=blocking_disk_usage)

    async def complete_independent_work() -> None:
        await asyncio.sleep(0)
        independent_task_completed.set()

    reservation_task = asyncio.create_task(manager.reserve_verification(1))
    independent_task = asyncio.create_task(complete_independent_work())
    try:
        reservation = await asyncio.wait_for(reservation_task, timeout=2)
        await asyncio.wait_for(independent_task, timeout=2)
    finally:
        await asyncio.to_thread(releaser.join, 1)

    assert not releaser.is_alive()
    if releaser_failures:
        raise releaser_failures[0]
    assert independent_completed_before_probe_return == [True]
    await reservation.close()
    assert list(tmp_path.iterdir()) == []
    assert manager.reserved_size_bytes == 0
    assert manager.in_flight_count == 0


@pytest.mark.asyncio
async def test_stalled_receive_uses_real_time_backstop_and_cleans_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stream stuck inside ``__anext__`` cannot retain admission state indefinitely."""

    monkeypatch.setattr(spool_module, "_RECEIVE_WINDOW_SECONDS", 0.01)
    manager = SpoolManager(
        tmp_path,
        disk_usage=lambda _root: SimpleNamespace(free=_FREE_SPACE_BYTES),
    )
    never_resume = asyncio.Event()

    async def stalled_stream() -> AsyncIterator[bytes]:
        await never_resume.wait()
        yield b"unreachable"

    with pytest.raises(ObjectStorageError) as raised:
        async with manager.receive_stream(stalled_stream(), 1):
            pytest.fail("a stalled stream must not yield a spool")

    assert raised.value.error_code is ErrorCode.OBJECT_STORAGE_INPUT_INVALID
    assert raised.value.safe_details["reason"] is STREAM_INVALID
    assert list(tmp_path.iterdir()) == []
    assert manager.reserved_size_bytes == 0
    assert manager.in_flight_count == 0


# --- Four permits and aggregate reservation -------------------------------


@pytest.mark.asyncio
async def test_four_active_permits_and_fifth_waits(tmp_path: Path) -> None:
    head_gate = asyncio.Event()
    store, client, metrics = build_repeating_store(tmp_path, head_gate=head_gate)
    tasks = [
        asyncio.create_task(
            store.store_stream(chunks(f"permit-{index}".encode()), 8, "application/octet-stream")
        )
        for index in range(5)
    ]

    await _wait_until(
        lambda: store.spool_manager.in_flight_count == 4 and store.single_flight_entry_count == 4,
        description="four receive permits admitted and four entries joined",
    )
    assert not tasks[4].done()
    assert metrics.in_flight_count(ObjectStorageOperation.STORE) == 5
    assert metrics.maximum_in_flight == 5

    head_gate.set()
    receipts = await asyncio.gather(*tasks)

    assert len({receipt.content_digest for receipt in receipts}) == 5
    assert client.object_count == 5
    _assert_no_residual_state(store, tmp_path)


@pytest.mark.asyncio
async def test_aggregate_reservation_capped_with_one_verification_spool(
    tmp_path: Path,
) -> None:
    budget_bytes = 5 * _ONE_MEBIBYTE
    head_gate = asyncio.Event()
    limits = SpoolLimits(
        maximum_object_size_bytes=_ONE_MEBIBYTE,
        maximum_reserved_size_bytes=budget_bytes,
    )
    store, client, metrics = build_repeating_store(tmp_path, limits=limits, head_gate=head_gate)
    payloads = [bytes([index]) * _ONE_MEBIBYTE for index in range(4)]
    tasks = [
        asyncio.create_task(
            store.store_stream(chunks(payload), _ONE_MEBIBYTE, "application/octet-stream")
        )
        for payload in payloads
    ]

    await _wait_until(
        lambda: store.spool_manager.in_flight_count == 4,
        description="four maximum input spools retained",
    )
    head_gate.set()
    receipts = await asyncio.gather(*tasks)

    assert len(receipts) == 4
    # All four input spools were retained together at the first conditional
    # PUT; the observed aggregate peak is exactly four inputs plus one maximum
    # verification spool, never the unbounded eight.
    assert client.first_put_observed_reserved_bytes == 4 * _ONE_MEBIBYTE
    assert client.maximum_observed_reserved_bytes == budget_bytes
    assert metrics.maximum_reserved_size_bytes == budget_bytes
    _assert_no_residual_state(store, tmp_path)


# --- Same-digest single flight ---------------------------------------------


@pytest.mark.asyncio
async def test_same_digest_stores_share_one_r2_sequence(tmp_path: Path) -> None:
    head_gate = asyncio.Event()
    store, client, metrics = build_repeating_store(tmp_path, head_gate=head_gate)
    payload = b"shared single-flight payload"
    owner = asyncio.create_task(
        store.store_stream(chunks(payload), len(payload), "application/octet-stream")
    )
    await _wait_until(
        lambda: store.single_flight_entry_count == 1, description="owner joined the table"
    )
    waiter = asyncio.create_task(
        store.store_stream(chunks(payload), len(payload), "application/octet-stream")
    )
    await _wait_until(
        lambda: store.single_flight_waiter_count == 1, description="waiter joined the entry"
    )

    head_gate.set()
    owner_receipt, waiter_receipt = await asyncio.gather(owner, waiter)

    # Both callers receive a fully verified receipt while the scripted client
    # saw exactly one store's worth of R2 work: HEAD, conditional PUT,
    # verification HEAD, verification GET.
    assert owner_receipt.content_digest == waiter_receipt.content_digest
    assert owner_receipt.verification_method is VerificationMethod.UPLOADED_FULL_READ
    assert waiter_receipt.verification_method is VerificationMethod.UPLOADED_FULL_READ
    assert client.calls == ["head_object", "put_object", "head_object", "get_object"]
    assert client.object_count == 1
    store_records = [
        record for record in metrics.operations if record.operation is ObjectStorageOperation.STORE
    ]
    assert len(store_records) == 2
    assert metrics.maximum_in_flight == 2
    _assert_no_residual_state(store, tmp_path)


@pytest.mark.asyncio
async def test_same_digest_failure_is_fresh_for_waiter_with_zero_attempts(
    tmp_path: Path,
) -> None:
    """A waiter gets an equivalent typed failure without inheriting owner work."""

    head_gate = asyncio.Event()
    store, _client, metrics = build_repeating_store(
        tmp_path,
        head_gate=head_gate,
        head_failure=_client_error("AccessDenied", 403),
    )
    payload = b"shared single-flight failure"
    owner = asyncio.create_task(
        store.store_stream(chunks(payload), len(payload), "application/octet-stream")
    )
    await _wait_until(
        lambda: store.single_flight_entry_count == 1, description="owner joined the table"
    )
    waiter = asyncio.create_task(
        store.store_stream(chunks(payload), len(payload), "application/octet-stream")
    )
    await _wait_until(
        lambda: store.single_flight_waiter_count == 1, description="waiter joined the entry"
    )

    head_gate.set()
    owner_failure, waiter_failure = await asyncio.gather(owner, waiter, return_exceptions=True)

    assert isinstance(owner_failure, ObjectStorageError)
    assert isinstance(waiter_failure, ObjectStorageError)
    assert owner_failure.error_code is ErrorCode.OBJECT_STORAGE_ACCESS_DENIED
    assert waiter_failure.to_safe_dict() == owner_failure.to_safe_dict()
    assert waiter_failure is not owner_failure
    assert waiter_failure.__context__ is None
    assert waiter_failure.__cause__ is None
    failed_records = [
        record
        for record in metrics.operations
        if record.operation is ObjectStorageOperation.STORE
        and record.result is ObjectStorageResult.FAILED
    ]
    assert sorted(record.attempt_count for record in failed_records) == [0, 1]
    _assert_no_residual_state(store, tmp_path)


@pytest.mark.asyncio
async def test_internal_application_error_records_failed_operation(tmp_path: Path) -> None:
    """Unknown provider-boundary failures still count as failed operations."""

    store, _client, metrics = build_repeating_store(
        tmp_path,
        head_failure=RuntimeError("internal-provider-boundary-failure"),
    )
    payload = b"internal failure metrics"

    with pytest.raises(InternalApplicationError) as raised:
        await store.store_stream(chunks(payload), len(payload), "application/octet-stream")

    assert raised.value.error_code is ErrorCode.INTERNAL_ERROR
    failed_records = [
        record
        for record in metrics.operations
        if record.operation is ObjectStorageOperation.STORE
        and record.result is ObjectStorageResult.FAILED
    ]
    assert len(failed_records) == 1
    assert failed_records[0].error_code is ErrorCode.INTERNAL_ERROR
    assert failed_records[0].attempt_count == 1
    _assert_no_residual_state(store, tmp_path)


@pytest.mark.asyncio
async def test_reservation_gauge_emits_after_failed_verification_mutations(
    tmp_path: Path,
) -> None:
    """Verification acquire/release samples survive a failure between them."""

    metrics = _ReservationRecordingMetrics()
    store, _client, _ = build_repeating_store(
        tmp_path,
        get_failure=_client_error("AccessDenied", 403),
        metrics=metrics,
    )
    payload = b"reservation mutation failure"

    with pytest.raises(ObjectStorageError) as raised:
        await store.store_stream(chunks(payload), len(payload), "application/octet-stream")

    assert raised.value.error_code is ErrorCode.OBJECT_STORAGE_ACCESS_DENIED
    assert metrics.reservation_samples == [
        len(payload),
        2 * len(payload),
        len(payload),
        0,
    ]
    _assert_no_residual_state(store, tmp_path)


@pytest.mark.asyncio
async def test_waiter_cancellation_keeps_owner_and_other_waiters_running(
    tmp_path: Path,
) -> None:
    head_gate = asyncio.Event()
    store, client, _metrics = build_repeating_store(tmp_path, head_gate=head_gate)
    payload = b"waiter cancellation payload"
    owner = asyncio.create_task(
        store.store_stream(chunks(payload), len(payload), "application/octet-stream")
    )
    await _wait_until(
        lambda: store.single_flight_entry_count == 1, description="owner joined the table"
    )
    waiter_kept = asyncio.create_task(
        store.store_stream(chunks(payload), len(payload), "application/octet-stream")
    )
    waiter_cancelled = asyncio.create_task(
        store.store_stream(chunks(payload), len(payload), "application/octet-stream")
    )
    await _wait_until(
        lambda: store.single_flight_waiter_count == 2,
        description="both waiters joined the entry",
    )

    waiter_cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter_cancelled

    # The cancelled waiter detached without cancelling the shared owner or the
    # surviving waiter: the entry remains with exactly one waiter.
    await _wait_until(
        lambda: store.single_flight_waiter_count == 1,
        description="cancelled waiter detached",
    )
    assert store.single_flight_entry_count == 1
    assert not owner.done()
    assert not waiter_kept.done()

    head_gate.set()
    owner_receipt, kept_receipt = await asyncio.gather(owner, waiter_kept)

    assert owner_receipt.content_digest == kept_receipt.content_digest
    assert client.calls == ["head_object", "put_object", "head_object", "get_object"]
    _assert_no_residual_state(store, tmp_path)


@pytest.mark.asyncio
async def test_owner_cancellation_cleans_entry_and_fails_waiter_closed(
    tmp_path: Path,
) -> None:
    head_gate = asyncio.Event()
    store, client, metrics = build_repeating_store(tmp_path, head_gate=head_gate)
    payload = b"owner cancellation payload"
    owner = asyncio.create_task(
        store.store_stream(chunks(payload), len(payload), "application/octet-stream")
    )
    await _wait_until(
        lambda: store.single_flight_entry_count == 1, description="owner joined the table"
    )
    waiter = asyncio.create_task(
        store.store_stream(chunks(payload), len(payload), "application/octet-stream")
    )
    await _wait_until(
        lambda: store.single_flight_waiter_count == 1, description="waiter joined the entry"
    )

    owner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await owner

    # The waiter does not hang: the removed entry surfaces a typed unavailable
    # outcome, never a provider value.
    with pytest.raises(ObjectStorageError) as waiter_error:
        await waiter
    assert waiter_error.value.error_code is ErrorCode.OBJECT_STORAGE_UNAVAILABLE

    assert client.calls == ["head_object"]
    failed_records = [
        record
        for record in metrics.operations
        if record.operation is ObjectStorageOperation.STORE and record.error_code is not None
    ]
    assert len(failed_records) == 1
    assert failed_records[0].error_code is ErrorCode.OBJECT_STORAGE_UNAVAILABLE
    _assert_no_residual_state(store, tmp_path)


# --- Client shutdown and the 10,000-item capstone --------------------------


@pytest.mark.asyncio
async def test_close_shuts_client_down_exactly_once(tmp_path: Path) -> None:
    store, client, _metrics = build_repeating_store(tmp_path)
    payload = b"shutdown payload"

    receipt = await store.store_stream(chunks(payload), len(payload), "application/octet-stream")

    assert receipt.size_bytes == len(payload)
    await store.close()
    await store.close()
    assert client.close_count == 1
    _assert_no_residual_state(store, tmp_path)


@pytest.mark.asyncio
async def test_ten_thousand_items_leave_constant_state(tmp_path: Path) -> None:
    store, client, metrics = build_repeating_store(tmp_path)
    await run_bounded(
        store.store_stream(chunks(index.to_bytes(4, "big")), 4, "application/octet-stream")
        for index in range(10_000)
    )
    assert store.single_flight_entry_count == 0
    assert store.spool_manager.reserved_size_bytes == 0
    assert store.spool_manager.in_flight_count == 0
    assert metrics.maximum_in_flight <= 4
    assert metrics.maximum_reserved_size_bytes <= 536_870_912
    # Every item really completed its full HEAD/PUT/HEAD/GET sequence against
    # the scripted client; 10,000 distinct canonical objects exist.
    assert len(client.calls) == 40_000
    assert client.object_count == 10_000
