"""Exclusion-policy draft persistence: exact-version replacement over PostgreSQL.

:class:`PostgresqlPolicyDraftStore` implements the durable
:class:`~personal_os.exclusion_policy.ports.PolicyDraftStore` and
``PolicyQueryStore`` ports over the migrated policy schema.
``replace_rules`` runs one ``READ COMMITTED`` transaction behind the pinned
``SET LOCAL`` bounds: the draft row is selected ``FOR UPDATE``, the exact
``expected_draft_version`` is compared under the lock, the complete child
rule set is replaced (one typed operand column per closed rule kind, with
the Task 3 database CHECKs as the final operand-shape guard), the guarded
``UPDATE`` increments the version exactly once with the database
transaction timestamp, exactly the ``ready`` previews bound to the prior
draft version are expired, and the ``exclusion_policy.draft_replaced``
audit row — identifiers, result and the draft semantic digest only — joins
the same commit. A stale version raises the typed draft conflict carrying
only ``current_draft_version``; a missing graph raises the typed
not-initialized error; nothing about another workspace is ever disclosed.

Driver failures are classified through the existing safe database error
classifier and mapped onto the closed policy error registry, with bounded
contention retry and an evidence-based recovery lookup for the
uncertain-commit case: a recovered replacement must show the exact
incremented version and the identical rule-set digest before it is returned
(never a guess). SQLSTATE values, SQL, parameters and driver text never
leave the adapter.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final
from uuid import UUID, uuid7

import sqlalchemy as sa
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from personal_os.diagnostics.context import DiagnosticContext
from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import ApplicationError, InternalApplicationError
from personal_os.exclusion_policy.contracts import (
    ExactSourceIdOperand,
    ExclusionRule,
    ExtensionOperand,
    FolderPrefixOperand,
    MaximumSizeOperand,
    MediaTypeOperand,
    PathGlobOperand,
    RuleKind,
    SourceTypeOperand,
)
from personal_os.exclusion_policy.drafts import compute_draft_semantic_sha256
from personal_os.exclusion_policy.errors import ExclusionPolicyError
from personal_os.exclusion_policy.normalization import normalize_rule
from personal_os.exclusion_policy.ports import (
    PolicyActor,
    PolicyDraft,
    PolicyStatus,
)
from postgresql_source_store.engine import apply_transaction_bounds
from postgresql_source_store.error_mapping import (
    RETRY_JITTER_MAXIMUM_SECONDS,
    RETRY_JITTER_MINIMUM_SECONDS,
    DatabaseFailureKind,
    classify_database_failure,
)
from postgresql_source_store.tables import (
    audit_events,
    policy_draft_rules,
    policy_drafts,
    policy_previews,
    workspace_policy_state,
)

#: Audit-row literals for the in-transaction draft-replacement audit.
DRAFT_REPLACED_AUDIT_ACTION: Final[str] = "exclusion_policy.draft_replaced"
POLICY_DRAFT_AUDIT_TARGET_KIND: Final[str] = "policy_draft"
AUDIT_RESULT_SUCCEEDED: Final[str] = "succeeded"

#: Closed preview lifecycle literals written by the invalidation transition.
PREVIEW_STATE_READY: Final[str] = "ready"
PREVIEW_STATE_EXPIRED: Final[str] = "expired"

#: One row of a draft/rules read: a SQLAlchemy row mapping from the
#: adapter's ``.mappings()`` results or an equivalent mapping in tests.
type _MappedRow = RowMapping | Mapping[str, Any]


def map_policy_database_failure(cause: BaseException) -> ApplicationError:
    """Map a database or driver failure onto the closed policy error registry.

    Classification reuses the existing safe SQLSTATE classifier; only the
    terminal mapping is policy-specific. Contention exhausted after the
    bounded retries and any unclassified or unavailable failure map to the
    retryable ``exclusion_policy_commit_outcome_unknown``, which carries no
    safe details; a non-database exception is an internal bug and crosses
    the boundary as ``internal_error``. The cause remains chained only; its
    text never enters the mapped error.
    """

    failure_kind = classify_database_failure(cause)
    if failure_kind is DatabaseFailureKind.NOT_DATABASE:
        return InternalApplicationError(ErrorCode.INTERNAL_ERROR)
    return ExclusionPolicyError(ErrorCode.EXCLUSION_POLICY_COMMIT_OUTCOME_UNKNOWN)


@dataclass(frozen=True, slots=True)
class PolicyDatabaseRetryPolicy:
    """Bounded retry for policy stores over the shared contention classifier.

    At most ``maximum_attempts`` attempts run with the shared cancellable
    50-250 ms jitter. Typed application errors pass through untouched; a
    write transaction may supply ``recover`` for the uncertain-commit case:
    a connection-class failure resolves through the fresh-connection lookup
    only, a proven recovery is returned, a proven absence retries, and an
    unavailable lookup raises the retryable unknown-outcome error without
    ever claiming a rollback. Every other database failure — including
    integrity violations returned on a healthy connection — proves a
    deterministic rollback and maps immediately.
    """

    maximum_attempts: int = 3

    async def run[T](
        self,
        operation: Callable[[int], Awaitable[T]],
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[float, float], float] = random.uniform,
        recover: Callable[[], Awaitable[T | None]] | None = None,
    ) -> T:
        for attempt in range(1, self.maximum_attempts + 1):
            try:
                return await operation(attempt)
            except ApplicationError:
                raise
            except Exception as cause:
                failure_kind = classify_database_failure(cause)
                if failure_kind is DatabaseFailureKind.NOT_DATABASE:
                    raise map_policy_database_failure(cause) from cause
                if failure_kind is DatabaseFailureKind.CONTENTION:
                    if attempt == self.maximum_attempts:
                        raise map_policy_database_failure(cause) from cause
                elif failure_kind is DatabaseFailureKind.UNAVAILABLE and recover is not None:
                    recovered = await self._resolve_uncertain_outcome(recover)
                    if recovered is not None:
                        return recovered
                    if attempt == self.maximum_attempts:
                        raise map_policy_database_failure(cause) from cause
                else:
                    raise map_policy_database_failure(cause) from cause
                await sleep(jitter(RETRY_JITTER_MINIMUM_SECONDS, RETRY_JITTER_MAXIMUM_SECONDS))
        raise AssertionError("retry loop exhausted without a result")

    @staticmethod
    async def _resolve_uncertain_outcome[T](recover: Callable[[], Awaitable[T | None]]) -> T | None:
        """Run the fresh-connection outcome lookup for an ambiguous commit.

        A typed application error propagates untouched; any other lookup
        failure means PostgreSQL could not prove presence or absence, so the
        outcome stays unknown and retryable — a rollback is never claimed
        without evidence.
        """

        try:
            return await recover()
        except ApplicationError:
            raise
        except Exception as lookup_cause:
            raise ExclusionPolicyError(
                ErrorCode.EXCLUSION_POLICY_COMMIT_OUTCOME_UNKNOWN
            ) from lookup_cause


def draft_lock_statement(draft_id: UUID) -> sa.Select[tuple[Any, ...]]:
    """Build the schema-qualified, parameter-bound ``FOR UPDATE`` draft lock."""

    return (
        sa.select(
            policy_drafts.c.workspace_id,
            policy_drafts.c.draft_version,
            policy_drafts.c.base_policy_revision_id,
        )
        .where(policy_drafts.c.policy_draft_id == draft_id)
        .with_for_update()
    )


def build_draft_rule_values(draft_id: UUID, rule: ExclusionRule) -> dict[str, Any]:
    """Map one domain rule onto its row values with one typed operand column.

    The mapping mirrors the closed kind-to-column grammar the Task 3 CHECK
    constraints enforce: ``exact_source_id`` populates ``source_id_operand``,
    the five text kinds populate ``text_operand``, ``maximum_size`` populates
    ``size_bytes_operand``, and every other column stays null.
    """

    operand = rule.operand
    source_id_operand: UUID | None = None
    text_operand: str | None = None
    size_bytes_operand: int | None = None
    if isinstance(operand, ExactSourceIdOperand):
        source_id_operand = operand.source_id
    elif isinstance(operand, (FolderPrefixOperand, PathGlobOperand)):
        text_operand = (
            operand.folder_prefix
            if isinstance(operand, FolderPrefixOperand)
            else operand.normalized_pattern
        )
    elif isinstance(operand, ExtensionOperand):
        text_operand = operand.extension
    elif isinstance(operand, MediaTypeOperand):
        exact = operand.exact_media_type
        text_operand = exact.value if exact is not None else f"{operand.family_type}/*"
    elif isinstance(operand, MaximumSizeOperand):
        size_bytes_operand = operand.maximum_size_bytes
    else:
        assert isinstance(operand, SourceTypeOperand)
        text_operand = operand.source_type.value
    return {
        "policy_draft_id": draft_id,
        "rule_id": rule.rule_id,
        "rule_kind": rule.rule_kind.value,
        "source_id_operand": source_id_operand,
        "text_operand": text_operand,
        "size_bytes_operand": size_bytes_operand,
        "semantic_fingerprint": rule.semantic_fingerprint,
    }


def hydrate_policy_draft(draft_row: _MappedRow, rule_rows: Sequence[_MappedRow]) -> PolicyDraft:
    """Build the immutable draft from mapped rows through the sanctioned rule path.

    Every stored rule re-enters the domain through :func:`normalize_rule`,
    so a row outside the closed kind grammar, a wrong operand shape or a
    duplicate rule ID — all impossible through the store and prevented by
    the Task 3 CHECKs — fails closed as the public ``internal_error`` with
    the cause chained only.
    """

    try:
        rules = tuple(
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
        return PolicyDraft(
            draft_id=draft_row["policy_draft_id"],
            workspace_id=draft_row["workspace_id"],
            draft_version=int(draft_row["draft_version"]),
            base_policy_revision_id=draft_row["base_policy_revision_id"],
            rules=rules,
        )
    except (ExclusionPolicyError, ValueError, TypeError) as cause:
        raise InternalApplicationError(ErrorCode.INTERNAL_ERROR) from cause


def draft_conflict_error(current_draft_version: int) -> ExclusionPolicyError:
    """Build the typed stale-version conflict with only the current version."""

    return ExclusionPolicyError(
        ErrorCode.EXCLUSION_POLICY_DRAFT_CONFLICT,
        safe_details={"current_draft_version": current_draft_version},
    )


def draft_not_initialized_error() -> ExclusionPolicyError:
    """Build the typed missing-policy-graph error carrying no safe details."""

    return ExclusionPolicyError(ErrorCode.EXCLUSION_POLICY_NOT_INITIALIZED)


def expire_ready_previews_statement(draft_id: UUID, prior_draft_version: int) -> sa.Update:
    """Build the ready-preview expiry for the prior draft version.

    Only ``ready`` previews bound to the replaced draft and exactly the
    prior version expire; pending, leased and running previews finish and
    become stale through their own version binding, and previews of older
    versions were already expired by their owning edit.
    """

    return (
        sa.update(policy_previews)
        .values(state=PREVIEW_STATE_EXPIRED)
        .where(
            policy_previews.c.policy_draft_id == draft_id,
            policy_previews.c.draft_version == prior_draft_version,
            policy_previews.c.state == PREVIEW_STATE_READY,
        )
    )


def build_draft_replaced_audit_values(
    *,
    policy_draft_id: UUID,
    workspace_id: UUID,
    actor: PolicyActor,
    safe_diff_hash: str,
    occurred_at: datetime,
    request_id: UUID,
    client_request_id: UUID | None,
    trace_id: str | None,
) -> dict[str, Any]:
    """Build the ``exclusion_policy.draft_replaced`` audit-row values.

    The row carries identifiers, the closed actor/action/result literals
    and the draft semantic digest only: rule operands, locators and counts
    never enter the audit table (spec 21).
    """

    return {
        "audit_event_id": uuid7(),
        "workspace_id": workspace_id,
        "actor_kind": actor.actor_kind.value,
        "actor_id": actor.user_id,
        "actor_reference": None,
        "action": DRAFT_REPLACED_AUDIT_ACTION,
        "target_kind": POLICY_DRAFT_AUDIT_TARGET_KIND,
        "target_id": policy_draft_id,
        "request_id": request_id,
        "client_request_id": client_request_id,
        "trace_id": trace_id,
        "result": AUDIT_RESULT_SUCCEEDED,
        "reason_code": None,
        "safe_diff_hash": safe_diff_hash,
        "occurred_at": occurred_at,
    }


def matches_recovered_replacement(
    draft: PolicyDraft, expected_draft_version: int, rules: tuple[ExclusionRule, ...]
) -> bool:
    """Prove a recovered draft is exactly the requested replacement.

    Only the exact incremented version together with the identical rule-set
    digest counts as evidence that an uncertain commit landed; anything else
    is absence and the bounded retry decides the outcome.
    """

    return draft.draft_version == expected_draft_version + 1 and (
        compute_draft_semantic_sha256(draft.rules) == compute_draft_semantic_sha256(rules)
    )


class PostgresqlPolicyDraftStore:
    """Durable policy draft and status store over the canonical baseline.

    The store takes the composition-owned :class:`AsyncEngine`; it opens no
    connection at construction. Reads run one bounded transaction; the
    replacement runs the locked compare-and-swap transition with the
    recovery lookup wired into the bounded retry for the uncertain-commit
    case.
    """

    def __init__(
        self, engine: AsyncEngine, *, retry: PolicyDatabaseRetryPolicy | None = None
    ) -> None:
        self._engine = engine
        self._retry = retry if retry is not None else PolicyDatabaseRetryPolicy()

    async def load_draft(self, workspace_id: UUID, context: DiagnosticContext) -> PolicyDraft:
        return await self._retry.run(lambda _attempt: self._load_draft_once(workspace_id, context))

    async def get_policy_status(
        self, workspace_id: UUID, context: DiagnosticContext
    ) -> PolicyStatus:
        return await self._retry.run(
            lambda _attempt: self._get_policy_status_once(workspace_id, context)
        )

    async def replace_rules(
        self,
        draft_id: UUID,
        expected_draft_version: int,
        rules: tuple[ExclusionRule, ...],
        actor: PolicyActor,
        context: DiagnosticContext,
    ) -> PolicyDraft:
        return await self._retry.run(
            lambda _attempt: self._replace_rules_once(
                draft_id, expected_draft_version, rules, actor, context
            ),
            recover=lambda: self._recover_replacement(draft_id, expected_draft_version, rules),
        )

    async def _load_draft_once(self, workspace_id: UUID, context: DiagnosticContext) -> PolicyDraft:
        async with (
            self._engine.connect() as connection,
            connection.begin(),
        ):
            await apply_transaction_bounds(connection)
            draft_row = await self._select_draft_row(connection, workspace_id=workspace_id)
            if draft_row is None:
                raise draft_not_initialized_error()
            rule_rows = await self._select_rule_rows(connection, draft_row["policy_draft_id"])
        return hydrate_policy_draft(draft_row, rule_rows)

    async def _get_policy_status_once(
        self, workspace_id: UUID, context: DiagnosticContext
    ) -> PolicyStatus:
        async with (
            self._engine.connect() as connection,
            connection.begin(),
        ):
            await apply_transaction_bounds(connection)
            state_result = await connection.execute(
                sa.select(
                    workspace_policy_state.c.active_policy_revision_id,
                    workspace_policy_state.c.active_revision_number,
                ).where(workspace_policy_state.c.workspace_id == workspace_id)
            )
            state_row = state_result.mappings().first()
            if state_row is None:
                raise draft_not_initialized_error()
            draft_row = await self._select_draft_row(connection, workspace_id=workspace_id)
            if draft_row is None:
                # Bootstrap creates the state row and the draft together, so
                # a missing draft under an existing state row is corruption.
                raise InternalApplicationError(ErrorCode.INTERNAL_ERROR)
            rule_rows = await self._select_rule_rows(connection, draft_row["policy_draft_id"])
        draft = hydrate_policy_draft(draft_row, rule_rows)
        return PolicyStatus(
            workspace_id=workspace_id,
            active_policy_revision_id=state_row["active_policy_revision_id"],
            active_revision_number=int(state_row["active_revision_number"]),
            draft=draft,
        )

    async def _replace_rules_once(
        self,
        draft_id: UUID,
        expected_draft_version: int,
        rules: tuple[ExclusionRule, ...],
        actor: PolicyActor,
        context: DiagnosticContext,
    ) -> PolicyDraft:
        async with (
            self._engine.connect() as connection,
            connection.begin(),
        ):
            await apply_transaction_bounds(connection)
            locked = await self._select_locked_draft(connection, draft_id)
            if locked is None:
                raise draft_not_initialized_error()
            workspace_id, current_version, base_policy_revision_id = locked
            if current_version != expected_draft_version:
                raise draft_conflict_error(current_version)
            occurred_at = await self._select_now(connection)
            await connection.execute(
                sa.delete(policy_draft_rules).where(
                    policy_draft_rules.c.policy_draft_id == draft_id
                )
            )
            if rules:
                await connection.execute(
                    sa.insert(policy_draft_rules).values(
                        [build_draft_rule_values(draft_id, rule) for rule in rules]
                    )
                )
            guarded = await connection.execute(
                sa.update(policy_drafts)
                .values(
                    draft_version=expected_draft_version + 1,
                    updated_by_user_id=actor.user_id,
                    updated_at=occurred_at,
                )
                .where(
                    policy_drafts.c.policy_draft_id == draft_id,
                    policy_drafts.c.draft_version == expected_draft_version,
                )
            )
            if guarded.rowcount != 1:
                # Impossible under the row lock; fail closed as corruption.
                raise InternalApplicationError(ErrorCode.INTERNAL_ERROR)
            await connection.execute(
                expire_ready_previews_statement(draft_id, expected_draft_version)
            )
            await connection.execute(
                sa.insert(audit_events).values(
                    **build_draft_replaced_audit_values(
                        policy_draft_id=draft_id,
                        workspace_id=workspace_id,
                        actor=actor,
                        safe_diff_hash=compute_draft_semantic_sha256(rules),
                        occurred_at=occurred_at,
                        request_id=context.request_id,
                        client_request_id=context.client_request_id,
                        trace_id=context.trace.trace_id.value,
                    )
                )
            )
        return PolicyDraft(
            draft_id=draft_id,
            workspace_id=workspace_id,
            draft_version=expected_draft_version + 1,
            base_policy_revision_id=base_policy_revision_id,
            rules=rules,
        )

    async def _recover_replacement(
        self,
        draft_id: UUID,
        expected_draft_version: int,
        rules: tuple[ExclusionRule, ...],
    ) -> PolicyDraft | None:
        """Prove or disprove that an uncertain replacement commit landed.

        The proof reuses the recovery path of the shared retry runner: the
        lookup runs on a fresh connection and only the exact incremented
        version with the identical rule-set digest counts as a committed
        replacement.
        """

        draft = await self._load_draft_by_id(draft_id)
        if draft is not None and matches_recovered_replacement(
            draft, expected_draft_version, rules
        ):
            return draft
        return None

    async def _load_draft_by_id(self, draft_id: UUID) -> PolicyDraft | None:
        async with (
            self._engine.connect() as connection,
            connection.begin(),
        ):
            await apply_transaction_bounds(connection)
            draft_row = await self._select_draft_row(connection, draft_id=draft_id)
            if draft_row is None:
                return None
            rule_rows = await self._select_rule_rows(connection, draft_id)
        return hydrate_policy_draft(draft_row, rule_rows)

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
    async def _select_rule_rows(connection: AsyncConnection, draft_id: UUID) -> list[_MappedRow]:
        result = await connection.execute(
            sa.select(
                policy_draft_rules.c.rule_id,
                policy_draft_rules.c.rule_kind,
                policy_draft_rules.c.source_id_operand,
                policy_draft_rules.c.text_operand,
                policy_draft_rules.c.size_bytes_operand,
                policy_draft_rules.c.semantic_fingerprint,
            )
            .where(policy_draft_rules.c.policy_draft_id == draft_id)
            .order_by(policy_draft_rules.c.rule_id)
        )
        return list(result.mappings().all())

    @staticmethod
    async def _select_locked_draft(
        connection: AsyncConnection, draft_id: UUID
    ) -> tuple[UUID, int, UUID | None] | None:
        result = await connection.execute(draft_lock_statement(draft_id))
        row = result.one_or_none()
        if row is None:
            return None
        return row.workspace_id, int(row.draft_version), row.base_policy_revision_id

    @staticmethod
    async def _select_now(connection: AsyncConnection) -> datetime:
        """Read the transaction-stable timestamp shared by every written row."""

        result = await connection.execute(sa.text("SELECT now()"))
        occurred_at = result.scalar_one()
        if not isinstance(occurred_at, datetime):  # pragma: no cover - driver contract
            raise TypeError("SELECT now() did not return a datetime")
        return occurred_at
