"""Provider-neutral projection lease state machine: pins, backoff and diagnostics.

These tests pin the dispatch bounds from design section 11.2 (batch 50, lease
60 seconds, exponential backoff ``min(300, 2 ** prior_attempt_count)``), prove
the leased view is immutable and closed over the registered projection kinds
and operations, and prove the reclaim and stale-lease diagnostic payloads are
accepted by the registered event contracts without any unsafe value.
"""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from personal_os.diagnostics.events import (
    DiagnosticEvent,
    EventName,
    RejectedDiagnosticPayload,
    SafeToken,
    build_registered_event,
)
from personal_os.sources.projection_dispatch import (
    LEASE_EXPIRED_ERROR_CODE,
    PROJECTION_BACKOFF_CAP_SECONDS,
    PROJECTION_CLAIM_BATCH_LIMIT,
    PROJECTION_LEASE_SECONDS,
    LeasedProjectionIntent,
    lease_reclaimed_diagnostic_fields,
    projection_retry_backoff_seconds,
    retry_available_at,
    stale_lease_diagnostic_fields,
)

_NOW: datetime = datetime(2026, 8, 14, 12, 0, 0, tzinfo=UTC)


def _leased_intent(
    *,
    projection_kind: SafeToken | None = None,
    operation: SafeToken | None = None,
    attempt_count: int = 0,
    source_version_id: UUID | None = None,
    leased_until: datetime | None = None,
) -> LeasedProjectionIntent:
    return LeasedProjectionIntent(
        projection_intent_id=uuid4(),
        workspace_id=uuid4(),
        event_id=uuid4(),
        source_id=uuid4(),
        source_version_id=uuid4() if source_version_id is None else source_version_id,
        projection_kind=SafeToken.parse("qdrant") if projection_kind is None else projection_kind,
        operation=SafeToken.parse("upsert") if operation is None else operation,
        attempt_count=attempt_count,
        lease_token=uuid4(),
        leased_until=(_NOW + timedelta(seconds=60) if leased_until is None else leased_until),
    )


def test_claim_batch_limit_is_pinned_at_fifty() -> None:
    assert PROJECTION_CLAIM_BATCH_LIMIT == 50


def test_lease_duration_is_pinned_at_sixty_seconds() -> None:
    assert PROJECTION_LEASE_SECONDS == 60


def test_backoff_doubles_from_one_second() -> None:
    assert [projection_retry_backoff_seconds(count) for count in (0, 1, 2, 3, 5)] == [
        1,
        2,
        4,
        8,
        32,
    ]


def test_backoff_is_capped_at_three_hundred_seconds() -> None:
    assert PROJECTION_BACKOFF_CAP_SECONDS == 300
    assert projection_retry_backoff_seconds(8) == 256
    assert projection_retry_backoff_seconds(9) == 300
    assert projection_retry_backoff_seconds(30) == 300


def test_backoff_rejects_negative_prior_attempt_count() -> None:
    with pytest.raises(ValueError, match="prior_attempt_count"):
        projection_retry_backoff_seconds(-1)


def test_retry_available_at_adds_the_bounded_backoff_to_now() -> None:
    assert retry_available_at(_NOW, 0) == _NOW + timedelta(seconds=1)
    assert retry_available_at(_NOW, 4) == _NOW + timedelta(seconds=16)
    assert retry_available_at(_NOW, 12) == _NOW + timedelta(seconds=300)


def test_leased_intent_is_immutable() -> None:
    intent = _leased_intent()
    with pytest.raises(dataclasses.FrozenInstanceError):
        intent.attempt_count = 7  # type: ignore[misc]


def test_leased_intent_accepts_both_registered_kinds_and_operations() -> None:
    for kind in ("qdrant", "neo4j"):
        for operation in ("upsert", "delete"):
            intent = _leased_intent(
                projection_kind=SafeToken.parse(kind),
                operation=SafeToken.parse(operation),
                source_version_id=None if operation == "delete" else uuid4(),
            )
            assert str(intent.projection_kind) == kind
            assert str(intent.operation) == operation


def test_leased_intent_rejects_unregistered_projection_kind() -> None:
    with pytest.raises(ValueError, match="projection_kind"):
        _leased_intent(projection_kind=SafeToken.parse("redis"))


def test_leased_intent_rejects_unregistered_operation() -> None:
    with pytest.raises(ValueError, match="operation"):
        _leased_intent(operation=SafeToken.parse("patch"))


def test_leased_intent_rejects_negative_attempt_count() -> None:
    with pytest.raises(ValueError, match="attempt_count"):
        _leased_intent(attempt_count=-1)


def test_leased_intent_rejects_naive_lease_expiry() -> None:
    with pytest.raises(ValueError, match="leased_until"):
        _leased_intent(leased_until=datetime(2026, 8, 14, 12, 1, 0))


