"""Publication-reconciliation persistence and batch execution over PostgreSQL.

:class:`PostgresqlPolicyReconciliationStore` implements the durable halves of
spec 15. The leased-outbox half mirrors the projection and preview
dispatchers: ``claim_pending`` selects due ``pending`` rows joined with their
revision's source checkpoint behind ``FOR UPDATE OF`` the intent table
``SKIP LOCKED`` in the pinned order, writes the leased status with a
caller-injected UUIDv7 fence token and a database-time expiry, and commits
before any Temporal I/O; ``reclaim_expired`` returns overdue leases to
``pending`` with the capped exponential backoff; the fenced transitions
(acknowledge dispatched, retryable release, terminal failure) affect a row
only when the exact intent ID, state and lease token all match.

The workflow half executes one bounded batch per activity call in exactly one
``READ COMMITTED`` transaction: it reads the active revision pointer without
ever locking policy state while it acquires source rows (a superseded
revision returns the closed superseded outcome and writes nothing), loads the
immutable revision rules, streams one stable ``source_id`` keyset page of at
most 500 current valid sources joined with their current-version evidence and
per-source current event sequence, evaluates every source with a canonical
subject through the pure evaluator, derives each previous/proposed enforced
transition from the parent revision's most recent evaluation rows (with the
closed fallbacks), re-checks the active pointer and every planned subject
sequence immediately before the first write — any drift aborts the batch as
the retryable stale error so the retry re-reads and replans — then inserts or
verifies the exact immutable ``policy_evaluations`` identities and inserts
deterministic ``policy_transition`` projection intents with ``ON CONFLICT DO
NOTHING`` against the partial origin uniqueness, verifying the existing
operation on replay. Metrics record only after the commit; heartbeats fire
once per committed batch; completion writes the idempotent append-only
``exclusion_policy.reconciliation_completed`` audit row with the counters
digest and records the reconciliation lag, and failure releases the durable
row (retryable back to ``pending`` with bounded backoff, terminal otherwise)
with the ``exclusion_policy.reconciliation_failed`` audit row. SQLSTATE
values, SQL, parameters and driver text never leave the adapter.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Any, Final
from uuid import UUID, uuid7

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from personal_os.diagnostics.context import DiagnosticContext
from personal_os.diagnostics.events import SafeToken
from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import ApplicationError, InternalApplicationError
from personal_os.exclusion_policy.canonical_json import (
    CanonicalJsonValue,
    canonicalize_json_value,
)
from personal_os.exclusion_policy.contracts import (
    EnforcedPolicyDecision,
    ExclusionPolicyRevision,
    PolicySubjectField,
    RawPolicyDecision,
)
from personal_os.exclusion_policy.errors import ExclusionPolicyError
from personal_os.exclusion_policy.evaluation import evaluate_policy
from personal_os.exclusion_policy.previews import compute_subject_fingerprint
from personal_os.exclusion_policy.reconciliation import (
    RECONCILIATION_BATCH_SIZE,
    PolicyEvaluation,
    PolicyTransitionOperation,
    PolicyTransitionProjectionKind,
    ReconciliationCounters,
    ReconciliationMetrics,
    ReconciliationProgress,
    ReconciliationTransition,
    derive_reconciliation_transition,
    policy_transition_intent_plans,
    previous_enforced_without_policy,
    previous_enforced_without_prior_evaluation,
)
from postgresql_source_store.engine import apply_transaction_bounds
from postgresql_source_store.error_mapping import DatabaseFailureKind, classify_database_failure
from postgresql_source_store.policy_drafts import PolicyDatabaseRetryPolicy
from postgresql_source_store.policy_previews import (
    hydrate_policy_revision_rules,
    preview_subject_for_row,
)
from postgresql_source_store.tables import (
    audit_events,
    content_objects,
    policy_evaluations,
    policy_reconciliation_intents,
    policy_rules,
    projection_intents,
    source_policies,
    source_versions,
    sources,
    sync_events,
    workspace_policy_state,
)

#: Audit-row literals for the durable reconciliation diagnostics (spec 21).
RECONCILIATION_COMPLETED_AUDIT_ACTION: Final[str] = "exclusion_policy.reconciliation_completed"
RECONCILIATION_FAILED_AUDIT_ACTION: Final[str] = "exclusion_policy.reconciliation_failed"
POLICY_RECONCILIATION_AUDIT_TARGET_KIND: Final[str] = "policy_revision"
AUDIT_RESULT_SUCCEEDED: Final[str] = "succeeded"
AUDIT_RESULT_FAILED: Final[str] = "failed"

#: Closed safe error codes recorded on reconciliation intent rows. Every token
#: satisfies the ``safe_error_code`` column grammar of the migration.
RECONCILIATION_LEASE_EXPIRED_ERROR_CODE: Final[SafeToken] = SafeToken.parse(
    "reconciliation_lease_expired"
)
RECONCILIATION_EXECUTION_FAILED_ERROR_CODE: Final[SafeToken] = SafeToken.parse(
    "reconciliation_execution_failed"
)
RECONCILIATION_DISPATCH_TERMINAL_ERROR_CODE: Final[SafeToken] = SafeToken.parse(
    "reconciliation_dispatch_terminal"
)

#: Contract tag hashed into the completion counters digest.
RECONCILIATION_COMPLETION_DIGEST_CONTRACT: Final[str] = (
    "exclusion_policy_reconciliation_completion/v1"
)

#: Reconciliation leases cover only the workflow start call (the batches run
#: inside Temporal's own retry), so the pinned duration mirrors the dispatch
#: and preview leases.
POLICY_RECONCILIATION_LEASE_SECONDS: Final[int] = 60

#: The pinned maximum reconciliation intents one dispatch cycle claims.
POLICY_RECONCILIATION_CLAIM_BATCH_LIMIT: Final[int] = 20

#: The bounded exponential backoff cap for lease reclaim, retryable dispatch
#: releases and workflow-side failure releases (doubling from the row's own
#: attempt count, capped at five minutes).
POLICY_RECONCILIATION_BACKOFF_CAP_SECONDS: Final[int] = 300

#: The closed lifecycle states a workflow-side failure release accepts.
_EXECUTABLE_STATES: Final[tuple[str, ...]] = ("leased", "dispatched")

#: The ``safe_error_code`` CHECK constraint accepts ``^[a-z][a-z0-9_]{0,99}$``;
#: ``SafeToken`` is wider, so the stricter column grammar is enforced here.
_SAFE_ERROR_CODE_COLUMN_GRAMMAR: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9_]{0,99}$")

_KNOWN_POLICY_TRANSITION_OPERATIONS: Final[frozenset[str]] = frozenset(
    operation.value for operation in PolicyTransitionOperation
)
_KNOWN_POLICY_TRANSITION_KINDS: Final[frozenset[str]] = frozenset(
    kind.value for kind in PolicyTransitionProjectionKind
)

#: Injected heartbeat over the closed progress value; the activity layer
#: supplies the Temporal heartbeat, unit tests a recorder.
type ReconciliationHeartbeat = Callable[[ReconciliationProgress], Awaitable[None]]

#: One row of a batch page or verification read: a SQLAlchemy row mapping or
#: an equivalent mapping in tests.
type _MappedRow = Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class LeasedPolicyReconciliation:
    """One leased reconciliation intent handed to the workflow starter."""

    policy_reconciliation_intent_id: UUID
    workspace_id: UUID
    policy_revision_id: UUID
    workflow_id: str
    source_checkpoint_event_sequence: int
    attempt_count: int
    lease_token: UUID
    leased_until: datetime


@dataclass(frozen=True, slots=True)
class ReconciliationBatchOutcome:
    """The closed outcome of one committed reconciliation batch.

    ``superseded`` marks the clean stop of a revision that stopped being
    active before the batch's write recheck — nothing was written. Counts are
    the batch's closed transition counters; the workflow accumulates them.
    """

    superseded: bool
    has_more: bool
    last_source_id: UUID | None
    evaluated_sources: int
    to_excluded_sources: int
    to_allowed_sources: int
    unchanged_sources: int


def map_reconciliation_database_failure(cause: BaseException) -> ApplicationError:
    """Map a database or driver failure onto the closed policy registry."""

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


# --- leased outbox statements ---------------------------------------------------------


def claim_pending_reconciliations_select_statement(
    now: datetime, limit: int
) -> sa.Select[tuple[Any, ...]]:
    """Build the due pending-intent claim select with the pinned row skip.

    Only ``pending`` rows whose availability has passed the injected ``now``
    reading match; the join resolves the revision's publication source
    checkpoint for the closed workflow input, and the row lock applies to the
    intent table only — never to the immutable revision row.
    """

    _require_aware(now, "now")
    if limit < 1 or limit > POLICY_RECONCILIATION_CLAIM_BATCH_LIMIT:
        raise ValueError("limit must be between 1 and the pinned claim batch limit")
    return (
        sa.select(
            policy_reconciliation_intents.c.policy_reconciliation_intent_id,
            policy_reconciliation_intents.c.workspace_id,
            policy_reconciliation_intents.c.policy_revision_id,
            policy_reconciliation_intents.c.workflow_id,
            source_policies.c.source_checkpoint_event_sequence,
            policy_reconciliation_intents.c.attempt_count,
        )
        .select_from(policy_reconciliation_intents)
        .join(
            source_policies,
            sa.and_(
                source_policies.c.workspace_id == policy_reconciliation_intents.c.workspace_id,
                source_policies.c.policy_revision_id
                == policy_reconciliation_intents.c.policy_revision_id,
            ),
        )
        .where(
            policy_reconciliation_intents.c.state == "pending",
            policy_reconciliation_intents.c.available_at
            <= sa.bindparam("now", now, type_=sa.DateTime(timezone=True)),
        )
        .order_by(
            policy_reconciliation_intents.c.available_at,
            policy_reconciliation_intents.c.created_at,
            policy_reconciliation_intents.c.policy_reconciliation_intent_id,
        )
        .limit(limit)
        .with_for_update(skip_locked=True, of=policy_reconciliation_intents)
    )


def lease_reconciliation_update_statement(intent_id: UUID) -> sa.Update:
    """Build the guarded lease write with database-time expiry.

    The fence matches only the still-``pending`` unleased row; the expiry is
    ``CURRENT_TIMESTAMP`` plus the pinned lease duration, so the lease CHECK
    constraint always holds with one clock, and the attempt count increments
    exactly once per claim.
    """

    return (
        sa.update(policy_reconciliation_intents)
        .values(
            state="leased",
            lease_token=sa.bindparam("lease_token", type_=sa.Uuid()),
            leased_until=sa.func.current_timestamp()
            + sa.func.make_interval(0, 0, 0, 0, 0, 0, POLICY_RECONCILIATION_LEASE_SECONDS),
            attempt_count=policy_reconciliation_intents.c.attempt_count + 1,
        )
        .where(
            policy_reconciliation_intents.c.policy_reconciliation_intent_id == intent_id,
            policy_reconciliation_intents.c.state == "pending",
            policy_reconciliation_intents.c.lease_token.is_(None),
        )
        .returning(policy_reconciliation_intents.c.leased_until)
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
            POLICY_RECONCILIATION_BACKOFF_CAP_SECONDS,
            sa.cast(sa.func.power(2, policy_reconciliation_intents.c.attempt_count), sa.Integer()),
        ),
    )


def reclaim_lease_update_statement(now: datetime) -> sa.Update:
    """Build the expired-lease return to ``pending`` with bounded backoff."""

    _require_aware(now, "now")
    return (
        sa.update(policy_reconciliation_intents)
        .values(
            state="pending",
            lease_token=sa.null(),
            leased_until=sa.null(),
            attempt_count=policy_reconciliation_intents.c.attempt_count + 1,
            safe_error_code=RECONCILIATION_LEASE_EXPIRED_ERROR_CODE.value,
            available_at=sa.func.current_timestamp() + _bounded_backoff_interval(),
            updated_at=sa.func.current_timestamp(),
        )
        .where(
            policy_reconciliation_intents.c.state == "leased",
            policy_reconciliation_intents.c.leased_until
            <= sa.bindparam("now", now, type_=sa.DateTime(timezone=True)),
        )
    )


def acknowledge_dispatched_statement(intent_id: UUID, lease_token: UUID) -> sa.Update:
    """Build the fenced dispatched acknowledgement.

    ``dispatched`` is the durable resting state: the deterministic workflow
    owns the revision's reconciliation and completion evidence is the
    append-only audit row plus the evaluations themselves.
    """

    return (
        sa.update(policy_reconciliation_intents)
        .values(
            state="dispatched",
            lease_token=sa.null(),
            leased_until=sa.null(),
            dispatched_at=sa.func.current_timestamp(),
            updated_at=sa.func.current_timestamp(),
        )
        .where(
            policy_reconciliation_intents.c.policy_reconciliation_intent_id == intent_id,
            policy_reconciliation_intents.c.state == "leased",
            policy_reconciliation_intents.c.lease_token
            == sa.bindparam("lease_token", lease_token, type_=sa.Uuid()),
        )
    )


def release_retry_statement(intent_id: UUID, lease_token: UUID, error_code: SafeToken) -> sa.Update:
    """Build the fenced retryable release back to ``pending`` with backoff."""

    _require_column_safe_error_code(error_code)
    return (
        sa.update(policy_reconciliation_intents)
        .values(
            state="pending",
            lease_token=sa.null(),
            leased_until=sa.null(),
            attempt_count=policy_reconciliation_intents.c.attempt_count + 1,
            safe_error_code=error_code.value,
            available_at=sa.func.current_timestamp() + _bounded_backoff_interval(),
            updated_at=sa.func.current_timestamp(),
        )
        .where(
            policy_reconciliation_intents.c.policy_reconciliation_intent_id == intent_id,
            policy_reconciliation_intents.c.state == "leased",
            policy_reconciliation_intents.c.lease_token == lease_token,
        )
    )


def mark_terminal_statement(intent_id: UUID, lease_token: UUID, error_code: SafeToken) -> sa.Update:
    """Build the fenced non-retryable terminal transition."""

    _require_column_safe_error_code(error_code)
    return (
        sa.update(policy_reconciliation_intents)
        .values(
            state="terminal",
            lease_token=sa.null(),
            leased_until=sa.null(),
            safe_error_code=error_code.value,
            updated_at=sa.func.current_timestamp(),
        )
        .where(
            policy_reconciliation_intents.c.policy_reconciliation_intent_id == intent_id,
            policy_reconciliation_intents.c.state == "leased",
            policy_reconciliation_intents.c.lease_token == lease_token,
        )
    )


def fail_dispatched_statement(
    workspace_id: UUID,
    policy_revision_id: UUID,
    error_code: SafeToken,
    *,
    retryable: bool = True,
) -> sa.Update:
    """Build the workflow-side failure release of the durable row.

    A dependency failure returns the revision's intent to ``pending`` with
    bounded backoff (spec 15: reconciliation stays pending with bounded
    retry/backoff and alertable lag); a non-retryable contract failure marks
    it terminal with the closed error code. The fence accepts only the two
    workflow-owned states of the exact workspace/revision row.
    """

    _require_column_safe_error_code(error_code)
    values: dict[str, Any] = {
        "state": "pending" if retryable else "terminal",
        "lease_token": sa.null(),
        "leased_until": sa.null(),
        # Leaving the workflow-owned states must also clear the dispatch
        # timestamp: the CHECK ties it exactly to the dispatched state.
        "dispatched_at": sa.null(),
        "safe_error_code": error_code.value,
        "updated_at": sa.func.current_timestamp(),
    }
    if retryable:
        values["attempt_count"] = policy_reconciliation_intents.c.attempt_count + 1
        values["available_at"] = sa.func.current_timestamp() + _bounded_backoff_interval()
    return (
        sa.update(policy_reconciliation_intents)
        .values(**values)
        .where(
            policy_reconciliation_intents.c.workspace_id == workspace_id,
            policy_reconciliation_intents.c.policy_revision_id == policy_revision_id,
            policy_reconciliation_intents.c.state.in_(_EXECUTABLE_STATES),
        )
    )


# --- batch execution statements --------------------------------------------------------


def active_revision_select_statement(workspace_id: UUID) -> sa.Select[tuple[Any, ...]]:
    """Build the lock-free active-pointer read of one workspace.

    Reconciliation must never hold a policy-state lock while acquiring source
    rows, so every batch reads the pointer plainly and re-reads it before the
    first write instead of serializing against publication.
    """

    return sa.select(workspace_policy_state.c.active_policy_revision_id).where(
        workspace_policy_state.c.workspace_id == workspace_id
    )


def revision_identity_select_statement(
    workspace_id: UUID, policy_revision_id: UUID
) -> sa.Select[tuple[Any, ...]]:
    """Build the workspace-bound revision lookup with its parent pointer."""

    return sa.select(
        source_policies.c.policy_revision_id,
        source_policies.c.revision_number,
        source_policies.c.parent_policy_revision_id,
        source_policies.c.published_at,
    ).where(
        source_policies.c.workspace_id == workspace_id,
        source_policies.c.policy_revision_id == policy_revision_id,
    )


def _current_event_sequence_scalar(workspace_id: UUID) -> sa.ScalarSelect[Any]:
    """The correlated per-source current canonical event sequence."""

    return (
        sa.select(sa.func.max(sync_events.c.event_sequence))
        .where(
            sync_events.c.workspace_id == workspace_id,
            sync_events.c.source_id == sources.c.source_id,
        )
        .scalar_subquery()
    )


def reconciliation_batch_select_statement(
    workspace_id: UUID, *, after_source_id: UUID | None, limit: int
) -> sa.Select[tuple[Any, ...]]:
    """Build one stable keyset page of the workspace's current valid sources.

    Rows stream in ascending ``source_id`` order with the workspace bound,
    soft-deleted sources excluded and the page capped at the pinned 500 rows.
    Current content evidence (media type, byte size) joins through the current
    version's content object and stays absent when no current version exists;
    the correlated per-source ``MAX(event_sequence)`` is the subject's current
    canonical event sequence and stays absent when no canonical event exists.
    """

    if limit < 1 or limit > RECONCILIATION_BATCH_SIZE:
        raise ValueError("limit must be between 1 and the pinned reconciliation batch size")
    statement = (
        sa.select(
            sources.c.source_id,
            sources.c.source_type,
            sources.c.current_version_id,
            content_objects.c.media_type,
            content_objects.c.byte_size,
            _current_event_sequence_scalar(workspace_id).label("subject_event_sequence"),
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
        statement = statement.where(
            sources.c.source_id > sa.bindparam("after_source_id", after_source_id, type_=sa.Uuid())
        )
    return statement


def prior_evaluations_select_statement(
    parent_policy_revision_id: UUID, source_ids: Sequence[UUID]
) -> sa.Select[tuple[Any, ...]]:
    """Build the most recent prior evaluation lookup per source.

    ``DISTINCT ON (source_id)`` ordered by descending subject event sequence
    resolves the parent revision's latest recorded evaluation of each
    requested source; sources without a row fall back to the closed
    no-prior-evaluation semantics.
    """

    if not source_ids:
        raise ValueError("source_ids must not be empty")
    return (
        sa.select(
            policy_evaluations.c.source_id,
            policy_evaluations.c.subject_event_sequence,
            policy_evaluations.c.enforced_decision,
        )
        .where(
            policy_evaluations.c.policy_revision_id == parent_policy_revision_id,
            policy_evaluations.c.source_id.in_(
                sa.bindparam("source_ids", list(source_ids), type_=sa.Uuid(), expanding=True)
            ),
        )
        .order_by(
            policy_evaluations.c.source_id,
            policy_evaluations.c.subject_event_sequence.desc(),
        )
        .distinct(policy_evaluations.c.source_id)
    )


def batch_sequences_select_statement(
    workspace_id: UUID, source_ids: Sequence[UUID]
) -> sa.Select[tuple[Any, ...]]:
    """Build the pre-write recheck of the planned sources' sequences."""

    if not source_ids:
        raise ValueError("source_ids must not be empty")
    return sa.select(
        sources.c.source_id,
        _current_event_sequence_scalar(workspace_id).label("subject_event_sequence"),
    ).where(
        sources.c.workspace_id == workspace_id,
        sources.c.source_id.in_(
            sa.bindparam("source_ids", list(source_ids), type_=sa.Uuid(), expanding=True)
        ),
    )


