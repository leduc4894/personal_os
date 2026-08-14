"""Projection-intent claim, reclaim and fenced-transition statement contracts.

These tests compile the schema-qualified Core statements without a database
and pin the design section 11.2 lease mechanics: the claim runs ``FOR UPDATE
SKIP LOCKED`` ordered by ``(available_at, created_at, projection_intent_id)``
over due ``pending`` rows only, the batch limit is bounded by the pinned
constant, expired-lease reclaim selects overdue leases under the same row
skip, and every acknowledgement/retry/terminal transition fences on the exact
intent ID, ``status='leased'`` and lease token while incrementing the attempt
count exactly once and clearing the lease columns.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# Imported first: loading the diagnostics package before the error-contracts
# exceptions module keeps their module-level re-export cycle resolvable.
from personal_os.diagnostics.events import SafeToken
from personal_os.sources.projection_dispatch import (
    LEASE_EXPIRED_ERROR_CODE,
    PROJECTION_CLAIM_BATCH_LIMIT,
    PROJECTION_LEASE_SECONDS,
    projection_retry_backoff_seconds,
    retry_available_at,
)
from postgresql_source_store.projection_intents import (
    acknowledge_dispatched_statement,
    claim_available_select_statement,
    expired_lease_select_statement,
    lease_intent_update_statement,
    mark_terminal_statement,
    reclaim_lease_update_statement,
    release_retry_statement,
)

_NOW: datetime = datetime(2026, 8, 14, 12, 0, 0, tzinfo=UTC)
_LATER: datetime = _NOW + timedelta(seconds=61)


def _sql(statement: sa.ClauseElement) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


def _compact(statement: sa.ClauseElement) -> str:
    return "".join(_sql(statement).split())


def _params(statement: sa.ClauseElement) -> dict[str, Any]:
    return dict(statement.compile(dialect=postgresql.dialect()).params)


def test_claim_select_uses_for_update_skip_locked() -> None:
    assert "FORUPDATESKIPLOCKED" in _compact(claim_available_select_statement(_NOW, 10))


def test_claim_select_orders_by_availability_then_creation_then_identity() -> None:
    sql = _sql(claim_available_select_statement(_NOW, 10))
    ordering = sql.index("ORDER BY")
    assert (
        sql.index("projection_intents.available_at", ordering)
        < sql.index("projection_intents.created_at", ordering)
        < sql.index("projection_intents.projection_intent_id", ordering)
    )


def test_claim_select_matches_only_due_pending_rows() -> None:
    statement = claim_available_select_statement(_NOW, 10)
    params = _params(statement)
    compact = _compact(statement)
    assert "pending" in params.values()
    assert params.get("now") == _NOW
    assert "projection_intents.status=%(" in compact
    assert "projection_intents.available_at<=%(" in compact


def test_claim_select_bounds_the_batch_by_the_pinned_limit() -> None:
    statement = claim_available_select_statement(_NOW, 3)
    assert "LIMIT" in _sql(statement)
    assert 3 in _params(statement).values()
    assert claim_available_select_statement(_NOW, PROJECTION_CLAIM_BATCH_LIMIT) is not None


@pytest.mark.parametrize("limit", [0, -1, PROJECTION_CLAIM_BATCH_LIMIT + 1, 10_000])
def test_claim_select_rejects_limits_outside_the_pinned_batch_bound(limit: int) -> None:
    with pytest.raises(ValueError, match="limit"):
        claim_available_select_statement(_NOW, limit)


def test_claim_select_rejects_naive_clock_reading() -> None:
    with pytest.raises(ValueError, match="now"):
        claim_available_select_statement(datetime(2026, 8, 14, 12, 0, 0), 10)


def test_lease_update_sets_leased_state_with_database_time_expiry() -> None:
    statement = lease_intent_update_statement(uuid4())
    compact = _compact(statement)
    params = _params(statement)
    assert "CURRENT_TIMESTAMP" in compact
    assert "make_interval(" in compact
    assert PROJECTION_LEASE_SECONDS in params.values()
    assert "leased" in params.values()


def test_lease_update_fences_on_the_pending_unleased_row() -> None:
    intent_id = uuid4()
    statement = lease_intent_update_statement(intent_id)
    params = _params(statement)
    compact = _compact(statement)
    assert intent_id in params.values()
    assert "pending" in params.values()
    assert "lease_tokenISNULL" in compact


def test_expired_lease_select_uses_for_update_skip_locked() -> None:
    statement = expired_lease_select_statement(_LATER)
    assert "FORUPDATESKIPLOCKED" in _compact(statement)
    params = _params(statement)
    assert "leased" in params.values()
    assert params.get("now") == _LATER
    assert "projection_intents.leased_until<=%(" in _compact(statement)


def test_reclaim_update_returns_to_pending_with_attempt_increment_and_backoff() -> None:
    intent_id = uuid4()
    lease_token = uuid4()
    prior_attempt_count = 3
    statement = reclaim_lease_update_statement(
        intent_id, lease_token, projection_retry_backoff_seconds(prior_attempt_count)
    )
    compact = _compact(statement)
    params = _params(statement)
    assert "attempt_count+" in compact
    assert projection_retry_backoff_seconds(prior_attempt_count) in params.values()
    assert lease_token in params.values()
    assert "leased" in params.values()
    assert intent_id in params.values()
    assert "CURRENT_TIMESTAMP" in compact
    assert "lease_token=NULL" in compact
    assert "leased_until=NULL" in compact
    assert "last_error_code=%(" in compact


def test_reclaim_update_records_the_lease_expired_error_code() -> None:
    statement = reclaim_lease_update_statement(uuid4(), uuid4(), 4)
    assert LEASE_EXPIRED_ERROR_CODE.value in _params(statement).values()


def test_retry_availability_helper_mirrors_the_bounded_backoff() -> None:
    assert retry_available_at(_NOW, 0) == _NOW + timedelta(seconds=1)
    assert retry_available_at(_NOW, 9) == _NOW + timedelta(seconds=300)


@pytest.mark.parametrize(
    "statement_builder",
    [
        lambda intent_id, token: acknowledge_dispatched_statement(intent_id, token),
        lambda intent_id, token: release_retry_statement(
            intent_id,
            token,
            SafeToken.parse("projection_dispatch_unavailable"),
            _NOW + timedelta(seconds=2),
            _NOW,
        ),
        lambda intent_id, token: mark_terminal_statement(
            intent_id, token, SafeToken.parse("projection_intent_contract_invalid"), _NOW
        ),
    ],
    ids=["acknowledge", "release-retry", "mark-terminal"],
)
def test_fenced_transitions_match_intent_status_and_exact_lease_token(
    statement_builder: Any,
) -> None:
    intent_id = uuid4()
    lease_token = uuid4()
    statement = statement_builder(intent_id, lease_token)
    compact = _compact(statement)
    params = _params(statement)
    assert intent_id in params.values(), "transition must fence on the exact intent ID"
    assert "leased" in params.values(), "transition must require status='leased'"
    assert lease_token in params.values(), "transition must fence on the exact lease token"
    assert "attempt_count+" in compact, "transition must increment the attempt count once"
    assert "lease_token=NULL" in compact, "transition must clear the lease token"
    assert "leased_until=NULL" in compact, "transition must clear the lease expiry"
    assert "CURRENT_TIMESTAMP" in compact


def test_acknowledge_sets_dispatched_state_and_clears_the_error() -> None:
    statement = acknowledge_dispatched_statement(uuid4(), uuid4())
    compact = _compact(statement)
    set_clause = compact.index("SET")
    assert compact.index("dispatched_at", set_clause) > -1
    assert "last_error_code=NULL" in compact
    assert "dispatched" in _params(statement).values()


def test_release_retry_keeps_pending_state_and_bounded_availability() -> None:
    available_at = _NOW + timedelta(seconds=16)
    statement = release_retry_statement(
        uuid4(),
        uuid4(),
        SafeToken.parse("projection_dispatch_unavailable"),
        available_at,
        _NOW,
    )
    compact = _compact(statement)
    params = _params(statement)
    assert "pending" in params.values()
    assert available_at in params.values()
    assert "dispatched_at" not in compact
    assert "projection_dispatch_unavailable" in params.values()


def test_mark_terminal_keeps_the_terminal_error_code() -> None:
    statement = mark_terminal_statement(
        uuid4(), uuid4(), SafeToken.parse("projection_intent_contract_invalid"), _NOW
    )
    params = _params(statement)
    assert "terminal" in params.values()
    assert "projection_intent_contract_invalid" in params.values()


def test_release_retry_rejects_availability_before_now() -> None:
    with pytest.raises(ValueError, match="available_at"):
        release_retry_statement(
            uuid4(),
            uuid4(),
            SafeToken.parse("projection_dispatch_unavailable"),
            _NOW - timedelta(seconds=1),
            _NOW,
        )


def test_release_retry_rejects_naive_datetimes() -> None:
    naive_now = datetime(2026, 8, 14, 12, 0, 0)
    with pytest.raises(ValueError, match="now"):
        release_retry_statement(
            uuid4(),
            uuid4(),
            SafeToken.parse("projection_dispatch_unavailable"),
            naive_now + timedelta(seconds=2),
            naive_now,
        )


def test_transitions_reject_error_codes_the_column_constraint_forbids() -> None:
    with pytest.raises(ValueError, match="error_code"):
        mark_terminal_statement(uuid4(), uuid4(), SafeToken.parse("not.a-column-code"), _NOW)
