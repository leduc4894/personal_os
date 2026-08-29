"""Real R2/PostgreSQL races over one frozen multipart event (spec 9.2).

Concurrent create/resume/complete for one event and competing device/session
authorization attempts run against the composed production service, the real
durable store and the real dedicated R2 TEST bucket. Every race resolves into
exactly one durable session, one provider staging upload, one committed
source version and one canonical object, and a competing device never
observes or touches another device's session.
"""

from __future__ import annotations

import asyncio

import pytest
from tests.integration.multipart_upload.conftest import LiveR2MultipartHarness

from personal_os.error_contracts.codes import ErrorCode
from personal_os.multipart_upload.contracts import MultipartSessionState
from personal_os.multipart_upload.errors import MultipartUploadError

pytestmark = [pytest.mark.local_stack, pytest.mark.r2_live]


@pytest.mark.asyncio
async def test_concurrent_create_resolves_one_session_and_one_staging_upload(
    live_harness: LiveR2MultipartHarness,
) -> None:
    results = await asyncio.gather(
        live_harness.open_transfer(),
        live_harness.open_transfer(),
        return_exceptions=True,
    )
    plans = [result for result in results if not isinstance(result, BaseException)]
    assert plans, "at least one concurrent create must return a plan"
    for result in results:
        if isinstance(result, BaseException):
            assert isinstance(result, MultipartUploadError), (
                "a lost concurrent create must surface the closed typed error"
            )

    # One durable session row ever; every returned plan addresses it.
    assert await live_harness.session_count() == 1
    for plan in plans:
        assert plan.session_id == live_harness.session_id
    assert live_harness.staging_key_count() == 1
    assert await live_harness.cleanup_manifest_contains_only_session_resources()


@pytest.mark.asyncio
async def test_foreign_device_cannot_touch_another_devices_session(
    live_harness: LiveR2MultipartHarness,
) -> None:
    await live_harness.open_transfer()
    await live_harness.upload_part(1)
    foreign = await live_harness.seed_foreign_device()

    with pytest.raises(MultipartUploadError) as url_rejection:
        await live_harness.service.issue_part_url(
            session_id=live_harness.session_id,
            part_number=2,
            device_context=foreign,
            diagnostic_context=live_harness.diagnostic_context(),
        )
    assert url_rejection.value.error_code is ErrorCode.MULTIPART_SESSION_NOT_FOUND

    with pytest.raises(MultipartUploadError) as completion_rejection:
        await live_harness.service.complete(
            session_id=live_harness.session_id,
            device_context=foreign,
            diagnostic_context=live_harness.diagnostic_context(),
        )
    assert completion_rejection.value.error_code is ErrorCode.MULTIPART_SESSION_NOT_FOUND

    # The owner still owns the session; the competing device mutated nothing.
    status = await live_harness.service.status(
        session_id=live_harness.session_id,
        device_context=live_harness.device,
        diagnostic_context=live_harness.diagnostic_context(),
    )
    assert status.completed_part_numbers == frozenset({1})
    assert await live_harness.source_version_count() == 0


@pytest.mark.asyncio
async def test_concurrent_completion_commits_exactly_one_version(
    live_harness: LiveR2MultipartHarness,
) -> None:
    await live_harness.upload_all_parts()

    results = await asyncio.gather(
        live_harness.complete(),
        live_harness.complete(),
        return_exceptions=True,
    )
    committed = [
        result
        for result in results
        if not isinstance(result, BaseException) and result.state is MultipartSessionState.COMMITTED
    ]
    assert committed, "at least one concurrent completion must commit"
    for result in results:
        if isinstance(result, BaseException):
            assert isinstance(result, MultipartUploadError)
            assert result.error_code is ErrorCode.MULTIPART_COMPLETION_IN_PROGRESS, (
                f"unexpected concurrent completion failure: {result.error_code.value}"
            )

    assert await live_harness.source_version_count() == 1
    assert await live_harness.sync_event_count() == 1
    assert await live_harness.canonical_object_exists()
    assert not await live_harness.staging_object_exists()
    assert await live_harness.cleanup_manifest_contains_only_session_resources()
