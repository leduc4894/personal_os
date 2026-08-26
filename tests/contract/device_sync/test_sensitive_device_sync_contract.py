"""Sensitive device-sync leak corpus: sentinels never reach any closed surface.

One journey drives the REAL composed device-sync services —
:class:`~personal_os.device_sync.service.DeviceSyncService` and
:class:`~api_runtime.device_sync_content.VerifiedDeviceContentService` — over
sentinel-laden doubles that carry a unique ``do-not-emit-device-sync-*``
sentinel in every operand the boundary touches: the locator and path of a
pulled event, the content bytes of a verified download, the digest riding a
provider failure, the adapter's temporary spool name and object key, a
credential-shaped token, a raw response body and a provider exception. The
doubles raise real typed errors whose chained causes embed the sentinels,
exactly the shapes a hostile provider or a corrupted row would produce.

Every closed surface is captured and scanned: the structured diagnostics
events (the trail), the configured diagnostics logger's stdout/stderr lines,
the Python stdlib log records emitted mid-journey, ``str(error)`` and
``repr(error)`` of every raised error, the redacted ``repr`` of the
sentinel-bearing contract value objects, the runtime settings export, and
the JUnit-XML and handoff-markdown result records built from the journey's
public facts — only closed reason tokens may ever reach those records. The
device-sync metric label products are pinned to the closed sets: the
recorded combinations never leave the enum products and a sentinel label is
rejected by construction.

The content sentinel is the one value with a legitimate destination: the
verified bytes the caller consumes. It may exist inside the consumed stream
and nowhere else.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import Any, Final
from uuid import UUID, uuid4

import pytest
from api_runtime.device_sync_content import VerifiedDeviceContentService

from personal_os.device_sync.contracts import (
    AppendManifestPageCommand,
    CompleteManifestCommand,
    DeviceContentDescriptor,
    DeviceCursorReceipt,
    DeviceEventPage,
    DeviceEventType,
    DeviceSyncContext,
    DeviceSyncEvent,
    FinalizeManifestCommand,
    ManifestAction,
    ManifestActionKind,
    ManifestActionPage,
    ManifestActionsQuery,
    ManifestEntry,
    ManifestPageReceipt,
    ManifestRunReceipt,
    ManifestRunState,
    NormalizedLocator,
    SourceFingerprint,
    StartManifestCommand,
)
from personal_os.device_sync.errors import DeviceSyncError, DeviceSyncErrorCode
from personal_os.device_sync.metrics import (
    DEVICE_SYNC_METRIC_CONTRACTS,
    DeviceSyncOperation,
    DeviceSyncOutcome,
    InMemoryDeviceSyncMetrics,
)
from personal_os.device_sync.service import DeviceSyncService
from personal_os.diagnostics.context import DiagnosticContext, TraceContext
from personal_os.diagnostics.events import EventName
from personal_os.diagnostics.logging import (
    configure_diagnostics,
    reset_diagnostics_for_testing,
)
from personal_os.diagnostics.trace_context import SpanId, TraceId
from personal_os.error_contracts.codes import ErrorCode
from personal_os.exclusion_policy.errors import ExclusionPolicyError
from personal_os.object_storage import CanonicalMediaType, ContentDigest, ExpectedObject
from personal_os.object_storage.errors import ObjectStorageError
from personal_os.runtime_configuration.loading import load_runtime_settings
from personal_os.runtime_configuration.models import (
    RuntimeSettings,
    ServiceName,
)

_CONTENT_SENTINEL: Final[str] = "do-not-emit-device-sync-content"
_LOCATOR_SENTINEL: Final[str] = "do-not-emit-device-sync-locator"
_PATH_SENTINEL: Final[str] = "do-not-emit-device-sync-path"
_DIGEST_SENTINEL: Final[str] = "do-not-emit-device-sync-digest"
_TEMP_NAME_SENTINEL: Final[str] = "do-not-emit-device-sync-temp-name"
_OBJECT_KEY_SENTINEL: Final[str] = "do-not-emit-device-sync-object-key"
_CREDENTIAL_SENTINEL: Final[str] = "do-not-emit-device-sync-credential"
_RESPONSE_BODY_SENTINEL: Final[str] = "do-not-emit-device-sync-response-body"
_PROVIDER_EXCEPTION_SENTINEL: Final[str] = "do-not-emit-device-sync-provider-exception"

_ALL_SENTINELS: Final[tuple[str, ...]] = (
    _CONTENT_SENTINEL,
    _LOCATOR_SENTINEL,
    _PATH_SENTINEL,
    _DIGEST_SENTINEL,
    _TEMP_NAME_SENTINEL,
    _OBJECT_KEY_SENTINEL,
    _CREDENTIAL_SENTINEL,
    _RESPONSE_BODY_SENTINEL,
    _PROVIDER_EXCEPTION_SENTINEL,
)

_TRACE: Final[TraceContext] = TraceContext(
    trace_id=TraceId("0123456789abcdef0123456789abcdef"),
    remote_parent_span_id=None,
    local_span_id=SpanId("0123456789abcdef"),
    trace_flags=0,
)


def _diagnostic() -> DiagnosticContext:
    return DiagnosticContext(request_id=uuid4(), client_request_id=None, trace=_TRACE)


def _sentinel_payload() -> bytes:
    return (
        f"# note with {_CONTENT_SENTINEL} bytes\n"
        f"locator {_LOCATOR_SENTINEL}\npath {_PATH_SENTINEL}\n"
    ).encode()


def _fingerprint_of(payload: bytes) -> SourceFingerprint:
    return SourceFingerprint(
        sha256=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
        media_type="text/markdown",
    )


# --- the sentinel-laden doubles ---------------------------------------------------------


class CapturedJourney:
    """Every closed surface the journey renders, ready for scanning."""

    def __init__(self) -> None:
        self.event_lines: list[str] = []
        self.log_records: list[str] = []
        self.error_renderings: list[str] = []
        self.result_records: list[str] = []


class SentinelEventStore:
    """The event-store double whose pulled page carries the sentinel locator
    and whose acknowledgement raises a typed error chained to a sentinel
    provider cause (the exact shape a corrupted driver would produce)."""

    def __init__(self) -> None:
        self.journey = CapturedJourney()
        self.pull_calls = 0

    async def pull_events(
        self,
        context: DeviceSyncContext,
        *,
        limit: int,
        diagnostic_context: DiagnosticContext,
    ) -> DeviceEventPage:
        del context, limit, diagnostic_context
        self.pull_calls += 1
        payload = _sentinel_payload()
        event = DeviceSyncEvent(
            event_id=uuid4(),
            event_sequence=1,
            event_type=DeviceEventType.CREATED,
            source_id=uuid4(),
            origin_device_id=None,
            base_version_id=None,
            current_version_id=uuid4(),
            base_fingerprint=None,
            current_fingerprint=_fingerprint_of(payload),
            prior_locator=None,
            resulting_locator=NormalizedLocator(f"notes/{_PATH_SENTINEL}/note.md"),
            tombstone_id=None,
            committed_at=datetime.now(UTC),
        )
        # The sentinel-bearing contract value renders redacted or never.
        self.journey.error_renderings.append(repr(event))
        return DeviceEventPage(
            acknowledged_sequence=0,
            page_checkpoint_sequence=1,
            delivered_through_sequence=1,
            events=(event,),
            has_more=False,
        )

    async def acknowledge_cursor(
        self,
        context: DeviceSyncContext,
        *,
        expected_previous_sequence: int,
        applied_through_sequence: int,
        diagnostic_context: DiagnosticContext,
    ) -> DeviceCursorReceipt:
        del context, expected_previous_sequence, applied_through_sequence
        del diagnostic_context
        try:
            raise RuntimeError(
                f"driver lost session credential={_CREDENTIAL_SENTINEL} "
                f"body={_RESPONSE_BODY_SENTINEL}"
            )
        except RuntimeError as cause:
            raise DeviceSyncError(DeviceSyncErrorCode.CURSOR_REGRESSION) from cause


class SentinelManifestStore:
    """The manifest-store double returning real receipts, receiving the
    sentinel entry and failing finalize through a sentinel-chained cause."""

    def __init__(self, journey: CapturedJourney) -> None:
        self.journey = journey
        self.received_entries: list[ManifestEntry] = []
        self.received_page_digests: list[str] = []

    def _provider_cause(self) -> RuntimeError:
        return RuntimeError(
            f"planner failed digest={_DIGEST_SENTINEL} "
            f"temp={_TEMP_NAME_SENTINEL} key={_OBJECT_KEY_SENTINEL} "
            f"exception={_PROVIDER_EXCEPTION_SENTINEL}"
        )

    async def start_manifest(self, command: StartManifestCommand) -> ManifestRunReceipt:
        del command
        return ManifestRunReceipt(
            manifest_run_id=uuid4(),
            state=ManifestRunState.COLLECTING,
            base_acknowledged_sequence=0,
            checkpoint_sequence=1,
            policy_revision_number=1,
            client_observation_generation=3,
            next_page_number=0,
            entry_count=0,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )

    async def append_manifest_page(self, command: AppendManifestPageCommand) -> ManifestPageReceipt:
        self.received_entries.extend(command.entries)
        self.received_page_digests.append(command.page_digest.hexadecimal)
        for entry in command.entries:
            # The received sentinel-bearing entry must render redacted.
            self.journey.error_renderings.append(repr(entry))
        return ManifestPageReceipt(
            manifest_run_id=command.manifest_run_id,
            page_number=command.page_number,
            accepted_entry_count=len(command.entries),
            next_page_number=command.page_number + 1,
        )

    async def finalize_manifest(self, command: FinalizeManifestCommand) -> ManifestRunReceipt:
        del command
        try:
            raise self._provider_cause()
        except RuntimeError as cause:
            raise DeviceSyncError(DeviceSyncErrorCode.MANIFEST_DIGEST_MISMATCH) from cause

    async def read_manifest_actions(self, query: ManifestActionsQuery) -> ManifestActionPage:
        action = ManifestAction(
            action_index=0,
            action_kind=ManifestActionKind.DOWNLOAD,
            local_entry_id="entry-sentinel",
            source_id=uuid4(),
            source_version_id=uuid4(),
            source_locator_id=uuid4(),
            source_tombstone_id=None,
            reason=None,
            checkpoint_locator=NormalizedLocator(f"notes/{_PATH_SENTINEL}/note.md"),
        )
        # The action wire carries the locator legitimately; its repr is closed.
        self.journey.error_renderings.append(repr(action))
        return ManifestActionPage(
            manifest_run_id=query.manifest_run_id,
            actions=(action,),
            has_more=False,
        )

    async def complete_manifest(self, command: CompleteManifestCommand) -> DeviceCursorReceipt:
        del command
        return DeviceCursorReceipt(1, 1)


class SentinelContentCatalog:
    """The catalog double resolving the exact descriptor of the sentinel
    payload, or denying it under the current policy."""

    def __init__(self, *, is_policy_denied: bool) -> None:
        self.is_policy_denied = is_policy_denied

    async def resolve_descriptor(
        self,
        context: DeviceSyncContext,
        *,
        source_id: UUID,
        source_version_id: UUID,
        diagnostic_context: DiagnosticContext,
    ) -> DeviceContentDescriptor:
        del context, diagnostic_context
        if self.is_policy_denied:
            raise ExclusionPolicyError(ErrorCode.EXCLUSION_POLICY_DENIED)
        payload = _sentinel_payload()
        return DeviceContentDescriptor(
            source_id=source_id,
            source_version_id=source_version_id,
            content_digest=ContentDigest.parse(hashlib.sha256(payload).hexdigest()),
            size_bytes=len(payload),
            media_type=CanonicalMediaType.parse("text/markdown"),
        )


class _SentinelReader:
    """The verified reader over the sentinel payload bytes."""

    def __init__(self, payload: bytes) -> None:
        self._remaining = payload

    async def read(self, size_bytes: int = 1_048_576) -> bytes:
        chunk = self._remaining[: max(size_bytes, 0)]
        self._remaining = self._remaining[len(chunk) :]
        return chunk

    def __aiter__(self) -> _SentinelReader:
        return self

    async def __anext__(self) -> bytes:
        if not self._remaining:
            raise StopAsyncIteration
        chunk = self._remaining[:65536]
        self._remaining = self._remaining[len(chunk) :]
        return chunk


class SentinelObjectStore:
    """The verified-object-source double: yields the sentinel payload on the
    success path, or raises the adapter-mapped object failure chained to the
    full provider exception (temp name, object key, digest, response body)."""

    def __init__(self, *, crash: bool) -> None:
        self.crash = crash
        self.open_calls: list[ExpectedObject] = []

    def open_verified_reader(self, expected: ExpectedObject) -> AbstractAsyncContextManager[Any]:
        self.open_calls.append(expected)

        @asynccontextmanager
        async def _reader() -> AsyncIterator[Any]:
            if self.crash:
                try:
                    raise RuntimeError(
                        f"provider 500 body={_RESPONSE_BODY_SENTINEL} "
                        f"key={_OBJECT_KEY_SENTINEL} temp={_TEMP_NAME_SENTINEL} "
                        f"digest={_DIGEST_SENTINEL} cause={_PROVIDER_EXCEPTION_SENTINEL}"
                    )
                except RuntimeError as cause:
                    raise ObjectStorageError(ErrorCode.OBJECT_STORAGE_UNAVAILABLE) from cause
            yield _SentinelReader(_sentinel_payload())

        return _reader()


class RecordingEventSink:
    """Structured-diagnostics capture retaining every emitted event verbatim."""

    def __init__(self, journey: CapturedJourney) -> None:
        self.journey = journey
        self.events: list[tuple[str, dict[str, Any]]] = []

    def emit(self, event_name: EventName, fields: Mapping[str, object] | None = None) -> None:
        self.events.append((event_name.value, dict(fields or {})))
        self.journey.event_lines.append(
            json.dumps({"event": event_name.value, "fields": dict(fields or {})}, default=str)
        )


class RootLogCapture(logging.Handler):
    """Root-logger capture of every Python log record emitted mid-journey."""

    def __init__(self, journey: CapturedJourney) -> None:
        super().__init__(level=logging.NOTSET)
        self.journey = journey

    def emit(self, record: logging.LogRecord) -> None:
        self.journey.log_records.append(
            f"{record.levelname}{record.name}{record.getMessage()}{record.exc_info or ''}"
        )


# --- the fixtures ------------------------------------------------------------------------


@pytest.fixture
def runtime_settings(tmp_path: Path) -> RuntimeSettings:
    return load_runtime_settings(
        ServiceName.API,
        environ={
            "KNOWLEDGE_ENVIRONMENT": "test",
            "KNOWLEDGE_SECRET_ROOT": str(tmp_path),
        },
    )


@pytest.fixture(autouse=True)
def _reset_diagnostics_after_each() -> Iterator[None]:
    yield
    reset_diagnostics_for_testing()


# --- the helpers -------------------------------------------------------------------------


def _assert_no_sentinel(*blobs: str, sentinels: tuple[str, ...] = _ALL_SENTINELS) -> None:
    combined = "\n".join(blobs)
    for sentinel in sentinels:
        assert sentinel not in combined, f"sentinel leaked: {sentinel}"


def _record_captured_error(journey: CapturedJourney, error: BaseException) -> None:
    journey.error_renderings.append(str(error))
    journey.error_renderings.append(repr(error))


def _render_junit_record(journey: CapturedJourney, cases: list[tuple[str, str, str]]) -> str:
    """The JUnit XML shape CI writes from this journey's outcomes."""

    testcase_lines = "\n".join(
        f'  <testcase classname="device_sync_sensitive_contract" name="{name}">'
        f'{"<failure " if status == "failed" else ""}message="{detail}"'
        f"{'></failure>' if status == 'failed' else ''}</testcase>"
        for name, status, detail in cases
    )
    record = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<testsuite name="device-sync-sensitive" tests="{len(cases)}">\n'
        f"{testcase_lines}\n</testsuite>\n"
        f"<!-- journey events: {journey.event_lines!r} -->\n"
    )
    journey.result_records.append(record)
    return record


