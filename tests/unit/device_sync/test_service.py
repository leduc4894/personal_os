"""Device-sync service instrumentation: outcomes, reasons and privacy.

Proves every service method records its closed operation outcome and emits
the registered structured event before a typed error re-raises unchanged:
successes emit ``device_sync_operation_completed`` carrying only the
operation and duration, non-retryable rejections and retryable failures emit
``device_sync_operation_rejected``/``device_sync_operation_failed`` carrying
only the operation, the closed reason code and the duration, caller
cancellation is never caught or metered, an absent sink degrades to
build-only validation, and no identifier, locator or digest ever reaches a
metric label or event field.
"""

from __future__ import annotations

import asyncio

import pytest
from tests.unit.device_sync.fakes import (
    GapRaisingEventStore,
    RecordingEventSink,
    SequenceMonotonic,
    UnusedManifestStore,
    build_actions_query,
    build_append_command,
    build_complete_command,
    build_device_sync_context,
    build_diagnostic_context,
    build_finalize_command,
    build_service_harness,
    build_start_command,
)

from personal_os.device_sync.contracts import MAX_PULL_EVENTS
from personal_os.device_sync.errors import DeviceSyncError, DeviceSyncErrorCode
from personal_os.device_sync.metrics import (
    DeviceSyncOperation,
    DeviceSyncOutcome,
    InMemoryDeviceSyncMetrics,
)
from personal_os.device_sync.service import DeviceSyncService
from personal_os.diagnostics.events import EventName


@pytest.mark.asyncio
async def test_pull_gap_records_closed_reason_before_reraising() -> None:
    sink = RecordingEventSink()
    service = DeviceSyncService(
        events=GapRaisingEventStore(),
        manifests=UnusedManifestStore(),
        metrics=InMemoryDeviceSyncMetrics(),
        diagnostics=sink,
        monotonic=SequenceMonotonic(moments=[10.0, 10.25]),
    )
    with pytest.raises(DeviceSyncError) as raised:
        await service.pull_events(
            context=build_device_sync_context(),
            diagnostic_context=build_diagnostic_context(),
        )
    assert raised.value.code is DeviceSyncErrorCode.CURSOR_GAP
    fields = sink.last_fields()
    assert fields["operation"] is DeviceSyncOperation.PULL
    assert fields["reason"] is DeviceSyncErrorCode.CURSOR_GAP
    assert isinstance(fields["duration_ms"], int)
    assert fields["duration_ms"] == 250
    assert sink.last_event_name() is EventName.DEVICE_SYNC_OPERATION_REJECTED


@pytest.mark.asyncio
async def test_pull_success_records_the_closed_operation_outcome() -> None:
    harness = build_service_harness()
    context = build_device_sync_context()
    page = await harness.service.pull_events(
        context=context, diagnostic_context=build_diagnostic_context()
    )
    assert page == harness.events.page
    assert harness.events.pull_limits == [MAX_PULL_EVENTS]
    assert (
        harness.metrics.operation_count(
            operation=DeviceSyncOperation.PULL, outcome=DeviceSyncOutcome.SUCCEEDED
        )
        == 1
    )
    assert harness.sink.last_event_name() is EventName.DEVICE_SYNC_OPERATION_COMPLETED
    fields = harness.sink.last_fields()
    assert set(fields) == {"operation", "duration_ms"}
    assert fields["operation"] is DeviceSyncOperation.PULL
    assert fields["duration_ms"] == 500


@pytest.mark.asyncio
async def test_retryable_failure_records_the_failed_outcome_and_event() -> None:
    harness = build_service_harness()
    harness.events.pull_error = DeviceSyncError(DeviceSyncErrorCode.DEPENDENCY_UNAVAILABLE)
    with pytest.raises(DeviceSyncError) as raised:
        await harness.service.pull_events(
            context=build_device_sync_context(),
            diagnostic_context=build_diagnostic_context(),
        )
    assert raised.value.code is DeviceSyncErrorCode.DEPENDENCY_UNAVAILABLE
    assert raised.value.is_retryable is True
    assert (
        harness.metrics.operation_count(
            operation=DeviceSyncOperation.PULL,
            outcome=DeviceSyncOutcome.FAILED,
            reason=DeviceSyncErrorCode.DEPENDENCY_UNAVAILABLE,
        )
        == 1
    )
    assert harness.sink.last_event_name() is EventName.DEVICE_SYNC_OPERATION_FAILED
    fields = harness.sink.last_fields()
    assert set(fields) == {"operation", "reason", "duration_ms"}
    assert fields["reason"] is DeviceSyncErrorCode.DEPENDENCY_UNAVAILABLE


@pytest.mark.asyncio
async def test_acknowledge_success_and_regression_rejection() -> None:
    harness = build_service_harness()
    context = build_device_sync_context()
    diagnostic_context = build_diagnostic_context()
    receipt = await harness.service.acknowledge_cursor(
        context=context,
        expected_previous_sequence=0,
        applied_through_sequence=1,
        diagnostic_context=diagnostic_context,
    )
    assert receipt == harness.events.receipt
    assert harness.events.acknowledge_sequences == [(0, 1)]
    assert harness.sink.last_fields()["operation"] is DeviceSyncOperation.ACKNOWLEDGE

    harness.events.acknowledge_error = DeviceSyncError(DeviceSyncErrorCode.CURSOR_REGRESSION)
    with pytest.raises(DeviceSyncError) as raised:
        await harness.service.acknowledge_cursor(
            context=context,
            expected_previous_sequence=5,
            applied_through_sequence=1,
            diagnostic_context=diagnostic_context,
        )
    assert raised.value.code is DeviceSyncErrorCode.CURSOR_REGRESSION
    assert harness.sink.last_event_name() is EventName.DEVICE_SYNC_OPERATION_REJECTED
    assert harness.sink.last_fields()["reason"] is DeviceSyncErrorCode.CURSOR_REGRESSION


