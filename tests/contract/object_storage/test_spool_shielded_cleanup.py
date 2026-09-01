"""Spool shielded-cleanup cancellation contract.

These tests prove the spool's shielded-cleanup helper keeps the same invariant
as the adapter's ``_run_shielded`` (commit ``f9c27df``): a cleanup that itself
fails while the caller is cancelled must never mask that cancellation. The
caller's ``CancelledError`` always propagates; a cleanup failure observed only
during that cancellation is routed to the call site's ``on_cleanup_failure``
sink, and ``None`` means unobservable by design. Without caller cancellation a
failing cleanup propagates its own error unchanged and the sink stays idle.
"""

from __future__ import annotations

import asyncio

import pytest

from r2_object_storage.spool import _run_shielded_cleanup


@pytest.mark.asyncio
async def test_raising_cleanup_does_not_mask_caller_cancellation() -> None:
    """A cleanup that raises during caller cancellation never masks the cancellation."""

    async def cleanup() -> None:
        raise RuntimeError("cleanup exploded")

    task = asyncio.ensure_future(_run_shielded_cleanup(cleanup()))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_cleanup_failure_alone_reaches_the_callback() -> None:
    """Without cancellation the cleanup error propagates unchanged; the sink stays idle."""

    seen: list[BaseException] = []

    async def cleanup() -> None:
        raise RuntimeError("cleanup exploded")

    with pytest.raises(RuntimeError):
        await _run_shielded_cleanup(cleanup(), on_cleanup_failure=seen.append)
    assert len(seen) == 0  # no cancellation: the error propagates unchanged


@pytest.mark.asyncio
async def test_cleanup_failure_during_cancellation_reaches_the_callback() -> None:
    """A cleanup failing while the caller is cancelled routes to the sink exactly once.

    The cancellation still propagates, and the cleanup failure reaches the
    call site's ``on_cleanup_failure`` sink: that sink is the readable surface
    for the newly closed cleanup-failure path.
    """

    allow_cleanup_failure = asyncio.Event()

    async def failing_cleanup() -> None:
        await allow_cleanup_failure.wait()
        raise RuntimeError("cleanup exploded")

    cleanup_failures: list[BaseException] = []

    def record_cleanup_failure(cleanup_error: BaseException) -> None:
        cleanup_failures.append(cleanup_error)

    shield_task = asyncio.ensure_future(
        _run_shielded_cleanup(failing_cleanup(), on_cleanup_failure=record_cleanup_failure)
    )
    await asyncio.sleep(0)  # the shielded cleanup is now in flight
    shield_task.cancel()
    allow_cleanup_failure.set()

    with pytest.raises(asyncio.CancelledError):
        await shield_task

    assert len(cleanup_failures) == 1
    assert isinstance(cleanup_failures[0], RuntimeError)
