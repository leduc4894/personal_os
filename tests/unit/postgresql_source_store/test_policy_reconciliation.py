"""Pure-helper contracts of the PostgreSQL policy-reconciliation store (spec 15).

These tests pin the reconciliation persistence helpers without touching a
database: the leased outbox claim joins the revision for its source checkpoint
behind ``FOR UPDATE SKIP LOCKED`` in the pinned order, the fenced lifecycle
statements (lease with database-time expiry, reclaim with capped backoff,
dispatched acknowledgement, retryable release, terminal failure, and the
workflow-side failure release of a dispatched row), the stable keyset batch
scan joining current-version evidence plus the per-source current event
sequence, the ``DISTINCT ON`` prior-evaluation lookup, the insert-once
``policy_evaluations`` statement conflicting on the exact immutable identity,
the deterministic policy-transition intent insert conflicting on the partial
origin uniqueness with ``ON CONFLICT DO NOTHING``, the closed audit values
(system actor, closed actions, counters digest only), and the safe error code
column grammar. SQL text, driver payloads and sensitive values never leave the
statements under test.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from personal_os.diagnostics.events import SafeToken
from personal_os.error_contracts.codes import ErrorCode
from personal_os.exclusion_policy.contracts import (
    EnforcedPolicyDecision,
    RawPolicyDecision,
)
from personal_os.exclusion_policy.reconciliation import (
    RECONCILIATION_BATCH_SIZE,
    ReconciliationTransition,
)
from postgresql_source_store.policy_reconciliation import (
    _SAFE_ERROR_CODE_COLUMN_GRAMMAR,
    POLICY_RECONCILIATION_AUDIT_TARGET_KIND,
    POLICY_RECONCILIATION_BACKOFF_CAP_SECONDS,
    POLICY_RECONCILIATION_CLAIM_BATCH_LIMIT,
    POLICY_RECONCILIATION_LEASE_SECONDS,
    RECONCILIATION_COMPLETED_AUDIT_ACTION,
    RECONCILIATION_DISPATCH_TERMINAL_ERROR_CODE,
    RECONCILIATION_EXECUTION_FAILED_ERROR_CODE,
    RECONCILIATION_FAILED_AUDIT_ACTION,
    RECONCILIATION_LEASE_EXPIRED_ERROR_CODE,
    acknowledge_dispatched_statement,
    active_revision_select_statement,
    build_policy_evaluation_row_values,
    build_policy_transition_intent_values,
    build_reconciliation_audit_values,
    claim_pending_reconciliations_select_statement,
    fail_dispatched_statement,
    lease_reconciliation_update_statement,
    mark_terminal_statement,
    policy_evaluations_insert_statement,
    policy_evaluations_verify_select_statement,
    policy_transition_intent_insert_statement,
    policy_transition_intent_verify_select_statement,
    prior_evaluations_select_statement,
    reclaim_lease_update_statement,
    reconciliation_batch_select_statement,
    release_retry_statement,
    verify_planned_batch_sequences,
)

WORKSPACE_ID = UUID("018f47a0-7b00-7000-8000-0000000000f1")
POLICY_REVISION_ID = UUID("018f47a0-7b00-7000-8000-0000000000f2")
PARENT_REVISION_ID = UUID("018f47a0-7b00-7000-8000-0000000000f3")
SOURCE_ID = UUID("018f47a0-7b00-7000-8000-0000000000f4")
SOURCE_VERSION_ID = UUID("018f47a0-7b00-7000-8000-0000000000f5")
AFTER_SOURCE_ID = UUID("018f47a0-7b00-7000-8000-0000000000f6")
INTENT_ID = UUID("018f47a0-7b00-7000-8000-0000000000f7")
LEASE_TOKEN = UUID("018f47a0-7b00-7000-8000-0000000000f8")
REQUEST_ID = uuid4()
NOW = datetime(2026, 8, 17, 11, 0, 0, tzinfo=UTC)

_SENTINEL_TITLE = "sentinel-title"
_SENTINEL_LOCATOR = "private/notes/sentinel-locator.md"


def _compile(statement: sa.Executable) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


def _params(statement: sa.Executable) -> dict[str, Any]:
    return dict(statement.compile(dialect=postgresql.dialect()).params)


# --- leased outbox ------------------------------------------------------------------


def test_claim_select_matches_only_due_pending_rows_with_the_checkpoint_join() -> None:
    statement = claim_pending_reconciliations_select_statement(NOW, 5)
    compiled = _compile(statement)
    assert "SKIP LOCKED" in compiled
    assert "FOR UPDATE OF policy_reconciliation_intents" in compiled
    assert "source_policies" in compiled
    assert "source_checkpoint_event_sequence" in compiled
    assert "ORDER BY" in compiled and "available_at" in compiled
    parameters = _params(statement)
    assert parameters["now"] == NOW
    assert parameters["state_1"] == "pending"


def test_claim_select_bounds_the_batch_limit_to_the_pinned_pin() -> None:
    assert POLICY_RECONCILIATION_CLAIM_BATCH_LIMIT == 20
    with pytest.raises(ValueError):
        claim_pending_reconciliations_select_statement(NOW, 0)
    with pytest.raises(ValueError):
        claim_pending_reconciliations_select_statement(
            NOW, POLICY_RECONCILIATION_CLAIM_BATCH_LIMIT + 1
        )
    with pytest.raises(ValueError):
        claim_pending_reconciliations_select_statement(NOW.replace(tzinfo=None), 5)


def test_lease_write_fences_on_pending_with_database_time_expiry() -> None:
    statement = lease_reconciliation_update_statement(INTENT_ID)
    compiled = _compile(statement)
    assert "lease_token IS NULL" in compiled
    assert "make_interval" in compiled
    assert "current_timestamp" in compiled.lower()
    assert "attempt_count=(" in compiled
    assert POLICY_RECONCILIATION_LEASE_SECONDS == 60
    parameters = _params(statement)
    assert parameters["state"] == "leased"
    assert parameters["state_1"] == "pending"
    assert parameters["attempt_count_1"] == 1


def test_reclaim_returns_overdue_leases_to_pending_with_capped_backoff() -> None:
    statement = reclaim_lease_update_statement(NOW)
    compiled = _compile(statement)
    assert "leased_until" in compiled
    assert "least" in compiled.lower()
    assert POLICY_RECONCILIATION_BACKOFF_CAP_SECONDS == 300
    parameters = _params(statement)
    assert parameters["state_1"] == "leased"
    assert parameters["state"] == "pending"
    assert parameters["now"] == NOW
    assert parameters["safe_error_code"] == RECONCILIATION_LEASE_EXPIRED_ERROR_CODE.value
    assert parameters["attempt_count_1"] == 1


def test_acknowledge_dispatched_fences_on_the_exact_lease_token() -> None:
    statement = acknowledge_dispatched_statement(INTENT_ID, LEASE_TOKEN)
    compiled = _compile(statement)
    assert "dispatched_at" in compiled
    assert "current_timestamp" in compiled.lower()
    assert "lease_token = " in compiled.replace("lease_token IS NULL", "")
    parameters = _params(statement)
    assert parameters["lease_token"] == LEASE_TOKEN
    assert parameters["state_1"] == "leased"
    assert parameters["state"] == "dispatched"


def test_release_retry_fences_and_applies_the_bounded_backoff() -> None:
    statement = release_retry_statement(
        INTENT_ID, LEASE_TOKEN, RECONCILIATION_EXECUTION_FAILED_ERROR_CODE
    )
    compiled = _compile(statement)
    assert "safe_error_code" in compiled
    assert "attempt_count=(" in compiled
    assert "current_timestamp" in compiled.lower()
    parameters = _params(statement)
    assert parameters["state_1"] == "leased"
    assert parameters["state"] == "pending"
    assert parameters["safe_error_code"] == RECONCILIATION_EXECUTION_FAILED_ERROR_CODE.value
    assert parameters["attempt_count_1"] == 1


def test_mark_terminal_requires_the_closed_error_code() -> None:
    statement = mark_terminal_statement(
        INTENT_ID, LEASE_TOKEN, RECONCILIATION_DISPATCH_TERMINAL_ERROR_CODE
    )
    compiled = _compile(statement)
    assert "safe_error_code" in compiled
    assert _params(statement)["state"] == "terminal"
    assert (
        _params(statement)["safe_error_code"] == RECONCILIATION_DISPATCH_TERMINAL_ERROR_CODE.value
    )
    with pytest.raises(ValueError):
        mark_terminal_statement(INTENT_ID, LEASE_TOKEN, SafeToken.parse("UPPER-case"))


def test_fail_dispatched_release_fences_on_the_dispatched_state() -> None:
    retryable = fail_dispatched_statement(
        WORKSPACE_ID, POLICY_REVISION_ID, RECONCILIATION_EXECUTION_FAILED_ERROR_CODE
    )
    compiled = _compile(retryable)
    assert "attempt_count=(" in compiled
    assert "safe_error_code" in compiled
    parameters = _params(retryable)
    assert parameters["state"] == "pending"
    assert " IN " in compiled
    assert set(parameters["state_1"]) == {"leased", "dispatched"}
    assert parameters["attempt_count_1"] == 1
    terminal = fail_dispatched_statement(
        WORKSPACE_ID,
        POLICY_REVISION_ID,
        RECONCILIATION_DISPATCH_TERMINAL_ERROR_CODE,
        retryable=False,
    )
    assert _params(terminal)["state"] == "terminal"


def test_safe_error_code_column_grammar_is_enforced() -> None:
    assert _SAFE_ERROR_CODE_COLUMN_GRAMMAR.fullmatch("reconciliation_execution_failed")
    assert _SAFE_ERROR_CODE_COLUMN_GRAMMAR.fullmatch("UPPER") is None
    assert _SAFE_ERROR_CODE_COLUMN_GRAMMAR.fullmatch("1startswithdigit") is None


# --- batch scan and prior evaluation -------------------------------------------------


def test_batch_select_streams_a_stable_keyset_page_with_subject_sequences() -> None:
    statement = reconciliation_batch_select_statement(
        WORKSPACE_ID, after_source_id=AFTER_SOURCE_ID, limit=RECONCILIATION_BATCH_SIZE
    )
    compiled = _compile(statement)
    assert "deleted_at IS NULL" in compiled
    assert "source_id >" in compiled
    assert "ORDER BY knowledge.sources.source_id" in compiled
    assert "max(knowledge.sync_events.event_sequence)" in compiled
    assert "source_versions" in compiled and "content_objects" in compiled
    assert "current_version_id" in compiled
    assert _params(statement)["after_source_id"] == AFTER_SOURCE_ID
    assert "FOR UPDATE" not in compiled


def test_batch_select_bounds_the_page_to_the_pinned_batch_size() -> None:
    with pytest.raises(ValueError):
        reconciliation_batch_select_statement(WORKSPACE_ID, after_source_id=None, limit=0)
    with pytest.raises(ValueError):
        reconciliation_batch_select_statement(
            WORKSPACE_ID, after_source_id=None, limit=RECONCILIATION_BATCH_SIZE + 1
        )
    first_page = reconciliation_batch_select_statement(WORKSPACE_ID, after_source_id=None, limit=10)
    assert "source_id >" not in _compile(first_page)


def test_prior_evaluation_lookup_takes_the_most_recent_sequence_per_source() -> None:
    statement = prior_evaluations_select_statement(PARENT_REVISION_ID, (SOURCE_ID,))
    compiled = _compile(statement)
    assert "DISTINCT ON" in compiled and "policy_evaluations.source_id" in compiled
    assert (
        "ORDER BY knowledge.policy_evaluations.source_id, "
        "knowledge.policy_evaluations.subject_event_sequence DESC" in compiled
    )
    assert "policy_revision_id" in compiled


def test_active_revision_lookup_reads_the_pointer_without_locking() -> None:
    statement = active_revision_select_statement(WORKSPACE_ID)
    compiled = _compile(statement)
    assert "active_policy_revision_id" in compiled
    assert "FOR UPDATE" not in compiled


# --- insert-once evidence and deterministic intents -----------------------------------


def test_evaluation_insert_conflicts_only_on_the_immutable_identity() -> None:
    statement = policy_evaluations_insert_statement(
        [
            build_policy_evaluation_row_values(
                policy_evaluation_id=uuid4(),
                workspace_id=WORKSPACE_ID,
                policy_revision_id=POLICY_REVISION_ID,
                source_id=SOURCE_ID,
                subject_event_sequence=4,
                raw_decision=RawPolicyDecision.ALLOWED,
                enforced_decision=EnforcedPolicyDecision.ALLOWED,
                matched_rule_ids=(),
                missing_fields=(),
                subject_fingerprint="a" * 64,
                evaluated_at=None,
            )
        ]
    )
    compiled = _compile(statement)
    assert "ON CONFLICT" in compiled and "DO NOTHING" in compiled
    assert "(policy_revision_id, source_id, subject_event_sequence)" in compiled.replace(
        "knowledge.", ""
    )


def test_evaluation_verify_select_reads_the_compared_columns() -> None:
    statement = policy_evaluations_verify_select_statement(POLICY_REVISION_ID, (SOURCE_ID,))
    compiled = _compile(statement)
    for column in (
        "raw_decision",
        "enforced_decision",
        "subject_fingerprint",
        "subject_event_sequence",
    ):
        assert column in compiled


def test_intent_insert_conflicts_on_the_partial_policy_transition_uniqueness() -> None:
    statement = policy_transition_intent_insert_statement(
        [
            build_policy_transition_intent_values(
                projection_intent_id=uuid4(),
                workspace_id=WORKSPACE_ID,
                policy_revision_id=POLICY_REVISION_ID,
                source_id=SOURCE_ID,
                source_version_id=SOURCE_VERSION_ID,
                projection_kind="qdrant",
                operation="delete",
                available_at=None,
            )
        ]
    )
    compiled = _compile(statement)
    assert "ON CONFLICT" in compiled and "DO NOTHING" in compiled
    assert "(policy_revision_id, source_id, projection_kind)" in compiled.replace("knowledge.", "")
    assert "origin_kind = 'policy_transition'" in compiled


def test_intent_verify_select_reads_operation_and_source_version() -> None:
    statement = policy_transition_intent_verify_select_statement(POLICY_REVISION_ID, (SOURCE_ID,))
    compiled = _compile(statement)
    assert "operation" in compiled
    assert "source_version_id" in compiled
    assert "origin_kind" in compiled


def test_evaluation_row_values_carry_only_closed_evidence() -> None:
    values = build_policy_evaluation_row_values(
        policy_evaluation_id=uuid4(),
        workspace_id=WORKSPACE_ID,
        policy_revision_id=POLICY_REVISION_ID,
        source_id=SOURCE_ID,
        subject_event_sequence=4,
        raw_decision=RawPolicyDecision.ALLOWED,
        enforced_decision=EnforcedPolicyDecision.ALLOWED,
        matched_rule_ids=(),
        missing_fields=(),
        subject_fingerprint="a" * 64,
        evaluated_at=NOW,
    )
    assert values["raw_decision"] == "allowed"
    assert values["enforced_decision"] == "allowed"
    assert values["matched_rule_ids"] == ""
    assert values["missing_fields"] == ""
    assert _SENTINEL_TITLE not in str(values)
    assert _SENTINEL_LOCATOR not in str(values)


def test_intent_row_values_populate_exactly_the_policy_transition_origin() -> None:
    values = build_policy_transition_intent_values(
        projection_intent_id=uuid4(),
        workspace_id=WORKSPACE_ID,
        policy_revision_id=POLICY_REVISION_ID,
        source_id=SOURCE_ID,
        source_version_id=SOURCE_VERSION_ID,
        projection_kind="qdrant",
        operation="delete",
        available_at=NOW,
    )
    assert values["origin_kind"] == "policy_transition"
    assert values["event_id"] is None
    assert values["policy_revision_id"] == POLICY_REVISION_ID
    assert values["status"] == "pending"
    assert values["attempt_count"] == 0
    assert values["lease_token"] is None
    assert values["operation"] == "delete"


def test_upsert_intent_row_values_require_a_source_version() -> None:
    with pytest.raises(ValueError):
        build_policy_transition_intent_values(
            projection_intent_id=uuid4(),
            workspace_id=WORKSPACE_ID,
            policy_revision_id=POLICY_REVISION_ID,
            source_id=SOURCE_ID,
            source_version_id=None,
            projection_kind="qdrant",
            operation="upsert",
            available_at=NOW,
        )


# --- write-recheck helper -------------------------------------------------------------


def test_verify_planned_batch_sequences_rejects_drift_with_the_typed_stale_error() -> None:
    planned = {SOURCE_ID: 4}
    matching = [{"source_id": SOURCE_ID, "subject_event_sequence": 4}]
    verify_planned_batch_sequences(planned, matching)  # no raise
    drifted = [{"source_id": SOURCE_ID, "subject_event_sequence": 5}]
    with pytest.raises(Exception) as raised:
        verify_planned_batch_sequences(planned, drifted)
    assert getattr(raised.value, "error_code", None) is ErrorCode.EXCLUSION_POLICY_SNAPSHOT_OUTDATED


def test_verify_planned_batch_sequences_rejects_a_missing_source_row() -> None:
    with pytest.raises(Exception) as raised:
        verify_planned_batch_sequences({SOURCE_ID: 4}, [])
    assert getattr(raised.value, "error_code", None) is ErrorCode.EXCLUSION_POLICY_SNAPSHOT_OUTDATED


# --- audit values ---------------------------------------------------------------------


def test_completion_audit_values_use_the_system_actor_and_counters_digest() -> None:
    values = build_reconciliation_audit_values(
        action=RECONCILIATION_COMPLETED_AUDIT_ACTION,
        workspace_id=WORKSPACE_ID,
        policy_revision_id=POLICY_REVISION_ID,
        result="succeeded",
        reason_code=None,
        evaluated_sources=10,
        to_excluded_sources=3,
        to_allowed_sources=2,
        unchanged_sources=5,
        occurred_at=NOW,
        request_id=REQUEST_ID,
    )
    assert values["actor_kind"] == "system"
    assert values["actor_id"] is None
    assert values["action"] == "exclusion_policy.reconciliation_completed"
    assert values["target_kind"] == POLICY_RECONCILIATION_AUDIT_TARGET_KIND
    assert values["target_id"] == POLICY_REVISION_ID
    assert values["result"] == "succeeded"
    assert len(values["safe_diff_hash"]) == 64
    assert _SENTINEL_TITLE not in str(values)
    assert _SENTINEL_LOCATOR not in str(values)


def test_failure_audit_values_carry_the_closed_reason_only() -> None:
    values = build_reconciliation_audit_values(
        action=RECONCILIATION_FAILED_AUDIT_ACTION,
        workspace_id=WORKSPACE_ID,
        policy_revision_id=POLICY_REVISION_ID,
        result="failed",
        reason_code=RECONCILIATION_EXECUTION_FAILED_ERROR_CODE.value,
        evaluated_sources=0,
        to_excluded_sources=0,
        to_allowed_sources=0,
        unchanged_sources=0,
        occurred_at=NOW,
        request_id=REQUEST_ID,
    )
    assert values["action"] == "exclusion_policy.reconciliation_failed"
    assert values["result"] == "failed"
    assert values["reason_code"] == "reconciliation_execution_failed"
    assert values["safe_diff_hash"] is None


def test_transition_counter_labels_stay_closed() -> None:
    # The audit row stores counters as the counters digest only; the closed
    # transition labels exist solely for the metrics sink (spec 21).
    assert frozenset(transition.value for transition in ReconciliationTransition) == frozenset(
        {"to_excluded", "to_allowed", "unchanged"}
    )


# --- batch flow over a fake engine ----------------------------------------------------


@dataclass
class _FakeResult:
    """Minimal async result surface the batch flow consumes."""

    rows: list[dict[str, Any]] = field(default_factory=list)
    scalar: Any = None

    def mappings(self) -> _FakeResult:
        return self

    def all(self) -> list[dict[str, Any]]:
        return list(self.rows)

    def first(self) -> dict[str, Any] | None:
        return self.rows[0] if self.rows else None

    def one_or_none(self) -> dict[str, Any] | None:
        return self.rows[0] if self.rows else None

    def scalar_one_or_none(self) -> Any:
        return self.scalar


class _FakeConnection:
    """One transaction's scripted statements over an in-memory page."""

    def __init__(self, script: dict[str, _FakeResult]) -> None:
        self._script = script
        self.executed: list[str] = []

    async def execute(self, statement: Any) -> _FakeResult:
        if isinstance(statement, sa.TextClause):
            self.executed.append(str(statement))
            return _FakeResult()
        compiled = str(statement.compile(dialect=postgresql.dialect()))
        self.executed.append(compiled)
        for discriminator, result in self._script.items():
            if discriminator in compiled:
                return result
        raise AssertionError(f"unexpected statement: {compiled[:120]}")

    def begin(self) -> _FakeConnection:
        return self

    async def __aenter__(self) -> _FakeConnection:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None


