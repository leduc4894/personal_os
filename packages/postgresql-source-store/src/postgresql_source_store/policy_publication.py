"""Atomic signed exclusion-policy publication over PostgreSQL (spec 11).

:class:`PostgresqlPolicyPublicationStore` implements the durable
:class:`~personal_os.exclusion_policy.publication.PolicyPublicationStore`
port. ``commit_publication`` runs exactly one ``READ COMMITTED``
transaction behind the pinned ``SET LOCAL`` bounds and the frozen lock
order — the policy-idempotency transaction advisory lock, the
``workspace_policy_state`` serialization row ``FOR UPDATE``, then the draft
and preview rows — and inside that order it re-resolves replay or rejects
identity reuse, rechecks actor/workspace ownership, the exact confirmation,
preview state/expiry, draft identity/version/hash, active parent and source
checkpoint, allocates the next revision identity, invokes the domain
snapshot builder so the canonical payload is built, signed and verified
while the serialization row is locked, inserts the immutable revision,
rules and signature bytes, swaps the active pointer through the guarded
transition, commits one pending reconciliation intent and one append-only
``exclusion_policy.published`` audit row, marks the preview consumed and
rebases/increments the same draft. One commit lands every effect or none of
them.

``resolve_committed`` is the lock-free indexed preflight by workspace and
idempotency key: an exact fingerprint match hydrates the original result
without mutation, and a different fingerprint under the same key is terminal
misuse audited as ``exclusion_policy.publish_rejected`` with a closed reason
only. Business rejections detected inside the commit transaction abort and
roll it back through :class:`PolicyRejectionAbort` and audit after the
rollback; input rejected before the policy trust boundary raises the typed
error with no audit row. An ambiguous commit acknowledgement resolves
through the fresh-connection evidence lookup wired into the bounded retry
runner: proven committed evidence returns the exact replay, proven absence
permits one normal retry, and PostgreSQL unavailability stays the retryable
commit-outcome-unknown error that never assumes a rollback and never signs
or inserts a second revision.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final
from uuid import UUID, uuid7

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from personal_os.diagnostics.context import DiagnosticContext
from personal_os.diagnostics.events import SafeToken
from personal_os.error_contracts.codes import ErrorCode
from personal_os.error_contracts.exceptions import InternalApplicationError
from personal_os.exclusion_policy.contracts import ExclusionRule
from personal_os.exclusion_policy.drafts import compute_draft_semantic_sha256
from personal_os.exclusion_policy.errors import ExclusionPolicyError
from personal_os.exclusion_policy.ports import PolicyActor, PolicyDraft
from personal_os.exclusion_policy.publication import (
    ACTOR_INVALID,
    CONFIRMATION_PHRASE,
    PolicyRequestFingerprint,
    PublicationSnapshotMaterial,
    PublishedPolicyResult,
    PublishPolicyCommand,
    SignedSnapshotBuilder,
    signing_unavailable_error,
)
from personal_os.exclusion_policy.signatures import (
    ED25519_PUBLIC_KEY_BYTES,
    SNAPSHOT_PAYLOAD_CONTRACT,
    derive_ed25519_key_id,
)
from personal_os.sources.actors import reject_nil_uuid
from personal_os.sources.commands import IdempotencyKey
from postgresql_source_store.engine import apply_transaction_bounds
from postgresql_source_store.locks import (
    advisory_xact_lock_statement,
    signed_first_sha256_word,
)
from postgresql_source_store.policy_drafts import (
    PolicyDatabaseRetryPolicy,
    build_draft_rule_values,
    draft_lock_statement,
    hydrate_policy_draft,
)
from postgresql_source_store.policy_previews import source_checkpoint_select_statement
from postgresql_source_store.tables import (
    audit_events,
    policy_draft_rules,
    policy_drafts,
    policy_previews,
    policy_reconciliation_intents,
    policy_rules,
    policy_signing_keys,
    source_policies,
    workspace_policy_state,
    workspaces,
)

#: Policy publication lock namespace (``"SVPI"`` ASCII): the replay-identity
#: advisory-lock family for exclusion-policy publications, disjoint from the
#: source idempotency and source lock families.
POLICY_IDEMPOTENCY_LOCK_NAMESPACE: Final[int] = 0x53565049

#: Audit-row literals for the in-transaction publication audit.
PUBLISHED_AUDIT_ACTION: Final[str] = "exclusion_policy.published"
POLICY_REVISION_AUDIT_TARGET_KIND: Final[str] = "policy_revision"
POLICY_PREVIEW_AUDIT_TARGET_KIND: Final[str] = "policy_preview"
AUDIT_RESULT_SUCCEEDED: Final[str] = "succeeded"

#: Audit-row literals for the standalone business-rejection audit.
PUBLISH_REJECTED_AUDIT_ACTION: Final[str] = "exclusion_policy.publish_rejected"
AUDIT_RESULT_REJECTED: Final[str] = "rejected"

#: Closed rejection reason codes for the standalone audit row.
REASON_IDEMPOTENCY_MISMATCH: Final[str] = "idempotency_mismatch"
REASON_ACTOR_INVALID: Final[str] = "actor_invalid"
REASON_CONFIRMATION_INVALID: Final[str] = "confirmation_invalid"
REASON_PREVIEW_NOT_READY: Final[str] = "preview_not_ready"
REASON_PREVIEW_FAILED: Final[str] = "preview_failed"
REASON_PREVIEW_EXPIRED: Final[str] = "preview_expired"
REASON_PREVIEW_MISSING: Final[str] = "preview_missing"
REASON_PREVIEW_STALE: Final[str] = "preview_stale"
REASON_SNAPSHOT_OUTDATED: Final[str] = "snapshot_outdated"

#: Closed typed reason tokens carried by the safe details of rejections.
IDEMPOTENCY_MISMATCH_REASON: Final[SafeToken] = SafeToken.parse("idempotency_mismatch")
PREVIEW_MISSING_REASON: Final[SafeToken] = SafeToken.parse("preview_missing")
PREVIEW_DRAFT_STALE_REASON: Final[SafeToken] = SafeToken.parse("preview_draft_stale")
PREVIEW_BASE_REVISION_STALE_REASON: Final[SafeToken] = SafeToken.parse(
    "preview_base_revision_stale"
)
PREVIEW_CHECKPOINT_STALE_REASON: Final[SafeToken] = SafeToken.parse(
    "preview_source_checkpoint_stale"
)
PREVIEW_DIGEST_STALE_REASON: Final[SafeToken] = SafeToken.parse("preview_digest_stale")

#: Reconciliation intent lifecycle literals (exactly the migration CHECK set).
RECONCILIATION_STATE_PENDING: Final[str] = "pending"
RECONCILIATION_WORKFLOW_ID_PREFIX: Final[str] = "exclusion-policy-reconciliation"

#: Closed preview lifecycle literals referenced by the recheck chain.
PREVIEW_STATE_READY: Final[str] = "ready"
PREVIEW_STATE_FAILED: Final[str] = "failed"
PREVIEW_STATE_EXPIRED: Final[str] = "expired"
PREVIEW_STATE_CONSUMED: Final[str] = "consumed"

#: Signing-key algorithm and default-decision literals pinned by the schema.
SIGNING_KEY_ALGORITHM_ED25519: Final[str] = "Ed25519"
DEFAULT_DECISION_ALLOWED: Final[str] = "allowed"


@dataclass(frozen=True, slots=True)
class PolicyPublicationIdentities:
    """Backend UUIDv7 identities for one publication service invocation.

    The three generated identities are allocated once per invocation and
    reused through the bounded transaction attempts, so a retry rewrites the
    same canonical identity rather than leaking a new revision identity per
    attempt. Timestamps stay PostgreSQL-owned.
    """

    policy_revision_id: UUID
    policy_reconciliation_intent_id: UUID
    audit_event_id: UUID

    @classmethod
    def allocate(cls) -> PolicyPublicationIdentities:
        """Allocate the three fresh time-ordered UUIDv7 identities."""
        return cls(
            policy_revision_id=uuid7(),
            policy_reconciliation_intent_id=uuid7(),
            audit_event_id=uuid7(),
        )


@dataclass(frozen=True, slots=True)
class _PendingPolicyRejection:
    """A detected business rejection to audit and raise after the rollback."""

    reason_code: str | None
    error: ExclusionPolicyError


class PolicyRejectionAbort(Exception):
    """Carries a pending rejection out of the open transaction to force rollback.

    A business rejection detected inside the commit transaction must never
    let the surrounding ``connection.begin()`` block exit normally, because a
    normal exit commits. Raising this abort rolls the whole transaction back;
    the store catches it immediately after the block, writes the standalone
    ``exclusion_policy.publish_rejected`` audit and raises the typed business
    error.
    """

    def __init__(self, rejection: _PendingPolicyRejection) -> None:
        super().__init__("pending business rejection aborts the publication transaction")
        self.rejection = rejection


def policy_idempotency_lock_key(workspace_id: UUID, key: IdempotencyKey) -> int:
    """Derive the transaction lock key for a policy publication identity.

    The material mirrors the source idempotency family — the workspace UUID
    bytes, a NUL separator that cannot appear in the ASCII key, and the exact
    key bytes — hashed through the shared signed first-SHA-256 word
    derivation under the policy namespace.
    """

    material = workspace_id.bytes + b"\x00" + key.value.encode("ascii")
    return signed_first_sha256_word(material)


def policy_idempotency_lock_statement(workspace_id: UUID, key: IdempotencyKey) -> sa.TextClause:
    """Build the policy publication idempotency advisory lock statement."""
    return advisory_xact_lock_statement(
        POLICY_IDEMPOTENCY_LOCK_NAMESPACE,
        policy_idempotency_lock_key(workspace_id, key),
    )


def reconciliation_workflow_id(workspace_id: UUID, policy_revision_id: UUID) -> str:
    """Derive the deterministic reconciliation workflow identity (spec 15).

    Retried and concurrent dispatches of one revision converge on the same
    execution because the identity is a pure function of the two opaque IDs.
    """

    reject_nil_uuid("workspace_id", workspace_id)
    reject_nil_uuid("policy_revision_id", policy_revision_id)
    return f"{RECONCILIATION_WORKFLOW_ID_PREFIX}/{workspace_id}/{policy_revision_id}"


def policy_state_lock_statement(workspace_id: UUID) -> sa.Select[tuple[Any, ...]]:
    """Build the ``FOR UPDATE`` lock of the publication serialization row."""

    return (
        sa.select(
            workspace_policy_state.c.active_policy_revision_id,
            workspace_policy_state.c.active_revision_number,
        )
        .where(workspace_policy_state.c.workspace_id == workspace_id)
        .with_for_update()
    )


def preview_lock_statement(
    workspace_id: UUID, policy_preview_id: UUID
) -> sa.Select[tuple[Any, ...]]:
    """Build the workspace-bound ``FOR UPDATE`` lock of the preview row."""

    return (
        sa.select(
            policy_previews.c.policy_draft_id,
            policy_previews.c.draft_version,
            policy_previews.c.draft_sha256,
            policy_previews.c.base_policy_revision_id,
            policy_previews.c.source_checkpoint_event_sequence,
            policy_previews.c.state,
            policy_previews.c.impact_digest,
            policy_previews.c.safe_error_code,
            policy_previews.c.expires_at,
        )
        .where(
            policy_previews.c.policy_preview_id == policy_preview_id,
            policy_previews.c.workspace_id == workspace_id,
        )
        .with_for_update()
    )


def workspace_owner_select_statement(workspace_id: UUID) -> sa.Select[tuple[Any, ...]]:
    """Build the workspace owner lookup of the ownership recheck."""

    return sa.select(workspaces.c.owner_user_id).where(workspaces.c.workspace_id == workspace_id)


def replay_lookup_by_key_statement(
    workspace_id: UUID, idempotency_key: IdempotencyKey
) -> sa.Select[tuple[Any, ...]]:
    """Build the schema-qualified replay lookup by workspace and key.

    Joins the persisted signing-key row for key-ID derivation and derives the
    reconciliation state and rule count as correlated scalar subqueries, so
    one indexed read hydrates the complete replay evidence.
    """

    rule_count = (
        sa.select(sa.func.count())
        .where(policy_rules.c.policy_revision_id == source_policies.c.policy_revision_id)
        .correlate(source_policies)
        .scalar_subquery()
    )
    reconciliation_state = (
        sa.select(policy_reconciliation_intents.c.state)
        .where(
            policy_reconciliation_intents.c.workspace_id == source_policies.c.workspace_id,
            policy_reconciliation_intents.c.policy_revision_id
            == source_policies.c.policy_revision_id,
        )
        .correlate(source_policies)
        .limit(1)
        .scalar_subquery()
    )
    return (
        sa.select(
            source_policies.c.workspace_id,
            source_policies.c.policy_revision_id,
            source_policies.c.revision_number,
            source_policies.c.parent_policy_revision_id,
            source_policies.c.request_fingerprint,
            source_policies.c.snapshot_payload_sha256,
            source_policies.c.published_at,
            policy_signing_keys.c.public_key_bytes,
            reconciliation_state.label("reconciliation_state"),
            rule_count.label("rule_count"),
        )
        .select_from(source_policies)
        .join(
            policy_signing_keys,
            policy_signing_keys.c.signing_key_id == source_policies.c.signing_key_id,
        )
        .where(
            source_policies.c.workspace_id == workspace_id,
            source_policies.c.publication_idempotency_key == idempotency_key.value,
        )
    )


def signing_key_rows_select_statement(workspace_id: UUID) -> sa.Select[tuple[Any, ...]]:
    """Build the workspace signing-key rows lookup for key-ID resolution."""

    return sa.select(
        policy_signing_keys.c.signing_key_id,
        policy_signing_keys.c.public_key_bytes,
    ).where(
        policy_signing_keys.c.workspace_id == workspace_id,
        policy_signing_keys.c.algorithm == SIGNING_KEY_ALGORITHM_ED25519,
    )


def mark_preview_consumed_statement(policy_preview_id: UUID, consumed_at: datetime) -> sa.Update:
    """Build the fenced ready-to-consumed transition of the published preview."""

    return (
        sa.update(policy_previews)
        .values(state=PREVIEW_STATE_CONSUMED, consumed_at=consumed_at)
        .where(
            policy_previews.c.policy_preview_id == policy_preview_id,
            policy_previews.c.state == PREVIEW_STATE_READY,
        )
    )


def rebase_draft_after_publication_statement(
    policy_draft_id: UUID,
    expected_draft_version: int,
    new_base_policy_revision_id: UUID,
    updated_by_user_id: UUID,
    occurred_at: datetime,
) -> sa.Update:
    """Build the guarded draft rebase onto the just-published revision.

    The fence matches the exact locked version; success increments the
    version exactly once, points the base at the new revision and keeps the
    rule rows, so the draft retains the just-published rule set (spec 9).
    """

    return (
        sa.update(policy_drafts)
        .values(
            base_policy_revision_id=new_base_policy_revision_id,
            draft_version=expected_draft_version + 1,
            updated_by_user_id=updated_by_user_id,
            updated_at=occurred_at,
        )
        .where(
            policy_drafts.c.policy_draft_id == policy_draft_id,
            policy_drafts.c.draft_version == expected_draft_version,
        )
    )


def swap_active_pointer_statement(
    workspace_id: UUID,
    expected_active_policy_revision_id: UUID | None,
    expected_active_revision_number: int,
    new_policy_revision_id: UUID,
    new_revision_number: int,
    occurred_at: datetime,
) -> sa.Update:
    """Build the guarded active-pointer swap of the serialization row.

    The fence matches the workspace row whose pointer and revision number
    still equal the rechecked expectations; a null expected pointer compiles
    to ``IS NULL`` so the initial publication swaps revision zero exactly
    once.
    """

    return (
        sa.update(workspace_policy_state)
        .values(
            active_policy_revision_id=new_policy_revision_id,
            active_revision_number=new_revision_number,
            updated_at=occurred_at,
        )
        .where(
            workspace_policy_state.c.workspace_id == workspace_id,
            workspace_policy_state.c.active_policy_revision_id
            == expected_active_policy_revision_id,
            workspace_policy_state.c.active_revision_number == expected_active_revision_number,
        )
    )


def build_reconciliation_intent_values(
    *,
    policy_reconciliation_intent_id: UUID,
    workspace_id: UUID,
    policy_revision_id: UUID,
    workflow_id: str,
    occurred_at: datetime,
) -> dict[str, Any]:
    """Build the one pending durable reconciliation-work row (spec 15).

    Publication commits exactly the intent; the leased dispatcher of the
    reconciliation phase owns the workflow start and the per-source
    projection work. Every timestamp is the one transaction timestamp.
    """

    return {
        "policy_reconciliation_intent_id": policy_reconciliation_intent_id,
        "workspace_id": workspace_id,
        "policy_revision_id": policy_revision_id,
        "workflow_id": workflow_id,
        "state": RECONCILIATION_STATE_PENDING,
        "attempt_count": 0,
        "available_at": occurred_at,
        "lease_token": None,
        "leased_until": None,
        "dispatched_at": None,
        "safe_error_code": None,
        "created_at": occurred_at,
        "updated_at": occurred_at,
    }


def build_policy_rule_values(policy_revision_id: UUID, rule: ExclusionRule) -> dict[str, Any]:
    """Map one domain rule onto its immutable ``policy_rules`` row values.

    The mapping mirrors the closed kind-to-column grammar enforced by the
    database CHECK constraints: ``exact_source_id`` populates
    ``source_id_operand``, the five text kinds populate ``text_operand``,
    ``maximum_size`` populates ``size_bytes_operand`` and every other column
    stays null.
    """

    draft_values = build_draft_rule_values(policy_revision_id, rule)
    return {
        "policy_revision_id": policy_revision_id,
        "rule_id": draft_values["rule_id"],
        "rule_kind": draft_values["rule_kind"],
        "source_id_operand": draft_values["source_id_operand"],
        "text_operand": draft_values["text_operand"],
        "size_bytes_operand": draft_values["size_bytes_operand"],
        "semantic_fingerprint": draft_values["semantic_fingerprint"],
    }


def build_published_audit_values(
    *,
    policy_revision_id: UUID,
    workspace_id: UUID,
    actor: PolicyActor,
    payload_sha256: str,
    occurred_at: datetime,
    request_id: UUID,
    client_request_id: UUID | None,
    trace_id: str | None,
) -> dict[str, Any]:
    """Build the ``exclusion_policy.published`` audit-row values.

    The row carries identifiers, the closed actor/action/result literals and
    the committed payload hash only: rule operands, snapshot bytes and
    signature material never enter the audit table (spec 21).
    """

    return {
        "audit_event_id": uuid7(),
        "workspace_id": workspace_id,
        "actor_kind": actor.actor_kind.value,
        "actor_id": actor.user_id,
        "actor_reference": None,
        "action": PUBLISHED_AUDIT_ACTION,
        "target_kind": POLICY_REVISION_AUDIT_TARGET_KIND,
        "target_id": policy_revision_id,
        "request_id": request_id,
        "client_request_id": client_request_id,
        "trace_id": trace_id,
        "result": AUDIT_RESULT_SUCCEEDED,
        "reason_code": None,
        "safe_diff_hash": payload_sha256,
        "occurred_at": occurred_at,
    }


def build_publish_rejected_audit_values(
    *,
    workspace_id: UUID,
    actor: PolicyActor,
    target_id: UUID,
    target_kind: str,
    reason_code: str | None,
    occurred_at: datetime,
    request_id: UUID,
    client_request_id: UUID | None,
    trace_id: str | None,
) -> dict[str, Any]:
    """Build the standalone ``exclusion_policy.publish_rejected`` audit values.

    Written only after a trusted workspace/actor resolution, in its own
    transaction after the rejected publication transaction rolled back. The
    row carries the closed reason code and identifiers only — never a
    rejected value, operand or digest (spec 11.2).
    """

    return {
        "audit_event_id": uuid7(),
        "workspace_id": workspace_id,
        "actor_kind": actor.actor_kind.value,
        "actor_id": actor.user_id,
        "actor_reference": None,
        "action": PUBLISH_REJECTED_AUDIT_ACTION,
        "target_kind": target_kind,
        "target_id": target_id,
        "request_id": request_id,
        "client_request_id": client_request_id,
        "trace_id": trace_id,
        "result": AUDIT_RESULT_REJECTED,
        "reason_code": reason_code,
        "safe_diff_hash": None,
        "occurred_at": occurred_at,
    }


def hydration_key_id(public_key_bytes: Any) -> str:
    """Derive the Ed25519 key ID of persisted public bytes, failing closed."""

    if not isinstance(public_key_bytes, (bytes, bytearray)) or len(public_key_bytes) != (
        ED25519_PUBLIC_KEY_BYTES
    ):
        raise ValueError("public key bytes must be exactly 32 raw Ed25519 bytes")
    return derive_ed25519_key_id(bytes(public_key_bytes))


def hydrate_replay_result(row: Any, workspace_id: UUID) -> PublishedPolicyResult:
    """Hydrate the canonical exact-replay result from one lookup row.

    Containment is rechecked against the requested workspace and every shape
    must be closed — a positive revision number, an aware publication time, a
    lowercase-hex payload hash and exactly 32 signing-key bytes. Every
    violation fails closed as the public ``internal_error`` with the cause
    chained only.
    """

    try:
        if row["workspace_id"] != workspace_id:
            raise ValueError("replay row belongs to another workspace")
        published_at = row["published_at"]
        if not isinstance(published_at, datetime) or published_at.tzinfo is None:
            raise ValueError("published_at must be timezone-aware")
        reconciliation_status = row["reconciliation_state"]
        if reconciliation_status is None:
            reconciliation_status = RECONCILIATION_STATE_PENDING
        return PublishedPolicyResult(
            workspace_id=row["workspace_id"],
            policy_revision_id=row["policy_revision_id"],
            revision_number=int(row["revision_number"]),
            parent_policy_revision_id=row["parent_policy_revision_id"],
            payload_sha256=row["snapshot_payload_sha256"],
            signing_key_id=hydration_key_id(row["public_key_bytes"]),
            published_at=published_at,
            rule_count=int(row["rule_count"]),
            reconciliation_status=str(reconciliation_status),
            is_replay=True,
        )
    except InternalApplicationError:
        raise
    except (KeyError, TypeError, ValueError) as cause:
        raise InternalApplicationError(ErrorCode.INTERNAL_ERROR) from cause


class PostgresqlPolicyPublicationStore:
    """Durable signed-publication store over the canonical policy schema.

    The store takes the composition-owned :class:`AsyncEngine`; it opens no
    connection at construction. The snapshot builder arrives per call
    through the domain port signature, so the store itself never holds key
    material — it invokes the builder while the serialization row is locked
    and persists exactly the bytes the builder produced.
    """

    def __init__(
        self, engine: AsyncEngine, *, retry: PolicyDatabaseRetryPolicy | None = None
    ) -> None:
        self._engine = engine
        self._retry = retry if retry is not None else PolicyDatabaseRetryPolicy()

    # --- port ---------------------------------------------------------------------

    async def resolve_committed(
        self,
        command: PublishPolicyCommand,
        fingerprint: PolicyRequestFingerprint,
        context: DiagnosticContext,
    ) -> PublishedPolicyResult | None:
        return await self._retry.run(
            lambda _attempt: self._resolve_committed_once(command, fingerprint, context)
        )

    async def commit_publication(
        self,
        command: PublishPolicyCommand,
        fingerprint: PolicyRequestFingerprint,
        build_signed_snapshot: SignedSnapshotBuilder,
        context: DiagnosticContext,
    ) -> PublishedPolicyResult:
        identities = PolicyPublicationIdentities.allocate()
        return await self._retry.run(
            lambda _attempt: self._commit_publication_once(
                command, fingerprint, build_signed_snapshot, context, identities
            ),
            recover=lambda: self._resolve_committed_once(command, fingerprint, context),
        )

    # --- replay resolution ----------------------------------------------------------

    async def _resolve_committed_once(
        self,
        command: PublishPolicyCommand,
        fingerprint: PolicyRequestFingerprint,
        context: DiagnosticContext,
    ) -> PublishedPolicyResult | None:
        """Lock-free indexed replay preflight by workspace and key.

        An exact fingerprint match hydrates the original result without
        mutation. A different fingerprint under the same key is terminal
        misuse: the standalone rejection audit is written in its own
        transaction and the typed input error follows.
        """

        result: PublishedPolicyResult | None = None
        rejection: _PendingPolicyRejection | None = None
        async with (
            self._engine.connect() as connection,
            connection.begin(),
        ):
            await apply_transaction_bounds(connection)
            replay_row = await self._fetch_replay_row(connection, command)
            if replay_row is not None:
                if replay_row["request_fingerprint"] != fingerprint.hexadecimal:
                    rejection = _PendingPolicyRejection(
                        reason_code=REASON_IDEMPOTENCY_MISMATCH,
                        error=ExclusionPolicyError(
                            ErrorCode.EXCLUSION_POLICY_INPUT_INVALID,
                            safe_details={"reason": IDEMPOTENCY_MISMATCH_REASON},
                        ),
                    )
                else:
                    try:
                        result = hydrate_replay_result(replay_row, command.workspace_id)
                    except InternalApplicationError as invariant_cause:
                        rejection = _PendingPolicyRejection(
                            reason_code=None,
                            error=ExclusionPolicyError(
                                ErrorCode.EXCLUSION_POLICY_INPUT_INVALID,
                                safe_details={"reason": IDEMPOTENCY_MISMATCH_REASON},
                            ),
                        )
                        rejection.error.__cause__ = invariant_cause
        if rejection is not None:
            await self._write_rejection_audit(command, context, rejection.reason_code)
            raise rejection.error
        return result

    # --- the one signed publication transaction --------------------------------------

    async def _commit_publication_once(
        self,
        command: PublishPolicyCommand,
        fingerprint: PolicyRequestFingerprint,
        build_signed_snapshot: SignedSnapshotBuilder,
        context: DiagnosticContext,
        identities: PolicyPublicationIdentities,
    ) -> PublishedPolicyResult:
        result: PublishedPolicyResult | None = None
        rejection: _PendingPolicyRejection | None = None
        try:
            async with (
                self._engine.connect() as connection,
                connection.begin(),
            ):
                await apply_transaction_bounds(connection)
                # Lock order (spec 11.1): policy idempotency advisory lock,
                # then the serialization row, then the draft and preview rows.
                await connection.execute(
                    policy_idempotency_lock_statement(command.workspace_id, command.idempotency_key)
                )
                replay_row = await self._fetch_replay_row(connection, command)
                if replay_row is not None:
                    if replay_row["request_fingerprint"] != fingerprint.hexadecimal:
                        raise PolicyRejectionAbort(
                            _PendingPolicyRejection(
                                reason_code=REASON_IDEMPOTENCY_MISMATCH,
                                error=ExclusionPolicyError(
                                    ErrorCode.EXCLUSION_POLICY_INPUT_INVALID,
                                    safe_details={"reason": IDEMPOTENCY_MISMATCH_REASON},
                                ),
                            )
                        )
                    # An exact replay under the lock acknowledges the original
                    # revision without signing or inserting again.
                    result = hydrate_replay_result(replay_row, command.workspace_id)
                else:
                    result = await self._publish_locked(
                        connection,
                        command,
                        fingerprint,
                        build_signed_snapshot,
                        context,
                        identities,
                    )
        except PolicyRejectionAbort as abort:
            rejection = abort.rejection
        if rejection is not None:
            # A failure writing this standalone audit surfaces as the database
            # failure and replaces the business rejection error: the service
            # must never claim an audit that does not exist.
            await self._write_rejection_audit(command, context, rejection.reason_code)
            raise rejection.error
        if result is None:  # pragma: no cover - every branch assigns or raises
            raise InternalApplicationError(ErrorCode.INTERNAL_ERROR)
        return result

    async def _publish_locked(
        self,
        connection: AsyncConnection,
        command: PublishPolicyCommand,
        fingerprint: PolicyRequestFingerprint,
        build_signed_snapshot: SignedSnapshotBuilder,
        context: DiagnosticContext,
        identities: PolicyPublicationIdentities,
    ) -> PublishedPolicyResult:
        """Execute the full locked publication transition (spec 11.1 steps 3-12)."""

        state_row = await self._select_locked_policy_state(connection, command.workspace_id)
        if state_row is None:
            # Before the policy trust boundary: typed diagnostics only, no audit.
            raise ExclusionPolicyError(ErrorCode.EXCLUSION_POLICY_NOT_INITIALIZED)
        draft_row = await self._select_locked_draft(connection, command)
        if draft_row is None:
            raise ExclusionPolicyError(ErrorCode.EXCLUSION_POLICY_NOT_INITIALIZED)
        preview_row = await self._select_locked_preview(connection, command)
        if preview_row is None:
            raise PolicyRejectionAbort(
                _PendingPolicyRejection(
                    reason_code=REASON_PREVIEW_MISSING,
                    error=ExclusionPolicyError(
                        ErrorCode.EXCLUSION_POLICY_PREVIEW_FAILED,
                        safe_details={"reason": PREVIEW_MISSING_REASON},
                    ),
                )
            )
        # Rechecks under the locks, in the spec order: ownership,
        # confirmation, preview state/expiry, draft binding, active parent,
        # source checkpoint.
        actor_user_id = command.actor.user_id
        owner_user_id = await self._select_workspace_owner(connection, command.workspace_id)
        if actor_user_id is None or owner_user_id != actor_user_id:
            raise PolicyRejectionAbort(
                _PendingPolicyRejection(
                    reason_code=REASON_ACTOR_INVALID,
                    error=ExclusionPolicyError(
                        ErrorCode.EXCLUSION_POLICY_INPUT_INVALID,
                        safe_details={"reason": ACTOR_INVALID},
                    ),
                )
            )
        if command.confirmation != CONFIRMATION_PHRASE:
            raise PolicyRejectionAbort(
                _PendingPolicyRejection(
                    reason_code=REASON_CONFIRMATION_INVALID,
                    error=ExclusionPolicyError(ErrorCode.EXCLUSION_POLICY_CONFIRMATION_INVALID),
                )
            )
        occurred_at = await self._select_now(connection)
        self._recheck_preview_state(preview_row, occurred_at)
        draft = hydrate_policy_draft(
            draft_row, await self._select_draft_rule_rows(connection, command)
        )
        # Binding recheck family (spec 11.1 step 4). The command-versus-preview
        # checks are input-integrity guards; the active-parent check follows
        # them because a publication that advanced the active revision also
        # rebased the draft in the same commit, so the race outcome must name
        # the retryable snapshot-outdated error with the current revision
        # number rather than the draft staleness it implies. The final
        # draft-table consistency guard is unreachable through the store and
        # fails closed on corruption only.
        self._recheck_command_binding(command, preview_row)
        active_revision_number = self._recheck_active_parent(command, state_row, preview_row)
        self._recheck_draft_consistency(draft, preview_row)
        await self._recheck_source_checkpoint(connection, command, preview_row)

        # Allocation and the in-transaction build/sign/verify while the
        # serialization row is locked (spec 11.1 steps 5-7).
        revision_number = active_revision_number + 1
        material = PublicationSnapshotMaterial(
            workspace_id=command.workspace_id,
            policy_revision_id=identities.policy_revision_id,
            revision_number=revision_number,
            parent_policy_revision_id=state_row["active_policy_revision_id"],
            published_at=occurred_at,
            rules=draft.rules,
        )
        signed = build_signed_snapshot(material)
        signing_key_row_id = await self._resolve_signing_key_row_id(
            connection, command.workspace_id, signed.key_id
        )

        await connection.execute(
            sa.insert(source_policies).values(
                policy_revision_id=identities.policy_revision_id,
                workspace_id=command.workspace_id,
                revision_number=revision_number,
                parent_policy_revision_id=state_row["active_policy_revision_id"],
                default_decision=DEFAULT_DECISION_ALLOWED,
                source_checkpoint_event_sequence=int(
                    preview_row["source_checkpoint_event_sequence"]
                ),
                policy_preview_id=command.policy_preview_id,
                publication_idempotency_key=command.idempotency_key.value,
                request_fingerprint=fingerprint.hexadecimal,
                snapshot_contract=SNAPSHOT_PAYLOAD_CONTRACT,
                snapshot_payload_bytes=signed.payload_bytes,
                snapshot_payload_sha256=signed.payload_sha256,
                signing_key_id=signing_key_row_id,
                signature_bytes=signed.signature_bytes,
                published_by_user_id=actor_user_id,
                published_at=occurred_at,
            )
        )
        if draft.rules:
            await connection.execute(
                sa.insert(policy_rules).values(
                    [
                        build_policy_rule_values(identities.policy_revision_id, rule)
                        for rule in draft.rules
                    ]
                )
            )
        swapped = await connection.execute(
            swap_active_pointer_statement(
                command.workspace_id,
                state_row["active_policy_revision_id"],
                active_revision_number,
                identities.policy_revision_id,
                revision_number,
                occurred_at,
            )
        )
        if swapped.rowcount != 1:
            # Impossible under the row lock; fail closed as corruption.
            raise InternalApplicationError(ErrorCode.INTERNAL_ERROR)
        await connection.execute(
            sa.insert(policy_reconciliation_intents).values(
                **build_reconciliation_intent_values(
                    policy_reconciliation_intent_id=(identities.policy_reconciliation_intent_id),
                    workspace_id=command.workspace_id,
                    policy_revision_id=identities.policy_revision_id,
                    workflow_id=reconciliation_workflow_id(
                        command.workspace_id, identities.policy_revision_id
                    ),
                    occurred_at=occurred_at,
                )
            )
        )
        await connection.execute(
            sa.insert(audit_events).values(
                **build_published_audit_values(
                    policy_revision_id=identities.policy_revision_id,
                    workspace_id=command.workspace_id,
                    actor=command.actor,
                    payload_sha256=signed.payload_sha256,
                    occurred_at=occurred_at,
                    request_id=context.request_id,
                    client_request_id=context.client_request_id,
                    trace_id=context.trace.trace_id.value,
                )
            )
        )
        consumed = await connection.execute(
            mark_preview_consumed_statement(command.policy_preview_id, occurred_at)
        )
        if consumed.rowcount != 1:
            raise InternalApplicationError(ErrorCode.INTERNAL_ERROR)
        rebased = await connection.execute(
            rebase_draft_after_publication_statement(
                command.policy_draft_id,
                int(preview_row["draft_version"]),
                identities.policy_revision_id,
                actor_user_id,
                occurred_at,
            )
        )
        if rebased.rowcount != 1:
            raise InternalApplicationError(ErrorCode.INTERNAL_ERROR)
        return PublishedPolicyResult(
            workspace_id=command.workspace_id,
            policy_revision_id=identities.policy_revision_id,
            revision_number=revision_number,
            parent_policy_revision_id=state_row["active_policy_revision_id"],
            payload_sha256=signed.payload_sha256,
            signing_key_id=signed.key_id,
            published_at=occurred_at,
            rule_count=len(draft.rules),
            reconciliation_status=RECONCILIATION_STATE_PENDING,
            is_replay=False,
        )

    # --- recheck chain ---------------------------------------------------------------

    @staticmethod
    def _recheck_preview_state(preview_row: Any, occurred_at: datetime) -> None:
        """Reject previews that are not exactly ready and unexpired."""

        state = str(preview_row["state"])
        if state == PREVIEW_STATE_FAILED:
            stored_reason = preview_row["safe_error_code"]
            reason = (
                SafeToken.parse(str(stored_reason)) if stored_reason else PREVIEW_MISSING_REASON
            )
            raise PolicyRejectionAbort(
                _PendingPolicyRejection(
                    reason_code=REASON_PREVIEW_FAILED,
                    error=ExclusionPolicyError(
                        ErrorCode.EXCLUSION_POLICY_PREVIEW_FAILED,
                        safe_details={"reason": reason},
                    ),
                )
            )
        if state in (PREVIEW_STATE_EXPIRED, PREVIEW_STATE_CONSUMED):
            raise PolicyRejectionAbort(
                _PendingPolicyRejection(
                    reason_code=REASON_PREVIEW_EXPIRED,
                    error=ExclusionPolicyError(ErrorCode.EXCLUSION_POLICY_PREVIEW_EXPIRED),
                )
            )
        if state != PREVIEW_STATE_READY:
            raise PolicyRejectionAbort(
                _PendingPolicyRejection(
                    reason_code=REASON_PREVIEW_NOT_READY,
                    error=ExclusionPolicyError(ErrorCode.EXCLUSION_POLICY_PREVIEW_PENDING),
                )
            )
        expires_at = preview_row["expires_at"]
        if expires_at is None or occurred_at >= expires_at:
            # The row stays ready-but-overdue; the sweep and read paths expire
            # it lazily. Publication only refuses it.
            raise PolicyRejectionAbort(
                _PendingPolicyRejection(
                    reason_code=REASON_PREVIEW_EXPIRED,
                    error=ExclusionPolicyError(ErrorCode.EXCLUSION_POLICY_PREVIEW_EXPIRED),
                )
            )

    @staticmethod
    def _recheck_command_binding(command: PublishPolicyCommand, preview_row: Any) -> None:
        """Reject any drift between the command and the locked preview row.

        A ready preview row is immutable, so a mismatch here means the command
        was built from different values than the preview actually carries —
        client-side staleness, not a race.
        """

        def stale(reason: SafeToken) -> PolicyRejectionAbort:
            return PolicyRejectionAbort(
                _PendingPolicyRejection(
                    reason_code=REASON_PREVIEW_STALE,
                    error=ExclusionPolicyError(
                        ErrorCode.EXCLUSION_POLICY_PREVIEW_STALE,
                        safe_details={"reason": reason},
                    ),
                )
            )

        if preview_row["policy_draft_id"] != command.policy_draft_id:
            raise stale(PREVIEW_DRAFT_STALE_REASON)
        if int(preview_row["draft_version"]) != command.expected_draft_version:
            raise stale(PREVIEW_DRAFT_STALE_REASON)
        if preview_row["draft_sha256"] != command.expected_draft_sha256:
            raise stale(PREVIEW_DRAFT_STALE_REASON)
        if preview_row["impact_digest"] != command.preview_impact_digest:
            raise stale(PREVIEW_DIGEST_STALE_REASON)
        if preview_row["base_policy_revision_id"] != command.expected_active_policy_revision_id:
            raise stale(PREVIEW_BASE_REVISION_STALE_REASON)

    @staticmethod
    def _recheck_draft_consistency(draft: PolicyDraft, preview_row: Any) -> None:
        """Fail closed on draft-table drift the store itself cannot produce.

        An explicit edit would have expired the ready preview and a
        publication rebase is caught by the active-parent recheck, so drift
        here is corruption of the locked graph.
        """

        if draft.draft_version != int(preview_row["draft_version"]):
            raise PolicyRejectionAbort(
                _PendingPolicyRejection(
                    reason_code=REASON_PREVIEW_STALE,
                    error=ExclusionPolicyError(
                        ErrorCode.EXCLUSION_POLICY_PREVIEW_STALE,
                        safe_details={"reason": PREVIEW_DRAFT_STALE_REASON},
                    ),
                )
            )
        if compute_draft_semantic_sha256(draft.rules) != preview_row["draft_sha256"]:
            raise PolicyRejectionAbort(
                _PendingPolicyRejection(
                    reason_code=REASON_PREVIEW_STALE,
                    error=ExclusionPolicyError(
                        ErrorCode.EXCLUSION_POLICY_PREVIEW_STALE,
                        safe_details={"reason": PREVIEW_DRAFT_STALE_REASON},
                    ),
                )
            )

    @staticmethod
    def _recheck_active_parent(
        command: PublishPolicyCommand, state_row: Any, preview_row: Any
    ) -> int:
        """Reject when the active revision moved past the preview's base."""

        active_id = state_row["active_policy_revision_id"]
        active_number = int(state_row["active_revision_number"])
        if (
            active_id != preview_row["base_policy_revision_id"]
            or active_id != command.expected_active_policy_revision_id
            or active_number != command.expected_active_revision_number
        ):
            raise PolicyRejectionAbort(
                _PendingPolicyRejection(
                    reason_code=REASON_SNAPSHOT_OUTDATED,
                    error=ExclusionPolicyError(
                        ErrorCode.EXCLUSION_POLICY_SNAPSHOT_OUTDATED,
                        safe_details={"current_policy_revision_number": active_number},
                    ),
                )
            )
        return active_number

    @staticmethod
    async def _recheck_source_checkpoint(
        connection: AsyncConnection, command: PublishPolicyCommand, preview_row: Any
    ) -> None:
        """Reject when the workspace source checkpoint advanced past the preview."""

        checkpoint_result = await connection.execute(
            source_checkpoint_select_statement(command.workspace_id)
        )
        checkpoint = int(checkpoint_result.scalar_one())
        if checkpoint != int(preview_row["source_checkpoint_event_sequence"]):
            raise PolicyRejectionAbort(
                _PendingPolicyRejection(
                    reason_code=REASON_PREVIEW_STALE,
                    error=ExclusionPolicyError(
                        ErrorCode.EXCLUSION_POLICY_PREVIEW_STALE,
                        safe_details={"reason": PREVIEW_CHECKPOINT_STALE_REASON},
                    ),
                )
            )

    # --- shared selects ---------------------------------------------------------------

    @staticmethod
    async def _fetch_replay_row(
        connection: AsyncConnection, command: PublishPolicyCommand
    ) -> Any | None:
        result = await connection.execute(
            replay_lookup_by_key_statement(command.workspace_id, command.idempotency_key)
        )
        return result.mappings().first()

    @staticmethod
    async def _select_locked_policy_state(
        connection: AsyncConnection, workspace_id: UUID
    ) -> Any | None:
        result = await connection.execute(policy_state_lock_statement(workspace_id))
        return result.mappings().first()

    @staticmethod
    async def _select_locked_draft(
        connection: AsyncConnection, command: PublishPolicyCommand
    ) -> dict[str, Any] | None:
        result = await connection.execute(draft_lock_statement(command.policy_draft_id))
        row = result.one_or_none()
        if row is None or row.workspace_id != command.workspace_id:
            # A foreign draft is indistinguishable from a missing one.
            return None
        return {
            "policy_draft_id": command.policy_draft_id,
            "workspace_id": row.workspace_id,
            "draft_version": int(row.draft_version),
            "base_policy_revision_id": row.base_policy_revision_id,
        }

    @staticmethod
    async def _select_locked_preview(
        connection: AsyncConnection, command: PublishPolicyCommand
    ) -> Any | None:
        result = await connection.execute(
            preview_lock_statement(command.workspace_id, command.policy_preview_id)
        )
        return result.mappings().first()

    @staticmethod
    async def _select_workspace_owner(
        connection: AsyncConnection, workspace_id: UUID
    ) -> UUID | None:
        result = await connection.execute(workspace_owner_select_statement(workspace_id))
        owner = result.scalar_one_or_none()
        return owner if isinstance(owner, UUID) else None

    @staticmethod
    async def _select_draft_rule_rows(
        connection: AsyncConnection, command: PublishPolicyCommand
    ) -> list[Any]:
        result = await connection.execute(
            sa.select(
                policy_draft_rules.c.rule_id,
                policy_draft_rules.c.rule_kind,
                policy_draft_rules.c.source_id_operand,
                policy_draft_rules.c.text_operand,
                policy_draft_rules.c.size_bytes_operand,
                policy_draft_rules.c.semantic_fingerprint,
            )
            .where(policy_draft_rules.c.policy_draft_id == command.policy_draft_id)
            .order_by(policy_draft_rules.c.rule_id)
        )
        return list(result.mappings().all())

    @staticmethod
    async def _resolve_signing_key_row_id(
        connection: AsyncConnection, workspace_id: UUID, derived_key_id: str
    ) -> UUID:
        """Map the signing key ID onto its persisted public-key row.

        The workspace's signing-key history is bounded by the keyset
        ceilings, so the indexed read is tiny. A derived key ID with no
        matching persisted row means the configured private key is not the
        workspace's announced trust anchor: publication fails closed as the
        typed signing-unavailable error rather than signing under a key the
        plugin chain cannot verify.
        """

        result = await connection.execute(signing_key_rows_select_statement(workspace_id))
        for row in result.mappings():
            if hydration_key_id(row["public_key_bytes"]) == derived_key_id:
                signing_key_row_id = row["signing_key_id"]
                if not isinstance(signing_key_row_id, UUID):  # pragma: no cover
                    raise InternalApplicationError(ErrorCode.INTERNAL_ERROR)
                return signing_key_row_id
        raise signing_unavailable_error()

    async def _write_rejection_audit(
        self,
        command: PublishPolicyCommand,
        context: DiagnosticContext,
        reason_code: str | None,
    ) -> None:
        async with (
            self._engine.connect() as connection,
            connection.begin(),
        ):
            await apply_transaction_bounds(connection)
            await connection.execute(
                sa.insert(audit_events).values(
                    **build_publish_rejected_audit_values(
                        workspace_id=command.workspace_id,
                        actor=command.actor,
                        target_id=command.policy_preview_id,
                        target_kind=POLICY_PREVIEW_AUDIT_TARGET_KIND,
                        reason_code=reason_code,
                        occurred_at=await self._select_now(connection),
                        request_id=context.request_id,
                        client_request_id=context.client_request_id,
                        trace_id=context.trace.trace_id.value,
                    )
                )
            )

    @staticmethod
    async def _select_now(connection: AsyncConnection) -> datetime:
        """Read the transaction-stable timestamp shared by every written row."""

        result = await connection.execute(sa.text("SELECT now()"))
        occurred_at = result.scalar_one()
        if not isinstance(occurred_at, datetime):  # pragma: no cover - driver contract
            raise TypeError("SELECT now() did not return a datetime")
        return occurred_at


