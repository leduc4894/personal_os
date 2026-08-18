"""Exact-snapshot policy-preview persistence and execution over PostgreSQL.

:class:`PostgresqlPolicyPreviewStore` implements the durable
:class:`~personal_os.exclusion_policy.previews.PolicyPreviewStore` port over
the migrated preview schema. ``request_preview`` runs one ``READ COMMITTED``
transaction that captures the binding — draft identity and version, the
draft's semantic digest, its base revision, the workspace's last assigned
source-event sequence and the requesting actor — and commits the durable
pending row plus the ``exclusion_policy.preview_requested`` audit row.

``run_preview_activity`` is the single-activity execution of spec 10: one
``REPEATABLE READ`` transaction re-verifies the binding (the draft still has
the exact bound version, the active pointer still equals the base revision
and the source checkpoint still equals the captured sequence), then streams
the workspace's current valid sources in stable ``source_id`` keyset pages of
500, evaluates the previous and proposed policy over each subject through the
pure domain comparison, writes the result rows page by page, heartbeats
between pages through the injected progress callback and marks the preview
ready with its counters, impact digest and the 15-minute expiry in that same
transaction — so cancellation, the injected failure seam or any database
failure rolls back every result and the durable row. Batches from different
snapshots are never merged: the snapshot is fixed before the first page and
the complete evidence commits once. Retry is idempotent: a replay of a ready
preview returns it, and an uncertain commit resolves only through the
fresh-connection recovery lookup proving the ready row exists.

The leased-outbox half mirrors the projection dispatcher conventions: claim
due pending rows behind ``FOR UPDATE SKIP LOCKED`` with a caller-injected
UUIDv7 fence token and a database-time expiry, reclaim expired leases to
pending with bounded backoff, sweep the 15-minute execution deadline and the
ready expiry, and fail previews through the fenced transition that requires a
closed safe error code. Result reads re-verify the workspace source checkpoint
before serving a page and refuse stale display state with the typed stale
error. SQLSTATE values, SQL, parameters and driver text never leave the
adapter.
"""

from __future__ import annotations

import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Protocol, runtime_checkable
from uuid import UUID, uuid7

import sqlalchemy as sa
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from personal_os.diagnostics.context import DiagnosticContext
from personal_os.diagnostics.events import SafeToken
from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import ApplicationError, InternalApplicationError
from personal_os.exclusion_policy.contracts import (
    EnforcedPolicyDecision,
    ExclusionPolicyRevision,
    PolicySubject,
    PolicySubjectField,
    PreviewMatchState,
    RawPolicyDecision,
    RuleKind,
)
from personal_os.exclusion_policy.drafts import compute_draft_semantic_sha256
from personal_os.exclusion_policy.errors import ExclusionPolicyError
from personal_os.exclusion_policy.metrics import PreviewMetricOutcome
from personal_os.exclusion_policy.normalization import normalize_rule
from personal_os.exclusion_policy.ports import PolicyActor
from personal_os.exclusion_policy.previews import (
    PREVIEW_EXECUTION_DEADLINE_SECONDS,
    PREVIEW_READY_EXPIRY_SECONDS,
    PREVIEW_RESULT_PAGE_MAXIMUM,
    PREVIEW_SCAN_PAGE_SIZE,
    PolicyPreviewBinding,
    PolicyPreviewRecord,
    PolicyPreviewResultPage,
    PolicyPreviewResultRow,
    PreviewImpactClass,
    PreviewProgress,
    PreviewResultCursor,
    PreviewStatus,
    PreviewSubjectOutcome,
    compute_impact_digest,
    evaluate_preview_subject,
)
from personal_os.object_storage import CanonicalMediaType
from personal_os.sources.actors import reject_nil_uuid
from personal_os.sources.commands import SourceType
from postgresql_source_store.engine import (
    TRANSACTION_BOUND_STATEMENTS,
    apply_transaction_bounds,
)
from postgresql_source_store.error_mapping import DatabaseFailureKind, classify_database_failure
from postgresql_source_store.policy_drafts import (
    PolicyDatabaseRetryPolicy,
    hydrate_policy_draft,
)
from postgresql_source_store.tables import (
    audit_events,
    content_objects,
    policy_draft_rules,
    policy_drafts,
    policy_preview_results,
    policy_previews,
    policy_rules,
    source_policies,
    source_versions,
    sources,
    sync_events,
    workspace_policy_state,
)

#: Audit-row literals for the in-transaction preview request audit.
PREVIEW_REQUESTED_AUDIT_ACTION: Final[str] = "exclusion_policy.preview_requested"
POLICY_PREVIEW_AUDIT_TARGET_KIND: Final[str] = "policy_preview"
AUDIT_RESULT_SUCCEEDED: Final[str] = "succeeded"

#: Closed safe error codes recorded on failed preview rows. Every token
#: satisfies the ``safe_error_code`` column grammar of the migration.
PREVIEW_DRAFT_STALE_ERROR_CODE: Final[SafeToken] = SafeToken.parse("preview_draft_stale")
PREVIEW_BASE_REVISION_STALE_ERROR_CODE: Final[SafeToken] = SafeToken.parse(
    "preview_base_revision_stale"
)
PREVIEW_SOURCE_CHECKPOINT_STALE_ERROR_CODE: Final[SafeToken] = SafeToken.parse(
    "preview_source_checkpoint_stale"
)
PREVIEW_EXECUTION_FAILED_ERROR_CODE: Final[SafeToken] = SafeToken.parse("preview_execution_failed")
PREVIEW_EXECUTION_DEADLINE_ERROR_CODE: Final[SafeToken] = SafeToken.parse(
    "preview_execution_deadline"
)
PREVIEW_LEASE_EXPIRED_ERROR_CODE: Final[SafeToken] = SafeToken.parse("preview_lease_expired")
PREVIEW_DISPATCH_TERMINAL_ERROR_CODE: Final[SafeToken] = SafeToken.parse(
    "preview_dispatch_terminal"
)
PREVIEW_MISSING_ERROR_CODE: Final[SafeToken] = SafeToken.parse("preview_missing")

#: The states one dispatch claim may lease.
_DISPATCHABLE_STATES: Final[tuple[str, ...]] = (
    PreviewStatus.PENDING.value,
    PreviewStatus.LEASED.value,
)

#: The states the atomic ready/failure transitions accept.
_EXECUTABLE_STATES: Final[tuple[str, ...]] = (
    PreviewStatus.PENDING.value,
    PreviewStatus.LEASED.value,
    PreviewStatus.RUNNING.value,
)

#: Preview leases cover only the workflow start call (the activity runs inside
#: Temporal's own retry), so the pinned duration mirrors the dispatch lease.
POLICY_PREVIEW_LEASE_SECONDS: Final[int] = 60

#: The pinned maximum previews one dispatch cycle claims.
POLICY_PREVIEW_CLAIM_BATCH_LIMIT: Final[int] = 20

#: The bounded exponential backoff cap for lease reclaim and retryable
#: dispatch releases (a one-second initial value doubling from the row's own
#: attempt count, capped at five minutes).
POLICY_PREVIEW_BACKOFF_CAP_SECONDS: Final[int] = 300

#: The ``safe_error_code`` CHECK constraint accepts ``^[a-z][a-z0-9_]{0,99}$``;
#: ``SafeToken`` is wider, so the stricter column grammar is enforced here.
_SAFE_ERROR_CODE_COLUMN_GRAMMAR: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9_]{0,99}$")

#: One row of a preview read: a SQLAlchemy row mapping from the adapter's
#: ``.mappings()`` results or an equivalent mapping in tests.
type _MappedRow = RowMapping | Mapping[str, Any]

#: Injected heartbeat over the closed progress value; the activity layer
#: supplies the Temporal heartbeat, unit tests a recorder.
type PreviewHeartbeat = Callable[[PreviewProgress], Awaitable[None]]

