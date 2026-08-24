"""Provider-neutral publication service orchestration proven with narrow fakes.

Pins the exact cross-port call order for an exact replay (zero object-store
calls) and a new commit (preflight, object-store resolve before any upload,
receipt validation, commit), the stop points (mismatch before R2, invalid
receipt before commit) and the receipt reuse rules across bounded database
retries versus fresh service invocations.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Final, cast

import pytest
from tests.unit.sources.fakes import (
    OBJECT_STORE_RESOLVE,
    OBJECT_STORE_STORE_STREAM,
    POLICY_GUARD_PUBLICATION,
    STORE_COMMIT_CREATE,
    STORE_COMMIT_UPDATE,
    STORE_RESOLVE_COMMITTED,
    AllowingPolicyGuard,
    CallLedger,
    DenyingPolicyGuard,
    FakeCanonicalObjectStore,
    FakeSourcePublicationStore,
    ProbedByteStream,
    SequencedUtcClock,
    build_committed_result,
    build_create_command,
    build_diagnostic_context,
    build_expected_object,
    build_idempotency_mismatch_error,
    build_policy_decision,
    build_update_command,
    build_verified_receipt,
    denying_policy_guard,
)

from personal_os.diagnostics.events import EventName
from personal_os.error_contracts.codes import ErrorCategory, ErrorCode
from personal_os.exclusion_policy.enforcement import (
    AllowedPolicyRevisionBinding,
    PublicationPolicyEvidence,
)
from personal_os.exclusion_policy.errors import ExclusionPolicyError
from personal_os.object_storage import ContentDigest, ExpectedObject, VerifiedObjectReceipt
from personal_os.source_locators.values import NormalizedLocator
from personal_os.sources import (
    PublicationOperation,
    PublicationRejectionReason,
    SourceVersionPublicationService,
)
from personal_os.sources.commands import CreateSourceVersion
from personal_os.sources.errors import SourcePublicationError
from personal_os.sources.fingerprint import compute_request_fingerprint
from personal_os.sources.metrics import (
    InMemorySourcePublicationMetrics,
    PublicationMetricOutcome,
)
from personal_os.sources.results import SourceVersionPublicationResult

_PUBLICATION_START: Final[datetime] = datetime(2026, 8, 14, 12, 0, 0, tzinfo=UTC)


class RecordingDiagnosticSink:
    """Capture the service's validated event boundary for one unit test."""

    def __init__(self) -> None:
        self.events: list[tuple[EventName, dict[str, object]]] = []

    def emit(self, event_name: EventName, fields: dict[str, object]) -> None:
        self.events.append((event_name, fields))


def _build_service(
    *,
    committed_result: SourceVersionPublicationResult | None = None,
    resolve_error: SourcePublicationError | None = None,
    internal_retry_attempts: int = 0,
    resolve_receipts: list[VerifiedObjectReceipt | None] | None = None,
    store_receipt: VerifiedObjectReceipt | None = None,
    publication_evidence: PublicationPolicyEvidence | None = None,
    diagnostics: RecordingDiagnosticSink | None = None,
) -> tuple[
    SourceVersionPublicationService,
    FakeSourcePublicationStore,
    FakeCanonicalObjectStore,
    InMemorySourcePublicationMetrics,
    CallLedger,
]:
    """Wire the service against fakes; returns the service plus every fake."""

    ledger = CallLedger()
    store = FakeSourcePublicationStore(
        ledger=ledger,
        commit_result=build_committed_result(build_create_command()),
        committed_result=committed_result,
        resolve_error=resolve_error,
        internal_retry_attempts=internal_retry_attempts,
    )
    object_store = FakeCanonicalObjectStore(
        ledger=ledger,
        resolve_receipts=list(resolve_receipts or []),
        store_receipt=store_receipt,
    )
    metrics = InMemorySourcePublicationMetrics()
    service = SourceVersionPublicationService(
        store=store,
        object_store=object_store,
        metrics=metrics,
        clock=SequencedUtcClock(moments=[_PUBLICATION_START, _PUBLICATION_START]),
        policy_guard=AllowingPolicyGuard(
            ledger=ledger,
            publication_evidence=publication_evidence,
        ),
        diagnostics=diagnostics,
    )
    return service, store, object_store, metrics, ledger