def verify_planned_batch_sequences(planned: Mapping[UUID, int], rows: Sequence[_MappedRow]) -> None:
    """Fail the batch when any planned subject sequence drifted.

    The comparison is the pre-write recheck: a source whose current canonical
    event sequence moved since the page read (or vanished) must never receive
    an evaluation of the stale subject, so the caller aborts with the
    retryable stale error and the retry re-reads and replans.
    """

    observed = {row["source_id"]: row["subject_event_sequence"] for row in rows}
    for source_id, planned_sequence in planned.items():
        observed_sequence = observed.get(source_id)
        if observed_sequence is None or int(observed_sequence) != planned_sequence:
            raise ExclusionPolicyError(ErrorCode.EXCLUSION_POLICY_SNAPSHOT_OUTDATED)


def build_policy_evaluation_row_values(
    *,
    policy_evaluation_id: UUID,
    workspace_id: UUID,
    policy_revision_id: UUID,
    source_id: UUID,
    subject_event_sequence: int,
    raw_decision: RawPolicyDecision,
    enforced_decision: EnforcedPolicyDecision,
    matched_rule_ids: Sequence[UUID],
    missing_fields: Sequence[PolicySubjectField],
    subject_fingerprint: str,
    evaluated_at: datetime | None,
) -> dict[str, Any]:
    """Map one evaluation onto its immutable ``policy_evaluations`` row values.

    Rule IDs and missing field names render as sorted space-separated text —
    exactly the column CHECK grammar — and no operand, locator or display
    value has a field to occupy. A null ``evaluated_at`` leaves the database
    timestamp default in force.
    """

    del workspace_id  # The identity columns carry the workspace binding via FKs.
    values: dict[str, Any] = {
        "policy_evaluation_id": policy_evaluation_id,
        "policy_revision_id": policy_revision_id,
        "source_id": source_id,
        "subject_event_sequence": subject_event_sequence,
        "raw_decision": raw_decision.value,
        "enforced_decision": enforced_decision.value,
        "matched_rule_ids": " ".join(str(rule_id) for rule_id in matched_rule_ids),
        "missing_fields": " ".join(field.value for field in missing_fields),
        "subject_fingerprint": subject_fingerprint,
    }
    if evaluated_at is not None:
        values["evaluated_at"] = evaluated_at
    return values