#: Closed subject-field lookup for the stored missing-field text.
_SUBJECT_FIELD_BY_VALUE: Final[dict[str, PolicySubjectField]] = {
    field.value: field for field in PolicySubjectField
}


class InjectedPreviewFailure(InternalApplicationError):
    """Test-seam failure raised after N evaluated subjects to prove rollback.

    Subclassing the typed internal error lets the failure pass untouched
    through the shared retry runner's ``except ApplicationError`` arm — the
    still-open repeatable-read transaction rolls back every written result
    and lifecycle transition, exactly like a crash between pages, and the
    test observes the exact seam type.
    """

    def __init__(self) -> None:
        super().__init__(ErrorCode.INTERNAL_ERROR)


@dataclass(frozen=True, slots=True)
class LeasedPolicyPreview:
    """One leased preview row handed to the deterministic workflow starter."""

    policy_preview_id: UUID
    workspace_id: UUID
    source_event_checkpoint: int
    attempt_count: int
    lease_token: UUID
    leased_until: datetime


@dataclass(frozen=True, slots=True)
class PolicyPreviewSweepResult:
    """Counts of one overdue sweep: execution-deadline failures and expiries."""

    execution_deadline_failed: int
    ready_expired: int


@runtime_checkable
class PreviewMetricsSink(Protocol):
    """Structural metrics sink the composition root may inject."""

    def record_preview(self, *, outcome: PreviewMetricOutcome, duration_seconds: float) -> None: ...


def map_preview_database_failure(cause: BaseException) -> ApplicationError:
    """Map a database or driver failure onto the closed policy registry.

    Reuses the shared policy mapping: contention exhausted after the bounded
    retries and any unclassified or unavailable failure map to the retryable
    commit-outcome-unknown error; a non-database exception is an internal bug
    and crosses the boundary as ``internal_error``. The cause remains chained
    only; its text never enters the mapped error.
    """

    failure_kind = classify_database_failure(cause)
    if failure_kind is DatabaseFailureKind.NOT_DATABASE:
        return InternalApplicationError(ErrorCode.INTERNAL_ERROR)
    return ExclusionPolicyError(ErrorCode.EXCLUSION_POLICY_COMMIT_OUTCOME_UNKNOWN)


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None:
        raise ValueError(f"{field_name} must be timezone-aware")


def _require_column_safe_error_code(error_code: SafeToken) -> None:
    if _SAFE_ERROR_CODE_COLUMN_GRAMMAR.fullmatch(error_code.value) is None:
        raise ValueError("error_code does not satisfy the safe_error_code column contract")


def _stale_error(reason: SafeToken) -> ExclusionPolicyError:
    """Build the typed stale-binding error carrying only the closed reason."""

    return ExclusionPolicyError(
        ErrorCode.EXCLUSION_POLICY_PREVIEW_STALE, safe_details={"reason": reason}
    )


def preview_missing_error() -> ExclusionPolicyError:
    """Build the typed missing-preview error carrying no tenant details."""

    return ExclusionPolicyError(
        ErrorCode.EXCLUSION_POLICY_PREVIEW_FAILED,
        safe_details={"reason": PREVIEW_MISSING_ERROR_CODE},
    )


def _failed_error(safe_error_code: str) -> ExclusionPolicyError:
    """Build the typed failed-preview error from a stored safe error code."""

    return ExclusionPolicyError(
        ErrorCode.EXCLUSION_POLICY_PREVIEW_FAILED,
        safe_details={"reason": SafeToken.parse(safe_error_code)},
    )