@pytest.mark.asyncio
async def test_retryable_publication_failure_is_not_recorded_as_a_rejection() -> None:
    """A busy dependency must remain retryable in metrics and diagnostics.

    This catches the bug where every ``SourcePublicationError`` is recorded
    under the terminal rejected outcome, which makes transient dependency
    pressure indistinguishable from a known business rejection.
    """

    diagnostics = RecordingDiagnosticSink()
    busy_command = build_create_command()
    service, _, _, metrics, _ = _build_service(
        resolve_error=SourcePublicationError(
            ErrorCode.SOURCE_CONCURRENCY_BUSY,
            safe_details={"source_id": busy_command.source_id},
        ),
        diagnostics=diagnostics,
    )

    with pytest.raises(SourcePublicationError):
        await service.publish_create(
            command=busy_command,
            stream=ProbedByteStream([b"unused"]),
            diagnostic_context=build_diagnostic_context(),
        )

    assert PublicationMetricOutcome.REJECTED not in [
        record.outcome for record in metrics.publication_records()
    ]
    assert [event_name for event_name, _ in diagnostics.events] == [
        EventName.SOURCE_VERSION_PUBLISH_FAILED
    ]
    assert set(diagnostics.events[0][1]) == {
        "operation",
        "outcome",
        "duration_ms",
        "error_code",
        "error_category",
        "is_retryable",
        "source_id",
        "event_id",
    }


class _LocatorConflictCommitStore:
    """Store double whose locked create rejects with the typed locator conflict.

    Models the durable store's guarded pre-check: the replay preflight proves
    absence (the conflicting ACTIVE locator belongs to a foreign source, so no
    retry could ever hydrate a replay), and the locked create transition
    rejects with the typed, non-retryable ``source_locator_conflict`` before
    the initial-locator INSERT.
    """

    async def resolve_committed(
        self,
        command: object,
        request_fingerprint: object,
        diagnostic_context: object,
    ) -> None:
        del command, request_fingerprint, diagnostic_context
        return None

    async def commit_create(
        self,
        command: CreateSourceVersion,
        request_fingerprint: object,
        receipt: object,
        diagnostic_context: object,
        *,
        preflight_decision: object = None,
    ) -> SourceVersionPublicationResult:
        del command, request_fingerprint, receipt, diagnostic_context, preflight_decision
        # The registry admits no safe detail field for this code: the rejected
        # source identity rides the diagnostic event fields and the audit row.
        raise SourcePublicationError(ErrorCode.SOURCE_LOCATOR_CONFLICT)

    async def commit_update(
        self,
        command: object,
        request_fingerprint: object,
        receipt: object,
        diagnostic_context: object,
        *,
        preflight_decision: object = None,
    ) -> SourceVersionPublicationResult:
        del command, request_fingerprint, receipt, diagnostic_context, preflight_decision
        raise AssertionError("update path is not under test")