def build_policy_transition_intent_values(
    *,
    projection_intent_id: UUID,
    workspace_id: UUID,
    policy_revision_id: UUID,
    source_id: UUID,
    source_version_id: UUID | None,
    projection_kind: str,
    operation: str,
    available_at: datetime | None,
) -> dict[str, Any]:
    """Map one planned transition onto its deterministic intent row values.

    Exactly the ``policy_transition`` origin is populated — ``event_id`` stays
    null so the source-event dispatcher's claim can never select the row — and
    an upsert requires its current source version by the migration CHECK.
    """

    if projection_kind not in _KNOWN_POLICY_TRANSITION_KINDS:
        raise ValueError("projection_kind is not a registered projection kind")
    if operation not in _KNOWN_POLICY_TRANSITION_OPERATIONS:
        raise ValueError("operation is not a registered projection operation")
    if operation == PolicyTransitionOperation.UPSERT.value and source_version_id is None:
        raise ValueError("an upsert intent requires the current source version")
    values: dict[str, Any] = {
        "projection_intent_id": projection_intent_id,
        "workspace_id": workspace_id,
        "origin_kind": "policy_transition",
        "event_id": None,
        "policy_revision_id": policy_revision_id,
        "source_id": source_id,
        "source_version_id": source_version_id,
        "projection_kind": projection_kind,
        "operation": operation,
        "status": "pending",
        "attempt_count": 0,
        "lease_token": None,
        "leased_until": None,
        "dispatched_at": None,
        "last_error_code": None,
    }
    if available_at is not None:
        values["available_at"] = available_at
        values["created_at"] = available_at
        values["updated_at"] = available_at
    return values