def test_lease_reclaimed_diagnostic_fields_build_the_registered_event() -> None:
    fields = lease_reclaimed_diagnostic_fields(projection_kind=SafeToken.parse("neo4j"), count=3)
    built = build_registered_event(EventName.PROJECTION_INTENT_LEASE_RECLAIMED, fields)
    assert isinstance(built, DiagnosticEvent)
    assert built.fields["projection_kind"].value == "neo4j"  # type: ignore[attr-defined]
    assert built.fields["count"] == 3


def test_lease_reclaimed_diagnostic_fields_reject_non_positive_counts() -> None:
    with pytest.raises(ValueError, match="count"):
        lease_reclaimed_diagnostic_fields(projection_kind=SafeToken.parse("qdrant"), count=0)


def test_stale_lease_diagnostic_fields_build_the_registered_event() -> None:
    intent_id = uuid4()
    fields = stale_lease_diagnostic_fields(
        projection_kind=SafeToken.parse("qdrant"),
        intent_id=intent_id,
        attempt_count=4,
    )
    built = build_registered_event(EventName.PROJECTION_INTENT_DISPATCH_FAILED, fields)
    assert isinstance(built, DiagnosticEvent)
    assert isinstance(built.fields["intent_id"], UUID)
    assert built.fields["intent_id"] == intent_id
    assert built.fields["error_code"].value == LEASE_EXPIRED_ERROR_CODE.value  # type: ignore[attr-defined]
    assert built.fields["is_retryable"] is False


def test_stale_lease_diagnostic_fields_reject_negative_attempt_count() -> None:
    with pytest.raises(ValueError, match="attempt_count"):
        stale_lease_diagnostic_fields(
            projection_kind=SafeToken.parse("qdrant"), intent_id=uuid4(), attempt_count=-2
        )


def test_stale_lease_diagnostic_is_never_a_rejected_payload() -> None:
    for attempt_count in (0, 1, 9):
        built = build_registered_event(
            EventName.PROJECTION_INTENT_DISPATCH_FAILED,
            stale_lease_diagnostic_fields(
                projection_kind=SafeToken.parse("neo4j"),
                intent_id=uuid4(),
                attempt_count=attempt_count,
            ),
        )
        assert not isinstance(built, RejectedDiagnosticPayload)


def test_intent_store_port_shape_is_implementable_and_callable() -> None:
    from personal_os.sources.ports import ProjectionIntentStore

    class RecordingIntentStore:
        """Minimal fake implementing the exact port shape."""

        def __init__(self) -> None:
            self.calls: list[str] = []

        async def reclaim_expired(self, now: datetime) -> int:
            self.calls.append(f"reclaim_expired:{now.isoformat()}")
            return 0

        async def claim_batch(
            self, now: datetime, limit: int
        ) -> tuple[LeasedProjectionIntent, ...]:
            self.calls.append(f"claim_batch:{now.isoformat()}:{limit}")
            return ()

        async def acknowledge_dispatched(
            self, intent_id: UUID, lease_token: UUID, now: datetime
        ) -> bool:
            self.calls.append(f"acknowledge_dispatched:{intent_id}:{lease_token}:{now.isoformat()}")
            return True

        async def release_retry(
            self,
            intent_id: UUID,
            lease_token: UUID,
            error_code: SafeToken,
            available_at: datetime,
            now: datetime,
        ) -> bool:
            self.calls.append(f"release_retry:{intent_id}:{error_code}:{available_at.isoformat()}")
            return True

        async def mark_terminal(
            self, intent_id: UUID, lease_token: UUID, error_code: SafeToken, now: datetime
        ) -> bool:
            self.calls.append(f"mark_terminal:{intent_id}:{error_code}")
            return True

    store: ProjectionIntentStore = RecordingIntentStore()
    intent = _leased_intent()

    async def call_every_port_method() -> list[object]:
        return list(
            await asyncio.gather(
                store.reclaim_expired(_NOW),
                store.claim_batch(_NOW, PROJECTION_CLAIM_BATCH_LIMIT),
                store.acknowledge_dispatched(intent.projection_intent_id, intent.lease_token, _NOW),
                store.release_retry(
                    intent.projection_intent_id,
                    intent.lease_token,
                    LEASE_EXPIRED_ERROR_CODE,
                    retry_available_at(_NOW, intent.attempt_count),
                    _NOW,
                ),
                store.mark_terminal(
                    intent.projection_intent_id, intent.lease_token, LEASE_EXPIRED_ERROR_CODE, _NOW
                ),
            )
        )

    assert asyncio.run(call_every_port_method()) == [0, (), True, True, True]
