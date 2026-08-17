"""Exclusion-policy draft store pure-helper contracts of the PostgreSQL adapter.

These tests pin the draft persistence helpers without touching a database:
the ``FOR UPDATE`` draft lock statement, the typed operand column mapping for
every closed rule kind in both directions (domain rule to row values, mapped
rows back to the immutable domain value through the sanctioned normalizer),
the corruption mapping for stored rows outside the closed kind/operand
grammar, the closed draft-conflict error with only ``current_draft_version``,
the ready-preview expiry statement bound to the prior draft version, the
``exclusion_policy.draft_replaced`` audit values containing identifiers and
the semantic digest only, the policy database failure mapping, the bounded
contention retry with its evidence-based recovery predicate, and the replay
recovery requiring the exact incremented version and rule set.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
import sqlalchemy.exc as sa_exc
from sqlalchemy.dialects import postgresql
from tests.unit.exclusion_policy.fakes import extension_rule, rule

# Imported first: loading the diagnostics package before the error-contracts
# exceptions module keeps their module-level re-export cycle resolvable.
from personal_os.diagnostics.events import SafeToken  # noqa: F401
from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import InternalApplicationError
from personal_os.exclusion_policy.contracts import (
    ExclusionRule,
    RuleKind,
)
from personal_os.exclusion_policy.drafts import compute_draft_semantic_sha256
from personal_os.exclusion_policy.errors import ExclusionPolicyError
from personal_os.exclusion_policy.ports import (
    PolicyActor,
    PolicyActorKind,
    PolicyDraft,
)
from postgresql_source_store.error_mapping import (
    RETRY_JITTER_MAXIMUM_SECONDS,
    RETRY_JITTER_MINIMUM_SECONDS,
)
from postgresql_source_store.policy_drafts import (
    AUDIT_RESULT_SUCCEEDED,
    DRAFT_REPLACED_AUDIT_ACTION,
    POLICY_DRAFT_AUDIT_TARGET_KIND,
    PolicyDatabaseRetryPolicy,
    build_draft_replaced_audit_values,
    build_draft_rule_values,
    draft_conflict_error,
    draft_lock_statement,
    expire_ready_previews_statement,
    hydrate_policy_draft,
    map_policy_database_failure,
    matches_recovered_replacement,
)
from postgresql_source_store.tables import policy_drafts, policy_previews

WORKSPACE_ID = UUID("018f47a0-7b00-7000-8000-0000000000d1")
DRAFT_ID = UUID("018f47a0-7b00-7000-8000-0000000000d2")
USER_ID = UUID("018f47a0-7b00-7000-8000-0000000000d3")
REQUEST_ID = uuid4()
OCCURRED_AT = datetime(2026, 8, 17, 9, 0, 0, tzinfo=UTC)
SOURCE_ID_OPERAND = UUID("018f47a0-7b00-7000-8000-0000000000e1")

_SENTINEL_STATEMENT = "SELECT do-not-emit-sql FROM knowledge.policy_drafts"
_SENTINEL_DRIVER_TEXT = "do-not-emit-driver-text"


class _DriverFailure(Exception):
    """Fake driver exception carrying a SQLSTATE and sentinel driver text."""

    def __init__(self, sqlstate: str | None) -> None:
        super().__init__(_SENTINEL_DRIVER_TEXT)
        self.sqlstate = sqlstate


def _contention_failure() -> sa_exc.DBAPIError:
    return sa_exc.DBAPIError(_SENTINEL_STATEMENT, {}, _DriverFailure("40P01"))


def _ambiguous_failure() -> sa_exc.DBAPIError:
    return sa_exc.DBAPIError(_SENTINEL_STATEMENT, {}, _DriverFailure("08006"))


def _actor() -> PolicyActor:
    return PolicyActor(actor_kind=PolicyActorKind.USER, user_id=USER_ID)


def _all_kinds_rules() -> tuple[ExclusionRule, ...]:
    return (
        rule(RuleKind.EXACT_SOURCE_ID, source_id_operand=SOURCE_ID_OPERAND),
        rule(RuleKind.FOLDER_PREFIX, text_operand="private/notes"),
        rule(RuleKind.PATH_GLOB, text_operand="attachments/**/*.tmp"),
        rule(RuleKind.EXTENSION, text_operand=".tmp"),
        rule(RuleKind.MEDIA_TYPE, text_operand="text/markdown"),
        rule(RuleKind.MAXIMUM_SIZE, size_bytes_operand=104857600),
        rule(RuleKind.SOURCE_TYPE, text_operand="pdf"),
    )


def _draft_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "policy_draft_id": DRAFT_ID,
        "workspace_id": WORKSPACE_ID,
        "draft_version": 4,
        "base_policy_revision_id": None,
    }
    row.update(overrides)
    return row


def _rule_row(rule: ExclusionRule, **overrides: Any) -> dict[str, Any]:
    values = build_draft_rule_values(DRAFT_ID, rule)
    row: dict[str, Any] = {
        "policy_draft_id": values["policy_draft_id"],
        "rule_id": values["rule_id"],
        "rule_kind": values["rule_kind"],
        "source_id_operand": values["source_id_operand"],
        "text_operand": values["text_operand"],
        "size_bytes_operand": values["size_bytes_operand"],
        "semantic_fingerprint": values["semantic_fingerprint"],
    }
    row.update(overrides)
    return row


def _compile(statement: sa.Executable) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


# --- draft row lock statement ----------------------------------------------------


def test_draft_lock_statement_locks_the_single_draft_row_for_update() -> None:
    compiled = _compile(draft_lock_statement(DRAFT_ID))
    assert "FOR UPDATE" in compiled
    assert ".policy_draft_id = " in compiled
    assert str(DRAFT_ID) not in compiled


# --- typed operand column mapping ------------------------------------------------


def test_build_draft_rule_values_stores_exactly_one_typed_operand_column() -> None:
    rules = _all_kinds_rules()
    rows = [build_draft_rule_values(DRAFT_ID, rule_value) for rule_value in rules]
    by_kind = {row["rule_kind"]: row for row in rows}
    assert set(by_kind) == {kind.value for kind in RuleKind}

    exact = by_kind[RuleKind.EXACT_SOURCE_ID.value]
    assert exact["source_id_operand"] == SOURCE_ID_OPERAND
    assert exact["text_operand"] is None
    assert exact["size_bytes_operand"] is None

    for text_kind in ("folder_prefix", "path_glob", "extension", "media_type", "source_type"):
        text_row = by_kind[text_kind]
        assert text_row["source_id_operand"] is None
        assert text_row["text_operand"] is not None
        assert text_row["size_bytes_operand"] is None

    size_row = by_kind[RuleKind.MAXIMUM_SIZE.value]
    assert size_row["source_id_operand"] is None
    assert size_row["text_operand"] is None
    assert size_row["size_bytes_operand"] == 104857600

    for row in rows:
        assert row["policy_draft_id"] == DRAFT_ID
        assert len(row["semantic_fingerprint"]) == 64


# --- immutable row-to-domain hydration -------------------------------------------


def test_hydrate_policy_draft_round_trips_every_rule_kind() -> None:
    rules = _all_kinds_rules()
    draft = hydrate_policy_draft(_draft_row(), [_rule_row(rule_value) for rule_value in rules])
    assert draft.draft_id == DRAFT_ID
    assert draft.workspace_id == WORKSPACE_ID
    assert draft.draft_version == 4
    assert draft.base_policy_revision_id is None
    assert draft.rules == rules


def test_hydrate_policy_draft_rejects_unknown_rule_kind_as_corruption() -> None:
    rows = [_rule_row(extension_rule(".tmp"), rule_kind="unsupported_kind")]
    with pytest.raises(InternalApplicationError) as raised:
        hydrate_policy_draft(_draft_row(), rows)
    assert raised.value.error_code is ErrorCode.INTERNAL_ERROR


def test_hydrate_policy_draft_rejects_invalid_operand_shape_as_corruption() -> None:
    rows = [_rule_row(extension_rule(".tmp"), size_bytes_operand=1024)]
    with pytest.raises(InternalApplicationError) as raised:
        hydrate_policy_draft(_draft_row(), rows)
    assert raised.value.error_code is ErrorCode.INTERNAL_ERROR


def test_hydrate_policy_draft_rejects_duplicate_rule_ids_as_corruption() -> None:
    first = extension_rule(".tmp")
    duplicated = rule(RuleKind.EXTENSION, rule_id=first.rule_id, text_operand=".bak")
    with pytest.raises(InternalApplicationError) as raised:
        hydrate_policy_draft(_draft_row(), [_rule_row(first), _rule_row(duplicated)])
    assert raised.value.error_code is ErrorCode.INTERNAL_ERROR


# --- draft conflict error and preview expiry -------------------------------------


def test_draft_conflict_error_carries_only_current_draft_version() -> None:
    error = draft_conflict_error(7)
    assert isinstance(error, ExclusionPolicyError)
    assert error.error_code is ErrorCode.EXCLUSION_POLICY_DRAFT_CONFLICT
    assert error.safe_details == {"current_draft_version": 7}


def test_expire_ready_previews_statement_targets_prior_version_ready_rows_only() -> None:
    statement = expire_ready_previews_statement(DRAFT_ID, 3)
    assert statement.table.name == policy_previews.name
    compiled = _compile(statement)
    assert compiled.lstrip().startswith("UPDATE")
    assert str(DRAFT_ID) not in compiled
    literal = str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "'expired'" in literal
    assert "'ready'" in literal
    assert str(DRAFT_ID) in literal


# --- audit values -----------------------------------------------------------------


def test_build_draft_replaced_audit_values_contains_ids_and_digest_only() -> None:
    rules = _all_kinds_rules()
    values = build_draft_replaced_audit_values(
        policy_draft_id=DRAFT_ID,
        workspace_id=WORKSPACE_ID,
        actor=_actor(),
        safe_diff_hash=compute_draft_semantic_sha256(rules),
        occurred_at=OCCURRED_AT,
        request_id=REQUEST_ID,
        client_request_id=None,
        trace_id="0123456789abcdef0123456789abcdef",
    )
    assert values["action"] == DRAFT_REPLACED_AUDIT_ACTION
    assert values["target_kind"] == POLICY_DRAFT_AUDIT_TARGET_KIND
    assert values["target_id"] == DRAFT_ID
    assert values["workspace_id"] == WORKSPACE_ID
    assert values["actor_kind"] == "user"
    assert values["actor_id"] == USER_ID
    assert values["result"] == AUDIT_RESULT_SUCCEEDED
    assert values["reason_code"] is None
    assert values["safe_diff_hash"] == compute_draft_semantic_sha256(rules)
    assert values["occurred_at"] == OCCURRED_AT
    assert set(values) == {
        "audit_event_id",
        "workspace_id",
        "actor_kind",
        "actor_id",
        "actor_reference",
        "action",
        "target_kind",
        "target_id",
        "request_id",
        "client_request_id",
        "trace_id",
        "result",
        "reason_code",
        "safe_diff_hash",
        "occurred_at",
    }
    rendered = repr(values)
    for forbidden in ("private/notes", ".tmp", "text/markdown", "104857600", "attachments"):
        assert forbidden not in rendered


def test_draft_lock_statement_targets_policy_drafts_table_only() -> None:
    statement = draft_lock_statement(DRAFT_ID)
    compiled_tables = {table.name for table in statement.get_final_froms()}
    assert compiled_tables == {policy_drafts.name}


# --- policy database failure mapping and retry -----------------------------------


def test_map_policy_database_failure_uses_closed_policy_codes_only() -> None:
    contention = map_policy_database_failure(_contention_failure())
    assert isinstance(contention, ExclusionPolicyError)
    assert contention.error_code is ErrorCode.EXCLUSION_POLICY_COMMIT_OUTCOME_UNKNOWN
    assert contention.safe_details == {}
    unavailable = map_policy_database_failure(_ambiguous_failure())
    assert isinstance(unavailable, ExclusionPolicyError)
    assert unavailable.error_code is ErrorCode.EXCLUSION_POLICY_COMMIT_OUTCOME_UNKNOWN
    not_database = map_policy_database_failure(RuntimeError(_SENTINEL_DRIVER_TEXT))
    assert isinstance(not_database, InternalApplicationError)
    assert not_database.error_code is ErrorCode.INTERNAL_ERROR


@pytest.mark.asyncio
async def test_retry_policy_passes_application_errors_through_untouched() -> None:
    attempts: list[int] = []

    async def operation(attempt: int) -> int:
        attempts.append(attempt)
        raise draft_conflict_error(7)

    with pytest.raises(ExclusionPolicyError) as raised:
        await PolicyDatabaseRetryPolicy().run(operation)
    assert raised.value.error_code is ErrorCode.EXCLUSION_POLICY_DRAFT_CONFLICT
    assert attempts == [1]


@pytest.mark.asyncio
async def test_retry_policy_retries_contention_with_bounded_jitter_then_maps() -> None:
    attempts: list[int] = []
    delays: list[float] = []
    bounds: list[tuple[float, float]] = []

    async def operation(attempt: int) -> int:
        attempts.append(attempt)
        raise _contention_failure()

    async def sleep(delay: float) -> None:
        delays.append(delay)

    def jitter(minimum: float, maximum: float) -> float:
        bounds.append((minimum, maximum))
        return minimum

    with pytest.raises(ExclusionPolicyError) as raised:
        await PolicyDatabaseRetryPolicy().run(operation, sleep=sleep, jitter=jitter)
    assert raised.value.error_code is ErrorCode.EXCLUSION_POLICY_COMMIT_OUTCOME_UNKNOWN
    assert attempts == [1, 2, 3]
    assert delays == [
        RETRY_JITTER_MINIMUM_SECONDS,
        RETRY_JITTER_MINIMUM_SECONDS,
    ]
    assert bounds == [
        (RETRY_JITTER_MINIMUM_SECONDS, RETRY_JITTER_MAXIMUM_SECONDS),
        (RETRY_JITTER_MINIMUM_SECONDS, RETRY_JITTER_MAXIMUM_SECONDS),
    ]


@pytest.mark.asyncio
async def test_retry_policy_succeeds_after_transient_contention() -> None:
    async def operation(attempt: int) -> str:
        if attempt == 1:
            raise _contention_failure()
        return "committed"

    result = await PolicyDatabaseRetryPolicy().run(
        operation, sleep=lambda _delay: _noop(), jitter=lambda minimum, _maximum: minimum
    )
    assert result == "committed"


@pytest.mark.asyncio
async def test_retry_policy_does_not_retry_unclassified_database_failures() -> None:
    attempts: list[int] = []

    async def operation(attempt: int) -> str:
        attempts.append(attempt)
        raise sa_exc.DBAPIError(_SENTINEL_STATEMENT, {}, _DriverFailure("23505"))

    with pytest.raises(ExclusionPolicyError) as raised:
        await PolicyDatabaseRetryPolicy().run(operation)
    assert raised.value.error_code is ErrorCode.EXCLUSION_POLICY_COMMIT_OUTCOME_UNKNOWN
    assert attempts == [1]


async def _noop() -> None:
    return None


# --- replay recovery predicate ----------------------------------------------------


def _stored_draft(version: int, rules: tuple[ExclusionRule, ...]) -> PolicyDraft:
    return PolicyDraft(
        draft_id=DRAFT_ID,
        workspace_id=WORKSPACE_ID,
        draft_version=version,
        base_policy_revision_id=None,
        rules=rules,
    )


def test_matches_recovered_replacement_requires_incremented_version_and_equal_rules() -> None:
    rules = _all_kinds_rules()
    assert matches_recovered_replacement(_stored_draft(2, rules), 1, rules) is True
    assert matches_recovered_replacement(_stored_draft(3, rules), 1, rules) is False
    assert matches_recovered_replacement(_stored_draft(2, ()), 1, rules) is False
    assert (
        matches_recovered_replacement(_stored_draft(2, (extension_rule(".bak"),)), 1, rules)
        is False
    )