class _FakeEngine:
    """Engine stub handing the store one scripted connection."""

    def __init__(self, connection: _FakeConnection) -> None:
        self._connection = connection

    def connect(self) -> _FakeConnection:
        return self._connection


def _eventless_page_script(active_id: UUID) -> dict[str, _FakeResult]:
    """Script the batch flow over one page whose sources have no events."""

    revision_row = {
        "policy_revision_id": POLICY_REVISION_ID,
        "revision_number": 1,
        "parent_policy_revision_id": None,
        "published_at": NOW,
    }
    eventless_page = [
        {
            "source_id": SOURCE_ID,
            "source_type": "markdown",
            "current_version_id": None,
            "media_type": None,
            "byte_size": None,
            "subject_event_sequence": None,
        }
    ]
    return {
        "workspace_policy_state": _FakeResult(scalar=active_id),
        "parent_policy_revision_id": _FakeResult(rows=[revision_row]),
        "policy_rules": _FakeResult(rows=[]),
        "FROM knowledge.sources": _FakeResult(rows=eventless_page),
    }


@pytest.mark.asyncio
async def test_all_eventless_page_completes_the_batch_without_writes() -> None:
    """A page whose sources all lack canonical events still completes.

    The page read tolerates sources without any canonical event (there is no
    subject state to evaluate), so a whole page of them must commit as an
    empty batch — cursor advanced, zero evaluations, zero intents — instead
    of crashing the pre-write recheck's empty-source statement builder and
    leaving the reconciliation in a non-converging retry loop.
    """

    from personal_os.exclusion_policy.reconciliation import ReconciliationProgress
    from postgresql_source_store.policy_drafts import PolicyDatabaseRetryPolicy
    from postgresql_source_store.policy_reconciliation import (
        PostgresqlPolicyReconciliationStore,
    )

    connection = _FakeConnection(_eventless_page_script(POLICY_REVISION_ID))
    store = PostgresqlPolicyReconciliationStore(
        _FakeEngine(connection),  # type: ignore[arg-type]
        retry=PolicyDatabaseRetryPolicy(maximum_attempts=1),
    )
    heartbeats: list[ReconciliationProgress] = []

    async def heartbeat(progress: ReconciliationProgress) -> None:
        heartbeats.append(progress)

    outcome = await store.run_reconciliation_batch(
        WORKSPACE_ID, POLICY_REVISION_ID, 0, None, heartbeat
    )

    assert outcome.superseded is False
    assert outcome.has_more is False
    assert outcome.last_source_id == SOURCE_ID
    assert outcome.evaluated_sources == 0
    assert outcome.to_excluded_sources == 0
    assert outcome.to_allowed_sources == 0
    assert outcome.unchanged_sources == 0
    # The one committed batch heartbeats its (empty) progress, and no write
    # statement ever executed: neither evaluations nor intents exist.
    assert heartbeats == [ReconciliationProgress(evaluated_sources=0, batch_count=1)]
    assert all("INSERT" not in compiled for compiled in connection.executed)
