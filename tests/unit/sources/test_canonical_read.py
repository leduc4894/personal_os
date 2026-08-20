"""Fail-closed canonical current-source read service proven with narrow fakes.

Pins the verified-reader contract: the command rejects nil UUIDs, the resolved
reference must be a positive content version, exact canonical bytes flow only
after the object store's verification passes, missing/corrupt bytes surface the
existing typed object-storage errors unchanged, caller cancellation closes the
reader and clears the spool, no canonical state is ever mutated, and outcome
metrics plus the registered events are recorded for both terminal paths.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from uuid import UUID, uuid4

import pytest
from tests.unit.sources.fakes import (
    AllowingPolicyGuard,
    CallLedger,
    FakeCanonicalSourceReadStore,
    LeakCheckingObjectStore,
    build_diagnostic_context,
    build_read_command,
    build_read_reference,
    denying_policy_guard,
)

from personal_os.diagnostics.events import (
    DiagnosticEvent,
    EventName,
    RejectedDiagnosticPayload,
    SafeToken,
)
from personal_os.error_contracts.codes import ErrorCategory, ErrorCode
from personal_os.error_contracts.exceptions import InternalApplicationError
from personal_os.exclusion_policy.errors import ExclusionPolicyError
from personal_os.object_storage.errors import ObjectStorageError
from personal_os.sources import (
    CanonicalReadMetrics,
    CanonicalReadStateError,
    CanonicalSourceReadService,
    InMemoryCanonicalReadMetrics,
    ReadCurrentSourceCommand,
    ReadOutcome,
    validate_read_current_source_command,
)
from personal_os.sources import reading as reading_module
from personal_os.sources.actors import NIL_UUID


@pytest.mark.parametrize("content_version", [0, -1])
def test_reference_hydration_requires_positive_content_version(content_version: int) -> None:
    command = build_read_command()
    with pytest.raises(ValueError, match="content_version"):
        build_read_reference(command, content_version=content_version)


def test_read_command_rejects_nil_uuids() -> None:
    for field_name in ("workspace_id", "source_id"):
        command = ReadCurrentSourceCommand(
            workspace_id=uuid4() if field_name != "workspace_id" else NIL_UUID,
            source_id=uuid4() if field_name != "source_id" else NIL_UUID,
        )
        with pytest.raises(ValueError, match=field_name):
            validate_read_current_source_command(command)


@pytest.mark.asyncio
async def test_service_rejects_nil_uuids_before_any_port_call() -> None:
    command = ReadCurrentSourceCommand(workspace_id=UUID(int=0), source_id=uuid4())
    # The reference is never consulted: command validation stops the call.
    store = FakeCanonicalSourceReadStore(build_read_reference(build_read_command()))
    object_store = LeakCheckingObjectStore()
    service = CanonicalSourceReadService(
        store=store,
        object_store=object_store,
        metrics=InMemoryCanonicalReadMetrics(),
        policy_guard=AllowingPolicyGuard(ledger=CallLedger()),
    )
    with pytest.raises(ValueError, match="workspace_id"):
        await service.read_current_source_bytes(command, build_diagnostic_context())
    assert store.resolve_calls == []
    assert object_store.opened == []


@pytest.mark.asyncio
async def test_verified_read_returns_exact_bytes_and_emits_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command = build_read_command()
    reference = build_read_reference(command)
    store = FakeCanonicalSourceReadStore(reference)
    object_store = LeakCheckingObjectStore()
    metrics = InMemoryCanonicalReadMetrics()
    service = CanonicalSourceReadService(
        store=store,
        object_store=object_store,
        metrics=metrics,
        policy_guard=AllowingPolicyGuard(ledger=CallLedger()),
    )
    registry_calls: list[tuple[EventName, dict[str, object]]] = []
    original_registry = reading_module.build_registered_event

    def recording_registry(
        event_name: EventName, fields: Mapping[str, object]
    ) -> DiagnosticEvent | RejectedDiagnosticPayload:
        registry_calls.append((event_name, dict(fields)))
        return original_registry(event_name, fields)

    # Spy inside the service's module so the test observes the service path,
    # not just the pure builder: removing the service's event emission would
    # leave registry_calls empty and fail this test.
    monkeypatch.setattr(reading_module, "build_registered_event", recording_registry)

    data = await service.read_current_source_bytes(command, build_diagnostic_context())

    assert data == object_store.canonical_bytes
    assert store.resolve_calls == [(command.workspace_id, command.source_id)]
    assert object_store.opened == [reference.expected_object.content_digest]
    assert object_store.spool_removed == [reference.expected_object.content_digest]
    assert object_store.mutation_calls == []
    assert metrics.read_count(ReadOutcome.SUCCEEDED) == 1
    assert metrics.read_count(ReadOutcome.FAILED) == 0
    (record,) = metrics.read_records()
    assert record.outcome is ReadOutcome.SUCCEEDED
    assert 0.0 <= record.duration_seconds < 60.0
    assert registry_calls == [
        (
            EventName.CANONICAL_SOURCE_READ_SUCCEEDED,
            {
                "source_id": command.source_id,
                "workspace_id": command.workspace_id,
                "source_version_id": reference.source_version_id,
            },
        )
    ]
    built = original_registry(*registry_calls[0])
    assert isinstance(built, DiagnosticEvent)


@pytest.mark.asyncio
async def test_read_never_updates_any_canonical_state() -> None:
    command = build_read_command()
    reference = build_read_reference(command)
    store = FakeCanonicalSourceReadStore(reference)
    object_store = LeakCheckingObjectStore()
    metrics = InMemoryCanonicalReadMetrics()
    service = CanonicalSourceReadService(
        store=store,
        object_store=object_store,
        metrics=metrics,
        policy_guard=AllowingPolicyGuard(ledger=CallLedger()),
    )

    await service.read_current_source_bytes(command, build_diagnostic_context())

    # The read port has exactly one (read-only) call; the object store sees no
    # resolve/store/verify mutation call; nothing else exists to mutate.
    assert store.resolve_calls == [(command.workspace_id, command.source_id)]
    assert object_store.mutation_calls == []
    assert object_store.opened == [reference.expected_object.content_digest]


@pytest.mark.asyncio
async def test_missing_reference_fails_closed_with_state_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command = build_read_command()
    store = FakeCanonicalSourceReadStore(None)
    object_store = LeakCheckingObjectStore()
    metrics = InMemoryCanonicalReadMetrics()
    service = CanonicalSourceReadService(
        store=store,
        object_store=object_store,
        metrics=metrics,
        policy_guard=AllowingPolicyGuard(ledger=CallLedger()),
    )
    registry_calls: list[tuple[EventName, dict[str, object]]] = []
    original_registry = reading_module.build_registered_event

    def recording_registry(
        event_name: EventName, fields: Mapping[str, object]
    ) -> DiagnosticEvent | RejectedDiagnosticPayload:
        registry_calls.append((event_name, dict(fields)))
        return original_registry(event_name, fields)

    monkeypatch.setattr(reading_module, "build_registered_event", recording_registry)

    body_entered = False
    with pytest.raises(CanonicalReadStateError) as excinfo:
        async with service.open_current_source(command, build_diagnostic_context()):
            body_entered = True

    assert body_entered is False
    error = excinfo.value
    assert error.error_code is ErrorCode.CANONICAL_READ_STATE_INVALID
    assert error.category is ErrorCategory.INTEGRITY
    assert error.is_retryable is False
    assert error.safe_details == {"source_id": command.source_id}
    assert object_store.opened == []
    assert metrics.read_count(ReadOutcome.FAILED) == 1
    assert metrics.read_count(ReadOutcome.SUCCEEDED) == 0
    assert registry_calls == [
        (
            EventName.CANONICAL_SOURCE_READ_FAILED,
            {
                "source_id": command.source_id,
                "workspace_id": command.workspace_id,
                "error_code": ErrorCode.CANONICAL_READ_STATE_INVALID,
            },
        )
    ]
    built = original_registry(*registry_calls[0])
    assert isinstance(built, DiagnosticEvent)


@pytest.mark.asyncio
async def test_reference_identity_mismatch_fails_closed_with_state_invalid() -> None:
    command = build_read_command()
    other = build_read_command()
    store = FakeCanonicalSourceReadStore(build_read_reference(other))
    object_store = LeakCheckingObjectStore()
    service = CanonicalSourceReadService(
        store=store,
        object_store=object_store,
        metrics=InMemoryCanonicalReadMetrics(),
        policy_guard=AllowingPolicyGuard(ledger=CallLedger()),
    )

    with pytest.raises(CanonicalReadStateError) as excinfo:
        await service.read_current_source_bytes(command, build_diagnostic_context())

    assert excinfo.value.error_code is ErrorCode.CANONICAL_READ_STATE_INVALID
    assert object_store.opened == []


@pytest.mark.asyncio
async def test_object_store_missing_error_surfaces_unchanged() -> None:
    command = build_read_command()
    store = FakeCanonicalSourceReadStore(build_read_reference(command))
    object_store = LeakCheckingObjectStore(missing=True)
    metrics = InMemoryCanonicalReadMetrics()
    service = CanonicalSourceReadService(
        store=store,
        object_store=object_store,
        metrics=metrics,
        policy_guard=AllowingPolicyGuard(ledger=CallLedger()),
    )

    with pytest.raises(ObjectStorageError) as excinfo:
        await service.read_current_source_bytes(command, build_diagnostic_context())

    # The existing typed error is raised as-is, never wrapped in a less
    # precise code (spec 15).
    assert type(excinfo.value) is ObjectStorageError
    assert excinfo.value.error_code is ErrorCode.OBJECT_STORAGE_OBJECT_MISSING
    assert metrics.read_count(ReadOutcome.FAILED) == 1


@pytest.mark.asyncio
async def test_corrupt_object_error_surfaces_before_any_byte_reaches_consumer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command = build_read_command()
    reference = build_read_reference(command)
    store = FakeCanonicalSourceReadStore(reference)
    object_store = LeakCheckingObjectStore(fail_verification=True)
    metrics = InMemoryCanonicalReadMetrics()
    service = CanonicalSourceReadService(
        store=store,
        object_store=object_store,
        metrics=metrics,
        policy_guard=AllowingPolicyGuard(ledger=CallLedger()),
    )
    registry_calls: list[tuple[EventName, dict[str, object]]] = []
    original_registry = reading_module.build_registered_event

    def recording_registry(
        event_name: EventName, fields: Mapping[str, object]
    ) -> DiagnosticEvent | RejectedDiagnosticPayload:
        registry_calls.append((event_name, dict(fields)))
        return original_registry(event_name, fields)

    monkeypatch.setattr(reading_module, "build_registered_event", recording_registry)

    body_entered = False
    observed_bytes = b""
    with pytest.raises(ObjectStorageError) as excinfo:
        async with service.open_current_source(command, build_diagnostic_context()) as (
            _resolved,
            reader,
        ):
            body_entered = True
            observed_bytes += await reader.read()

    assert body_entered is False
    assert observed_bytes == b""
    assert excinfo.value.error_code is ErrorCode.OBJECT_STORAGE_INTEGRITY_FAILED
    assert metrics.read_count(ReadOutcome.FAILED) == 1
    assert metrics.read_count(ReadOutcome.SUCCEEDED) == 0
    assert registry_calls == [
        (
            EventName.CANONICAL_SOURCE_READ_FAILED,
            {
                "source_id": command.source_id,
                "workspace_id": command.workspace_id,
                "error_code": ErrorCode.OBJECT_STORAGE_INTEGRITY_FAILED,
            },
        )
    ]


@pytest.mark.asyncio
async def test_consumer_application_error_does_not_record_read_failure_telemetry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command = build_read_command()
    store = FakeCanonicalSourceReadStore(build_read_reference(command))
    object_store = LeakCheckingObjectStore()
    metrics = InMemoryCanonicalReadMetrics()
    service = CanonicalSourceReadService(
        store=store,
        object_store=object_store,
        metrics=metrics,
        policy_guard=AllowingPolicyGuard(ledger=CallLedger()),
    )
    registry_calls: list[tuple[EventName, dict[str, object]]] = []
    original_registry = reading_module.build_registered_event

    def recording_registry(
        event_name: EventName, fields: Mapping[str, object]
    ) -> DiagnosticEvent | RejectedDiagnosticPayload:
        registry_calls.append((event_name, dict(fields)))
        return original_registry(event_name, fields)

    monkeypatch.setattr(reading_module, "build_registered_event", recording_registry)

    with pytest.raises(InternalApplicationError):
        async with service.open_current_source(command, build_diagnostic_context()):
            raise InternalApplicationError(ErrorCode.INTERNAL_ERROR)

    assert metrics.read_records() == []
    assert registry_calls == []


@pytest.mark.asyncio
async def test_caller_cancellation_closes_reader_and_clears_spool_state() -> None:
    command = build_read_command()
    reference = build_read_reference(command)
    store = FakeCanonicalSourceReadStore(reference)
    object_store = LeakCheckingObjectStore()
    service = CanonicalSourceReadService(
        store=store,
        object_store=object_store,
        metrics=InMemoryCanonicalReadMetrics(),
        policy_guard=AllowingPolicyGuard(ledger=CallLedger()),
    )

    with pytest.raises(asyncio.CancelledError):
        async with service.open_current_source(command, build_diagnostic_context()):
            raise asyncio.CancelledError

    assert object_store.closed == 1
    assert object_store.spool_removed == [reference.expected_object.content_digest]


@pytest.mark.asyncio
async def test_service_raises_internal_error_when_registry_rejects_the_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command = build_read_command()
    store = FakeCanonicalSourceReadStore(build_read_reference(command))
    object_store = LeakCheckingObjectStore()
    service = CanonicalSourceReadService(
        store=store,
        object_store=object_store,
        metrics=InMemoryCanonicalReadMetrics(),
        policy_guard=AllowingPolicyGuard(ledger=CallLedger()),
    )

    def rejecting_registry(
        event_name: EventName, fields: Mapping[str, object]
    ) -> RejectedDiagnosticPayload:
        return RejectedDiagnosticPayload(reason=SafeToken.parse("unsafe_value"), count=1)

    monkeypatch.setattr(reading_module, "build_registered_event", rejecting_registry)

    with pytest.raises(InternalApplicationError):
        await service.read_current_source_bytes(command, build_diagnostic_context())


def test_metrics_record_outcome_and_duration() -> None:
    recorder = InMemoryCanonicalReadMetrics()
    assert isinstance(recorder, CanonicalReadMetrics)
    recorder.record_read(outcome=ReadOutcome.SUCCEEDED, duration_seconds=0.25)
    recorder.record_read(outcome=ReadOutcome.FAILED, duration_seconds=0.5)

    assert recorder.read_count(ReadOutcome.SUCCEEDED) == 1
    assert recorder.read_count(ReadOutcome.FAILED) == 1
    durations = {record.outcome: record.duration_seconds for record in recorder.read_records()}
    assert durations == {ReadOutcome.SUCCEEDED: 0.25, ReadOutcome.FAILED: 0.5}
    assert repr(recorder) == "InMemoryCanonicalReadMetrics(redacted)"


@pytest.mark.parametrize("duration_seconds", [-0.01, float("nan"), float("inf"), -1.0])
def test_read_recorder_rejects_invalid_durations(duration_seconds: float) -> None:
    recorder = InMemoryCanonicalReadMetrics()
    with pytest.raises(ValueError, match="duration_seconds"):
        recorder.record_read(outcome=ReadOutcome.SUCCEEDED, duration_seconds=duration_seconds)
    assert recorder.read_count(ReadOutcome.SUCCEEDED) == 0


def test_reference_exposes_resolved_identity_and_expected_object() -> None:
    command = build_read_command()
    reference = build_read_reference(command, content_version=3)

    assert reference.workspace_id == command.workspace_id
    assert reference.source_id == command.source_id
    assert isinstance(reference.source_version_id, UUID)
    assert reference.content_version == 3
    assert reference.expected_object.size_bytes == len(LeakCheckingObjectStore().canonical_bytes)
    assert reference.committed_at.tzinfo is not None


class RecordingDiagnosticSink:
    """Composition-owned sink fake recording every delivered event."""

    def __init__(self) -> None:
        self.events: list[tuple[EventName, dict[str, object]]] = []

    def emit(self, event_name: EventName, fields: Mapping[str, object] | None = None) -> None:
        self.events.append((event_name, dict(fields or {})))


@pytest.mark.asyncio
async def test_success_event_is_delivered_to_the_bound_diagnostics_sink() -> None:
    command = build_read_command()
    reference = build_read_reference(command)
    store = FakeCanonicalSourceReadStore(reference)
    object_store = LeakCheckingObjectStore()
    sink = RecordingDiagnosticSink()
    service = CanonicalSourceReadService(
        store=store,
        object_store=object_store,
        metrics=InMemoryCanonicalReadMetrics(),
        policy_guard=AllowingPolicyGuard(ledger=CallLedger()),
        diagnostics=sink,
    )

    await service.read_current_source_bytes(command, build_diagnostic_context())

    assert sink.events == [
        (
            EventName.CANONICAL_SOURCE_READ_SUCCEEDED,
            {
                "source_id": command.source_id,
                "workspace_id": command.workspace_id,
                "source_version_id": reference.source_version_id,
            },
        )
    ]


@pytest.mark.asyncio
async def test_failure_event_is_delivered_to_the_bound_diagnostics_sink() -> None:
    command = build_read_command()
    store = FakeCanonicalSourceReadStore(None)
    object_store = LeakCheckingObjectStore()
    sink = RecordingDiagnosticSink()
    service = CanonicalSourceReadService(
        store=store,
        object_store=object_store,
        metrics=InMemoryCanonicalReadMetrics(),
        policy_guard=AllowingPolicyGuard(ledger=CallLedger()),
        diagnostics=sink,
    )

    with pytest.raises(CanonicalReadStateError):
        await service.read_current_source_bytes(command, build_diagnostic_context())

    assert len(sink.events) == 1
    event_name, event_fields = sink.events[0]
    assert event_name is EventName.CANONICAL_SOURCE_READ_FAILED
    assert event_fields["source_id"] == command.source_id
    assert event_fields["workspace_id"] == command.workspace_id
    assert event_fields["error_code"] is ErrorCode.CANONICAL_READ_STATE_INVALID


# --- mandatory policy re-check before the object-store request (spec 14) ---------


@pytest.mark.asyncio
async def test_denied_reference_never_reaches_the_object_store() -> None:
    command = build_read_command()
    reference = build_read_reference(command)
    store = FakeCanonicalSourceReadStore(reference)
    object_store = LeakCheckingObjectStore()
    metrics = InMemoryCanonicalReadMetrics()
    service = CanonicalSourceReadService(
        store=store,
        object_store=object_store,
        metrics=metrics,
        policy_guard=denying_policy_guard(ErrorCode.EXCLUSION_POLICY_DENIED),
    )

    with pytest.raises(ExclusionPolicyError) as raised:
        await service.read_current_source_bytes(command, build_diagnostic_context())

    assert raised.value.error_code is ErrorCode.EXCLUSION_POLICY_DENIED
    # The store resolved the state transactionally, the guard denied, and no
    # object-store reader was ever opened.
    assert store.resolve_calls == [(command.workspace_id, command.source_id)]
    assert object_store.opened == []
    assert metrics.read_count(ReadOutcome.FAILED) == 1


@pytest.mark.asyncio
async def test_missing_active_policy_denies_the_read_before_object_store() -> None:
    command = build_read_command()
    service = CanonicalSourceReadService(
        store=FakeCanonicalSourceReadStore(build_read_reference(command)),
        object_store=LeakCheckingObjectStore(),
        metrics=InMemoryCanonicalReadMetrics(),
        policy_guard=denying_policy_guard(ErrorCode.EXCLUSION_POLICY_NOT_INITIALIZED),
    )

    with pytest.raises(ExclusionPolicyError) as raised:
        await service.read_current_source_bytes(command, build_diagnostic_context())

    assert raised.value.error_code is ErrorCode.EXCLUSION_POLICY_NOT_INITIALIZED


@pytest.mark.asyncio
async def test_guard_receives_the_resolved_reference_and_allows_the_read() -> None:
    command = build_read_command()
    reference = build_read_reference(command)
    ledger = CallLedger()
    guard = AllowingPolicyGuard(ledger=ledger)
    service = CanonicalSourceReadService(
        store=FakeCanonicalSourceReadStore(reference),
        object_store=LeakCheckingObjectStore(),
        metrics=InMemoryCanonicalReadMetrics(),
        policy_guard=guard,
    )

    await service.read_current_source_bytes(command, build_diagnostic_context())

    # The guard re-check happens after resolution and before the verified
    # reader opens, exactly once per read.
    assert guard.read_calls == [command.source_id]