@pytest.mark.asyncio
async def test_locator_conflict_rejection_is_a_registered_business_rejection() -> None:
    """The typed locator conflict crosses the service as the audited rejection.

    The stuck live loop proved the same deterministic conflict emitted as the
    retryable ``source_version_publish_failed`` event; once the durable store
    rejects with the typed conflict, the service must record the closed
    rejection reason and emit ``source_version_publish_rejected`` with
    ``is_retryable=false`` — the client's signal to park the event instead of
    retrying forever.
    """

    diagnostics = RecordingDiagnosticSink()
    expected = build_expected_object()
    receipt = build_verified_receipt(expected, _PUBLICATION_START)
    metrics = InMemorySourcePublicationMetrics()
    service = SourceVersionPublicationService(
        store=cast(Any, _LocatorConflictCommitStore()),
        object_store=FakeCanonicalObjectStore(
            ledger=CallLedger(),
            resolve_receipts=[receipt],
            store_receipt=receipt,
        ),
        metrics=metrics,
        clock=SequencedUtcClock(moments=[_PUBLICATION_START, _PUBLICATION_START]),
        policy_guard=AllowingPolicyGuard(ledger=CallLedger()),
        diagnostics=diagnostics,
    )
    command = build_create_command(
        expected,
        initial_locator=NormalizedLocator("notes/conflicted.md"),
    )

    with pytest.raises(SourcePublicationError) as raised:
        await service.publish_create(
            command=command,
            stream=ProbedByteStream([b"canonical-bytes"]),
            diagnostic_context=build_diagnostic_context(),
        )

    assert raised.value.error_code is ErrorCode.SOURCE_LOCATOR_CONFLICT
    assert raised.value.is_retryable is False

    assert [event_name for event_name, _ in diagnostics.events] == [
        EventName.SOURCE_VERSION_PUBLISH_REJECTED
    ]
    fields = diagnostics.events[0][1]
    assert fields["error_code"] == ErrorCode.SOURCE_LOCATOR_CONFLICT
    assert fields["is_retryable"] is False
    assert fields["reason_code"] == PublicationRejectionReason.SOURCE_LOCATOR_CONFLICT
    assert (
        metrics.rejection_count(
            PublicationOperation.CREATE, PublicationRejectionReason.SOURCE_LOCATOR_CONFLICT
        )
        == 1
    )
    assert (
        metrics.publication_count(PublicationOperation.CREATE, PublicationMetricOutcome.REJECTED)
        == 1
    )


@pytest.mark.asyncio
async def test_exact_replay_returns_committed_result_without_object_store_calls() -> None:
    command = build_create_command()
    committed = build_committed_result(command)
    service, store, object_store, metrics, ledger = _build_service(committed_result=committed)

    result = await service.publish_create(
        command=command,
        stream=ProbedByteStream([b"unused"]),
        diagnostic_context=build_diagnostic_context(),
    )

    assert result is committed
    assert ledger.entries == [POLICY_GUARD_PUBLICATION, STORE_RESOLVE_COMMITTED]
    assert object_store.resolve_call_count() == 0
    assert store.resolve_committed_fingerprints == [compute_request_fingerprint(command)]
    assert metrics.replay_count(PublicationOperation.CREATE) == 1
    assert (
        metrics.publication_count(PublicationOperation.CREATE, PublicationMetricOutcome.REPLAYED)
        == 1
    )
    assert (
        metrics.publication_count(PublicationOperation.CREATE, PublicationMetricOutcome.SUCCEEDED)
        == 0
    )


@pytest.mark.asyncio
async def test_exact_replay_never_consumes_the_caller_stream() -> None:
    committed = build_committed_result(build_create_command())
    service, _, _, _, _ = _build_service(committed_result=committed)
    stream = ProbedByteStream([b"must-not-be-read"])

    await service.publish_create(
        command=build_create_command(),
        stream=stream,
        diagnostic_context=build_diagnostic_context(),
    )

    assert stream.was_consumed is False


@pytest.mark.asyncio
async def test_new_commit_resolves_existing_object_and_commits_without_upload() -> None:
    receipt = build_verified_receipt(build_expected_object(), _PUBLICATION_START)
    service, store, _, metrics, ledger = _build_service(resolve_receipts=[receipt])
    command = build_create_command()

    result = await service.publish_create(
        command=command,
        stream=ProbedByteStream([b"unused"]),
        diagnostic_context=build_diagnostic_context(),
    )

    assert ledger.entries == [
        POLICY_GUARD_PUBLICATION,
        STORE_RESOLVE_COMMITTED,
        OBJECT_STORE_RESOLVE,
        STORE_COMMIT_CREATE,
    ]
    assert result is store.commit_result
    assert store.commit_receipt_identities == [[id(receipt)]]
    assert store.commit_fingerprints == [compute_request_fingerprint(command)]
    assert (
        metrics.publication_count(PublicationOperation.CREATE, PublicationMetricOutcome.SUCCEEDED)
        == 1
    )
    assert metrics.replay_count(PublicationOperation.CREATE) == 0