def policy_evaluations_insert_statement(rows: Sequence[_MappedRow]) -> sa.Insert:
    """Build the insert-once evaluations statement.

    The conflict target is exactly the immutable identity; a replay inserts
    nothing and the verification select proves the existing row's equality.
    """

    if not rows:
        raise ValueError("rows must not be empty")
    return (
        postgresql_insert(policy_evaluations)
        .values([dict(row) for row in rows])
        .on_conflict_do_nothing(
            index_elements=[
                policy_evaluations.c.policy_revision_id,
                policy_evaluations.c.source_id,
                policy_evaluations.c.subject_event_sequence,
            ]
        )
    )


def policy_evaluations_verify_select_statement(
    policy_revision_id: UUID, source_ids: Sequence[UUID]
) -> sa.Select[tuple[Any, ...]]:
    """Build the post-insert verification read of the exact identities."""

    if not source_ids:
        raise ValueError("source_ids must not be empty")
    return sa.select(
        policy_evaluations.c.source_id,
        policy_evaluations.c.subject_event_sequence,
        policy_evaluations.c.raw_decision,
        policy_evaluations.c.enforced_decision,
        policy_evaluations.c.subject_fingerprint,
    ).where(
        policy_evaluations.c.policy_revision_id == policy_revision_id,
        policy_evaluations.c.source_id.in_(
            sa.bindparam("source_ids", list(source_ids), type_=sa.Uuid(), expanding=True)
        ),
    )


def policy_transition_intent_insert_statement(rows: Sequence[_MappedRow]) -> sa.Insert:
    """Build the deterministic policy-transition intent insert.

    The conflict target is the partial origin uniqueness
    ``(policy_revision_id, source_id, projection_kind) WHERE
    origin_kind = 'policy_transition'``: an exact replay inserts no second
    intent and the verification select proves the existing operation.
    """

    if not rows:
        raise ValueError("rows must not be empty")
    return (
        postgresql_insert(projection_intents)
        .values([dict(row) for row in rows])
        .on_conflict_do_nothing(
            index_elements=[
                projection_intents.c.policy_revision_id,
                projection_intents.c.source_id,
                projection_intents.c.projection_kind,
            ],
            index_where=sa.text("origin_kind = 'policy_transition'"),
        )
    )


def policy_transition_intent_verify_select_statement(
    policy_revision_id: UUID, source_ids: Sequence[UUID]
) -> sa.Select[tuple[Any, ...]]:
    """Build the post-insert verification read of the planned intents."""

    if not source_ids:
        raise ValueError("source_ids must not be empty")
    return sa.select(
        projection_intents.c.source_id,
        projection_intents.c.projection_kind,
        projection_intents.c.operation,
        projection_intents.c.source_version_id,
    ).where(
        projection_intents.c.origin_kind == "policy_transition",
        projection_intents.c.policy_revision_id == policy_revision_id,
        projection_intents.c.source_id.in_(
            sa.bindparam("source_ids", list(source_ids), type_=sa.Uuid(), expanding=True)
        ),
    )


# --- audit values -----------------------------------------------------------------------


def compute_completion_counters_digest(
    *,
    evaluated_sources: int,
    to_excluded_sources: int,
    to_allowed_sources: int,
    unchanged_sources: int,
) -> str:
    """Hash the closed completion counters into the audit digest (spec 21)."""

    payload: dict[str, CanonicalJsonValue] = {
        "contract": RECONCILIATION_COMPLETION_DIGEST_CONTRACT,
        "evaluated_sources": evaluated_sources,
        "to_excluded_sources": to_excluded_sources,
        "to_allowed_sources": to_allowed_sources,
        "unchanged_sources": unchanged_sources,
    }
    return sha256(canonicalize_json_value(payload)).hexdigest()


