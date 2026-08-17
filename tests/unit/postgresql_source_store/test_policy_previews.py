"""Pure-helper contracts of the PostgreSQL policy-preview store (spec 10).

These tests pin the preview persistence helpers without touching a database:
the leased outbox claim over ``FOR UPDATE SKIP LOCKED`` in the pinned order,
the fenced lifecycle statements (running marker, ready write with the 15-minute
expiry, failure, lease reclaim, ready-expiry and the 15-minute execution
deadline sweep), the stable ``(source_id)`` keyset source scan capped at the
pinned 500-row page, the stable ``(impact_class, source_id)`` result page
capped at the 200-row API bound, the result-row values carrying sorted
space-separated rule/field text only, the ``exclusion_policy.preview_requested``
audit values carrying identifiers and the draft digest only, the closed
safe-error-code grammar, and the reuse of the shared policy database failure
mapping.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
import sqlalchemy.exc as sa_exc
from sqlalchemy.dialects import postgresql
from tests.unit.exclusion_policy.fakes import WORKSPACE_ID as FAKES_WORKSPACE_ID
from tests.unit.exclusion_policy.fakes import rule, subject

from personal_os.diagnostics.context import DiagnosticContext, TraceContext

# Imported first: loading the diagnostics package before the error-contracts
# exceptions module keeps their module-level re-export cycle resolvable.
from personal_os.diagnostics.events import SafeToken
from personal_os.diagnostics.trace_context import SpanId, TraceId
from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import InternalApplicationError
from personal_os.exclusion_policy.contracts import (
    RuleKind,
)
from personal_os.exclusion_policy.ports import PolicyActor, PolicyActorKind
from personal_os.exclusion_policy.previews import (
    PREVIEW_RESULT_PAGE_MAXIMUM,
    PREVIEW_SCAN_PAGE_SIZE,
    PolicyPreviewBinding,
    PolicyPreviewRecord,
    PreviewImpactClass,
    PreviewResultCursor,
    PreviewStatus,
    evaluate_preview_subject,
)
from postgresql_source_store.policy_previews import (
    _SAFE_ERROR_CODE_COLUMN_GRAMMAR,
    POLICY_PREVIEW_AUDIT_TARGET_KIND,
    PREVIEW_DRAFT_STALE_ERROR_CODE,
    PREVIEW_EXECUTION_DEADLINE_ERROR_CODE,
    PREVIEW_EXECUTION_FAILED_ERROR_CODE,
    PREVIEW_LEASE_EXPIRED_ERROR_CODE,
    PREVIEW_REQUESTED_AUDIT_ACTION,
    build_preview_requested_audit_values,
    build_preview_result_row_values,
    claim_pending_previews_select_statement,
    expire_overdue_previews_statements,
    fail_preview_update_statement,
    hydrate_policy_preview_record,
    lease_preview_update_statement,
    map_preview_database_failure,
    mark_preview_ready_update_statement,
    mark_preview_running_update_statement,
    preview_result_page_select_statement,
    reclaim_lease_update_statement,
    source_checkpoint_select_statement,
    source_page_select_statement,
)

WORKSPACE_ID = UUID("018f47a0-7b00-7000-8000-0000000000d1")
DRAFT_ID = UUID("018f47a0-7b00-7000-8000-0000000000d2")
USER_ID = UUID("018f47a0-7b00-7000-8000-0000000000d3")
PREVIEW_ID = UUID("018f47a0-7b00-7000-8000-0000000000d4")
ACTIVE_REVISION_ID = UUID("018f47a0-7b00-7000-8000-0000000000d5")
SOURCE_ID = UUID("018f47a0-7b00-7000-8000-0000000000d6")
AFTER_SOURCE_ID = UUID("018f47a0-7b00-7000-8000-0000000000d7")
REQUEST_ID = uuid4()
NOW = datetime(2026, 8, 17, 10, 0, 0, tzinfo=UTC)

_SENTINEL_STATEMENT = "SELECT do-not-emit-sql FROM knowledge.policy_previews"
_SENTINEL_DRIVER_TEXT = "do-not-emit-driver-text"

_LEAKAGE_SENTINELS: tuple[str, ...] = (
    "sentinel-title",
    "private/notes/sentinel-locator.md",
    "sentinel operand",
)


class _DriverFailure(Exception):
    """Fake driver exception carrying a SQLSTATE and sentinel driver text."""

    def __init__(self, sqlstate: str | None) -> None:
        super().__init__(_SENTINEL_DRIVER_TEXT)
        self.sqlstate = sqlstate


_TRACE = TraceContext(
    trace_id=TraceId("0123456789abcdef0123456789abcdef"),
    remote_parent_span_id=None,
    local_span_id=SpanId("0123456789abcdef"),
    trace_flags=0,
)


def _context() -> DiagnosticContext:
    return DiagnosticContext(request_id=REQUEST_ID, client_request_id=None, trace=_TRACE)


def _actor() -> PolicyActor:
    return PolicyActor(actor_kind=PolicyActorKind.USER, user_id=USER_ID)


def _draft_revision_with(*rules: Any) -> Any:
    from personal_os.exclusion_policy.contracts import ExclusionPolicyRevision

    return ExclusionPolicyRevision(
        policy_revision_id=DRAFT_ID,
        workspace_id=FAKES_WORKSPACE_ID,
        revision_number=2,
        rules=rules,
    )


def _compile(statement: sa.Executable) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))

def _params(statement: sa.Executable) -> dict[str, Any]:
    return dict(statement.compile(dialect=postgresql.dialect()).params)



def _preview_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "policy_preview_id": PREVIEW_ID,
        "workspace_id": WORKSPACE_ID,
        "policy_draft_id": DRAFT_ID,
        "draft_version": 2,
        "draft_sha256": "a" * 64,
        "base_policy_revision_id": ACTIVE_REVISION_ID,
        "source_checkpoint_event_sequence": 9,
        "state": "pending",
        "newly_excluded_count": 1,
        "still_excluded_count": 2,
        "newly_allowed_count": 3,
        "still_allowed_count": 4,
        "indeterminate_count": 5,
        "impact_digest": None,
        "attempt_count": 0,
        "safe_error_code": None,
        "created_by_user_id": USER_ID,
        "created_at": NOW,
        "ready_at": None,
        "expires_at": None,
        "consumed_at": None,
    }
    row.update(overrides)
    return row


# --- leased outbox claim ---------------------------------------------------------


def test_claim_select_is_fenced_ordered_and_limited() -> None:
    compiled = _compile(claim_pending_previews_select_statement(NOW, 10))
    assert "FOR UPDATE SKIP LOCKED" in compiled
    assert ".state" in compiled
    assert "ORDER BY" in compiled
    # The pinned dispatch order keeps concurrent claimers deterministic.
    assert "available_at" in compiled
    assert str(WORKSPACE_ID) not in compiled


def test_claim_select_rejects_out_of_bound_limits_and_naive_clocks() -> None:
    with pytest.raises(ValueError):
        claim_pending_previews_select_statement(datetime(2026, 8, 17, 10, 0, 0), 10)
    with pytest.raises(ValueError):
        claim_pending_previews_select_statement(NOW, 0)
    with pytest.raises(ValueError):
        claim_pending_previews_select_statement(NOW, 10_000)


def test_lease_update_writes_lease_columns_and_attempts() -> None:
    statement = lease_preview_update_statement(PREVIEW_ID)
    compiled = _compile(statement)
    assert "lease_token" in compiled
    assert "leased_until" in compiled
    assert "attempt_count" in compiled
    assert _params(statement)["state"] == "leased"
    assert str(PREVIEW_ID) not in compiled


# --- fenced lifecycle statements -------------------------------------------------


def test_running_marker_is_fenced_over_dispatchable_states() -> None:
    statement = mark_preview_running_update_statement(PREVIEW_ID)
    compiled = _compile(statement)
    assert "state IN" in compiled
    assert _params(statement)["state"] == "running"
    assert "lease_token=NULL" in compiled


def test_ready_update_writes_digest_counters_and_expiry() -> None:
    statement = mark_preview_ready_update_statement(
        PREVIEW_ID,
        newly_excluded_count=1,
        still_excluded_count=2,
        newly_allowed_count=3,
        still_allowed_count=4,
        indeterminate_count=5,
        impact_digest="b" * 64,
    )
    compiled = _compile(statement)
    assert "ready_at" in compiled
    assert "expires_at" in compiled
    assert "make_interval" in compiled
    assert "impact_digest" in compiled
    assert _params(statement)["state"] == "ready"
    # The fence clears lease columns and accepts only dispatchable states.
    assert "lease_token=NULL" in compiled


def test_fail_update_requires_safe_error_code_and_dispatchable_states() -> None:
    statement = fail_preview_update_statement(PREVIEW_ID, PREVIEW_EXECUTION_FAILED_ERROR_CODE)
    compiled = _compile(statement)
    assert "safe_error_code" in compiled
    assert _params(statement)["state"] == "failed"
    assert _params(statement)["safe_error_code"] == "preview_execution_failed"
    with pytest.raises(ValueError):
        fail_preview_update_statement(PREVIEW_ID, SafeToken.parse("Not-Safe"))


def test_reclaim_returns_expired_leases_to_pending_with_backoff() -> None:
    statement = reclaim_lease_update_statement(now=NOW)
    compiled = _compile(statement)
    assert "make_interval" in compiled
    assert _params(statement)["state"] == "pending"
    assert _params(statement)["safe_error_code"] == PREVIEW_LEASE_EXPIRED_ERROR_CODE.value


def test_expire_overdue_statements_cover_deadline_and_ready_expiry() -> None:
    deadline_statement, ready_statement = expire_overdue_previews_statements(NOW)
    assert _params(deadline_statement)["state"] == "failed"
    assert (
        _params(deadline_statement)["safe_error_code"]
        == PREVIEW_EXECUTION_DEADLINE_ERROR_CODE.value
    )
    assert _params(ready_statement)["state"] == "expired"
    assert "expires_at" in _compile(ready_statement)


# --- snapshot source scan --------------------------------------------------------


def test_source_page_uses_stable_keyset_order_and_pinned_limit() -> None:
    compiled = _compile(
        source_page_select_statement(
            WORKSPACE_ID, after_source_id=AFTER_SOURCE_ID, limit=PREVIEW_SCAN_PAGE_SIZE
        )
    )
    assert "ORDER BY knowledge.sources.source_id" in compiled
    assert "knowledge.sources.source_id >" in compiled
    assert "deleted_at IS NULL" in compiled
    with pytest.raises(ValueError):
        source_page_select_statement(WORKSPACE_ID, after_source_id=None, limit=0)
    with pytest.raises(ValueError):
        source_page_select_statement(WORKSPACE_ID, after_source_id=None, limit=501)


def test_checkpoint_select_reads_the_workspace_maximum_sequence() -> None:
    compiled = _compile(source_checkpoint_select_statement(WORKSPACE_ID))
    assert "max" in compiled
    assert "sync_events" in compiled
    assert "event_sequence" in compiled


# --- result paging ---------------------------------------------------------------


def test_result_page_uses_stable_cursor_order_and_api_bound() -> None:
    cursor = PreviewResultCursor(
        impact_class=PreviewImpactClass.NEWLY_ALLOWED, source_id=SOURCE_ID
    )
    compiled = _compile(
        preview_result_page_select_statement(
            PREVIEW_ID, cursor=cursor, limit=PREVIEW_RESULT_PAGE_MAXIMUM
        )
    )
    assert "ORDER BY knowledge.policy_preview_results.impact_class" in compiled
    assert "knowledge.policy_preview_results.source_id" in compiled
    fresh = _compile(preview_result_page_select_statement(PREVIEW_ID, cursor=None, limit=200))
    assert "impact_class >" not in fresh
    with pytest.raises(ValueError):
        preview_result_page_select_statement(PREVIEW_ID, cursor=None, limit=202)
    with pytest.raises(ValueError):
        preview_result_page_select_statement(PREVIEW_ID, cursor=None, limit=0)


# --- result row values -----------------------------------------------------------


def test_result_row_values_render_sorted_space_separated_text() -> None:
    first_rule_id = uuid4()
    second_rule_id = uuid4()
    ordered = sorted([first_rule_id, second_rule_id])
    outcome = evaluate_preview_subject(
        previous_revision=None,
        proposed_revision=_draft_revision_with(
            rule(RuleKind.EXTENSION, text_operand=".tmp"),
        ),
        subject=subject(source_id=SOURCE_ID, normalized_locator=None),
    )
    object.__setattr__(
        outcome,
        "matched_rule_ids",
        tuple(ordered),
    )
    values = build_preview_result_row_values(PREVIEW_ID, outcome)
    assert values["policy_preview_id"] == PREVIEW_ID
    assert values["source_id"] == SOURCE_ID
    assert values["matched_rule_ids"] == f"{ordered[0]} {ordered[1]}"
    assert values["previous_raw_decision"] == "indeterminate"
    assert values["previous_enforced_decision"] == "excluded"
    assert values["proposed_match_state"] in {"matched", "not_matched", "indeterminate"}
    assert values["impact_class"] in {
        "newly_excluded",
        "still_excluded",
        "newly_allowed",
        "still_allowed",
        "indeterminate",
    }
    for sentinel in _LEAKAGE_SENTINELS:
        assert sentinel not in str(values)


# --- audit values ----------------------------------------------------------------


def test_preview_requested_audit_values_carry_identifiers_and_digest_only() -> None:
    values = build_preview_requested_audit_values(
        policy_preview_id=PREVIEW_ID,
        workspace_id=WORKSPACE_ID,
        actor=_actor(),
        draft_sha256="a" * 64,
        occurred_at=NOW,
        request_id=REQUEST_ID,
        client_request_id=None,
        trace_id="0123456789abcdef0123456789abcdef",
    )
    assert values["action"] == PREVIEW_REQUESTED_AUDIT_ACTION
    assert values["action"] == "exclusion_policy.preview_requested"
    assert values["target_kind"] == POLICY_PREVIEW_AUDIT_TARGET_KIND
    assert values["target_id"] == PREVIEW_ID
    assert values["result"] == "succeeded"
    assert values["safe_diff_hash"] == "a" * 64
    assert values["workspace_id"] == WORKSPACE_ID
    assert values["actor_id"] == USER_ID
    rendered = str(values)
    for sentinel in _LEAKAGE_SENTINELS:
        assert sentinel not in rendered


# --- hydration -------------------------------------------------------------------


def test_hydrate_policy_preview_record_maps_every_column() -> None:
    record = hydrate_policy_preview_record(_preview_row())
    assert isinstance(record, PolicyPreviewRecord)
    assert record.policy_preview_id == PREVIEW_ID
    assert record.status is PreviewStatus.PENDING
    assert record.binding == PolicyPreviewBinding(
        preview_id=PREVIEW_ID,
        draft_id=DRAFT_ID,
        draft_version=2,
        active_policy_revision_id=ACTIVE_REVISION_ID,
        source_event_checkpoint=9,
    )
    assert record.draft_sha256 == "a" * 64
    assert record.newly_excluded_count == 1
    assert record.indeterminate_count == 5


def test_hydrate_rejects_rows_outside_the_closed_state_set() -> None:
    with pytest.raises(Exception):  # noqa: B017 - closed mapping failure
        hydrate_policy_preview_record(_preview_row(state="awaiting"))


def test_hydrate_rejects_failed_rows_without_error_code() -> None:
    with pytest.raises(InternalApplicationError):
        hydrate_policy_preview_record(_preview_row(state="failed", safe_error_code=None))


# --- error codes and failure mapping ----------------------------------------------


def test_safe_error_codes_satisfy_the_column_grammar() -> None:
    for code in (
        PREVIEW_DRAFT_STALE_ERROR_CODE,
        PREVIEW_EXECUTION_DEADLINE_ERROR_CODE,
        PREVIEW_EXECUTION_FAILED_ERROR_CODE,
        PREVIEW_LEASE_EXPIRED_ERROR_CODE,
    ):
        assert isinstance(code, SafeToken)
        assert _SAFE_ERROR_CODE_COLUMN_GRAMMAR.fullmatch(code.value)


def test_database_failure_mapping_reuses_the_policy_registry() -> None:
    contention = sa_exc.DBAPIError(_SENTINEL_STATEMENT, {}, _DriverFailure("40P01"))
    mapped = map_preview_database_failure(contention)
    assert mapped.error_code is ErrorCode.EXCLUSION_POLICY_COMMIT_OUTCOME_UNKNOWN
    non_database = map_preview_database_failure(RuntimeError(_SENTINEL_DRIVER_TEXT))
    assert non_database.error_code is ErrorCode.INTERNAL_ERROR
    rendered = f"{mapped!r} {non_database!r}"
    assert _SENTINEL_STATEMENT not in rendered
    assert _SENTINEL_DRIVER_TEXT not in rendered