@pytest.mark.asyncio
async def test_bound_policy_evidence_flows_to_the_commit_unchanged() -> None:
    command = build_create_command()
    binding = AllowedPolicyRevisionBinding(
        workspace_id=command.workspace_id,
        policy_revision_number=7,
    )
    receipt = build_verified_receipt(command.expected_object, _PUBLICATION_START)
    service, store, _, _, _ = _build_service(
        resolve_receipts=[receipt],
        publication_evidence=binding,
    )

    await service.publish_create(
        command=command,
        stream=ProbedByteStream([b"unused"]),
        diagnostic_context=build_diagnostic_context(),
    )

    assert store.commit_policy_decisions == [binding]
    assert store.commit_policy_decisions[0] is binding


@pytest.mark.asyncio
async def test_new_commit_stores_caller_stream_when_no_object_exists() -> None:
    command = build_create_command()
    receipt = build_verified_receipt(command.expected_object, _PUBLICATION_START)
    service, _, object_store, _, ledger = _build_service(
        resolve_receipts=[None],
        store_receipt=receipt,
    )
    stream = ProbedByteStream([b"canonical ", b"publication ", b"bytes"])

    await service.publish_create(
        command=command,
        stream=stream,
        diagnostic_context=build_diagnostic_context(),
    )

    assert ledger.entries == [
        POLICY_GUARD_PUBLICATION,
        STORE_RESOLVE_COMMITTED,
        OBJECT_STORE_RESOLVE,
        OBJECT_STORE_STORE_STREAM,
        STORE_COMMIT_CREATE,
    ]
    assert stream.was_consumed is True
    assert object_store.store_stream_calls == [
        (
            command.expected_object.size_bytes,
            command.expected_object.media_type.value,
            command.expected_object.content_digest.hexadecimal,
            len(b"canonical publication bytes"),
        )
    ]


@pytest.mark.asyncio
async def test_update_publication_commits_via_commit_update() -> None:
    receipt = build_verified_receipt(build_expected_object(), _PUBLICATION_START)
    service, store, _, _, ledger = _build_service(resolve_receipts=[receipt])
    update_command = build_update_command()
    store.commit_result = build_committed_result(update_command)

    result = await service.publish_update(
        command=update_command,
        stream=ProbedByteStream([b"unused"]),
        diagnostic_context=build_diagnostic_context(),
    )

    assert ledger.entries == [
        POLICY_GUARD_PUBLICATION,
        STORE_RESOLVE_COMMITTED,
        OBJECT_STORE_RESOLVE,
        STORE_COMMIT_UPDATE,
    ]
    assert result is store.commit_result


@pytest.mark.asyncio
async def test_preflight_mismatch_stops_before_any_object_store_call() -> None:
    service, store, object_store, metrics, ledger = _build_service(
        resolve_error=build_idempotency_mismatch_error()
    )

    with pytest.raises(SourcePublicationError) as exc_info:
        await service.publish_create(
            command=build_create_command(),
            stream=ProbedByteStream([b"unused"]),
            diagnostic_context=build_diagnostic_context(),
        )

    assert exc_info.value.error_code is ErrorCode.SOURCE_IDEMPOTENCY_MISMATCH
    assert ledger.entries == [POLICY_GUARD_PUBLICATION, STORE_RESOLVE_COMMITTED]
    assert object_store.resolve_call_count() == 0
    assert store.commit_receipt_identities == []
    assert (
        metrics.rejection_count(
            PublicationOperation.CREATE, PublicationRejectionReason.SOURCE_IDEMPOTENCY_MISMATCH
        )
        == 1
    )
    assert (
        metrics.publication_count(PublicationOperation.CREATE, PublicationMetricOutcome.REJECTED)
        == 1
    )


@pytest.mark.asyncio
async def test_bounded_database_retry_reuses_the_single_obtained_receipt() -> None:
    receipt = build_verified_receipt(build_expected_object(), _PUBLICATION_START)
    service, store, object_store, _, _ = _build_service(
        resolve_receipts=[receipt],
        internal_retry_attempts=2,
    )

    await service.publish_create(
        command=build_create_command(),
        stream=ProbedByteStream([b"unused"]),
        diagnostic_context=build_diagnostic_context(),
    )

    # One receipt for one service invocation, reused by every adapter attempt;
    # the retry never returns to the object store.
    assert store.commit_receipt_identities == [[id(receipt), id(receipt), id(receipt)]]
    assert object_store.resolve_call_count() == 1


