"""PostgreSQL half of mandatory exclusion-policy enforcement (spec 14).

This module provides the store-side collaborators the guarded canonical
boundaries compose: the engine-backed
:class:`PostgresqlActivePolicySnapshotSource` and
:class:`PostgresqlPolicySubjectEvidenceSource` port adapters of the domain
enforcement service, and — the authoritative piece —
:func:`load_locked_active_policy_snapshot` plus
:func:`authorize_locked_publication_policy` and
:func:`evaluate_locked_policy_decision`, which lock the
``workspace_policy_state`` row ``FOR UPDATE``, load the active revision's
signed snapshot joined with its persisted trust anchor, and re-evaluate the
authoritative subject through the domain verify/parse/evaluate path. Source
publication may reuse matching bound allowed evidence only after verifying
that locked snapshot; changed revisions and ordinary decisions evaluate
unconditionally. Publication invokes the check between the idempotency recheck and the
source advisory lock; canonical read invokes it inside the same transaction
that resolves the current source state, so no object-store request can be
issued before the active policy permitted the subject.

A missing policy-state row, a null active pointer or a missing snapshot row
is the typed not-initialized denial; corrupt persisted material fails closed
through the domain signing-unavailable mapping. Driver failures leave through
the caller's error mapping; this module raises typed application errors only
and never logs or echoes payload bytes, signatures, keys or locators.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from personal_os.diagnostics.context import DiagnosticContext
from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import InternalApplicationError
from personal_os.exclusion_policy.contracts import PolicySubject
from personal_os.exclusion_policy.enforcement import (
    ActivePolicySnapshotMaterial,
    AllowedPolicyRevisionBinding,
    PolicyDecision,
    PolicyEnforcementService,
    PolicyTrustAnchorVerifier,
    PublicationPolicyEvidence,
    enforce_policy_decision,
    evaluate_policy_decision,
    parse_verified_policy_revision,
    policy_not_initialized_error,
    record_evaluation_metric,
)
from personal_os.exclusion_policy.metrics import (
    EvaluationMetricOutcome,
    ExclusionPolicyMetrics,
    PolicyBoundary,
)
from personal_os.sources.commands import SourceType
from personal_os.sources.errors import SourcePublicationError
from personal_os.sources.fingerprint import SourceVersionCommand
from postgresql_source_store.engine import apply_transaction_bounds
from postgresql_source_store.locks import policy_state_lock_statement
from postgresql_source_store.tables import (
    policy_signing_keys,
    source_policies,
    sources,
    workspace_policy_state,
)


def active_policy_snapshot_select_statement(
    policy_revision_id: UUID,
) -> sa.Select[tuple[Any, ...]]:
    """Build the schema-qualified active-snapshot lookup with its trust anchor.

    Joins the revision's persisted signing-key row so the material carries
    exactly the public key the workspace committed for that revision.
    """

    return (
        sa.select(
            source_policies.c.workspace_id.label("workspace_id"),
            source_policies.c.policy_revision_id.label("policy_revision_id"),
            source_policies.c.revision_number.label("revision_number"),
            source_policies.c.snapshot_payload_bytes.label("payload_bytes"),
            source_policies.c.snapshot_payload_sha256.label("payload_sha256"),
            source_policies.c.signature_bytes.label("signature_bytes"),
            policy_signing_keys.c.public_key_bytes.label("public_key_bytes"),
        )
        .select_from(source_policies)
        .join(
            policy_signing_keys,
            policy_signing_keys.c.signing_key_id == source_policies.c.signing_key_id,
        )
        .where(source_policies.c.policy_revision_id == policy_revision_id)
    )


def source_type_select_statement(workspace_id: UUID, source_id: UUID) -> sa.Select[tuple[Any, ...]]:
    """Build the workspace-bound stored source-type lookup."""

    return sa.select(sources.c.source_type).where(
        sources.c.workspace_id == workspace_id,
        sources.c.source_id == source_id,
    )


def hydrate_active_policy_snapshot(row: Any, workspace_id: UUID) -> ActivePolicySnapshotMaterial:
    """Build the snapshot material from one lookup row, failing closed.

    A row bound to another workspace or a non-positive revision number is
    schema drift the store itself cannot produce, so it fails as the public
    ``internal_error``; torn driver values fail on the closed dataclass
    geometry the same way.
    """

    if row["workspace_id"] != workspace_id:
        raise InternalApplicationError(ErrorCode.INTERNAL_ERROR)
    revision_number = row["revision_number"]
    if isinstance(revision_number, bool) or not isinstance(revision_number, int):
        raise InternalApplicationError(ErrorCode.INTERNAL_ERROR)
    try:
        return ActivePolicySnapshotMaterial(
            workspace_id=row["workspace_id"],
            policy_revision_id=row["policy_revision_id"],
            revision_number=int(revision_number),
            payload_bytes=bytes(row["payload_bytes"]),
            payload_sha256=str(row["payload_sha256"]),
            signature_bytes=bytes(row["signature_bytes"]),
            public_key_bytes=bytes(row["public_key_bytes"]),
        )
    except (KeyError, TypeError, ValueError) as cause:
        raise InternalApplicationError(ErrorCode.INTERNAL_ERROR) from cause


async def load_locked_active_policy_snapshot(
    connection: AsyncConnection, workspace_id: UUID
) -> ActivePolicySnapshotMaterial:
    """Lock the policy-state row and load the active signed snapshot material.

    Runs inside the caller's transaction: the ``FOR UPDATE`` row lock is the
    authoritative serialization point of the global lock order (publication
    idempotency advisory lock, then this row, then the source rows). A
    missing state row, a null active pointer or a missing snapshot row maps
    to the typed not-initialized denial.
    """

    state_result = await connection.execute(policy_state_lock_statement(workspace_id))
    state_row = state_result.one_or_none()
    if state_row is None or state_row.active_policy_revision_id is None:
        raise policy_not_initialized_error()
    snapshot_result = await connection.execute(
        active_policy_snapshot_select_statement(state_row.active_policy_revision_id)
    )
    snapshot_row = snapshot_result.mappings().first()
    if snapshot_row is None:
        # A dangling active pointer is policy-state corruption; deny closed
        # through the same missing-active-policy semantics.
        raise policy_not_initialized_error()
    return hydrate_active_policy_snapshot(snapshot_row, workspace_id)


async def evaluate_locked_policy_decision(
    connection: AsyncConnection,
    *,
    workspace_id: UUID,
    subject: PolicySubject,
    verifier: PolicyTrustAnchorVerifier,
    metrics: ExclusionPolicyMetrics | None = None,
    boundary: PolicyBoundary = PolicyBoundary.SOURCE_CREATE_UPDATE,
) -> PolicyDecision:
    """Re-evaluate the authoritative subject under the policy-state row lock.

    This is the transaction-final recheck of spec 14: the caller rebuilds the
    authoritative subject from locked canonical rows, this helper locks the
    policy-state row, verifies and parses the currently active signed
    revision, evaluates the subject against it and raises the typed denial
    unless the enforced decision allows. Any preflight evidence the caller
    holds is a non-authoritative hint and never consulted here.
    """

    started = time.monotonic()
    material = await load_locked_active_policy_snapshot(connection, workspace_id)
    revision = parse_verified_policy_revision(material, verifier=verifier)
    decision = evaluate_policy_decision(
        revision=revision, subject=subject, evaluated_at=datetime.now(UTC)
    )
    if metrics is not None:
        record_evaluation_metric(
            metrics,
            boundary=boundary,
            decision=decision,
            duration_seconds=time.monotonic() - started,
        )
    enforce_policy_decision(decision)
    return decision


async def authorize_locked_publication_policy(
    connection: AsyncConnection,
    command: SourceVersionCommand,
    subject: PolicySubject,
    policy_evidence: PublicationPolicyEvidence | None,
    verifier: PolicyTrustAnchorVerifier,
    metrics: ExclusionPolicyMetrics | None,
) -> PublicationPolicyEvidence:
    """Authorize publication against the verified transaction-final revision.

    A bound allowed revision may skip only the locator-free evaluator, never
    the locked active-snapshot load or signature verification. Changed bound
    revisions, ordinary decisions and absent evidence all retain the existing
    unconditional authoritative evaluation.
    """

    started_monotonic = time.monotonic()
    material = await load_locked_active_policy_snapshot(
        connection,
        command.workspace_id,
    )
    revision = parse_verified_policy_revision(material, verifier=verifier)
    if isinstance(policy_evidence, AllowedPolicyRevisionBinding):
        if policy_evidence.workspace_id != command.workspace_id:
            raise SourcePublicationError(
                ErrorCode.SOURCE_CONCURRENCY_INVARIANT_FAILED,
                safe_details={"source_id": command.source_id},
            )
        if revision.revision_number == policy_evidence.policy_revision_number:
            if metrics is not None:
                metrics.record_evaluation(
                    boundary=PolicyBoundary.SOURCE_CREATE_UPDATE,
                    decision=EvaluationMetricOutcome.ALLOWED,
                    duration_seconds=max(time.monotonic() - started_monotonic, 0.0),
                )
            return policy_evidence

    decision = evaluate_policy_decision(
        revision=revision,
        subject=subject,
        evaluated_at=datetime.now(UTC),
    )
    if metrics is not None:
        record_evaluation_metric(
            metrics,
            boundary=PolicyBoundary.SOURCE_CREATE_UPDATE,
            decision=decision,
            duration_seconds=max(time.monotonic() - started_monotonic, 0.0),
        )
    enforce_policy_decision(decision)
    return decision


class PostgresqlActivePolicySnapshotSource:
    """Engine-backed active-snapshot source port of the domain guard.

    One bounded ``READ COMMITTED`` read resolves the active pointer and the
    joined signed snapshot. The load is the non-authoritative preflight hint:
    the authoritative decision is always the locked recheck inside the
    guarded transaction.
    """

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def load_active_snapshot(
        self, workspace_id: UUID, context: DiagnosticContext
    ) -> ActivePolicySnapshotMaterial | None:
        del context  # Correlation stays with the calling boundary.
        async with (
            self._engine.connect() as connection,
            connection.begin(),
        ):
            await apply_transaction_bounds(connection)
            state_result = await connection.execute(
                sa.select(workspace_policy_state.c.active_policy_revision_id).where(
                    workspace_policy_state.c.workspace_id == workspace_id
                )
            )
            active_revision_id = state_result.scalar_one_or_none()
            if active_revision_id is None:
                return None
            snapshot_result = await connection.execute(
                active_policy_snapshot_select_statement(active_revision_id)
            )
            snapshot_row = snapshot_result.mappings().first()
        if snapshot_row is None:
            raise policy_not_initialized_error()
        return hydrate_active_policy_snapshot(snapshot_row, workspace_id)


class PostgresqlPolicySubjectEvidenceSource:
    """Engine-backed subject-evidence source port of the domain guard."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def load_subject_evidence(
        self, workspace_id: UUID, source_id: UUID, context: DiagnosticContext
    ) -> PolicySubject | None:
        del context  # Correlation stays with the calling boundary.
        async with (
            self._engine.connect() as connection,
            connection.begin(),
        ):
            await apply_transaction_bounds(connection)
            result = await connection.execute(source_type_select_statement(workspace_id, source_id))
            source_type_value = result.scalar_one_or_none()
        if source_type_value is None:
            return None
        try:
            source_type = SourceType(str(source_type_value))
        except ValueError as cause:
            # Impossible against the CHECK constraint; fail closed as drift.
            raise InternalApplicationError(ErrorCode.INTERNAL_ERROR) from cause
        return PolicySubject(
            workspace_id=workspace_id,
            source_id=source_id,
            source_type=source_type,
        )


def compose_policy_enforcement(
    engine: AsyncEngine,
    *,
    verifier: PolicyTrustAnchorVerifier,
    metrics: ExclusionPolicyMetrics | None = None,
) -> PolicyEnforcementService:
    """Build the domain enforcement service over the engine-backed ports."""

    return PolicyEnforcementService(
        snapshot_source=PostgresqlActivePolicySnapshotSource(engine),
        evidence_source=PostgresqlPolicySubjectEvidenceSource(engine),
        verifier=verifier,
        metrics=metrics,
    )


__all__ = [
    "PostgresqlActivePolicySnapshotSource",
    "PostgresqlPolicySubjectEvidenceSource",
    "active_policy_snapshot_select_statement",
    "authorize_locked_publication_policy",
    "compose_policy_enforcement",
    "evaluate_locked_policy_decision",
    "hydrate_active_policy_snapshot",
    "load_locked_active_policy_snapshot",
    "source_type_select_statement",
]
