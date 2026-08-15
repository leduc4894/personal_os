"""Low-cardinality source-publication metrics contract and in-memory recorder.

Asserts the exact metric-name to label-dimension contracts, the closed enum
vocabularies used in method signatures, that a UUID, idempotency key or digest
can never become a label value, and that recorders reject negative or
non-finite durations.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from personal_os.diagnostics.events import ShortDigest
from personal_os.sources.commands import IdempotencyKey
from personal_os.sources.metrics import (
    SOURCE_METRIC_CONTRACTS,
    CanonicalReadMetrics,
    InMemoryCanonicalReadMetrics,
    InMemorySourcePublicationMetrics,
    ProjectionBacklogStatus,
    ProjectionDispatchErrorCode,
    ProjectionDispatchOutcome,
    ProjectionKind,
    PublicationMetricOutcome,
    PublicationOperation,
    PublicationRejectionReason,
    ReadOutcome,
    SourcePublicationMetrics,
    TransactionRetryReason,
)

#: The exact required metric contracts from spec section 14 and the read spec.
EXPECTED_METRIC_CONTRACTS = {
    "source_version_publish_total": frozenset({"operation", "outcome"}),
    "source_version_publish_duration_seconds": frozenset({"operation", "outcome"}),
    "source_version_replay_total": frozenset({"operation"}),
    "source_version_rejection_total": frozenset({"operation", "reason_code"}),
    "source_version_transaction_retry_total": frozenset({"reason_code"}),
    "projection_intent_backlog": frozenset({"status", "projection_kind"}),
    "projection_intent_oldest_pending_seconds": frozenset({"projection_kind"}),
    "projection_dispatch_total": frozenset({"projection_kind", "outcome", "error_code"}),
    "projection_dispatch_duration_seconds": frozenset({"projection_kind", "outcome"}),
    "projection_lease_reclaimed_total": frozenset({"projection_kind"}),
    "canonical_source_read_total": frozenset({"outcome"}),
    "canonical_source_read_duration_seconds": frozenset({"outcome"}),
}


def test_metric_name_and_label_dimension_contracts_are_exact() -> None:
    assert set(SOURCE_METRIC_CONTRACTS) == set(EXPECTED_METRIC_CONTRACTS)
    for metric_name, dimensions in EXPECTED_METRIC_CONTRACTS.items():
        assert SOURCE_METRIC_CONTRACTS[metric_name] == dimensions, metric_name


def test_label_vocabularies_are_closed_enums() -> None:
    assert {member.value for member in PublicationOperation} == {"create", "update"}
    assert {member.value for member in PublicationMetricOutcome} == {
        "succeeded",
        "replayed",
        "rejected",
    }
    assert {member.value for member in TransactionRetryReason} == {
        "deadlock",
        "serialization_failure",
        "lock_contention",
    }
    assert {member.value for member in ProjectionKind} == {"qdrant", "neo4j"}
    assert {member.value for member in ProjectionDispatchOutcome} == {
        "dispatched",
        "pending",
        "terminal",
    }
    assert {member.value for member in ProjectionBacklogStatus} == {"pending", "leased"}
    assert {member.value for member in PublicationRejectionReason} == {
        "source_publish_input_invalid",
        "source_not_found",
        "source_already_exists",
        "source_state_invalid",
        "source_version_conflict",
        "source_idempotency_mismatch",
        "source_event_identity_mismatch",
        "source_verified_receipt_stale",
        "source_content_object_conflict",
    }
    assert {member.value for member in ProjectionDispatchErrorCode} == {
        "projection_dispatch_unavailable",
        "projection_intent_contract_invalid",
    }
    assert {member.value for member in ReadOutcome} == {"succeeded", "failed"}


def test_in_memory_recorder_satisfies_the_protocol() -> None:
    recorder = InMemorySourcePublicationMetrics()
    assert isinstance(recorder, SourcePublicationMetrics)
    read_recorder = InMemoryCanonicalReadMetrics()
    assert isinstance(read_recorder, CanonicalReadMetrics)


def test_in_memory_recorder_records_low_cardinality_values() -> None:
    recorder = InMemorySourcePublicationMetrics()
    recorder.record_publication(
        operation=PublicationOperation.CREATE,
        outcome=PublicationMetricOutcome.SUCCEEDED,
        duration_seconds=0.25,
    )
    recorder.record_replay(operation=PublicationOperation.UPDATE)
    recorder.record_rejection(
        operation=PublicationOperation.UPDATE,
        reason_code=PublicationRejectionReason.SOURCE_VERSION_CONFLICT,
    )
    recorder.record_transaction_retry(reason_code=TransactionRetryReason.DEADLOCK)
    recorder.record_dispatch(
        projection_kind=ProjectionKind.QDRANT,
        outcome=ProjectionDispatchOutcome.DISPATCHED,
        duration_seconds=0.1,
    )
    recorder.record_dispatch(
        projection_kind=ProjectionKind.NEO4J,
        outcome=ProjectionDispatchOutcome.TERMINAL,
        duration_seconds=0.2,
        error_code=ProjectionDispatchErrorCode.PROJECTION_INTENT_CONTRACT_INVALID,
    )
    recorder.record_lease_reclaimed(projection_kind=ProjectionKind.QDRANT)
    recorder.set_projection_backlog(
        status=ProjectionBacklogStatus.PENDING,
        projection_kind=ProjectionKind.NEO4J,
        count=7,
    )
    recorder.set_oldest_pending_age(projection_kind=ProjectionKind.NEO4J, age_seconds=12.5)

    assert (
        recorder.publication_count(PublicationOperation.CREATE, PublicationMetricOutcome.SUCCEEDED)
        == 1
    )
    assert recorder.replay_count(PublicationOperation.UPDATE) == 1
    assert (
        recorder.rejection_count(
            PublicationOperation.UPDATE, PublicationRejectionReason.SOURCE_VERSION_CONFLICT
        )
        == 1
    )
    assert recorder.transaction_retry_count(TransactionRetryReason.DEADLOCK) == 1
    assert recorder.dispatch_count(ProjectionKind.NEO4J, ProjectionDispatchOutcome.TERMINAL) == 1
    assert recorder.lease_reclaimed_count(ProjectionKind.QDRANT) == 1
    assert recorder.backlog_count(ProjectionBacklogStatus.PENDING, ProjectionKind.NEO4J) == 7
    assert recorder.oldest_pending_age(ProjectionKind.NEO4J) == 12.5
    assert repr(recorder) == "InMemorySourcePublicationMetrics(redacted)"


@pytest.mark.parametrize("duration_seconds", [-0.01, float("nan"), float("inf"), -1.0])
def test_recorder_rejects_invalid_publication_durations(duration_seconds: float) -> None:
    recorder = InMemorySourcePublicationMetrics()
    with pytest.raises(ValueError, match="duration_seconds"):
        recorder.record_publication(
            operation=PublicationOperation.CREATE,
            outcome=PublicationMetricOutcome.SUCCEEDED,
            duration_seconds=duration_seconds,
        )
    assert (
        recorder.publication_count(PublicationOperation.CREATE, PublicationMetricOutcome.SUCCEEDED)
        == 0
    )


def test_recorder_rejects_invalid_dispatch_and_gauge_values() -> None:
    recorder = InMemorySourcePublicationMetrics()
    with pytest.raises(ValueError, match="duration_seconds"):
        recorder.record_dispatch(
            projection_kind=ProjectionKind.QDRANT,
            outcome=ProjectionDispatchOutcome.DISPATCHED,
            duration_seconds=float("nan"),
        )
    with pytest.raises(ValueError, match="age_seconds"):
        recorder.set_oldest_pending_age(
            projection_kind=ProjectionKind.QDRANT, age_seconds=float("inf")
        )
    with pytest.raises(ValueError, match="count"):
        recorder.set_projection_backlog(
            status=ProjectionBacklogStatus.PENDING,
            projection_kind=ProjectionKind.QDRANT,
            count=-1,
        )


def test_uuids_keys_and_digests_can_never_be_labels() -> None:
    recorder = InMemorySourcePublicationMetrics()
    source_id = uuid4()
    key = IdempotencyKey("never-a-label-value")
    digest = ShortDigest("0123456789abcdef")
    with pytest.raises(ValueError, match="operation label"):
        recorder.record_publication(  # type: ignore[arg-type]
            operation=source_id,
            outcome=PublicationMetricOutcome.SUCCEEDED,
            duration_seconds=1.0,
        )
    with pytest.raises(ValueError, match="reason_code label"):
        recorder.record_rejection(
            operation=PublicationOperation.CREATE,
            reason_code=key.value,  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="projection_kind label"):
        recorder.record_dispatch(  # type: ignore[arg-type]
            projection_kind=digest.value,
            outcome=ProjectionDispatchOutcome.DISPATCHED,
            duration_seconds=1.0,
        )
    with pytest.raises(ValueError, match="error_code label"):
        recorder.record_dispatch(
            projection_kind=ProjectionKind.QDRANT,
            outcome=ProjectionDispatchOutcome.TERMINAL,
            duration_seconds=1.0,
            error_code="source_not_found",  # type: ignore[arg-type]
        )
    snapshot = repr(recorder) + repr(recorder.publication_records())
    assert str(source_id) not in snapshot
    assert key.value not in snapshot
    assert digest.value not in snapshot