async def apply_preview_snapshot_bounds(connection: AsyncConnection) -> None:
    """Open one repeatable-read snapshot with the pinned per-statement bounds.

    ``SET TRANSACTION ISOLATION LEVEL`` runs as the transaction's first
    statement, fixing the snapshot before any read; the shared ``SET LOCAL``
    bounds then cap each statement while the long-running scan stays inside
    them. The result set, the ready transition and the expiry share the one
    transaction timestamp.
    """

    await connection.execute(sa.text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
    for statement in TRANSACTION_BOUND_STATEMENTS:
        await connection.execute(sa.text(statement))


# --- request capture ---------------------------------------------------------------


def build_preview_requested_audit_values(
    *,
    policy_preview_id: UUID,
    workspace_id: UUID,
    actor: PolicyActor,
    draft_sha256: str,
    occurred_at: datetime,
    request_id: UUID,
    client_request_id: UUID | None,
    trace_id: str | None,
) -> dict[str, Any]:
    """Build the ``exclusion_policy.preview_requested`` audit-row values.

    The row carries identifiers, the closed actor/action/result literals and
    the draft semantic digest only: rule operands, locators and source display
    values never enter the audit table (spec 21).
    """

    return {
        "audit_event_id": uuid7(),
        "workspace_id": workspace_id,
        "actor_kind": actor.actor_kind.value,
        "actor_id": actor.user_id,
        "actor_reference": None,
        "action": PREVIEW_REQUESTED_AUDIT_ACTION,
        "target_kind": POLICY_PREVIEW_AUDIT_TARGET_KIND,
        "target_id": policy_preview_id,
        "request_id": request_id,
        "client_request_id": client_request_id,
        "trace_id": trace_id,
        "result": AUDIT_RESULT_SUCCEEDED,
        "reason_code": None,
        "safe_diff_hash": draft_sha256,
        "occurred_at": occurred_at,
    }


# --- leased outbox -----------------------------------------------------------------


def claim_pending_previews_select_statement(
    now: datetime, limit: int
) -> sa.Select[tuple[Any, ...]]:
    """Build the due pending-preview claim select with the pinned row skip.

    Only ``pending`` rows whose availability has passed the injected ``now``
    reading match; the pinned ``(available_at, created_at, policy_preview_id)``
    order and ``FOR UPDATE SKIP LOCKED`` keep concurrent claimers disjoint.
    """

    _require_aware(now, "now")
    if limit < 1 or limit > POLICY_PREVIEW_CLAIM_BATCH_LIMIT:
        raise ValueError("limit must be between 1 and the pinned preview claim batch limit")
    return (
        sa.select(
            policy_previews.c.policy_preview_id,
            policy_previews.c.workspace_id,
            policy_previews.c.source_checkpoint_event_sequence,
            policy_previews.c.attempt_count,
        )
        .where(
            policy_previews.c.state == PreviewStatus.PENDING.value,
            policy_previews.c.available_at
            <= sa.bindparam("now", now, type_=sa.DateTime(timezone=True)),
        )
        .order_by(
            policy_previews.c.available_at,
            policy_previews.c.created_at,
            policy_previews.c.policy_preview_id,
        )
        .limit(limit)
        .with_for_update(skip_locked=True)
    )


def lease_preview_update_statement(preview_id: UUID) -> sa.Update:
    """Build the guarded lease write with database-time expiry.

    The fence matches only the still-pending row; the expiry is
    ``CURRENT_TIMESTAMP`` plus the pinned lease duration, so the lease CHECK
    constraint always holds with one clock, and the attempt count increments
    exactly once per claim.
    """

    return (
        sa.update(policy_previews)
        .values(
            state=PreviewStatus.LEASED.value,
            lease_token=sa.bindparam("lease_token", type_=sa.Uuid()),
            leased_until=sa.func.current_timestamp()
            + sa.func.make_interval(0, 0, 0, 0, 0, 0, POLICY_PREVIEW_LEASE_SECONDS),
            attempt_count=policy_previews.c.attempt_count + 1,
        )
        .where(
            policy_previews.c.policy_preview_id == preview_id,
            policy_previews.c.state == PreviewStatus.PENDING.value,
            policy_previews.c.lease_token.is_(None),
        )
        .returning(policy_previews.c.leased_until)
    )


def _bounded_backoff_interval() -> sa.Function[Any]:
    """The doubled, capped availability delay over the row's attempt count."""

    return sa.func.make_interval(
        0,
        0,
        0,
        0,
        0,
        0,
        sa.func.least(
            POLICY_PREVIEW_BACKOFF_CAP_SECONDS,
            sa.cast(sa.func.power(2, policy_previews.c.attempt_count), sa.Integer),
        ),
    )


def reclaim_lease_update_statement(*, now: datetime) -> sa.Update:
    """Build the expired-lease return to ``pending`` with bounded backoff.

    Every overdue leased row returns to pending in one guarded statement: the
    attempt count increments exactly once (the expiry is a known outcome), the
    lease columns clear, the closed lease-expired error code is recorded and
    the availability delay doubles from the row's own attempt count.
    """

    _require_aware(now, "now")
    return (
        sa.update(policy_previews)
        .values(
            state=PreviewStatus.PENDING.value,
            lease_token=sa.null(),
            leased_until=sa.null(),
            attempt_count=policy_previews.c.attempt_count + 1,
            safe_error_code=PREVIEW_LEASE_EXPIRED_ERROR_CODE.value,
            available_at=sa.func.current_timestamp() + _bounded_backoff_interval(),
        )
        .where(
            policy_previews.c.state == PreviewStatus.LEASED.value,
            policy_previews.c.leased_until
            <= sa.bindparam("now", now, type_=sa.DateTime(timezone=True)),
        )
    )


def release_retry_update_statement(
    preview_id: UUID, lease_token: UUID, error_code: SafeToken
) -> sa.Update:
    """Build the fenced retryable release back to ``pending`` with backoff.

    The fence matches the exact leased row and its lease token; the bounded
    availability delay doubles from the row's own attempt count, so no caller
    clock or count is trusted.
    """

    _require_column_safe_error_code(error_code)
    return (
        sa.update(policy_previews)
        .values(
            state=PreviewStatus.PENDING.value,
            lease_token=sa.null(),
            leased_until=sa.null(),
            attempt_count=policy_previews.c.attempt_count + 1,
            safe_error_code=error_code.value,
            available_at=sa.func.current_timestamp() + _bounded_backoff_interval(),
        )
        .where(
            policy_previews.c.policy_preview_id == preview_id,
            policy_previews.c.state == PreviewStatus.LEASED.value,
            policy_previews.c.lease_token == lease_token,
        )
    )


def expire_overdue_previews_statements(
    now: datetime,
) -> tuple[sa.Update, sa.Update]:
    """Build the two overdue sweeps: the execution deadline and ready expiry.

    The deadline statement fails every still-executable preview whose creation
    precedes ``now`` minus the pinned fifteen-minute deadline; the ready
    statement expires every ready preview whose expiry has passed. Both are
    guarded by their exact source state, so neither can touch a terminal row.
    """

    _require_aware(now, "now")
    execution_deadline = (
        sa.update(policy_previews)
        .values(
            state=PreviewStatus.FAILED.value,
            lease_token=sa.null(),
            leased_until=sa.null(),
            safe_error_code=PREVIEW_EXECUTION_DEADLINE_ERROR_CODE.value,
        )
        .where(
            policy_previews.c.state.in_(_EXECUTABLE_STATES),
            policy_previews.c.created_at
            <= sa.bindparam("now", type_=sa.DateTime(timezone=True))
            - sa.func.make_interval(0, 0, 0, 0, 0, 0, PREVIEW_EXECUTION_DEADLINE_SECONDS),
        )
    )
    ready_expiry = (
        sa.update(policy_previews)
        .values(state=PreviewStatus.EXPIRED.value)
        .where(
            policy_previews.c.state == PreviewStatus.READY.value,
            policy_previews.c.expires_at <= sa.bindparam("now", type_=sa.DateTime(timezone=True)),
        )
    )
    return execution_deadline, ready_expiry


def fail_preview_update_statement(preview_id: UUID, error_code: SafeToken) -> sa.Update:
    """Build the fenced failure transition requiring a closed error code."""

    _require_column_safe_error_code(error_code)
    return (
        sa.update(policy_previews)
        .values(
            state=PreviewStatus.FAILED.value,
            lease_token=sa.null(),
            leased_until=sa.null(),
            safe_error_code=error_code.value,
        )
        .where(
            policy_previews.c.policy_preview_id == preview_id,
            policy_previews.c.state.in_(_EXECUTABLE_STATES),
        )
    )


def expire_ready_preview_update_statement(preview_id: UUID) -> sa.Update:
    """Build the lazy ready-to-expired transition for one overdue row."""

    return (
        sa.update(policy_previews)
        .values(state=PreviewStatus.EXPIRED.value)
        .where(
            policy_previews.c.policy_preview_id == preview_id,
            policy_previews.c.state == PreviewStatus.READY.value,
        )
    )


# --- execution statements -----------------------------------------------------------


def mark_preview_running_update_statement(preview_id: UUID) -> sa.Update:
    """Build the in-transaction running marker over dispatchable states.

    The write lives inside the repeatable-read execution transaction, so it
    commits together with the complete result set or rolls back with it — an
    external observer never sees ``running`` without the ready outcome.
    """

    return (
        sa.update(policy_previews)
        .values(
            state=PreviewStatus.RUNNING.value,
            lease_token=sa.null(),
            leased_until=sa.null(),
        )
        .where(
            policy_previews.c.policy_preview_id == preview_id,
            policy_previews.c.state.in_(_DISPATCHABLE_STATES),
        )
    )


def mark_preview_ready_update_statement(
    preview_id: UUID,
    *,
    newly_excluded_count: int,
    still_excluded_count: int,
    newly_allowed_count: int,
    still_allowed_count: int,
    indeterminate_count: int,
    impact_digest: str,
) -> sa.Update:
    """Build the atomic ready write with the fifteen-minute expiry.

    The transaction timestamp becomes both ``ready_at`` and the base of
    ``expires_at`` (``CURRENT_TIMESTAMP`` plus the pinned expiry), the lease
    columns clear, and the fence accepts only the three executable states —
    so the complete evidence, counters, digest and lifecycle land in one
    commit or not at all.
    """

    for name, value in (
        ("newly_excluded_count", newly_excluded_count),
        ("still_excluded_count", still_excluded_count),
        ("newly_allowed_count", newly_allowed_count),
        ("still_allowed_count", still_allowed_count),
        ("indeterminate_count", indeterminate_count),
    ):
        if value < 0:
            raise ValueError(f"{name} must not be negative")
    return (
        sa.update(policy_previews)
        .values(
            state=PreviewStatus.READY.value,
            lease_token=sa.null(),
            leased_until=sa.null(),
            safe_error_code=sa.null(),
            newly_excluded_count=newly_excluded_count,
            still_excluded_count=still_excluded_count,
            newly_allowed_count=newly_allowed_count,
            still_allowed_count=still_allowed_count,
            indeterminate_count=indeterminate_count,
            impact_digest=impact_digest,
            ready_at=sa.func.current_timestamp(),
            expires_at=sa.func.current_timestamp()
            + sa.func.make_interval(0, 0, 0, 0, 0, 0, PREVIEW_READY_EXPIRY_SECONDS),
        )
        .where(
            policy_previews.c.policy_preview_id == preview_id,
            policy_previews.c.state.in_(_EXECUTABLE_STATES),
        )
        .returning(
            policy_previews.c.ready_at,
            policy_previews.c.expires_at,
        )
    )


def source_checkpoint_select_statement(workspace_id: UUID) -> sa.Select[tuple[Any, ...]]:
    """Build the workspace's last assigned canonical source-event sequence.

    The checkpoint is ``COALESCE(MAX(event_sequence), 0)`` over the
    workspace's ``sync_events`` rows — zero before the first source event.
    """

    return sa.select(sa.func.coalesce(sa.func.max(sync_events.c.event_sequence), 0)).where(
        sync_events.c.workspace_id == workspace_id
    )


def source_page_select_statement(
    workspace_id: UUID, *, after_source_id: UUID | None, limit: int
) -> sa.Select[tuple[Any, ...]]:
    """Build one stable keyset page of the workspace's current valid sources.

    Rows stream in ascending ``source_id`` order — a server-side cursor over
    the primary key — with the workspace bound, soft-deleted sources excluded
    and each page capped at the pinned 500 rows. Current content evidence
    (media type, byte size) joins through the current version's content
    object and stays absent when no current version exists.
    """

    if limit < 1 or limit > PREVIEW_SCAN_PAGE_SIZE:
        raise ValueError("limit must be between 1 and the pinned preview scan page size")
    statement = (
        sa.select(
            sources.c.source_id,
            sources.c.source_type,
            content_objects.c.media_type,
            content_objects.c.byte_size,
        )
        .select_from(sources)
        .join(
            source_versions,
            source_versions.c.source_version_id == sources.c.current_version_id,
            isouter=True,
        )
        .join(
            content_objects,
            content_objects.c.content_object_id == source_versions.c.content_object_id,
            isouter=True,
        )
        .where(
            sources.c.workspace_id == workspace_id,
            sources.c.deleted_at.is_(None),
        )
        .order_by(sources.c.source_id)
        .limit(sa.bindparam("page_limit", limit, type_=sa.Integer()))
    )
    if after_source_id is not None:
        statement = statement.where(sources.c.source_id > after_source_id)
    return statement


def build_preview_result_row_values(
    preview_id: UUID, outcome: PreviewSubjectOutcome
) -> dict[str, Any]:
    """Map one comparison outcome onto its result-row values.

    Rule IDs and missing field names render as sorted space-separated text —
    exactly the column CHECK grammar — and no operand, locator or display
    value has a field to occupy.
    """

    return {
        "policy_preview_id": preview_id,
        "source_id": outcome.source_id,
        "previous_raw_decision": outcome.previous_raw.value,
        "previous_enforced_decision": outcome.previous_enforced.value,
        "proposed_raw_decision": outcome.proposed_raw.value,
        "proposed_enforced_decision": outcome.proposed_enforced.value,
        "proposed_match_state": outcome.proposed_match_state.value,
        "impact_class": outcome.impact_class.value,
        "matched_rule_ids": " ".join(str(rule_id) for rule_id in outcome.matched_rule_ids),
        "missing_fields": " ".join(field.value for field in outcome.missing_fields),
        "subject_fingerprint": outcome.subject_fingerprint,
    }


def preview_result_page_select_statement(
    preview_id: UUID,
    *,
    cursor: PreviewResultCursor | None,
    limit: int,
) -> sa.Select[tuple[Any, ...]]:
    """Build one bounded result page in the stable cursor order.

    Pages stream in ascending ``(impact_class, source_id)`` order with the
    keyset predicate excluding the cursor row and every earlier row; the
    caller caps the page at the pinned 200-row API bound.
    """

    if limit < 1 or limit > PREVIEW_RESULT_PAGE_MAXIMUM + 1:
        # The read path fetches ``limit + 1`` rows to detect continuation, so
        # one row of headroom beyond the API bound is the ceiling here.
        raise ValueError("limit must be positive within the pinned preview result page bound")
    statement = sa.select(
        policy_preview_results.c.source_id,
        policy_preview_results.c.previous_raw_decision,
        policy_preview_results.c.previous_enforced_decision,
        policy_preview_results.c.proposed_raw_decision,
        policy_preview_results.c.proposed_enforced_decision,
        policy_preview_results.c.proposed_match_state,
        policy_preview_results.c.impact_class,
        policy_preview_results.c.matched_rule_ids,
        policy_preview_results.c.missing_fields,
        policy_preview_results.c.subject_fingerprint,
    ).where(policy_preview_results.c.policy_preview_id == preview_id)
    if cursor is not None:
        statement = statement.where(
            sa.or_(
                policy_preview_results.c.impact_class > cursor.impact_class.value,
                sa.and_(
                    policy_preview_results.c.impact_class == cursor.impact_class.value,
                    policy_preview_results.c.source_id > cursor.source_id,
                ),
            )
        )
    return statement.order_by(
        policy_preview_results.c.impact_class,
        policy_preview_results.c.source_id,
    ).limit(sa.bindparam("page_limit", limit, type_=sa.Integer()))


# --- hydration ---------------------------------------------------------------------


#: Columns every preview-row read selects; keep the hydration keys in sync.
_PREVIEW_ROW_COLUMNS: Final[tuple[Any, ...]] = (
    policy_previews.c.policy_preview_id,
    policy_previews.c.workspace_id,
    policy_previews.c.policy_draft_id,
    policy_previews.c.draft_version,
    policy_previews.c.draft_sha256,
    policy_previews.c.base_policy_revision_id,
    policy_previews.c.source_checkpoint_event_sequence,
    policy_previews.c.state,
    policy_previews.c.newly_excluded_count,
    policy_previews.c.still_excluded_count,
    policy_previews.c.newly_allowed_count,
    policy_previews.c.still_allowed_count,
    policy_previews.c.indeterminate_count,
    policy_previews.c.impact_digest,
    policy_previews.c.safe_error_code,
    policy_previews.c.created_by_user_id,
    policy_previews.c.created_at,
    policy_previews.c.ready_at,
    policy_previews.c.expires_at,
    policy_previews.c.consumed_at,
)


def hydrate_policy_preview_record(row: _MappedRow) -> PolicyPreviewRecord:
    """Build the immutable preview record from one mapped row.

    A stored state outside the closed lifecycle vocabulary or an inconsistent
    failed/consumed shape fails closed as the public ``internal_error`` with
    the cause chained only.
    """

    try:
        return PolicyPreviewRecord(
            policy_preview_id=row["policy_preview_id"],
            workspace_id=row["workspace_id"],
            policy_draft_id=row["policy_draft_id"],
            draft_version=int(row["draft_version"]),
            draft_sha256=row["draft_sha256"],
            base_policy_revision_id=row["base_policy_revision_id"],
            source_checkpoint_event_sequence=int(row["source_checkpoint_event_sequence"]),
            status=PreviewStatus(row["state"]),
            impact_digest=row["impact_digest"],
            safe_error_code=row["safe_error_code"],
            created_by_user_id=row["created_by_user_id"],
            created_at=row["created_at"],
            ready_at=row["ready_at"],
            expires_at=row["expires_at"],
            consumed_at=row["consumed_at"],
            newly_excluded_count=int(row["newly_excluded_count"]),
            still_excluded_count=int(row["still_excluded_count"]),
            newly_allowed_count=int(row["newly_allowed_count"]),
            still_allowed_count=int(row["still_allowed_count"]),
            indeterminate_count=int(row["indeterminate_count"]),
        )
    except InternalApplicationError:
        raise
    except (ExclusionPolicyError, ValueError, TypeError, KeyError) as cause:
        raise InternalApplicationError(ErrorCode.INTERNAL_ERROR) from cause


def hydrate_preview_result_row(row: _MappedRow) -> PolicyPreviewResultRow:
    """Build one result row from a mapped page row through the closed types."""

    try:
        return PolicyPreviewResultRow(
            source_id=row["source_id"],
            previous_raw_decision=RawPolicyDecision(row["previous_raw_decision"]),
            previous_enforced_decision=EnforcedPolicyDecision(row["previous_enforced_decision"]),
            proposed_raw_decision=RawPolicyDecision(row["proposed_raw_decision"]),
            proposed_enforced_decision=EnforcedPolicyDecision(row["proposed_enforced_decision"]),
            proposed_match_state=PreviewMatchState(row["proposed_match_state"]),
            impact_class=PreviewImpactClass(row["impact_class"]),
            matched_rule_ids=tuple(
                UUID(value) for value in str(row["matched_rule_ids"]).split() if value
            ),
            missing_fields=tuple(
                _SUBJECT_FIELD_BY_VALUE[value]
                for value in str(row["missing_fields"]).split()
                if value
            ),
            subject_fingerprint=row["subject_fingerprint"],
        )
    except (ValueError, KeyError, TypeError) as cause:
        raise InternalApplicationError(ErrorCode.INTERNAL_ERROR) from cause


def preview_subject_for_row(workspace_id: UUID, row: _MappedRow) -> PolicySubject:
    """Build the canonical subject for one scanned source row.

    Only evidence the current canonical schema owns is populated: the opaque
    ID, the source type and — through the current version's content object —
    the media type and byte size. A genuinely absent field (no current
    version, no locator state in the current phase) stays absent and yields
    missing evidence, never an invented value. A stored source type outside
    the closed vocabulary is corruption and fails closed; an unparsable media
    type degrades to absent evidence.
    """

    try:
        source_type = SourceType(row["source_type"])
    except ValueError as cause:
        raise InternalApplicationError(ErrorCode.INTERNAL_ERROR) from cause
    media_type_text = row["media_type"]
    media_type = None
    if media_type_text is not None:
        try:
            media_type = CanonicalMediaType.parse(media_type_text)
        except ValueError:
            media_type = None
    return PolicySubject(
        workspace_id=workspace_id,
        source_id=row["source_id"],
        source_type=source_type,
        media_type=media_type,
        size_bytes=row["byte_size"],
    )


def hydrate_policy_revision_rules(rule_rows: list[_MappedRow]) -> tuple[Any, ...]:
    """Hydrate immutable published rules through the sanctioned normalizer."""

    try:
        return tuple(
            normalize_rule(
                row["rule_id"],
                RuleKind(row["rule_kind"]),
                source_id_operand=row["source_id_operand"],
                text_operand=row["text_operand"],
                size_bytes_operand=row["size_bytes_operand"],
                rule_index=index,
            )
            for index, row in enumerate(rule_rows)
        )
    except (ExclusionPolicyError, ValueError, TypeError) as cause:
        raise InternalApplicationError(ErrorCode.INTERNAL_ERROR) from cause


class PostgresqlPolicyPreviewStore:
    """Durable preview store and single-activity executor (spec 10).

    The store takes the composition-owned :class:`AsyncEngine`; it opens no
    connection at construction. Reads and the outbox transitions run bounded
    ``READ COMMITTED`` transactions; ``run_preview_activity`` runs the one
    repeatable-read snapshot execution with the evidence-based recovery
    lookup wired into the bounded retry for the uncertain-commit case.
    """

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        retry: PolicyDatabaseRetryPolicy | None = None,
        lease_token_generator: Callable[[], UUID] = uuid7,
        metrics: PreviewMetricsSink | None = None,
    ) -> None:
        self._engine = engine
        self._retry = retry if retry is not None else PolicyDatabaseRetryPolicy()
        self._lease_token_generator = lease_token_generator
        self._metrics = metrics

    # --- request -----------------------------------------------------------------

    async def request_preview(
        self, workspace_id: UUID, actor: PolicyActor, context: DiagnosticContext
    ) -> PolicyPreviewRecord:
        reject_nil_uuid("workspace_id", workspace_id)
        preview_id = uuid7()
        return await self._retry.run(
            lambda _attempt: self._request_preview_once(preview_id, workspace_id, actor, context),
            recover=lambda: self._load_ready_preview_record(preview_id),
        )

    async def _request_preview_once(
        self,
        preview_id: UUID,
        workspace_id: UUID,
        actor: PolicyActor,
        context: DiagnosticContext,
    ) -> PolicyPreviewRecord:
        created_by_user_id = _require_actor_user(actor)
        async with (
            self._engine.connect() as connection,
            connection.begin(),
        ):
            await apply_transaction_bounds(connection)
            draft_row = await self._select_draft_row(connection, workspace_id=workspace_id)
            if draft_row is None:
                raise ExclusionPolicyError(ErrorCode.EXCLUSION_POLICY_NOT_INITIALIZED)
            draft_rule_rows = await self._select_rule_rows(
                connection, policy_draft_rules, "policy_draft_id", draft_row["policy_draft_id"]
            )
            state_row = await self._select_policy_state_row(connection, workspace_id)
            if state_row is None:
                raise ExclusionPolicyError(ErrorCode.EXCLUSION_POLICY_NOT_INITIALIZED)
            checkpoint = await self._select_source_checkpoint(connection, workspace_id)
            draft = hydrate_policy_draft(draft_row, draft_rule_rows)
            draft_sha256 = compute_draft_semantic_sha256(draft.rules)
            occurred_at = await self._select_now(connection)
            await connection.execute(
                sa.insert(policy_previews).values(
                    policy_preview_id=preview_id,
                    workspace_id=workspace_id,
                    policy_draft_id=draft.draft_id,
                    draft_version=draft.draft_version,
                    draft_sha256=draft_sha256,
                    base_policy_revision_id=draft.base_policy_revision_id,
                    source_checkpoint_event_sequence=checkpoint,
                    state=PreviewStatus.PENDING.value,
                    created_by_user_id=created_by_user_id,
                )
            )
            await connection.execute(
                sa.insert(audit_events).values(
                    **build_preview_requested_audit_values(
                        policy_preview_id=preview_id,
                        workspace_id=workspace_id,
                        actor=actor,
                        draft_sha256=draft_sha256,
                        occurred_at=occurred_at,
                        request_id=context.request_id,
                        client_request_id=context.client_request_id,
                        trace_id=context.trace.trace_id.value,
                    )
                )
            )
        return PolicyPreviewRecord(
            policy_preview_id=preview_id,
            workspace_id=workspace_id,
            policy_draft_id=draft.draft_id,
            draft_version=draft.draft_version,
            draft_sha256=draft_sha256,
            base_policy_revision_id=draft.base_policy_revision_id,
            source_checkpoint_event_sequence=checkpoint,
            status=PreviewStatus.PENDING,
            impact_digest=None,
            safe_error_code=None,
            created_by_user_id=created_by_user_id,
            created_at=occurred_at,
            ready_at=None,
            expires_at=None,
            consumed_at=None,
        )

    # --- single-activity snapshot execution ---------------------------------------

    async def run_preview_activity(
        self,
        preview_id: UUID,
        context: DiagnosticContext,
        heartbeat: PreviewHeartbeat | None = None,
        *,
        fail_after_subjects: int | None = None,
    ) -> PolicyPreviewRecord:
        """Execute the one repeatable-read snapshot comparison (spec 10).

        The complete evidence set — every result row, the counters, the
        digest and the ready transition — commits once inside a single
        transaction or not at all; the injected failure seam and any
        cancellation or database failure roll everything back, so a Temporal
        retry restarts from the same captured inputs and can never compose
        results from different snapshots.
        """

        del context  # Correlation flowed through the request audit; no per-page rows.
        reject_nil_uuid("preview_id", preview_id)
        started_monotonic = time.monotonic()
        record = await self._retry.run(
            lambda _attempt: self._run_preview_activity_once(
                preview_id, heartbeat, fail_after_subjects
            ),
            recover=lambda: self._recover_execution(preview_id),
        )
        self._record_preview_metric(PreviewMetricOutcome.READY, started_monotonic)
        return record

    async def _run_preview_activity_once(
        self,
        preview_id: UUID,
        heartbeat: PreviewHeartbeat | None,
        fail_after_subjects: int | None,
    ) -> PolicyPreviewRecord:
        counters = {impact.value: 0 for impact in PreviewImpactClass}
        outcomes: list[PreviewSubjectOutcome] = []
        async with (
            self._engine.connect() as connection,
            connection.begin(),
        ):
            await apply_preview_snapshot_bounds(connection)
            preview_row = await self._select_preview_row(connection, preview_id)
            if preview_row is None:
                raise preview_missing_error()
            current_state = PreviewStatus(preview_row["state"])
            if current_state is PreviewStatus.READY:
                return hydrate_policy_preview_record(preview_row)
            if current_state in (PreviewStatus.CONSUMED, PreviewStatus.EXPIRED):
                raise ExclusionPolicyError(ErrorCode.EXCLUSION_POLICY_PREVIEW_EXPIRED)
            if current_state is PreviewStatus.FAILED:
                raise _failed_error(str(preview_row["safe_error_code"]))
            workspace_id: UUID = preview_row["workspace_id"]
            binding = PolicyPreviewBinding(
                preview_id=preview_id,
                draft_id=preview_row["policy_draft_id"],
                draft_version=int(preview_row["draft_version"]),
                active_policy_revision_id=preview_row["base_policy_revision_id"],
                source_event_checkpoint=int(preview_row["source_checkpoint_event_sequence"]),
            )
            await self._verify_binding(connection, workspace_id, binding)
            draft_row = await self._select_draft_row(connection, draft_id=binding.draft_id)
            if draft_row is None:  # pragma: no cover - the verification selected it
                raise ExclusionPolicyError(ErrorCode.EXCLUSION_POLICY_NOT_INITIALIZED)
            draft_rule_rows = await self._select_rule_rows(
                connection, policy_draft_rules, "policy_draft_id", binding.draft_id
            )
            draft = hydrate_policy_draft(draft_row, draft_rule_rows)
            proposed_revision = ExclusionPolicyRevision(
                policy_revision_id=draft.draft_id,
                workspace_id=workspace_id,
                revision_number=draft.draft_version,
                rules=draft.rules,
            )
            previous_revision = await self._load_previous_revision(
                connection, workspace_id, binding
            )
            await connection.execute(mark_preview_running_update_statement(preview_id))

            after_source_id: UUID | None = None
            evaluated_subjects = 0
            batch_count = 0
            while True:
                page = await self._fetch_source_page(connection, workspace_id, after_source_id)
                if not page:
                    break
                page_outcomes = [
                    evaluate_preview_subject(
                        previous_revision=previous_revision,
                        proposed_revision=proposed_revision,
                        subject=preview_subject_for_row(workspace_id, row),
                    )
                    for row in page
                ]
                await connection.execute(
                    sa.insert(policy_preview_results).values(
                        [
                            build_preview_result_row_values(preview_id, outcome)
                            for outcome in page_outcomes
                        ]
                    )
                )
                outcomes.extend(page_outcomes)
                evaluated_subjects += len(page_outcomes)
                batch_count += 1
                after_source_id = page[-1]["source_id"]
                if heartbeat is not None:
                    await heartbeat(
                        PreviewProgress(
                            evaluated_subjects=evaluated_subjects, batch_count=batch_count
                        )
                    )
                if fail_after_subjects is not None and evaluated_subjects >= fail_after_subjects:
                    raise InjectedPreviewFailure
                if len(page) < PREVIEW_SCAN_PAGE_SIZE:
                    break
            for outcome in outcomes:
                counters[outcome.impact_class.value] += 1
            impact_digest = compute_impact_digest(outcomes)
            ready_result = await connection.execute(
                mark_preview_ready_update_statement(
                    preview_id,
                    newly_excluded_count=counters[PreviewImpactClass.NEWLY_EXCLUDED.value],
                    still_excluded_count=counters[PreviewImpactClass.STILL_EXCLUDED.value],
                    newly_allowed_count=counters[PreviewImpactClass.NEWLY_ALLOWED.value],
                    still_allowed_count=counters[PreviewImpactClass.STILL_ALLOWED.value],
                    indeterminate_count=counters[PreviewImpactClass.INDETERMINATE.value],
                    impact_digest=impact_digest,
                )
            )
            ready_row = ready_result.one()
        return PolicyPreviewRecord(
            policy_preview_id=preview_id,
            workspace_id=workspace_id,
            policy_draft_id=binding.draft_id,
            draft_version=binding.draft_version,
            draft_sha256=preview_row["draft_sha256"],
            base_policy_revision_id=binding.active_policy_revision_id,
            source_checkpoint_event_sequence=binding.source_event_checkpoint,
            status=PreviewStatus.READY,
            impact_digest=impact_digest,
            safe_error_code=None,
            created_by_user_id=preview_row["created_by_user_id"],
            created_at=preview_row["created_at"],
            ready_at=ready_row.ready_at,
            expires_at=ready_row.expires_at,
            consumed_at=None,
            newly_excluded_count=counters[PreviewImpactClass.NEWLY_EXCLUDED.value],
            still_excluded_count=counters[PreviewImpactClass.STILL_EXCLUDED.value],
            newly_allowed_count=counters[PreviewImpactClass.NEWLY_ALLOWED.value],
            still_allowed_count=counters[PreviewImpactClass.STILL_ALLOWED.value],
            indeterminate_count=counters[PreviewImpactClass.INDETERMINATE.value],
        )

    async def _verify_binding(
        self, connection: AsyncConnection, workspace_id: UUID, binding: PolicyPreviewBinding
    ) -> None:
        """Re-verify the binding under the fresh snapshot before scanning.

        The draft must still carry the exact bound version, the active
        pointer must still equal the bound base revision and the workspace's
        last assigned source-event sequence must still equal the captured
        checkpoint; any drift is the typed stale error and never a scan.
        """

        draft_row = await self._select_draft_row(connection, draft_id=binding.draft_id)
        if draft_row is None:
            raise ExclusionPolicyError(ErrorCode.EXCLUSION_POLICY_NOT_INITIALIZED)
        if int(draft_row["draft_version"]) != binding.draft_version:
            raise _stale_error(PREVIEW_DRAFT_STALE_ERROR_CODE)
        if binding.active_policy_revision_id is not None:
            revision_result = await connection.execute(
                sa.select(source_policies.c.policy_revision_id).where(
                    source_policies.c.policy_revision_id == binding.active_policy_revision_id,
                    source_policies.c.workspace_id == workspace_id,
                )
            )
            if revision_result.one_or_none() is None:
                raise _stale_error(PREVIEW_BASE_REVISION_STALE_ERROR_CODE)
        state_row = await self._select_policy_state_row(connection, workspace_id)
        if state_row is None:
            raise ExclusionPolicyError(ErrorCode.EXCLUSION_POLICY_NOT_INITIALIZED)
        if state_row["active_policy_revision_id"] != binding.active_policy_revision_id:
            raise _stale_error(PREVIEW_BASE_REVISION_STALE_ERROR_CODE)
        checkpoint = await self._select_source_checkpoint(connection, workspace_id)
        if checkpoint != binding.source_event_checkpoint:
            raise _stale_error(PREVIEW_SOURCE_CHECKPOINT_STALE_ERROR_CODE)

    async def _load_previous_revision(
        self,
        connection: AsyncConnection,
        workspace_id: UUID,
        binding: PolicyPreviewBinding,
    ) -> ExclusionPolicyRevision | None:
        """Load the bound active revision with its immutable rules.

        A null base means the workspace has never published and every
        subject's previous decision is the closed no-active semantics.
        """

        if binding.active_policy_revision_id is None:
            return None
        revision_row = (
            await connection.execute(
                sa.select(source_policies.c.revision_number).where(
                    source_policies.c.policy_revision_id == binding.active_policy_revision_id,
                    source_policies.c.workspace_id == workspace_id,
                )
            )
        ).one()
        rule_rows = await self._select_rule_rows(
            connection,
            policy_rules,
            "policy_revision_id",
            binding.active_policy_revision_id,
        )
        return ExclusionPolicyRevision(
            policy_revision_id=binding.active_policy_revision_id,
            workspace_id=workspace_id,
            revision_number=int(revision_row.revision_number),
            rules=hydrate_policy_revision_rules(rule_rows),
        )

    async def _recover_execution(self, preview_id: UUID) -> PolicyPreviewRecord | None:
        """Prove or disprove that an uncertain execution commit landed.

        Only a durable ``ready`` row counts as evidence that the complete
        result set committed; anything else is absence and the bounded retry
        decides the outcome.
        """

        record = await self._load_preview_record(preview_id)
        if record is not None and record.status is PreviewStatus.READY:
            return record
        return None

    # --- reads ---------------------------------------------------------------------

    async def get_preview(
        self, preview_id: UUID, context: DiagnosticContext
    ) -> PolicyPreviewRecord:
        del context
        reject_nil_uuid("preview_id", preview_id)
        return await self._retry.run(lambda _attempt: self._get_preview_once(preview_id))

    async def _get_preview_once(self, preview_id: UUID) -> PolicyPreviewRecord:
        async with (
            self._engine.connect() as connection,
            connection.begin(),
        ):
            await apply_transaction_bounds(connection)
            preview_row = await self._select_preview_row(connection, preview_id)
            if preview_row is None:
                raise preview_missing_error()
            if await self._expire_overdue_ready_row(connection, preview_row):
                preview_row = {**preview_row, "state": PreviewStatus.EXPIRED.value}
        return hydrate_policy_preview_record(preview_row)

    async def list_preview_results(
        self,
        preview_id: UUID,
        context: DiagnosticContext,
        cursor: PreviewResultCursor | None = None,
        limit: int = PREVIEW_RESULT_PAGE_MAXIMUM,
    ) -> PolicyPreviewResultPage:
        del context
        reject_nil_uuid("preview_id", preview_id)
        if limit < 1 or limit > PREVIEW_RESULT_PAGE_MAXIMUM:
            raise ExclusionPolicyError(
                ErrorCode.EXCLUSION_POLICY_INPUT_INVALID,
                safe_details={"reason": SafeToken.parse("preview_limit_invalid")},
            )
        return await self._retry.run(
            lambda _attempt: self._list_preview_results_once(preview_id, cursor, limit)
        )

    async def _list_preview_results_once(
        self, preview_id: UUID, cursor: PreviewResultCursor | None, limit: int
    ) -> PolicyPreviewResultPage:
        async with (
            self._engine.connect() as connection,
            connection.begin(),
        ):
            await apply_transaction_bounds(connection)
            preview_row = await self._select_preview_row(connection, preview_id)
            if preview_row is None:
                raise preview_missing_error()
            if await self._expire_overdue_ready_row(connection, preview_row):
                preview_row = {**preview_row, "state": PreviewStatus.EXPIRED.value}
            state = PreviewStatus(preview_row["state"])
            if state is not PreviewStatus.READY:
                if state in (PreviewStatus.EXPIRED, PreviewStatus.CONSUMED):
                    raise ExclusionPolicyError(ErrorCode.EXCLUSION_POLICY_PREVIEW_EXPIRED)
                if state is PreviewStatus.FAILED:
                    raise _failed_error(str(preview_row["safe_error_code"]))
                raise ExclusionPolicyError(ErrorCode.EXCLUSION_POLICY_PREVIEW_PENDING)
            checkpoint = await self._select_source_checkpoint(
                connection, preview_row["workspace_id"]
            )
            if checkpoint != int(preview_row["source_checkpoint_event_sequence"]):
                # A stale checkpoint refuses the page rather than joining
                # display data from a source state that was not evaluated.
                raise _stale_error(PREVIEW_SOURCE_CHECKPOINT_STALE_ERROR_CODE)
            page_result = await connection.execute(
                preview_result_page_select_statement(preview_id, cursor=cursor, limit=limit + 1)
            )
            page_rows = list(page_result.mappings().all())
        has_more = len(page_rows) > limit
        rows = tuple(hydrate_preview_result_row(row) for row in page_rows[:limit])
        next_cursor = None
        if has_more and rows:
            next_cursor = PreviewResultCursor(
                impact_class=rows[-1].impact_class, source_id=rows[-1].source_id
            )
        return PolicyPreviewResultPage(rows=rows, next_cursor=next_cursor)

    async def count_results(self, preview_id: UUID) -> int:
        """Return one preview's persisted result count (test/diagnostic helper)."""

        reject_nil_uuid("preview_id", preview_id)
        async with self._engine.connect() as connection:
            result = await connection.execute(
                sa.select(sa.func.count()).where(
                    policy_preview_results.c.policy_preview_id == preview_id
                )
            )
            return int(result.scalar_one())

    # --- leased outbox --------------------------------------------------------------

    async def claim_pending_previews(self, now: datetime, limit: int) -> list[LeasedPolicyPreview]:
        """Claim due pending previews behind the pinned batch limit."""

        _require_aware(now, "now")
        return await self._retry.run(lambda _attempt: self._claim_pending_previews_once(now, limit))

    async def _claim_pending_previews_once(
        self, now: datetime, limit: int
    ) -> list[LeasedPolicyPreview]:
        async with (
            self._engine.connect() as connection,
            connection.begin(),
        ):
            await apply_transaction_bounds(connection)
            claim_result = await connection.execute(
                claim_pending_previews_select_statement(now, limit)
            )
            claimed: list[LeasedPolicyPreview] = []
            for row in claim_result.mappings():
                lease_token = self._lease_token_generator()
                lease_result = await connection.execute(
                    lease_preview_update_statement(row["policy_preview_id"]),
                    {"lease_token": lease_token},
                )
                leased_until = lease_result.scalar_one_or_none()
                if leased_until is None:
                    # A stale claim between the select and the guarded lease
                    # write: the row stays untouched and is not leased here.
                    continue
                claimed.append(
                    LeasedPolicyPreview(
                        policy_preview_id=row["policy_preview_id"],
                        workspace_id=row["workspace_id"],
                        source_event_checkpoint=int(row["source_checkpoint_event_sequence"]),
                        attempt_count=int(row["attempt_count"]) + 1,
                        lease_token=lease_token,
                        leased_until=leased_until,
                    )
                )
        return claimed

    async def reclaim_expired_leases(self, now: datetime) -> int:
        """Return every overdue lease to pending with bounded backoff."""

        _require_aware(now, "now")
        return await self._retry.run(lambda _attempt: self._reclaim_expired_leases_once(now))

    async def _reclaim_expired_leases_once(self, now: datetime) -> int:
        async with (
            self._engine.connect() as connection,
            connection.begin(),
        ):
            await apply_transaction_bounds(connection)
            result = await connection.execute(reclaim_lease_update_statement(now=now), {"now": now})
            return int(result.rowcount)

    async def expire_overdue_previews(self, now: datetime) -> PolicyPreviewSweepResult:
        """Sweep the execution deadline and the ready expiry (spec 10)."""

        _require_aware(now, "now")
        return await self._retry.run(lambda _attempt: self._expire_overdue_previews_once(now))

    async def _expire_overdue_previews_once(self, now: datetime) -> PolicyPreviewSweepResult:
        deadline_statement, ready_statement = expire_overdue_previews_statements(now)
        async with (
            self._engine.connect() as connection,
            connection.begin(),
        ):
            await apply_transaction_bounds(connection)
            deadline_result = await connection.execute(deadline_statement, {"now": now})
            ready_result = await connection.execute(ready_statement, {"now": now})
            return PolicyPreviewSweepResult(
                execution_deadline_failed=int(deadline_result.rowcount),
                ready_expired=int(ready_result.rowcount),
            )

    async def mark_preview_failed(self, preview_id: UUID, error_code: SafeToken) -> bool:
        """Fail one still-executable preview with a closed safe error code.

        Returns whether the fenced transition affected the row; a terminal
        row is never overwritten. The closed failed metric records only after
        the durable transition.
        """

        reject_nil_uuid("preview_id", preview_id)
        _require_column_safe_error_code(error_code)
        started_monotonic = time.monotonic()
        affected = await self._retry.run(
            lambda _attempt: self._mark_preview_failed_once(preview_id, error_code)
        )
        if affected:
            self._record_preview_metric(PreviewMetricOutcome.FAILED, started_monotonic)
        return affected

    async def _mark_preview_failed_once(self, preview_id: UUID, error_code: SafeToken) -> bool:
        async with (
            self._engine.connect() as connection,
            connection.begin(),
        ):
            await apply_transaction_bounds(connection)
            result = await connection.execute(fail_preview_update_statement(preview_id, error_code))
            return result.rowcount == 1

    async def release_retry(
        self, preview_id: UUID, lease_token: UUID, error_code: SafeToken, now: datetime
    ) -> bool:
        """Release one leased preview back to pending with bounded backoff.

        The availability delay uses database time off the row's own attempt
        count; the injected ``now`` reading is accepted for the port's
        uniform aware-clock contract.
        """

        _require_aware(now, "now")
        reject_nil_uuid("preview_id", preview_id)
        _require_column_safe_error_code(error_code)
        return await self._retry.run(
            lambda _attempt: self._release_retry_once(preview_id, lease_token, error_code)
        )

    async def _release_retry_once(
        self, preview_id: UUID, lease_token: UUID, error_code: SafeToken
    ) -> bool:
        async with (
            self._engine.connect() as connection,
            connection.begin(),
        ):
            await apply_transaction_bounds(connection)
            result = await connection.execute(
                release_retry_update_statement(preview_id, lease_token, error_code)
            )
            return result.rowcount == 1

    # --- shared selects -------------------------------------------------------------

    @staticmethod
    async def _select_preview_row(
        connection: AsyncConnection, preview_id: UUID
    ) -> _MappedRow | None:
        result = await connection.execute(
            sa.select(*_PREVIEW_ROW_COLUMNS).where(
                policy_previews.c.policy_preview_id == preview_id
            )
        )
        return result.mappings().first()

    @staticmethod
    async def _select_draft_row(
        connection: AsyncConnection,
        *,
        draft_id: UUID | None = None,
        workspace_id: UUID | None = None,
    ) -> _MappedRow | None:
        statement = sa.select(
            policy_drafts.c.policy_draft_id,
            policy_drafts.c.workspace_id,
            policy_drafts.c.draft_version,
            policy_drafts.c.base_policy_revision_id,
        )
        if draft_id is not None:
            statement = statement.where(policy_drafts.c.policy_draft_id == draft_id)
        if workspace_id is not None:
            statement = statement.where(policy_drafts.c.workspace_id == workspace_id)
        result = await connection.execute(statement)
        return result.mappings().first()

    @staticmethod
    async def _select_rule_rows(
        connection: AsyncConnection,
        rules_table: Any,
        owner_column: str,
        owner_id: UUID,
    ) -> list[_MappedRow]:
        owner = getattr(rules_table.c, owner_column)
        result = await connection.execute(
            sa.select(
                rules_table.c.rule_id,
                rules_table.c.rule_kind,
                rules_table.c.source_id_operand,
                rules_table.c.text_operand,
                rules_table.c.size_bytes_operand,
                rules_table.c.semantic_fingerprint,
            )
            .where(owner == owner_id)
            .order_by(rules_table.c.rule_id)
        )
        return list(result.mappings().all())

    @staticmethod
    async def _select_policy_state_row(
        connection: AsyncConnection, workspace_id: UUID
    ) -> _MappedRow | None:
        result = await connection.execute(
            sa.select(
                workspace_policy_state.c.active_policy_revision_id,
                workspace_policy_state.c.active_revision_number,
            ).where(workspace_policy_state.c.workspace_id == workspace_id)
        )
        return result.mappings().first()

    @staticmethod
    async def _select_source_checkpoint(connection: AsyncConnection, workspace_id: UUID) -> int:
        result = await connection.execute(source_checkpoint_select_statement(workspace_id))
        return int(result.scalar_one())

    @staticmethod
    async def _fetch_source_page(
        connection: AsyncConnection, workspace_id: UUID, after_source_id: UUID | None
    ) -> list[_MappedRow]:
        result = await connection.execute(
            source_page_select_statement(
                workspace_id, after_source_id=after_source_id, limit=PREVIEW_SCAN_PAGE_SIZE
            )
        )
        return list(result.mappings().all())

    @staticmethod
    async def _select_now(connection: AsyncConnection) -> datetime:
        """Read the transaction-stable timestamp shared by every written row."""

        result = await connection.execute(sa.text("SELECT now()"))
        occurred_at = result.scalar_one()
        if not isinstance(occurred_at, datetime):  # pragma: no cover - driver contract
            raise TypeError("SELECT now() did not return a datetime")
        return occurred_at

    async def _load_preview_record(self, preview_id: UUID) -> PolicyPreviewRecord | None:
        async with (
            self._engine.connect() as connection,
            connection.begin(),
        ):
            await apply_transaction_bounds(connection)
            preview_row = await self._select_preview_row(connection, preview_id)
            if preview_row is None:
                return None
        return hydrate_policy_preview_record(preview_row)

    async def _load_ready_preview_record(self, preview_id: UUID) -> PolicyPreviewRecord | None:
        """Fresh-connection recovery proof for an uncertain request commit."""

        record = await self._load_preview_record(preview_id)
        if record is not None and record.status is PreviewStatus.PENDING:
            return record
        return None

    async def _expire_overdue_ready_row(
        self, connection: AsyncConnection, preview_row: _MappedRow
    ) -> bool:
        """Lazily expire one ready row whose expiry passed; returns the change."""

        expires_at = preview_row["expires_at"]
        if preview_row["state"] != PreviewStatus.READY.value or expires_at is None:
            return False
        now_row = await connection.execute(sa.text("SELECT CURRENT_TIMESTAMP"))
        now = now_row.scalar_one()
        if now < expires_at:
            return False
        result = await connection.execute(
            expire_ready_preview_update_statement(preview_row["policy_preview_id"])
        )
        return result.rowcount == 1

    def _record_preview_metric(
        self, outcome: PreviewMetricOutcome, started_monotonic: float
    ) -> None:
        """Record the closed preview metric after the durable outcome only."""

        if self._metrics is None:
            return
        duration_seconds = max(0.0, time.monotonic() - started_monotonic)
        self._metrics.record_preview(outcome=outcome, duration_seconds=duration_seconds)