@pytest.mark.asyncio
async def test_fresh_invocation_obtains_another_receipt_when_preflight_misses() -> None:
    command = build_create_command()
    first_receipt = build_verified_receipt(command.expected_object, _PUBLICATION_START)
    # The second invocation's receipt is a fresh instance within the allowed
    # age window; the frozen clock repeats its last queued moment.
    second_receipt = build_verified_receipt(
        command.expected_object, _PUBLICATION_START - timedelta(seconds=1)
    )
    service, store, object_store, _, ledger = _build_service(
        resolve_receipts=[first_receipt, second_receipt]
    )
    diagnostic_context = build_diagnostic_context()

    await service.publish_create(
        command=command,
        stream=ProbedByteStream([b"unused"]),
        diagnostic_context=diagnostic_context,
    )
    await service.publish_create(
        command=command,
        stream=ProbedByteStream([b"unused"]),
        diagnostic_context=diagnostic_context,
    )

    assert ledger.entries == [
        POLICY_GUARD_PUBLICATION,
        STORE_RESOLVE_COMMITTED,
        OBJECT_STORE_RESOLVE,
        STORE_COMMIT_CREATE,
        POLICY_GUARD_PUBLICATION,
        STORE_RESOLVE_COMMITTED,
        OBJECT_STORE_RESOLVE,
        STORE_COMMIT_CREATE,
    ]
    assert object_store.resolve_call_count() == 2
    assert store.commit_receipt_identities == [[id(first_receipt)], [id(second_receipt)]]


@pytest.mark.asyncio
async def test_fresh_invocation_preflight_hit_makes_no_object_store_call() -> None:
    receipt = build_verified_receipt(build_expected_object(), _PUBLICATION_START)
    service, store, object_store, _, _ = _build_service(resolve_receipts=[receipt])
    command = build_create_command()
    diagnostic_context = build_diagnostic_context()

    first = await service.publish_create(
        command=command,
        stream=ProbedByteStream([b"unused"]),
        diagnostic_context=diagnostic_context,
    )
    # The first invocation's transaction committed, so the retry hits preflight.
    store.committed_result = first
    second = await service.publish_create(
        command=command,
        stream=ProbedByteStream([b"unused"]),
        diagnostic_context=diagnostic_context,
    )

    # The second invocation hits the committed preflight: no second receipt.
    assert object_store.resolve_call_count() == 1
    assert second is first


@pytest.mark.asyncio
async def test_invalid_expected_object_is_rejected_before_any_io() -> None:
    # Value-object constructors do not self-validate; the service must.
    invalid_digest = ContentDigest("z" * 64)
    service, store, object_store, metrics, ledger = _build_service(
        resolve_receipts=[build_verified_receipt(build_expected_object(), _PUBLICATION_START)]
    )
    command = build_create_command(
        ExpectedObject(
            content_digest=invalid_digest,
            size_bytes=8,
            media_type=build_expected_object().media_type,
        )
    )

    with pytest.raises(SourcePublicationError) as exc_info:
        await service.publish_create(
            command=command,
            stream=ProbedByteStream([b"unused"]),
            diagnostic_context=build_diagnostic_context(),
        )

    assert exc_info.value.error_code is ErrorCode.SOURCE_PUBLISH_INPUT_INVALID
    assert ledger.entries == []
    assert object_store.resolve_call_count() == 0
    assert store.commit_receipt_identities == []
    assert (
        metrics.rejection_count(
            PublicationOperation.CREATE,
            PublicationRejectionReason.SOURCE_PUBLISH_INPUT_INVALID,
        )
        == 1
    )


# --- mandatory policy preflight (spec 14) ----------------------------------------