def build_reconciliation_audit_values(
    *,
    action: str,
    workspace_id: UUID,
    policy_revision_id: UUID,
    result: str,
    reason_code: str | None,
    evaluated_sources: int,
    to_excluded_sources: int,
    to_allowed_sources: int,
    unchanged_sources: int,
    occurred_at: datetime,
    request_id: UUID,
) -> dict[str, Any]:
    """Build one durable reconciliation diagnostic audit-row value set.

    The row carries the closed ``system`` actor, the closed action/result
    literals, opaque IDs and the counters digest only: rule operands,
    locators, source display values and subject fingerprints never enter the
    audit table (spec 21). The counters digest exists only on completions.
    """

    return {
        "audit_event_id": uuid7(),
        "workspace_id": workspace_id,
        "actor_kind": "system",
        "actor_id": None,
        "actor_reference": None,
        "action": action,
        "target_kind": POLICY_RECONCILIATION_AUDIT_TARGET_KIND,
        "target_id": policy_revision_id,
        "request_id": request_id,
        "client_request_id": None,
        "trace_id": None,
        "result": result,
        "reason_code": reason_code,
        "safe_diff_hash": (
            compute_completion_counters_digest(
                evaluated_sources=evaluated_sources,
                to_excluded_sources=to_excluded_sources,
                to_allowed_sources=to_allowed_sources,
                unchanged_sources=unchanged_sources,
            )
            if action == RECONCILIATION_COMPLETED_AUDIT_ACTION
            else None
        ),
        "occurred_at": occurred_at,
    }


def completion_audit_exists_select_statement(
    policy_revision_id: UUID,
) -> sa.Select[tuple[Any, ...]]:
    """Build the idempotence lookup of one revision's completion audit row."""

    return (
        sa.select(audit_events.c.audit_event_id)
        .where(
            audit_events.c.action == RECONCILIATION_COMPLETED_AUDIT_ACTION,
            audit_events.c.target_id == policy_revision_id,
        )
        .limit(1)
    )


# --- planned subject ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _PlannedSubject:
    """One evaluated source with its derived transition and intent plans."""

    evaluation: PolicyEvaluation
    subject_fingerprint: str
    matched_rule_ids: tuple[UUID, ...]
    missing_fields: tuple[PolicySubjectField, ...]
    current_version_id: UUID | None
    previous_enforced: EnforcedPolicyDecision
    transition: ReconciliationTransition


