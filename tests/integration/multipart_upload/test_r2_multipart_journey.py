"""Real R2 multipart journey proof: resume, refusal, replay and exact cleanup.

Design section 9.2: every case here runs the composed production service —
the durable PostgreSQL session store, the real
:class:`R2MultipartStagingProvider` and the canonical ``R2S3ObjectStore`` —
against one dedicated private R2 TEST bucket through the live harness. The
harness PUTs each part through the real presigned URL, records every staging
identity in the per-run exact cleanup manifest before the first provider
mutation that can create it, and its teardown deletes exactly the recorded
identities — never a listing, prefix or wildcard. Nine spec cases are proved:
success, resume, corruption refusal, lost complete response, incomplete
abort, completed delete, expiry, failure/retry and cancellation.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from tests.integration.multipart_upload.conftest import LiveR2MultipartHarness

from personal_os.error_contracts.codes import ErrorCode
from personal_os.multipart_upload.contracts import MultipartSessionState
from personal_os.multipart_upload.errors import MultipartUploadError
from postgresql_source_store.multipart_upload_store import (
    MULTIPART_CLEANUP_RETRY_BASE_SECONDS,
)

pytestmark = [pytest.mark.local_stack, pytest.mark.r2_live]


@pytest.mark.asyncio
async def test_lost_complete_response_replays_one_version_and_cleans_exact_staging(
    live_harness: LiveR2MultipartHarness,
) -> None:
    await live_harness.upload_all_parts()
    await live_harness.drop_next_complete_response()
    await live_harness.complete_then_replay()
    assert await live_harness.source_version_count() == 1
    assert await live_harness.cleanup_manifest_contains_only_session_resources()


@pytest.mark.asyncio
async def test_corrupt_part_cannot_publish(live_harness: LiveR2MultipartHarness) -> None:
    await live_harness.upload_corrupt_part()
    await live_harness.complete_expect_integrity_failure()
    assert await live_harness.source_version_count() == 0


@pytest.mark.asyncio
async def test_successful_journey_commits_one_version_and_deletes_staging(
    live_harness: LiveR2MultipartHarness,
) -> None:
    await live_harness.upload_all_parts()
    completion = await live_harness.complete()

    assert completion.state is MultipartSessionState.COMMITTED
    assert completion.terminal_result is not None
    assert await live_harness.source_version_count() == 1
    assert await live_harness.sync_event_count() == 1
    # Completed delete (spec 6.3.7): the committed session's staging object is
    # gone while the exact canonical object of the declared digest exists.
    assert not await live_harness.staging_object_exists()
    assert not await live_harness.upload_in_flight()
    assert await live_harness.canonical_object_exists()
    assert await live_harness.cleanup_manifest_contains_only_session_resources()


@pytest.mark.asyncio
async def test_resumed_session_skips_completed_parts_and_publishes_once(
    live_harness: LiveR2MultipartHarness,
) -> None:
    plan = await live_harness.open_transfer()
    await live_harness.upload_part(1)

    # The restart-resume shape (spec 6.1): the client keeps its opaque
    # session identity, and one safe status reconciles the completed parts
    # from the provider's own observation of the real staging upload.
    status = await live_harness.service.status(
        session_id=plan.session_id,
        device_context=live_harness.device,
        diagnostic_context=live_harness.diagnostic_context(),
    )
    assert status.session_id == plan.session_id
    assert status.completed_part_numbers == frozenset({1})

    await live_harness.upload_remaining_parts()
    completion = await live_harness.complete()
    assert completion.state is MultipartSessionState.COMMITTED
    # The interrupted part was never retransmitted after the restart.
    assert live_harness.part_put_count(1) == 1
    assert await live_harness.source_version_count() == 1
    assert await live_harness.sync_event_count() == 1
    assert not await live_harness.staging_object_exists()
    assert await live_harness.canonical_object_exists()


@pytest.mark.asyncio
async def test_cancelled_incomplete_upload_aborts_exact_staging_without_publication(
    live_harness: LiveR2MultipartHarness,
) -> None:
    await live_harness.upload_part(1)

    cancelled = await live_harness.abort()

    assert cancelled.state is MultipartSessionState.CANCELLING
    assert await live_harness.source_version_count() == 0
    outcome = await live_harness.run_cleanup()
    assert outcome.cleaned_count == 1
    assert outcome.failed_count == 0
    # Incomplete abort: the real provider upload is aborted and the exact
    # staging object is absent — no canonical object was ever created.
    assert not await live_harness.upload_in_flight()
    assert not await live_harness.staging_object_exists()
    assert not await live_harness.canonical_object_exists()
    row = await live_harness.session_row()
    assert row["state"] == "cleaned"
    assert row["cleanup_state"] == "succeeded"


@pytest.mark.asyncio
async def test_expired_session_cleans_exact_staging_after_deadline(
    live_harness: LiveR2MultipartHarness,
) -> None:
    await live_harness.upload_part(1)
    live_harness.advance_clock(timedelta(hours=25))

    outcome = await live_harness.run_cleanup()

    assert outcome.cleaned_count == 1
    assert outcome.failed_count == 0
    row = await live_harness.session_row()
    assert row["state"] == "cleaned"
    assert not await live_harness.upload_in_flight()
    assert not await live_harness.staging_object_exists()
    assert await live_harness.source_version_count() == 0
    assert await live_harness.cleanup_manifest_contains_only_session_resources()


@pytest.mark.asyncio
async def test_failed_cleanup_records_closed_reason_then_next_retry_cleans(
    live_harness: LiveR2MultipartHarness,
) -> None:
    await live_harness.upload_part(1)
    await live_harness.abort()
    live_harness.fail_next_staging_delete(ErrorCode.MULTIPART_DEPENDENCY_UNAVAILABLE)

    first = await live_harness.run_cleanup()

    assert first.cleaned_count == 0
    assert first.failed_count == 1
    row = await live_harness.session_row()
    assert row["state"] == "cleanup_pending"
    assert row["cleanup_state"] == "failed"
    assert row["cleanup_reason_code"] == "multipart_dependency_unavailable"
    assert row["cleanup_next_retry_at"] is not None

    # The bounded backoff hides the failed row until its exact retry deadline.
    live_harness.advance_clock(
        timedelta(seconds=MULTIPART_CLEANUP_RETRY_BASE_SECONDS + 1)
    )
    second = await live_harness.run_cleanup()
    assert second.cleaned_count == 1
    assert second.failed_count == 0
    final_row = await live_harness.session_row()
    assert final_row["state"] == "cleaned"
    assert final_row["cleanup_state"] == "succeeded"
    assert not await live_harness.upload_in_flight()
    assert not await live_harness.staging_object_exists()


@pytest.mark.asyncio
async def test_upload_part_outside_geometry_is_rejected_without_provider_mutation(
    live_harness: LiveR2MultipartHarness,
) -> None:
    await live_harness.open_transfer()

    with pytest.raises(MultipartUploadError) as rejection:
        await live_harness.service.issue_part_url(
            session_id=live_harness.session_id,
            part_number=live_harness.part_count + 1,
            device_context=live_harness.device,
            diagnostic_context=live_harness.diagnostic_context(),
        )
    assert rejection.value.error_code is ErrorCode.MULTIPART_PART_INVALID