__all__ = [
    "AUDIT_RESULT_REJECTED",
    "AUDIT_RESULT_SUCCEEDED",
    "DEFAULT_DECISION_ALLOWED",
    "POLICY_IDEMPOTENCY_LOCK_NAMESPACE",
    "POLICY_PREVIEW_AUDIT_TARGET_KIND",
    "POLICY_REVISION_AUDIT_TARGET_KIND",
    "PREVIEW_STATE_CONSUMED",
    "PREVIEW_STATE_EXPIRED",
    "PREVIEW_STATE_FAILED",
    "PREVIEW_STATE_READY",
    "PUBLISHED_AUDIT_ACTION",
    "PUBLISH_REJECTED_AUDIT_ACTION",
    "REASON_ACTOR_INVALID",
    "REASON_CONFIRMATION_INVALID",
    "REASON_IDEMPOTENCY_MISMATCH",
    "REASON_PREVIEW_EXPIRED",
    "REASON_PREVIEW_FAILED",
    "REASON_PREVIEW_MISSING",
    "REASON_PREVIEW_NOT_READY",
    "REASON_PREVIEW_STALE",
    "REASON_SNAPSHOT_OUTDATED",
    "RECONCILIATION_STATE_PENDING",
    "RECONCILIATION_WORKFLOW_ID_PREFIX",
    "SIGNING_KEY_ALGORITHM_ED25519",
    "PolicyPublicationIdentities",
    "PolicyRejectionAbort",
    "build_policy_rule_values",
    "build_publish_rejected_audit_values",
    "build_published_audit_values",
    "build_reconciliation_intent_values",
    "hydrate_replay_result",
    "hydration_key_id",
    "mark_preview_consumed_statement",
    "policy_idempotency_lock_key",
    "policy_idempotency_lock_statement",
    "policy_state_lock_statement",
    "preview_lock_statement",
    "rebase_draft_after_publication_statement",
    "reconciliation_workflow_id",
    "replay_lookup_by_key_statement",
    "signing_key_rows_select_statement",
    "swap_active_pointer_statement",
    "workspace_owner_select_statement",
]