def _build_service_with_guard(
    guard: object,
    *,
    diagnostics: RecordingDiagnosticSink | None = None,
) -> tuple[
    SourceVersionPublicationService,
    FakeSourcePublicationStore,
    FakeCanonicalObjectStore,
    InMemorySourcePublicationMetrics,
    CallLedger,
]:
    """Wire the service against fakes with one scripted policy guard."""

    ledger = CallLedger()
    store = FakeSourcePublicationStore(
        ledger=ledger,
        commit_result=build_committed_result(build_create_command()),
    )
    object_store = FakeCanonicalObjectStore(
        ledger=ledger,
        resolve_receipts=[build_verified_receipt(build_expected_object(), _PUBLICATION_START)],
    )
    metrics = InMemorySourcePublicationMetrics()
    service = SourceVersionPublicationService(
        store=store,
        object_store=object_store,
        metrics=metrics,
        clock=SequencedUtcClock(moments=[_PUBLICATION_START, _PUBLICATION_START]),
        policy_guard=guard,  # type: ignore[arg-type]
        diagnostics=diagnostics,
    )
    return service, store, object_store, metrics, ledger


@pytest.mark.asyncio
async def test_excluded_source_never_calls_object_store() -> None:
    service, store, object_store, metrics, ledger = _build_service_with_guard(
        denying_policy_guard(ErrorCode.EXCLUSION_POLICY_DENIED)
    )

    with pytest.raises(ExclusionPolicyError) as raised:
        await service.publish_create(
            command=build_create_command(),
            stream=ProbedByteStream([b"must-not-be-read"]),
            diagnostic_context=build_diagnostic_context(),
        )

    assert raised.value.error_code is ErrorCode.EXCLUSION_POLICY_DENIED
    # The denial stops the invocation before the store preflight, before any
    # object-store call and before the caller's stream was read.
    assert ledger.entries == []
    assert object_store.resolve_call_count() == 0
    assert object_store.store_stream_calls == []
    assert store.commit_receipt_identities == []
    # The not-retryable verdict still records the terminal rejected
    # publication outcome (spec 2026-08-24 C3): a denial must leave a trail.
    assert (
        metrics.publication_count(PublicationOperation.CREATE, PublicationMetricOutcome.REJECTED)
        == 1
    )


@pytest.mark.asyncio
async def test_indeterminate_subject_fails_closed_before_any_io() -> None:
    service, _store, object_store, _, ledger = _build_service_with_guard(
        denying_policy_guard(ErrorCode.EXCLUSION_POLICY_INDETERMINATE)
    )

    with pytest.raises(ExclusionPolicyError) as raised:
        await service.publish_create(
            command=build_create_command(),
            stream=ProbedByteStream([b"must-not-be-read"]),
            diagnostic_context=build_diagnostic_context(),
        )

    assert raised.value.error_code is ErrorCode.EXCLUSION_POLICY_INDETERMINATE
    assert ledger.entries == []
    assert object_store.resolve_call_count() == 0


@pytest.mark.asyncio
async def test_missing_active_policy_fails_closed_before_any_io() -> None:
    service, _store, object_store, _, ledger = _build_service_with_guard(
        denying_policy_guard(ErrorCode.EXCLUSION_POLICY_NOT_INITIALIZED)
    )

    with pytest.raises(ExclusionPolicyError) as raised:
        await service.publish_create(
            command=build_create_command(),
            stream=ProbedByteStream([b"must-not-be-read"]),
            diagnostic_context=build_diagnostic_context(),
        )

    assert raised.value.error_code is ErrorCode.EXCLUSION_POLICY_NOT_INITIALIZED
    assert ledger.entries == []
    assert object_store.resolve_call_count() == 0


@pytest.mark.asyncio
async def test_corrupt_signature_material_fails_closed_before_any_io() -> None:
    service, _store, object_store, _, ledger = _build_service_with_guard(
        denying_policy_guard(ErrorCode.EXCLUSION_POLICY_SIGNING_UNAVAILABLE)
    )

    with pytest.raises(ExclusionPolicyError) as raised:
        await service.publish_create(
            command=build_create_command(),
            stream=ProbedByteStream([b"must-not-be-read"]),
            diagnostic_context=build_diagnostic_context(),
        )

    assert raised.value.error_code is ErrorCode.EXCLUSION_POLICY_SIGNING_UNAVAILABLE
    assert ledger.entries == []
    assert object_store.resolve_call_count() == 0