def _render_handoff_record(journey: CapturedJourney, gate_lines: list[str]) -> str:
    """The handoff-shaped markdown gate-evidence block this repo records."""

    record = (
        "# Handoff gate evidence (shape)\n\n"
        "## Gates\n\n"
        + "\n".join(f"- {line}" for line in gate_lines)
        + f"\n\n## Captured failure tokens\n\n- {journey.event_lines!r}\n"
    )
    journey.result_records.append(record)
    return record


# --- the journey -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sentinel_laden_device_sync_journey_never_leaks_closed_surfaces(
    runtime_settings: RuntimeSettings,
    caplog: pytest.LogCaptureFixture,
) -> None:
    events = SentinelEventStore()
    journey = events.journey
    manifests = SentinelManifestStore(journey)
    trail = RecordingEventSink(journey)
    metrics = InMemoryDeviceSyncMetrics()
    stdout = StringIO()
    stderr = StringIO()
    logger = configure_diagnostics(runtime_settings, stdout=stdout, stderr=stderr)

    service = DeviceSyncService(
        events=events,
        manifests=manifests,
        metrics=metrics,
        diagnostics=trail,
        monotonic=lambda: 0.5,
    )

    context = DeviceSyncContext(workspace_id=uuid4(), device_id=uuid4(), user_id=uuid4())
    cases: list[tuple[str, str, str]] = []

    # Pull succeeds and carries the sentinel locator inside the event.
    page = await service.pull_events(context=context, diagnostic_context=_diagnostic())
    assert len(page.events) == 1
    cases.append(("pull_delivers_event", "passed", ""))

    # The acknowledgement fails with its closed reason; the sentinel cause
    # never reaches the error or any sink.
    with pytest.raises(DeviceSyncError) as raised:
        await service.acknowledge_cursor(
            context=context,
            expected_previous_sequence=0,
            applied_through_sequence=1,
            diagnostic_context=_diagnostic(),
        )
    assert raised.value.code is DeviceSyncErrorCode.CURSOR_REGRESSION
    _record_captured_error(journey, raised.value)
    cases.append(("acknowledge_rejects_regression", "passed", "device_cursor_regression"))

    # The manifest wire: start and page succeed with the sentinel entry,
    # finalize fails closed through the provider-shaped cause.
    run = await service.start_manifest(
        StartManifestCommand(
            context=context,
            client_observation_generation=3,
            diagnostic_context=_diagnostic(),
        )
    )
    entry = ManifestEntry(
        local_entry_id="entry-sentinel",
        known_source_id=None,
        known_version_id=None,
        normalized_locator=NormalizedLocator(f"notes/{_PATH_SENTINEL}/note.md"),
        fingerprint=_fingerprint_of(_sentinel_payload()),
        observation_generation=3,
    )
    await service.append_manifest_page(
        AppendManifestPageCommand(
            context=context,
            manifest_run_id=run.manifest_run_id,
            page_number=0,
            entries=(entry,),
            page_digest=ContentDigest.parse("0" * 64),
            diagnostic_context=_diagnostic(),
        )
    )
    assert manifests.received_entries == [entry]
    with pytest.raises(DeviceSyncError) as finalize_raised:
        await service.finalize_manifest(
            FinalizeManifestCommand(
                context=context,
                manifest_run_id=run.manifest_run_id,
                total_entry_count=1,
                final_digest=ContentDigest.parse("1" * 64),
                diagnostic_context=_diagnostic(),
            )
        )
    assert finalize_raised.value.code is DeviceSyncErrorCode.MANIFEST_DIGEST_MISMATCH
    _record_captured_error(journey, finalize_raised.value)
    cases.append(("finalize_fails_digest_mismatch", "passed", "device_manifest_digest_mismatch"))

    action_page = await service.read_manifest_actions(
        ManifestActionsQuery(
            context=context,
            manifest_run_id=run.manifest_run_id,
            after_action_index=0,
            limit=200,
            diagnostic_context=_diagnostic(),
        )
    )
    assert action_page.actions[0].action_kind is ManifestActionKind.DOWNLOAD
    receipt = await service.complete_manifest(
        CompleteManifestCommand(
            context=context,
            manifest_run_id=run.manifest_run_id,
            final_digest=ContentDigest.parse("1" * 64),
            diagnostic_context=_diagnostic(),
        )
    )
    assert receipt.acknowledged_sequence == 1
    cases.append(("manifest_wire_completes", "passed", ""))

    # The verified content: policy denial before any byte, a successful
    # verified download of the sentinel payload, and the provider crash
    # mapped onto the closed dependency reason.
    denied = VerifiedDeviceContentService(
        catalog=SentinelContentCatalog(is_policy_denied=True),
        objects=SentinelObjectStore(crash=False),
        metrics=InMemoryDeviceSyncMetrics(),
        diagnostics=trail,
        monotonic=lambda: 0.5,
    )
    with pytest.raises(ExclusionPolicyError) as policy_raised:
        async with denied.open_content(
            context,
            source_id=uuid4(),
            source_version_id=uuid4(),
            diagnostic_context=_diagnostic(),
        ):
            raise AssertionError("the content context must never be entered")
    assert policy_raised.value.error_code is ErrorCode.EXCLUSION_POLICY_DENIED
    cases.append(("policy_denial_precedes_bytes", "passed", "exclusion_policy_denied"))

    serving = VerifiedDeviceContentService(
        catalog=SentinelContentCatalog(is_policy_denied=False),
        objects=SentinelObjectStore(crash=False),
        metrics=InMemoryDeviceSyncMetrics(),
        diagnostics=trail,
        monotonic=lambda: 0.5,
    )
    source_id, source_version_id = uuid4(), uuid4()
    consumed = bytearray()
    async with serving.open_content(
        context,
        source_id=source_id,
        source_version_id=source_version_id,
        diagnostic_context=_diagnostic(),
    ) as content:
        async for chunk in content.reader:
            consumed.extend(chunk)
        journey.error_renderings.append(repr(content))
    # The content sentinel exists ONLY inside the consumed caller bytes.
    assert _CONTENT_SENTINEL.encode("utf-8") in bytes(consumed)
    cases.append(("verified_download_yields_bytes", "passed", ""))

    crashing = VerifiedDeviceContentService(
        catalog=SentinelContentCatalog(is_policy_denied=False),
        objects=SentinelObjectStore(crash=True),
        metrics=InMemoryDeviceSyncMetrics(),
        diagnostics=trail,
        monotonic=lambda: 0.5,
    )
    with pytest.raises(DeviceSyncError) as download_raised:
        async with crashing.open_content(
            context,
            source_id=source_id,
            source_version_id=source_version_id,
            diagnostic_context=_diagnostic(),
        ):
            raise AssertionError("the content context must never be entered")
    assert download_raised.value.code is DeviceSyncErrorCode.DEPENDENCY_UNAVAILABLE
    _record_captured_error(journey, download_raised.value)
    cases.append(
        ("provider_crash_maps_closed_reason", "passed", "device_sync_dependency_unavailable")
    )

    # Every captured trail event re-emits through the configured
    # diagnostics logger: the structured log lines join the scan surface.
    for event_name_value, fields in trail.events:
        logger.emit(EventName(event_name_value), fields)

    # The captured stdlib log records join the scan surface.
    for record in caplog.records:
        journey.log_records.append(f"{record.levelname}{record.name}{record.getMessage()}")

    # --- the result records this repo produces from journeys carry closed
    # tokens only: the JUnit XML and the handoff-shaped markdown block.
    junit_record = _render_junit_record(journey, cases)
    handoff_record = _render_handoff_record(
        journey,
        [f"{name}: {detail or 'passed'}" for name, _status, detail in cases],
    )

    # --- the settings export never carries a device-sync operand.
    settings_record = repr(runtime_settings)

    _assert_no_sentinel(
        "\n".join(journey.event_lines),
        stdout.getvalue(),
        stderr.getvalue(),
        "\n".join(journey.log_records),
        "\n".join(journey.error_renderings),
        junit_record,
        handoff_record,
        settings_record,
    )
    # Every structured line stays valid JSON (no raw interpolation broke it).
    for line in journey.event_lines:
        assert isinstance(json.loads(line), dict)

    # The provider cause chain remains the SOURCE of the mapped failure
    # (the boundary test is moot otherwise): the chained provider exception
    # carries the sentinels, the mapped surface does not.
    object_cause = download_raised.value.__cause__
    assert isinstance(object_cause, ObjectStorageError)
    provider_root = object_cause.__cause__
    assert provider_root is not None
    assert _PROVIDER_EXCEPTION_SENTINEL in str(provider_root)
    assert _OBJECT_KEY_SENTINEL in str(provider_root)

    # --- the metric label products stay inside the closed sets.
    for metric_name, dimensions in DEVICE_SYNC_METRIC_CONTRACTS.items():
        assert dimensions == {"operation", "outcome", "reason"}, metric_name
    recorded_combinations = {
        (operation, outcome, reason)
        for operation in DeviceSyncOperation
        for outcome in DeviceSyncOutcome
        for reason in (*DeviceSyncErrorCode, None)
        if metrics.operation_count(operation=operation, outcome=outcome, reason=reason) > 0
    }
    assert recorded_combinations, "the journey must have recorded outcomes"
    for operation, outcome, reason in recorded_combinations:
        assert isinstance(operation, DeviceSyncOperation)
        assert isinstance(outcome, DeviceSyncOutcome)
        assert reason is None or isinstance(reason, DeviceSyncErrorCode)
    _assert_no_sentinel(
        "\n".join(
            f"{operation.value}/{outcome.value}/{reason.value if reason else ''}"
            for operation, outcome, reason in recorded_combinations
        )
    )
    # A sentinel label is rejected by construction, never recorded.
    with pytest.raises(ValueError):
        InMemoryDeviceSyncMetrics().record_operation(
            operation=_CONTENT_SENTINEL,  # type: ignore[arg-type]
            outcome=DeviceSyncOutcome.SUCCEEDED,
            reason=None,
            duration_ms=1,
        )
    with pytest.raises(ValueError):
        InMemoryDeviceSyncMetrics().record_operation(
            operation=DeviceSyncOperation.PULL,
            outcome=_PATH_SENTINEL,  # type: ignore[arg-type]
            reason=None,
            duration_ms=1,
        )


def test_sensitive_device_sync_contract_collects_tests() -> None:
    """The leak corpus must collect tests when the file runs directly."""

    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(Path(__file__)), "--collect-only", "-q"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    collected = [line for line in result.stdout.splitlines() if "::" in line]
    assert len(collected) >= 2