@pytest.mark.asyncio
async def test_manifest_operations_record_their_closed_operation_labels() -> None:
    harness = build_service_harness()
    context = build_device_sync_context()
    await harness.service.start_manifest(build_start_command(context))
    await harness.service.append_manifest_page(build_append_command(context))
    await harness.service.finalize_manifest(build_finalize_command(context))
    await harness.service.read_manifest_actions(build_actions_query(context))
    await harness.service.complete_manifest(build_complete_command(context))
    assert [name for name, _ in harness.sink.emitted] == [
        EventName.DEVICE_SYNC_OPERATION_COMPLETED
    ] * 5
    assert [fields["operation"] for _, fields in harness.sink.emitted] == [
        DeviceSyncOperation.MANIFEST_START,
        DeviceSyncOperation.MANIFEST_PAGE,
        DeviceSyncOperation.MANIFEST_FINALIZE,
        DeviceSyncOperation.MANIFEST_ACTIONS,
        DeviceSyncOperation.MANIFEST_COMPLETE,
    ]
    for operation in (
        DeviceSyncOperation.MANIFEST_START,
        DeviceSyncOperation.MANIFEST_PAGE,
        DeviceSyncOperation.MANIFEST_FINALIZE,
        DeviceSyncOperation.MANIFEST_ACTIONS,
        DeviceSyncOperation.MANIFEST_COMPLETE,
    ):
        assert (
            harness.metrics.operation_count(
                operation=operation, outcome=DeviceSyncOutcome.SUCCEEDED
            )
            == 1
        )


@pytest.mark.asyncio
async def test_manifest_expiry_records_rejected_reason_before_reraising() -> None:
    harness = build_service_harness()
    harness.manifests.start_error = DeviceSyncError(DeviceSyncErrorCode.MANIFEST_EXPIRED)
    with pytest.raises(DeviceSyncError) as raised:
        await harness.service.start_manifest(build_start_command(build_device_sync_context()))
    assert raised.value.code is DeviceSyncErrorCode.MANIFEST_EXPIRED
    assert (
        harness.metrics.operation_count(
            operation=DeviceSyncOperation.MANIFEST_START,
            outcome=DeviceSyncOutcome.REJECTED,
            reason=DeviceSyncErrorCode.MANIFEST_EXPIRED,
        )
        == 1
    )
    assert harness.sink.last_event_name() is EventName.DEVICE_SYNC_OPERATION_REJECTED
    assert harness.sink.last_fields()["reason"] is DeviceSyncErrorCode.MANIFEST_EXPIRED


@pytest.mark.asyncio
async def test_absent_sink_degrades_to_build_only_validation() -> None:
    harness = build_service_harness()
    service = DeviceSyncService(
        events=harness.events,
        manifests=harness.manifests,
        metrics=harness.metrics,
        diagnostics=None,
        monotonic=SequenceMonotonic(moments=[0.0, 0.5]),
    )
    page = await service.pull_events(
        context=build_device_sync_context(),
        diagnostic_context=build_diagnostic_context(),
    )
    assert page == harness.events.page
    assert harness.sink.emitted == []
    harness.events.pull_error = DeviceSyncError(DeviceSyncErrorCode.EVENT_INTEGRITY_FAILED)
    with pytest.raises(DeviceSyncError):
        await service.pull_events(
            context=build_device_sync_context(),
            diagnostic_context=build_diagnostic_context(),
        )
    assert harness.sink.emitted == []
    assert (
        harness.metrics.operation_count(
            operation=DeviceSyncOperation.PULL,
            outcome=DeviceSyncOutcome.REJECTED,
            reason=DeviceSyncErrorCode.EVENT_INTEGRITY_FAILED,
        )
        == 1
    )


@pytest.mark.asyncio
async def test_cancellation_propagates_unmetered() -> None:
    harness = build_service_harness()
    harness.events.pull_cancelled = True
    with pytest.raises(asyncio.CancelledError):
        await harness.service.pull_events(
            context=build_device_sync_context(),
            diagnostic_context=build_diagnostic_context(),
        )
    assert harness.sink.emitted == []
    for outcome in DeviceSyncOutcome:
        assert (
            harness.metrics.operation_count(operation=DeviceSyncOperation.PULL, outcome=outcome)
            == 0
        )


@pytest.mark.asyncio
async def test_no_identifier_or_operand_ever_reaches_an_event_field() -> None:
    harness = build_service_harness()
    context = build_device_sync_context()
    await harness.service.pull_events(
        context=context, diagnostic_context=build_diagnostic_context()
    )
    harness.events.pull_error = DeviceSyncError(DeviceSyncErrorCode.CURSOR_GAP)
    with pytest.raises(DeviceSyncError):
        await harness.service.pull_events(
            context=context, diagnostic_context=build_diagnostic_context()
        )
    for _, fields in harness.sink.emitted:
        rendered = repr(fields)
        for identifier in (
            str(context.workspace_id),
            str(context.device_id),
            str(context.user_id),
        ):
            assert identifier not in rendered
        assert set(fields) <= {"operation", "reason", "duration_ms"}