@pytest.mark.asyncio
async def test_exact_replay_now_excluded_returns_no_canonical_data() -> None:
    command = build_create_command()
    service, store, object_store, _, ledger = _build_service_with_guard(
        denying_policy_guard(ErrorCode.EXCLUSION_POLICY_DENIED)
    )
    # Even with the committed replay result already resolvable, the now-denied
    # subject must not receive the canonical replay data.
    store.committed_result = build_committed_result(command)

    with pytest.raises(ExclusionPolicyError) as raised:
        await service.publish_create(
            command=command,
            stream=ProbedByteStream([b"must-not-be-read"]),
            diagnostic_context=build_diagnostic_context(),
        )

    assert raised.value.error_code is ErrorCode.EXCLUSION_POLICY_DENIED
    # The denial precedes the store's replay lookup entirely.
    assert ledger.entries == []
    assert store.resolve_committed_fingerprints == []
    assert object_store.resolve_call_count() == 0


@pytest.mark.asyncio
async def test_preflight_evidence_flows_to_the_commit_as_a_hint() -> None:
    receipt = build_verified_receipt(build_expected_object(), _PUBLICATION_START)
    command = build_create_command()
    decision = build_policy_decision(workspace_id=command.workspace_id)
    ledger = CallLedger()
    guard = AllowingPolicyGuard(ledger=ledger, decision=decision)
    store = FakeSourcePublicationStore(ledger=ledger, commit_result=build_committed_result(command))
    object_store = FakeCanonicalObjectStore(ledger=ledger, resolve_receipts=[receipt])
    service = SourceVersionPublicationService(
        store=store,
        object_store=object_store,
        metrics=InMemorySourcePublicationMetrics(),
        clock=SequencedUtcClock(moments=[_PUBLICATION_START, _PUBLICATION_START]),
        policy_guard=guard,
    )

    result = await service.publish_create(
        command=command,
        stream=ProbedByteStream([b"unused"]),
        diagnostic_context=build_diagnostic_context(),
    )

    assert result is store.commit_result
    # The store receives exactly the guard's decision as non-authoritative
    # preflight evidence, once per commit invocation.
    assert store.commit_policy_decisions == [decision]
    assert guard.publication_calls == [command.source_id]


# --- policy-guard failure surfaces (spec 2026-08-24 C3, gap G3) --------------------


async def _publish_under_guard_denial(
    guard_error: ExclusionPolicyError,
) -> tuple[
    ExclusionPolicyError,
    RecordingDiagnosticSink,
    InMemorySourcePublicationMetrics,
    CallLedger,
    FakeCanonicalObjectStore,
]:
    """Run one create whose policy guard raises ``guard_error``; capture surfaces.

    Returns the raised error (for identity checks), the recorded diagnostic
    events, the publication metrics, the port ledger and the object store so
    each policy-guard test can assert the full failure trail in one shape.
    """

    diagnostics = RecordingDiagnosticSink()
    service, _store, object_store, metrics, ledger = _build_service_with_guard(
        DenyingPolicyGuard(error=guard_error),
        diagnostics=diagnostics,
    )

    with pytest.raises(ExclusionPolicyError) as raised:
        await service.publish_create(
            command=build_create_command(),
            stream=ProbedByteStream([b"must-not-be-read"]),
            diagnostic_context=build_diagnostic_context(),
        )

    return raised.value, diagnostics, metrics, ledger, object_store