class PostgresqlPolicyReconciliationStore:
    """Durable reconciliation-intent store and bounded batch executor (spec 15).

    The store takes the composition-owned :class:`AsyncEngine` plus the
    injected seams (retry policy, UUIDv7 lease-token generator, metrics sink);
    it opens no connection at construction. Outbox transitions and batch
    execution each run one ``READ COMMITTED`` transaction behind the pinned
    ``SET LOCAL`` bounds; a batch never locks policy state or source rows.
    """

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        retry: PolicyDatabaseRetryPolicy | None = None,
        lease_token_generator: Callable[[], UUID] = uuid7,
        metrics: ReconciliationMetrics | None = None,
    ) -> None:
        self._engine = engine
        self._retry = retry if retry is not None else PolicyDatabaseRetryPolicy()
        self._lease_token_generator = lease_token_generator
        self._metrics = metrics

    # --- leased outbox ------------------------------------------------------------

    async def reclaim_expired(self, now: datetime) -> int:
        """Return every overdue lease to pending with bounded backoff."""

        _require_aware(now, "now")
        return await self._retry.run(lambda _attempt: self._reclaim_expired_once(now))

    async def _reclaim_expired_once(self, now: datetime) -> int:
        async with (
            self._engine.connect() as connection,
            connection.begin(),
        ):
            await apply_transaction_bounds(connection)
            result = await connection.execute(reclaim_lease_update_statement(now), {"now": now})
            return int(result.rowcount)

    async def claim_pending(self, now: datetime, limit: int) -> list[LeasedPolicyReconciliation]:
        """Claim due pending reconciliation intents behind the pinned limit."""

        _require_aware(now, "now")
        return await self._retry.run(lambda _attempt: self._claim_pending_once(now, limit))

    async def _claim_pending_once(
        self, now: datetime, limit: int
    ) -> list[LeasedPolicyReconciliation]:
        claimed: list[LeasedPolicyReconciliation] = []
        async with (
            self._engine.connect() as connection,
            connection.begin(),
        ):
            await apply_transaction_bounds(connection)
            rows = (
                await connection.execute(claim_pending_reconciliations_select_statement(now, limit))
            ).all()
            for row in rows:
                lease_token = self._lease_token_generator()
                lease_result = await connection.execute(
                    lease_reconciliation_update_statement(row.policy_reconciliation_intent_id),
                    {"lease_token": lease_token},
                )
                leased_until = lease_result.scalar_one_or_none()
                if leased_until is None:
                    # A stale claim between the select and the guarded lease
                    # write: the row stays untouched and is not leased here.
                    continue
                claimed.append(
                    LeasedPolicyReconciliation(
                        policy_reconciliation_intent_id=row.policy_reconciliation_intent_id,
                        workspace_id=row.workspace_id,
                        policy_revision_id=row.policy_revision_id,
                        workflow_id=str(row.workflow_id),
                        source_checkpoint_event_sequence=int(row.source_checkpoint_event_sequence),
                        attempt_count=int(row.attempt_count) + 1,
                        lease_token=lease_token,
                        leased_until=leased_until,
                    )
                )
        return claimed

    async def acknowledge_dispatched(
        self, intent_id: UUID, lease_token: UUID, now: datetime
    ) -> bool:
        """Fence the dispatched acknowledgement on the exact lease token."""

        _require_aware(now, "now")
        return await self._retry.run(
            lambda _attempt: self._execute_fenced_update_once(
                acknowledge_dispatched_statement(intent_id, lease_token)
            )
        )

    async def release_retry(
        self,
        intent_id: UUID,
        lease_token: UUID,
        error_code: SafeToken,
        now: datetime,
    ) -> bool:
        """Release one leased intent back to pending with bounded backoff."""

        _require_aware(now, "now")
        return await self._retry.run(
            lambda _attempt: self._execute_fenced_update_once(
                release_retry_statement(intent_id, lease_token, error_code)
            )
        )

    async def mark_terminal(
        self,
        intent_id: UUID,
        lease_token: UUID,
        error_code: SafeToken,
        now: datetime,
    ) -> bool:
        """Mark one leased intent terminally failed with a closed code."""

        _require_aware(now, "now")
        return await self._retry.run(
            lambda _attempt: self._execute_fenced_update_once(
                mark_terminal_statement(intent_id, lease_token, error_code)
            )
        )

    async def _execute_fenced_update_once(self, statement: sa.Update) -> bool:
        async with (
            self._engine.connect() as connection,
            connection.begin(),
        ):
            await apply_transaction_bounds(connection)
            result = await connection.execute(statement)
            return int(result.rowcount) == 1

    # --- bounded batch execution ------------------------------------------------------

    async def run_reconciliation_batch(
        self,
        workspace_id: UUID,
        policy_revision_id: UUID,
        source_checkpoint_event_sequence: int,
        after_source_id: UUID | None,
        heartbeat: ReconciliationHeartbeat | None = None,
    ) -> ReconciliationBatchOutcome:
        """Execute one bounded reconciliation batch in one transaction.

        A superseded revision returns the closed superseded outcome with zero
        writes. Sequence drift between the page read and the first write
        aborts as the retryable stale error so the retry re-reads and replans;
        nothing was written at that point. Metrics record only after the
        commit and the heartbeat fires once per committed batch.
        """

        return await self._retry.run(
            lambda _attempt: self._run_reconciliation_batch_once(
                workspace_id,
                policy_revision_id,
                source_checkpoint_event_sequence,
                after_source_id,
                heartbeat,
            )
        )

    async def _run_reconciliation_batch_once(
        self,
        workspace_id: UUID,
        policy_revision_id: UUID,
        source_checkpoint_event_sequence: int,
        after_source_id: UUID | None,
        heartbeat: ReconciliationHeartbeat | None,
    ) -> ReconciliationBatchOutcome:
        del source_checkpoint_event_sequence  # The pointer recheck is authoritative.
        async with (
            self._engine.connect() as connection,
            connection.begin(),
        ):
            await apply_transaction_bounds(connection)
            if not await self._revision_still_active(connection, workspace_id, policy_revision_id):
                return self._superseded_outcome()
            revision_row = (
                (
                    await connection.execute(
                        revision_identity_select_statement(workspace_id, policy_revision_id)
                    )
                )
                .mappings()
                .first()
            )
            if revision_row is None:
                raise ExclusionPolicyError(ErrorCode.EXCLUSION_POLICY_NOT_INITIALIZED)
            revision = ExclusionPolicyRevision(
                policy_revision_id=policy_revision_id,
                workspace_id=workspace_id,
                revision_number=int(revision_row["revision_number"]),
                rules=await self._load_revision_rules(connection, policy_revision_id),
            )
            page = (
                (
                    await connection.execute(
                        reconciliation_batch_select_statement(
                            workspace_id,
                            after_source_id=after_source_id,
                            limit=RECONCILIATION_BATCH_SIZE,
                        )
                    )
                )
                .mappings()
                .all()
            )
            if not page:
                return ReconciliationBatchOutcome(
                    superseded=False,
                    has_more=False,
                    last_source_id=None,
                    evaluated_sources=0,
                    to_excluded_sources=0,
                    to_allowed_sources=0,
                    unchanged_sources=0,
                )
            evaluated = self._evaluate_page(workspace_id, policy_revision_id, revision, page)
            prior_decisions = await self._load_prior_decisions(
                connection,
                revision_row["parent_policy_revision_id"],
                [item.evaluation.source_id for item in evaluated],
            )
            planned = self._derive_transitions(
                evaluated,
                lambda source_id: prior_decisions.get(source_id),
                revision_row["parent_policy_revision_id"] is None,
            )
            # The pre-write recheck: the pointer must still name the target
            # revision and every planned subject sequence must still match,
            # otherwise nothing is written and the retry replans. A page with
            # no evaluable source (every sequence absent) plans nothing, so
            # the sequence recheck is skipped exactly like the writes below —
            # its statement builder rejects an empty source set.
            if not await self._revision_still_active(connection, workspace_id, policy_revision_id):
                return self._superseded_outcome()
            if planned:
                recheck_rows = (
                    (
                        await connection.execute(
                            batch_sequences_select_statement(
                                workspace_id, [item.evaluation.source_id for item in planned]
                            )
                        )
                    )
                    .mappings()
                    .all()
                )
                verify_planned_batch_sequences(
                    {
                        item.evaluation.source_id: item.evaluation.subject_event_sequence
                        for item in planned
                    },
                    list(recheck_rows),
                )
            await self._write_evaluations(connection, workspace_id, policy_revision_id, planned)
            await self._write_transition_intents(
                connection, workspace_id, policy_revision_id, planned
            )
            counters = _counters_of(planned)
        # Post-commit diagnostics only: closed transition counters and one
        # heartbeat per committed batch (spec 15/21).
        self._record_transition_metrics(counters)
        if heartbeat is not None:
            await heartbeat(
                ReconciliationProgress(evaluated_sources=counters.evaluated_sources, batch_count=1)
            )
        return ReconciliationBatchOutcome(
            superseded=False,
            has_more=len(page) == RECONCILIATION_BATCH_SIZE,
            last_source_id=page[-1]["source_id"],
            evaluated_sources=counters.evaluated_sources,
            to_excluded_sources=counters.to_excluded_sources,
            to_allowed_sources=counters.to_allowed_sources,
            unchanged_sources=counters.unchanged_sources,
        )

    @staticmethod
    async def _load_revision_rules(
        connection: AsyncConnection, policy_revision_id: UUID
    ) -> tuple[Any, ...]:
        rule_rows = (
            (
                await connection.execute(
                    sa.select(
                        policy_rules.c.rule_id,
                        policy_rules.c.rule_kind,
                        policy_rules.c.source_id_operand,
                        policy_rules.c.text_operand,
                        policy_rules.c.size_bytes_operand,
                        policy_rules.c.semantic_fingerprint,
                    )
                    .where(policy_rules.c.policy_revision_id == policy_revision_id)
                    .order_by(policy_rules.c.rule_id)
                )
            )
            .mappings()
            .all()
        )
        return hydrate_policy_revision_rules(list(rule_rows))

    @staticmethod
    def _evaluate_page(
        workspace_id: UUID,
        policy_revision_id: UUID,
        revision: ExclusionPolicyRevision,
        page: Sequence[_MappedRow],
    ) -> list[_EvaluatedSubject]:
        """Evaluate every source with a canonical event into planned evidence."""

        evaluated: list[_EvaluatedSubject] = []
        for row in page:
            sequence = row["subject_event_sequence"]
            if sequence is None:
                # A source without any canonical event has no subject state to
                # evaluate; the canonical flow never produces one.
                continue
            subject = preview_subject_for_row(workspace_id, row)
            outcome = evaluate_policy(revision=revision, subject=subject)
            if subject.source_id is None:  # pragma: no cover - the scan binds it
                raise InternalApplicationError(ErrorCode.INTERNAL_ERROR)
            evaluated.append(
                _EvaluatedSubject(
                    evaluation=PolicyEvaluation(
                        policy_revision_id=policy_revision_id,
                        source_id=subject.source_id,
                        subject_event_sequence=int(sequence),
                        raw_decision=outcome.raw,
                        enforced_decision=outcome.enforced,
                    ),
                    subject_fingerprint=compute_subject_fingerprint(subject),
                    matched_rule_ids=outcome.matched_rule_ids,
                    missing_fields=outcome.missing_fields,
                    current_version_id=row["current_version_id"],
                )
            )
        return evaluated

    @staticmethod
    def _derive_transitions(
        evaluated: Sequence[_EvaluatedSubject],
        prior_decision_of: Callable[[UUID], EnforcedPolicyDecision | None],
        parent_revision_missing: bool,
    ) -> list[_PlannedSubject]:
        """Derive each previous/proposed transition over the closed fallbacks.

        The parent revision's recorded evaluation rows win; a source without
        one was effectively allowed (it exists only after passing
        enforcement), and the first publication — no parent at all — compares
        against the fail-closed no-policy deny.
        """

        planned: list[_PlannedSubject] = []
        for item in evaluated:
            previous: EnforcedPolicyDecision
            if parent_revision_missing:
                previous = previous_enforced_without_policy()
            else:
                recorded = prior_decision_of(item.evaluation.source_id)
                if recorded is None:
                    previous = previous_enforced_without_prior_evaluation()
                else:
                    previous = recorded
            planned.append(
                _PlannedSubject(
                    evaluation=item.evaluation,
                    subject_fingerprint=item.subject_fingerprint,
                    matched_rule_ids=item.matched_rule_ids,
                    missing_fields=item.missing_fields,
                    current_version_id=item.current_version_id,
                    previous_enforced=previous,
                    transition=derive_reconciliation_transition(
                        previous_enforced=previous,
                        proposed_enforced=item.evaluation.enforced_decision,
                    ),
                )
            )
        return planned

    async def _load_prior_decisions(
        self,
        connection: AsyncConnection,
        parent_policy_revision_id: UUID | None,
        source_ids: Sequence[UUID],
    ) -> dict[UUID, EnforcedPolicyDecision]:
        """Load the parent revision's latest enforced decision per source."""

        if parent_policy_revision_id is None or not source_ids:
            return {}
        rows = (
            (
                await connection.execute(
                    prior_evaluations_select_statement(parent_policy_revision_id, source_ids)
                )
            )
            .mappings()
            .all()
        )
        return {row["source_id"]: EnforcedPolicyDecision(row["enforced_decision"]) for row in rows}

    async def _revision_still_active(
        self, connection: AsyncConnection, workspace_id: UUID, policy_revision_id: UUID
    ) -> bool:
        result = await connection.execute(active_revision_select_statement(workspace_id))
        return result.scalar_one_or_none() == policy_revision_id  # type: ignore[no-any-return]

    async def _write_evaluations(
        self,
        connection: AsyncConnection,
        workspace_id: UUID,
        policy_revision_id: UUID,
        planned: Sequence[_PlannedSubject],
    ) -> None:
        """Insert or verify the exact immutable evaluation identities."""

        if not planned:
            return
        rows = [
            build_policy_evaluation_row_values(
                policy_evaluation_id=uuid7(),
                workspace_id=workspace_id,
                policy_revision_id=policy_revision_id,
                source_id=item.evaluation.source_id,
                subject_event_sequence=item.evaluation.subject_event_sequence,
                raw_decision=item.evaluation.raw_decision,
                enforced_decision=item.evaluation.enforced_decision,
                matched_rule_ids=item.matched_rule_ids,
                missing_fields=item.missing_fields,
                subject_fingerprint=item.subject_fingerprint,
                evaluated_at=None,
            )
            for item in planned
        ]
        await connection.execute(policy_evaluations_insert_statement(rows))
        stored_rows = (
            (
                await connection.execute(
                    policy_evaluations_verify_select_statement(
                        policy_revision_id, [item.evaluation.source_id for item in planned]
                    )
                )
            )
            .mappings()
            .all()
        )
        stored = {
            (row["source_id"], int(row["subject_event_sequence"])): row for row in stored_rows
        }
        for item in planned:
            row = stored.get((item.evaluation.source_id, item.evaluation.subject_event_sequence))
            if row is None:
                raise InternalApplicationError(ErrorCode.INTERNAL_ERROR)
            if (
                str(row["raw_decision"]) != item.evaluation.raw_decision.value
                or str(row["enforced_decision"]) != item.evaluation.enforced_decision.value
                or str(row["subject_fingerprint"]) != item.subject_fingerprint
            ):
                # An exact replay verifies equality and never overwrites a
                # different result; a different result under the immutable
                # identity is impossible corruption.
                raise InternalApplicationError(ErrorCode.INTERNAL_ERROR)

    async def _write_transition_intents(
        self,
        connection: AsyncConnection,
        workspace_id: UUID,
        policy_revision_id: UUID,
        planned: Sequence[_PlannedSubject],
    ) -> None:
        """Insert the deterministic policy-transition intents and verify them.

        Only sources with a non-null current version receive intents; the
        deterministic identity is ``(revision, source, projection_kind)`` so
        ``ON CONFLICT DO NOTHING`` keeps the first intent of a replay and the
        verification proves the stored operation matches the derived one. A
        newer source version on a later batch does not overwrite the first
        intent: the projection consumer rechecks the then-active policy and
        source version before every write, so a stale pending upsert cannot
        reintroduce denied content.
        """

        rows: list[dict[str, Any]] = []
        planned_intents: list[
            tuple[UUID, PolicyTransitionProjectionKind, PolicyTransitionOperation]
        ] = []
        for item in planned:
            if item.current_version_id is None:
                continue
            for plan in policy_transition_intent_plans(item.transition, has_current_version=True):
                rows.append(
                    build_policy_transition_intent_values(
                        projection_intent_id=uuid7(),
                        workspace_id=workspace_id,
                        policy_revision_id=policy_revision_id,
                        source_id=item.evaluation.source_id,
                        source_version_id=item.current_version_id,
                        projection_kind=plan.projection_kind.value,
                        operation=plan.operation.value,
                        available_at=None,
                    )
                )
                planned_intents.append(
                    (item.evaluation.source_id, plan.projection_kind, plan.operation)
                )
        if not rows:
            return
        await connection.execute(policy_transition_intent_insert_statement(rows))
        stored_rows = (
            (
                await connection.execute(
                    policy_transition_intent_verify_select_statement(
                        policy_revision_id,
                        list({source_id for source_id, _kind, _operation in planned_intents}),
                    )
                )
            )
            .mappings()
            .all()
        )
        stored = {(row["source_id"], str(row["projection_kind"])): row for row in stored_rows}
        for source_id, kind, operation in planned_intents:
            row = stored.get((source_id, kind.value))
            if row is None or str(row["operation"]) != operation.value:
                raise InternalApplicationError(ErrorCode.INTERNAL_ERROR)

    @staticmethod
    def _superseded_outcome() -> ReconciliationBatchOutcome:
        return ReconciliationBatchOutcome(
            superseded=True,
            has_more=False,
            last_source_id=None,
            evaluated_sources=0,
            to_excluded_sources=0,
            to_allowed_sources=0,
            unchanged_sources=0,
        )

    def _record_transition_metrics(self, counters: ReconciliationCounters) -> None:
        if self._metrics is None:
            return
        for transition, count in (
            (ReconciliationTransition.TO_EXCLUDED, counters.to_excluded_sources),
            (ReconciliationTransition.TO_ALLOWED, counters.to_allowed_sources),
            (ReconciliationTransition.UNCHANGED, counters.unchanged_sources),
        ):
            if count > 0:
                self._metrics.record_sources(transition=transition, count=int(count))

    # --- completion and failure --------------------------------------------------------

    async def complete_reconciliation(
        self,
        workspace_id: UUID,
        policy_revision_id: UUID,
        counters: ReconciliationCounters,
        context: DiagnosticContext | None = None,
    ) -> bool:
        """Write the idempotent completion audit row and record the lag.

        Completion is durable evidence, not a state flip: the intent already
        rests at ``dispatched`` with the workflow owning it. An existing
        completion row acknowledges the replay without a second audit row or
        lag reading; the lag records only after the fresh durable row commits.
        """

        del context  # Correlation flows through the workflow identity; no per-row fields.
        written = await self._retry.run(
            lambda _attempt: self._complete_reconciliation_once(
                workspace_id, policy_revision_id, counters
            )
        )
        if written is None:
            return False
        if self._metrics is not None:
            self._metrics.record_lag(lag_seconds=written)
        return True

    async def _complete_reconciliation_once(
        self,
        workspace_id: UUID,
        policy_revision_id: UUID,
        counters: ReconciliationCounters,
    ) -> float | None:
        async with (
            self._engine.connect() as connection,
            connection.begin(),
        ):
            await apply_transaction_bounds(connection)
            existing = await connection.execute(
                completion_audit_exists_select_statement(policy_revision_id)
            )
            if existing.one_or_none() is not None:
                return None
            revision_row = (
                (
                    await connection.execute(
                        revision_identity_select_statement(workspace_id, policy_revision_id)
                    )
                )
                .mappings()
                .first()
            )
            if revision_row is None:
                raise ExclusionPolicyError(ErrorCode.EXCLUSION_POLICY_NOT_INITIALIZED)
            occurred_at = await self._select_now(connection)
            published_at = revision_row["published_at"]
            await connection.execute(
                sa.insert(audit_events).values(
                    **build_reconciliation_audit_values(
                        action=RECONCILIATION_COMPLETED_AUDIT_ACTION,
                        workspace_id=workspace_id,
                        policy_revision_id=policy_revision_id,
                        result=AUDIT_RESULT_SUCCEEDED,
                        reason_code=None,
                        evaluated_sources=counters.evaluated_sources,
                        to_excluded_sources=counters.to_excluded_sources,
                        to_allowed_sources=counters.to_allowed_sources,
                        unchanged_sources=counters.unchanged_sources,
                        occurred_at=occurred_at,
                        request_id=uuid7(),
                    )
                )
            )
            return max(0.0, float((occurred_at - published_at).total_seconds()))

    async def fail_reconciliation(
        self,
        workspace_id: UUID,
        policy_revision_id: UUID,
        error_code: SafeToken,
        *,
        retryable: bool,
    ) -> bool:
        """Durably release the revision's intent after a workflow failure.

        A retryable dependency failure returns the row to ``pending`` with
        bounded backoff (spec 22); a non-retryable contract failure marks it
        terminal. The failure audit row lands in the same transaction as the
        durable transition, so the diagnostic exists only after the state
        change is durable.
        """

        _require_column_safe_error_code(error_code)
        return await self._retry.run(
            lambda _attempt: self._fail_reconciliation_once(
                workspace_id, policy_revision_id, error_code, retryable
            )
        )

    async def _fail_reconciliation_once(
        self,
        workspace_id: UUID,
        policy_revision_id: UUID,
        error_code: SafeToken,
        retryable: bool,
    ) -> bool:
        async with (
            self._engine.connect() as connection,
            connection.begin(),
        ):
            await apply_transaction_bounds(connection)
            occurred_at = await self._select_now(connection)
            transitioned = await connection.execute(
                fail_dispatched_statement(
                    workspace_id, policy_revision_id, error_code, retryable=retryable
                )
            )
            if transitioned.rowcount != 1:
                return False
            await connection.execute(
                sa.insert(audit_events).values(
                    **build_reconciliation_audit_values(
                        action=RECONCILIATION_FAILED_AUDIT_ACTION,
                        workspace_id=workspace_id,
                        policy_revision_id=policy_revision_id,
                        result=AUDIT_RESULT_FAILED,
                        reason_code=error_code.value,
                        evaluated_sources=0,
                        to_excluded_sources=0,
                        to_allowed_sources=0,
                        unchanged_sources=0,
                        occurred_at=occurred_at,
                        request_id=uuid7(),
                    )
                )
            )
            return True

    @staticmethod
    async def _select_now(connection: AsyncConnection) -> datetime:
        """Read the transaction-stable timestamp shared by every written row."""

        result = await connection.execute(sa.text("SELECT now()"))
        occurred_at = result.scalar_one()
        if not isinstance(occurred_at, datetime):  # pragma: no cover - driver contract
            raise TypeError("SELECT now() did not return a datetime")
        return occurred_at