def _require_actor_user(actor: PolicyActor) -> UUID:
    """Previews are Admin-requested: the actor must name the requesting user."""

    if actor.user_id is None:
        raise ValueError("preview request requires a user actor")
    return actor.user_id


__all__ = [
    "AUDIT_RESULT_SUCCEEDED",
    "POLICY_PREVIEW_AUDIT_TARGET_KIND",
    "POLICY_PREVIEW_BACKOFF_CAP_SECONDS",
    "POLICY_PREVIEW_CLAIM_BATCH_LIMIT",
    "POLICY_PREVIEW_LEASE_SECONDS",
    "PREVIEW_BASE_REVISION_STALE_ERROR_CODE",
    "PREVIEW_DISPATCH_TERMINAL_ERROR_CODE",
    "PREVIEW_DRAFT_STALE_ERROR_CODE",
    "PREVIEW_EXECUTION_DEADLINE_ERROR_CODE",
    "PREVIEW_EXECUTION_FAILED_ERROR_CODE",
    "PREVIEW_LEASE_EXPIRED_ERROR_CODE",
    "PREVIEW_MISSING_ERROR_CODE",
    "PREVIEW_REQUESTED_AUDIT_ACTION",
    "PREVIEW_SOURCE_CHECKPOINT_STALE_ERROR_CODE",
    "InjectedPreviewFailure",
    "LeasedPolicyPreview",
    "PolicyPreviewSweepResult",
    "PostgresqlPolicyPreviewStore",
    "apply_preview_snapshot_bounds",
    "build_preview_requested_audit_values",
    "build_preview_result_row_values",
    "claim_pending_previews_select_statement",
    "expire_overdue_previews_statements",
    "expire_ready_preview_update_statement",
    "fail_preview_update_statement",
    "hydrate_policy_preview_record",
    "hydrate_policy_revision_rules",
    "hydrate_preview_result_row",
    "lease_preview_update_statement",
    "map_preview_database_failure",
    "mark_preview_ready_update_statement",
    "mark_preview_running_update_statement",
    "preview_missing_error",
    "preview_result_page_select_statement",
    "preview_subject_for_row",
    "reclaim_lease_update_statement",
    "release_retry_update_statement",
    "source_checkpoint_select_statement",
    "source_page_select_statement",
]