@pytest.mark.asyncio
async def test_policy_denial_records_failed_event_and_rejected_publication_outcome() -> None:
    """A guard denial crosses the service leaving the closed failure trail.

    The typed denial used to escape ``_publish`` uncaught, so the operator
    saw neither a publication outcome nor a failed event (gap G3). The
    not-retryable verdict now rides the existing failed-event shape with the
    closed registry code and the terminal rejected publication outcome, and
    the error re-raises unchanged for envelope rendering.
    """

    denial = ExclusionPolicyError(ErrorCode.EXCLUSION_POLICY_DENIED)
    raised, diagnostics, metrics, ledger, object_store = await _publish_under_guard_denial(denial)

    # The typed error re-raises unchanged: same instance, same closed code.
    assert raised is denial
    assert raised.error_code is ErrorCode.EXCLUSION_POLICY_DENIED
    # The denial still stops the invocation before the store preflight and
    # before any object-store access.
    assert ledger.entries == []
    assert object_store.resolve_call_count() == 0
    # The existing failed-event shape carries the closed registry code.
    assert [event_name for event_name, _ in diagnostics.events] == [
        EventName.SOURCE_VERSION_PUBLISH_FAILED
    ]
    fields = diagnostics.events[0][1]
    assert fields["error_code"] is ErrorCode.EXCLUSION_POLICY_DENIED
    assert fields["error_category"] is ErrorCategory.AUTHORIZATION
    assert fields["is_retryable"] is False
    assert set(fields) == {
        "operation",
        "outcome",
        "duration_ms",
        "error_code",
        "error_category",
        "is_retryable",
        "source_id",
        "event_id",
    }
    # The not-retryable verdict records the terminal rejected publication
    # outcome: the service never retries a policy verdict.
    assert (
        metrics.publication_count(PublicationOperation.CREATE, PublicationMetricOutcome.REJECTED)
        == 1
    )
    # A policy verdict is not a spec-10.3 business rejection: no rejection
    # counter fires, the closed business-rejection vocabulary stays intact.
    assert (
        sum(
            metrics.rejection_count(PublicationOperation.CREATE, reason)
            for reason in PublicationRejectionReason
        )
        == 0
    )


@pytest.mark.asyncio
async def test_policy_signing_outage_records_failed_event_and_rejected_outcome() -> None:
    """A signing-unavailable outage during publish leaves the closed trail.

    The system-failure outage is the headline G3 case: the guard cannot even
    decide, the service does not retry it, so the outage must surface as the
    failed event with its closed code plus the terminal rejected publication
    outcome instead of vanishing without a trail.
    """

    outage = ExclusionPolicyError(ErrorCode.EXCLUSION_POLICY_SIGNING_UNAVAILABLE)
    raised, diagnostics, metrics, ledger, object_store = await _publish_under_guard_denial(outage)

    assert raised is outage
    assert ledger.entries == []
    assert object_store.resolve_call_count() == 0
    assert [event_name for event_name, _ in diagnostics.events] == [
        EventName.SOURCE_VERSION_PUBLISH_FAILED
    ]
    fields = diagnostics.events[0][1]
    assert fields["error_code"] is ErrorCode.EXCLUSION_POLICY_SIGNING_UNAVAILABLE
    assert fields["error_category"] is ErrorCategory.DEPENDENCY
    assert fields["is_retryable"] is False
    assert (
        metrics.publication_count(PublicationOperation.CREATE, PublicationMetricOutcome.REJECTED)
        == 1
    )


@pytest.mark.asyncio
async def test_retryable_policy_error_records_failed_event_without_terminal_outcome() -> None:
    """A retryable policy registry code keeps the metric-free retryable shape.

    The publication outcome choice follows the not-retryable semantics of the
    underlying registry code: a retryable code assigns no terminal outcome —
    exactly like the busy dependency path — so a later successful retry never
    double-counts.
    """

    outdated = ExclusionPolicyError(ErrorCode.EXCLUSION_POLICY_SNAPSHOT_OUTDATED)
    raised, diagnostics, metrics, ledger, object_store = await _publish_under_guard_denial(outdated)

    assert raised is outdated
    assert ledger.entries == []
    assert object_store.resolve_call_count() == 0
    assert [event_name for event_name, _ in diagnostics.events] == [
        EventName.SOURCE_VERSION_PUBLISH_FAILED
    ]
    fields = diagnostics.events[0][1]
    assert fields["error_code"] is ErrorCode.EXCLUSION_POLICY_SNAPSHOT_OUTDATED
    assert fields["is_retryable"] is True
    assert metrics.publication_records() == []