@dataclass(frozen=True, slots=True)
class _EvaluatedSubject:
    """One evaluated source before transition derivation."""

    evaluation: PolicyEvaluation
    subject_fingerprint: str
    matched_rule_ids: tuple[UUID, ...]
    missing_fields: tuple[PolicySubjectField, ...]
    current_version_id: UUID | None


def _counters_of(planned: Sequence[_PlannedSubject]) -> ReconciliationCounters:
    counters = ReconciliationCounters()
    for item in planned:
        counters = counters.record(item.transition)
    return counters


__all__ = [
    "AUDIT_RESULT_FAILED",
    "AUDIT_RESULT_SUCCEEDED",
    "POLICY_RECONCILIATION_AUDIT_TARGET_KIND",
    "POLICY_RECONCILIATION_BACKOFF_CAP_SECONDS",
    "POLICY_RECONCILIATION_CLAIM_BATCH_LIMIT",
    "POLICY_RECONCILIATION_LEASE_SECONDS",
    "RECONCILIATION_COMPLETED_AUDIT_ACTION",
    "RECONCILIATION_COMPLETION_DIGEST_CONTRACT",
    "RECONCILIATION_DISPATCH_TERMINAL_ERROR_CODE",
    "RECONCILIATION_EXECUTION_FAILED_ERROR_CODE",
    "RECONCILIATION_FAILED_AUDIT_ACTION",
    "RECONCILIATION_LEASE_EXPIRED_ERROR_CODE",
    "LeasedPolicyReconciliation",
    "PostgresqlPolicyReconciliationStore",
    "ReconciliationBatchOutcome",
    "acknowledge_dispatched_statement",
    "active_revision_select_statement",
    "batch_sequences_select_statement",
    "build_policy_evaluation_row_values",
    "build_policy_transition_intent_values",
    "build_reconciliation_audit_values",
    "claim_pending_reconciliations_select_statement",
    "completion_audit_exists_select_statement",
    "compute_completion_counters_digest",
    "fail_dispatched_statement",
    "lease_reconciliation_update_statement",
    "map_reconciliation_database_failure",
    "mark_terminal_statement",
    "policy_evaluations_insert_statement",
    "policy_evaluations_verify_select_statement",
    "policy_transition_intent_insert_statement",
    "policy_transition_intent_verify_select_statement",
    "prior_evaluations_select_statement",
    "reclaim_lease_update_statement",
    "reconciliation_batch_select_statement",
    "release_retry_statement",
    "revision_identity_select_statement",
    "verify_planned_batch_sequences",
]
