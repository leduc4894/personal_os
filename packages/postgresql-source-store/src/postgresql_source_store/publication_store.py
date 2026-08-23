"""Authorization-aware idempotency preflight, replay hydration and atomic commits.

:class:`PostgresqlSourcePublicationStore` implements the durable
:class:`~personal_os.sources.ports.SourcePublicationStore` port over the
migrated canonical baseline. ``resolve_committed`` performs the lock-free
indexed preflight (design section 7): revalidate the trusted active
workspace/actor context, search ``(workspace_id, idempotency_key)``, then the
globally unique ``event_id``; an exact command/fingerprint replay is hydrated
by joining the event, its committed version and content object, and returned
without mutation. Key or event-identity misuse rejects with exactly one
standalone rejection audit — written only after a trusted workspace/actor is
established — and discloses only the requested source/event IDs, never
existing tenant data. Malformed context before the trust boundary produces
only the typed error, never an audit row.

``commit_create`` (design sections 8.3-8.5 and 10.1) and ``commit_update``
(design sections 8.6-8.8) each run one canonical ``READ COMMITTED``
transaction behind the same locked prefix: the pinned ``SET LOCAL`` bounds,
the idempotency advisory lock, the trusted workspace/actor revalidation, the
replay/mismatch recheck, the ``workspace_policy_state`` row lock with the
authoritative active-policy re-evaluation (spec 14: a policy change during
the upload fails closed without publishing the source version), then the
source advisory lock. The create then performs
the global source-existence rejection, the exact content-object
upsert/select/compare, the pending source, version 1 with a null parent, the
guarded active-pointer transition, the create event with a null base, the
guarded active-locator conflict rejection for a bound locator whose path a
foreign ACTIVE locator already holds, the initial locator insert, the
two upsert intents and the succeeded audit. The update selects the
source/current-version/current-object rows ``FOR UPDATE``, accepts only
``active`` and ``stored_not_indexed`` sources, compares the requested base
BEFORE the content, and either writes the no-change event/audit pair or the
changed graph — content object, version ``n+1`` with the current parent, the
guarded pointer advance, the update event, the two upsert intents and the
succeeded audit — never touching source type or title. Backend UUIDv7
identities are allocated once per service invocation and reused through
bounded transaction attempts; PostgreSQL owns the event identity sequence
and the transaction timestamps. An uncertain commit acknowledgement resolves
through the fresh-connection recovery lookup wired into the bounded retry
runner (design section 9.4).

Every statement is schema-qualified through the Task 6 Core metadata and
parameter-bound; driver failures are routed through
:mod:`postgresql_source_store.error_mapping` so SQLSTATE, SQL, parameters and
driver text never leave the adapter.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final
from uuid import UUID, uuid4, uuid7

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from personal_os.diagnostics.context import DiagnosticContext
from personal_os.diagnostics.events import SafeToken
from personal_os.error_contracts.codes import ErrorCode
from personal_os.exclusion_policy.contracts import PolicySubject
from personal_os.exclusion_policy.enforcement import (
    PolicyTrustAnchorVerifier,
    PublicationPolicyEvidence,
)
from personal_os.exclusion_policy.metrics import ExclusionPolicyMetrics
from personal_os.object_storage import VerifiedObjectReceipt
from personal_os.object_storage.keys import ContentDigest
from personal_os.small_file_sync.contracts import (
    SmallFileTerminalResult,
    SmallFileTerminalResultKind,
)
from personal_os.small_file_sync.ports import SmallFileBoundOperation
from personal_os.source_locators import NormalizedLocator
from personal_os.sources.actors import ActorKind, SourceActor
from personal_os.sources.commands import (
    CreateSourceVersion,
    IdempotencyKey,
    SourceType,
    UpdateSourceVersion,
)
from personal_os.sources.errors import ACTOR_INVALID, SOURCE_STATES, SourcePublicationError
from personal_os.sources.fingerprint import (
    RequestFingerprint,
    SafeDiffHash,
    SourceVersionCommand,
    compute_safe_diff_hash,
)
from personal_os.sources.results import PublicationOutcome, SourceVersionPublicationResult
from postgresql_source_store.engine import apply_transaction_bounds
from postgresql_source_store.error_mapping import DatabaseRetryPolicy
from postgresql_source_store.locks import idempotency_lock_statement, source_lock_statement
from postgresql_source_store.policy_enforcement import (
    authorize_locked_publication_policy,
    source_type_select_statement,
)
from postgresql_source_store.small_file_sync_operations import (
    PostgresqlSmallFileUploadOperationStore,
)
from postgresql_source_store.tables import (
    audit_events,
    content_objects,
    devices,
    projection_intents,
    source_locators,
    source_versions,
    sources,
    sync_events,
    workspaces,
)

#: Audit constants for the standalone business-rejection audit row.
REJECTION_AUDIT_ACTION: Final[str] = "source.version_publish_rejected"
REJECTION_AUDIT_TARGET_KIND: Final[str] = "source"
AUDIT_RESULT_REJECTED: Final[str] = "rejected"
REASON_IDEMPOTENCY_MISMATCH: Final[str] = "idempotency_mismatch"
REASON_EVENT_IDENTITY_MISMATCH: Final[str] = "event_identity_mismatch"
REASON_ACTOR_INVALID: Final[str] = "actor_invalid"
REASON_SOURCE_ALREADY_EXISTS: Final[str] = "source_already_exists"
REASON_CONTENT_OBJECT_CONFLICT: Final[str] = "content_object_metadata_conflict"
REASON_SOURCE_LOCATOR_CONFLICT: Final[str] = "source_locator_conflict"

#: Audit constants for the in-transaction success audit of a changed create.
SUCCESS_AUDIT_ACTION: Final[str] = "source.version_published"
AUDIT_TARGET_KIND_SOURCE: Final[str] = "source"
AUDIT_RESULT_SUCCEEDED: Final[str] = "succeeded"

#: Audit constants for the in-transaction success audit of a no-change update.
NO_CHANGE_AUDIT_ACTION: Final[str] = "source.version_no_change"
REASON_CONTENT_UNCHANGED: Final[str] = "content_unchanged"

#: Rejection reason codes for the update preconditions (spec 10.3).
REASON_SOURCE_NOT_FOUND: Final[str] = "source_not_found"
REASON_SOURCE_STATE_INVALID: Final[str] = "source_state_invalid"
REASON_VERSION_CONFLICT: Final[str] = "version_conflict"

#: Canonical create/update transition literals.
CREATE_EVENT_TYPE: Final[str] = "create"
UPDATE_EVENT_TYPE: Final[str] = "update"
CONTENT_VERSION_ONE: Final[int] = 1
PROJECTION_KIND_QDRANT: Final[str] = "qdrant"
PROJECTION_KIND_NEO4J: Final[str] = "neo4j"
PROJECTION_OPERATION_UPSERT: Final[str] = "upsert"

#: Workspace, device and source lifecycle states referenced by the transitions.
_WORKSPACE_STATUS_ACTIVE: Final[str] = "active"
_DEVICE_STATUS_ACTIVE: Final[str] = "active"
_SOURCE_STATE_PENDING: Final[str] = "pending"
_SOURCE_STATE_ACTIVE: Final[str] = "active"
_SOURCE_STATE_STORED_NOT_INDEXED: Final[str] = "stored_not_indexed"

#: The only source states an update may publish over (design 8.6).
_UPDATEABLE_SOURCE_STATES: Final[frozenset[str]] = frozenset(
    {_SOURCE_STATE_ACTIVE, _SOURCE_STATE_STORED_NOT_INDEXED}
)

#: Closed ``source_state`` audit tokens by their closed state value.
_SOURCE_STATE_TOKENS_BY_VALUE: Final[dict[str, SafeToken]] = {
    token.value: token for token in SOURCE_STATES
}

#: Rejection reason codes are the closed spec set plus ``None`` for the
#: invariant-failure rejection, which has no registered reason token.
_RejectionReasonCode = str | None


def classify_replay(
    event_type: str, base_version_id: UUID | None, committed_id: UUID | None
) -> PublicationOutcome:
    """Classify a committed create/update event shape exactly (spec 8.9).

    Any other create/update shape is an integrity failure.
    """
    if event_type == "create" and base_version_id is None and committed_id is not None:
        return PublicationOutcome.PUBLISHED
    if event_type == "update" and base_version_id is not None and committed_id == base_version_id:
        return PublicationOutcome.NO_CHANGE
    if event_type == "update" and base_version_id is not None and committed_id is not None:
        return PublicationOutcome.PUBLISHED
    raise SourcePublicationError(ErrorCode.SOURCE_CONCURRENCY_INVARIANT_FAILED)


@dataclass(frozen=True, slots=True)
class ReplayLookupRow:
    """One joined ``sync_events``/committed-version/content-object lookup row."""

    workspace_id: UUID
    source_id: UUID
    event_id: UUID
    event_sequence: int
    event_type: str
    base_version_id: UUID | None
    committed_version_id: UUID | None
    idempotency_key: str
    request_fingerprint: str
    committed_at: datetime
    source_version_id: UUID | None
    content_version: int | None
    content_hash: str | None

    @classmethod
    def from_result_row(cls, row: Any) -> ReplayLookupRow:
        """Build the typed row from one named result row of the lookup."""
        return cls(
            workspace_id=row.workspace_id,
            source_id=row.source_id,
            event_id=row.event_id,
            event_sequence=row.event_sequence,
            event_type=row.event_type,
            base_version_id=row.base_version_id,
            committed_version_id=row.committed_version_id,
            idempotency_key=row.idempotency_key,
            request_fingerprint=row.request_fingerprint,
            committed_at=row.committed_at,
            source_version_id=row.source_version_id,
            content_version=row.content_version,
            content_hash=row.content_hash,
        )


def hydrate_replay_result(
    row: ReplayLookupRow, command: SourceVersionCommand
) -> SourceVersionPublicationResult:
    """Hydrate the canonical replay result from an exact committed match.

    Containment is rechecked against the requested workspace and source, the
    committed version/object join must be complete and positive, the committed
    time must be aware, and the event shape must classify. Every violation is
    the integrity failure ``source_concurrency_invariant_failed``.
    """
    if row.workspace_id != command.workspace_id or row.source_id != command.source_id:
        raise SourcePublicationError(
            ErrorCode.SOURCE_CONCURRENCY_INVARIANT_FAILED,
            safe_details={"source_id": command.source_id},
        )
    if (
        row.source_version_id is None
        or row.content_version is None
        or row.content_hash is None
        or row.event_sequence < 1
        or row.content_version < 1
        or row.committed_at.tzinfo is None
    ):
        raise SourcePublicationError(
            ErrorCode.SOURCE_CONCURRENCY_INVARIANT_FAILED,
            safe_details={"source_id": command.source_id},
        )
    try:
        outcome = classify_replay(row.event_type, row.base_version_id, row.committed_version_id)
    except SourcePublicationError as shape_cause:
        raise SourcePublicationError(
            ErrorCode.SOURCE_CONCURRENCY_INVARIANT_FAILED,
            safe_details={"source_id": command.source_id},
        ) from shape_cause
    return SourceVersionPublicationResult(
        source_id=row.source_id,
        source_version_id=row.source_version_id,
        content_version=row.content_version,
        event_id=row.event_id,
        event_sequence=row.event_sequence,
        content_digest=ContentDigest.parse(row.content_hash),
        outcome=outcome,
        committed_at=row.committed_at,
    )


def replay_lookup_by_key_statement(
    workspace_id: UUID, idempotency_key: IdempotencyKey
) -> sa.Select[tuple[Any, ...]]:
    """Build the schema-qualified, parameter-bound key lookup."""
    return _replay_lookup_base().where(
        sync_events.c.workspace_id == workspace_id,
        sync_events.c.idempotency_key == idempotency_key.value,
    )


def replay_lookup_by_event_statement(event_id: UUID) -> sa.Select[tuple[Any, ...]]:
    """Build the schema-qualified, parameter-bound global event lookup."""
    return _replay_lookup_base().where(sync_events.c.event_id == event_id)


def _replay_lookup_base() -> sa.Select[tuple[Any, ...]]:
    return (
        sa.select(
            sync_events.c.workspace_id,
            sync_events.c.source_id,
            sync_events.c.event_id,
            sync_events.c.event_sequence,
            sync_events.c.event_type,
            sync_events.c.base_version_id,
            sync_events.c.committed_version_id,
            sync_events.c.idempotency_key,
            sync_events.c.request_fingerprint,
            sync_events.c.committed_at,
            source_versions.c.source_version_id,
            source_versions.c.content_version,
            content_objects.c.content_hash,
        )
        .select_from(sync_events)
        .outerjoin(
            source_versions,
            sa.and_(
                source_versions.c.workspace_id == sync_events.c.workspace_id,
                source_versions.c.source_id == sync_events.c.source_id,
                source_versions.c.source_version_id == sync_events.c.committed_version_id,
            ),
        )
        .outerjoin(
            content_objects,
            content_objects.c.content_object_id == source_versions.c.content_object_id,
        )
    )


@dataclass(frozen=True, slots=True)
class _PendingRejection:
    """A detected identity misuse to audit and raise after the lookup commits."""

    reason_code: _RejectionReasonCode
    error: SourcePublicationError


class _RejectionAbort(Exception):
    """Carries a pending rejection out of the open transaction to force rollback.

    A business rejection detected inside the commit transaction must never let
    the surrounding ``connection.begin()`` block exit normally, because a
    normal exit commits — and a rejection found after a canonical write (for
    example the guarded-pointer invariant failure) would otherwise commit the
    partial graph. Raising this abort rolls the whole transaction back; the
    store catches it immediately after the block, writes the standalone
    rejection audit and raises the typed business error.
    """

    def __init__(self, rejection: _PendingRejection) -> None:
        super().__init__("pending business rejection aborts the transaction")
        self.rejection = rejection


@dataclass(frozen=True, slots=True)
class SourceCreateIdentities:
    """Backend UUIDv7 identities for one create service invocation.

    The six generated identities are allocated once per service invocation
    and reused through the bounded transaction attempts, so a retry rewrites
    the same canonical identity rather than leaking a new one per attempt.
    The source and event identities come from the command, the initial
    locator identity is reserved alongside the create, and the event
    sequence and every timestamp stay PostgreSQL-owned.
    """

    content_object_id: UUID
    source_version_id: UUID
    source_locator_id: UUID
    qdrant_intent_id: UUID
    neo4j_intent_id: UUID
    audit_event_id: UUID

    @classmethod
    def allocate(cls) -> SourceCreateIdentities:
        """Allocate the six fresh time-ordered UUIDv7 identities."""
        return cls(
            content_object_id=uuid7(),
            source_version_id=uuid7(),
            source_locator_id=uuid7(),
            qdrant_intent_id=uuid7(),
            neo4j_intent_id=uuid7(),
            audit_event_id=uuid7(),
        )


@dataclass(frozen=True, slots=True)
class SourceUpdateIdentities:
    """Backend UUIDv7 identities for one update service invocation.

    Like :class:`SourceCreateIdentities`, the five generated identities are
    allocated once per service invocation and reused through the bounded
    transaction attempts, so a retry rewrites the same canonical identity.
    The version identity of the committed update event and every timestamp
    stay PostgreSQL-owned.
    """

    content_object_id: UUID
    source_version_id: UUID
    qdrant_intent_id: UUID
    neo4j_intent_id: UUID
    audit_event_id: UUID

    @classmethod
    def allocate(cls) -> SourceUpdateIdentities:
        """Allocate the five fresh time-ordered UUIDv7 identities."""
        return cls(
            content_object_id=uuid7(),
            source_version_id=uuid7(),
            qdrant_intent_id=uuid7(),
            neo4j_intent_id=uuid7(),
            audit_event_id=uuid7(),
        )


@dataclass(frozen=True, slots=True)
class LockedSourceRow:
    """The locked ``sources`` row selected ``FOR UPDATE`` for an update."""

    sync_state: str
    current_version_id: UUID | None
    deleted_at: datetime | None


@dataclass(frozen=True, slots=True)
class LockedCurrentVersionRow:
    """The locked current version joined with its content object ``FOR UPDATE``."""

    source_version_id: UUID
    content_version: int
    content_object_id: UUID
    content_hash: str
    object_key: str
    byte_size: int
    media_type: str


@dataclass(frozen=True, slots=True)
class ContentObjectLookupRow:
    """One canonical ``content_objects`` row selected by the full content hash."""

    content_object_id: UUID
    object_key: str
    byte_size: int
    media_type: str


def content_object_upsert_statement(
    content_object_id: UUID, receipt: VerifiedObjectReceipt
) -> postgresql.dml.Insert:
    """Build the exact content-object insert keyed by the content hash.

    ``ON CONFLICT (content_hash) DO NOTHING`` deduplicates identical verified
    bytes: a conflicting existing row survives with its original identity and
    ``verified_at``, never partially updated, and the follow-up lookup plus
    exact metadata comparison decide reuse versus rollback.
    """
    statement = postgresql.insert(content_objects).values(
        content_object_id=content_object_id,
        content_hash=receipt.content_digest.hexadecimal,
        object_key=receipt.object_key.value,
        byte_size=receipt.size_bytes,
        media_type=receipt.media_type.value,
        verified_at=receipt.verified_at,
        created_at=receipt.verified_at,
    )
    return statement.on_conflict_do_nothing(index_elements=[content_objects.c.content_hash])


def content_object_by_hash_statement(content_hash: str) -> sa.Select[tuple[Any, ...]]:
    """Build the schema-qualified, parameter-bound reuse lookup by full hash."""
    return sa.select(
        content_objects.c.content_object_id,
        content_objects.c.object_key,
        content_objects.c.byte_size,
        content_objects.c.media_type,
    ).where(content_objects.c.content_hash == content_hash)


def content_object_metadata_matches(
    receipt: VerifiedObjectReceipt, *, object_key: str, byte_size: int, media_type: str
) -> bool:
    """Compare the stored row against the receipt exactly (design 8.4 step 3).

    Only an exact object key, byte size and media type pair may reuse an
    existing content object; any divergence is the caller's rollback signal.
    """
    return (
        receipt.object_key.value == object_key
        and receipt.size_bytes == byte_size
        and receipt.media_type.value == media_type
    )


class PostgresqlSourcePublicationStore:
    """Durable source-publication store over the canonical PostgreSQL baseline.

    The store takes the composition-owned :class:`AsyncEngine`; it opens no
    connection at construction. ``resolve_committed`` runs the lock-free
    preflight inside one ``READ COMMITTED`` transaction with the pinned ``SET
    LOCAL`` bounds and the bounded contention retry; ``commit_create`` and
    ``commit_update`` run the shared locked prefix plus their canonical
    transition, and both wire the fresh-connection recovery lookup into the
    bounded retry for the uncertain-commit case.
    """

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        retry: DatabaseRetryPolicy | None = None,
        policy_verifier: PolicyTrustAnchorVerifier,
        policy_metrics: ExclusionPolicyMetrics | None = None,
        small_file_operation_store: PostgresqlSmallFileUploadOperationStore | None = None,
        small_file_bound_operation: SmallFileBoundOperation | None = None,
    ) -> None:
        if (small_file_operation_store is None) != (small_file_bound_operation is None):
            raise ValueError("small-file operation store and binding must be supplied together")
        self._engine = engine
        self._retry = retry if retry is not None else DatabaseRetryPolicy()
        self._policy_verifier = policy_verifier
        self._policy_metrics = policy_metrics
        self._small_file_operation_store = small_file_operation_store
        self._small_file_bound_operation = small_file_bound_operation

    def with_small_file_operation_fence(
        self,
        operation_store: PostgresqlSmallFileUploadOperationStore,
        bound: SmallFileBoundOperation,
    ) -> PostgresqlSourcePublicationStore:
        """Return an invocation-local store fenced to one claimed upload."""

        return PostgresqlSourcePublicationStore(
            self._engine,
            retry=self._retry,
            policy_verifier=self._policy_verifier,
            policy_metrics=self._policy_metrics,
            small_file_operation_store=operation_store,
            small_file_bound_operation=bound,
        )

    async def resolve_committed(
        self,
        command: SourceVersionCommand,
        request_fingerprint: RequestFingerprint,
        diagnostic_context: DiagnosticContext,
    ) -> SourceVersionPublicationResult | None:
        return await self._retry.run(
            lambda _attempt: self._resolve_committed_once(
                command, request_fingerprint, diagnostic_context
            ),
            source_id=command.source_id,
        )

    async def commit_create(
        self,
        command: CreateSourceVersion,
        request_fingerprint: RequestFingerprint,
        receipt: VerifiedObjectReceipt,
        diagnostic_context: DiagnosticContext,
        *,
        preflight_decision: PublicationPolicyEvidence | None = None,
    ) -> SourceVersionPublicationResult:
        identities = SourceCreateIdentities.allocate()
        return await self._retry.run(
            lambda _attempt: self._commit_create_once(
                command,
                request_fingerprint,
                receipt,
                diagnostic_context,
                identities,
                preflight_decision,
            ),
            source_id=command.source_id,
            recover=lambda: self._resolve_committed_once(
                command, request_fingerprint, diagnostic_context
            ),
        )

    async def commit_update(
        self,
        command: UpdateSourceVersion,
        request_fingerprint: RequestFingerprint,
        receipt: VerifiedObjectReceipt,
        diagnostic_context: DiagnosticContext,
        *,
        preflight_decision: PublicationPolicyEvidence | None = None,
    ) -> SourceVersionPublicationResult:
        identities = SourceUpdateIdentities.allocate()
        return await self._retry.run(
            lambda _attempt: self._commit_update_once(
                command,
                request_fingerprint,
                receipt,
                diagnostic_context,
                identities,
                preflight_decision,
            ),
            source_id=command.source_id,
            recover=lambda: self._resolve_committed_once(
                command, request_fingerprint, diagnostic_context
            ),
        )

    async def _commit_create_once(
        self,
        command: CreateSourceVersion,
        request_fingerprint: RequestFingerprint,
        receipt: VerifiedObjectReceipt,
        diagnostic_context: DiagnosticContext,
        identities: SourceCreateIdentities,
        preflight_decision: PublicationPolicyEvidence | None,
    ) -> SourceVersionPublicationResult:
        return await self._run_locked_transition(
            command,
            request_fingerprint,
            receipt,
            diagnostic_context,
            lambda connection: self._create_transition(
                connection,
                command,
                request_fingerprint,
                receipt,
                diagnostic_context,
                identities,
            ),
            preflight_decision=preflight_decision,
        )

    async def _commit_update_once(
        self,
        command: UpdateSourceVersion,
        request_fingerprint: RequestFingerprint,
        receipt: VerifiedObjectReceipt,
        diagnostic_context: DiagnosticContext,
        identities: SourceUpdateIdentities,
        preflight_decision: PublicationPolicyEvidence | None,
    ) -> SourceVersionPublicationResult:
        return await self._run_locked_transition(
            command,
            request_fingerprint,
            receipt,
            diagnostic_context,
            lambda connection: self._update_transition(
                connection,
                command,
                request_fingerprint,
                receipt,
                diagnostic_context,
                identities,
            ),
            preflight_decision=preflight_decision,
        )

    async def _run_locked_transition(
        self,
        command: SourceVersionCommand,
        request_fingerprint: RequestFingerprint,
        receipt: VerifiedObjectReceipt,
        diagnostic_context: DiagnosticContext,
        transition: Callable[
            [AsyncConnection],
            Awaitable[tuple[_PendingRejection | None, SourceVersionPublicationResult | None]],
        ],
        *,
        preflight_decision: PublicationPolicyEvidence | None = None,
    ) -> SourceVersionPublicationResult:
        """Run the common locked prefix and one command-specific transition.

        The prefix follows design section 8.3 plus the spec-14 policy recheck:
        ``SET LOCAL`` bounds, the idempotency advisory lock, the trusted
        workspace/actor revalidation, the replay/mismatch recheck under lock,
        then the ``workspace_policy_state`` row lock with authoritative signed
        policy verification, then the source advisory lock. The policy check
        runs for the replay-return path too — a replay must not return
        canonical data until the current policy permits the subject. A
        small-file create carries a bound initial locator that the locked
        subject surfaces so the publication guard can re-evaluate the
        locator-aware policy under the current revision (the receipt-level
        preflight may have authorized the preflight revision; the locked
        guard is the authoritative verdict). Matching bound allowed evidence
        skips only the locator-free evaluator after locked snapshot
        verification; changed revisions and ordinary decisions are evaluated
        unconditionally. Every rejection raises out of the ``async with`` block via
        :class:`_RejectionAbort` so the transaction always rolls back — a
        rejection found after a canonical write must never commit a partial
        graph — and the standalone rejection audit is written only after the
        rollback, in its own transaction. A policy denial is not a business
        rejection: it raises the typed exclusion-policy error, rolls the
        transaction back and writes no rejection audit row.
        """
        result: SourceVersionPublicationResult | None = None
        rejection: _PendingRejection | None = None
        bound_locator: NormalizedLocator | None = None
        if isinstance(command, CreateSourceVersion):
            bound_locator = command.initial_locator
        try:
            async with (
                self._engine.connect() as connection,
                connection.begin(),
            ):
                await apply_transaction_bounds(connection)
                if (
                    self._small_file_operation_store is not None
                    and self._small_file_bound_operation is not None
                ):
                    # Global lock order for the small-file path starts with
                    # operation identity, before source idempotency/policy/source.
                    operation_store = self._small_file_operation_store
                    await operation_store.acquire_bound_publication_fence_in_transaction(
                        connection, self._small_file_bound_operation
                    )
                await connection.execute(
                    idempotency_lock_statement(command.workspace_id, command.idempotency_key)
                )
                if not await self._select_workspace_is_active(connection, command.workspace_id):
                    # Before the trust boundary: typed diagnostics only, no audit.
                    raise SourcePublicationError(
                        ErrorCode.SOURCE_PUBLISH_INPUT_INVALID,
                        safe_details={"reason": ACTOR_INVALID},
                    )
                if not await self._is_actor_valid(connection, command):
                    raise _RejectionAbort(
                        _PendingRejection(
                            reason_code=REASON_ACTOR_INVALID,
                            error=SourcePublicationError(
                                ErrorCode.SOURCE_PUBLISH_INPUT_INVALID,
                                safe_details={"reason": ACTOR_INVALID},
                            ),
                        )
                    )
                rejection, result = await self._resolve_identity(
                    connection, command, request_fingerprint
                )
                if rejection is not None:
                    raise _RejectionAbort(rejection)
                if preflight_decision is not None and (
                    preflight_decision.workspace_id != command.workspace_id
                ):
                    # Evidence from another workspace can never authorize this
                    # publication; fail closed without publishing.
                    raise SourcePublicationError(
                        ErrorCode.SOURCE_CONCURRENCY_INVARIANT_FAILED,
                        safe_details={"source_id": command.source_id},
                    )
                subject = await self._build_authoritative_subject(
                    connection, command, receipt, bound_locator
                )
                await authorize_locked_publication_policy(
                    connection=connection,
                    command=command,
                    subject=subject,
                    policy_evidence=preflight_decision,
                    verifier=self._policy_verifier,
                    metrics=self._policy_metrics,
                )
                if result is None:
                    await connection.execute(source_lock_statement(command.source_id))
                    rejection, result = await transition(connection)
                    if rejection is not None:
                        raise _RejectionAbort(rejection)
                if (
                    result is not None
                    and self._small_file_operation_store is not None
                    and self._small_file_bound_operation is not None
                ):
                    result_kind = (
                        SmallFileTerminalResultKind.NO_CHANGE
                        if result.outcome is PublicationOutcome.NO_CHANGE
                        else SmallFileTerminalResultKind.COMMITTED
                    )
                    record_terminal = (
                        self._small_file_operation_store.record_bound_terminal_result_in_transaction
                    )
                    await record_terminal(
                        connection,
                        self._small_file_bound_operation,
                        SmallFileTerminalResult(
                            result_kind=result_kind,
                            source_id=result.source_id,
                            source_version_id=result.source_version_id,
                            content_version=result.content_version,
                            committed_at=result.committed_at,
                        ),
                    )
        except _RejectionAbort as abort:
            rejection = abort.rejection
        if rejection is not None:
            # A failure writing this standalone audit surfaces as the database
            # failure and replaces the business rejection error: the service
            # must never claim an audit that does not exist.
            await self._write_rejection_audit(command, diagnostic_context, rejection.reason_code)
            raise rejection.error
        if result is None:
            raise SourcePublicationError(
                ErrorCode.SOURCE_CONCURRENCY_INVARIANT_FAILED,
                safe_details={"source_id": command.source_id},
            )
        return result

    async def _build_authoritative_subject(
        self,
        connection: AsyncConnection,
        command: SourceVersionCommand,
        receipt: VerifiedObjectReceipt,
        bound_locator: NormalizedLocator | None = None,
    ) -> PolicySubject:
        """Rebuild the evaluation subject from the command's own evidence.

        A create carries its declared source type; an update reads the stored
        immutable source type of the workspace-bound row (a plain read before
        the source advisory lock — the type never changes, so the value is
        stable across the lock). The media type and byte size come from the
        verified receipt the service already validated against the expected
        object, so the subject reflects the canonical content being published
        rather than a client claim. A small-file create with a bound initial
        locator surfaces that locator on the subject so the locked
        publication guard can reach a definite verdict without rebuilding
        path evidence from the plugin request.
        """

        source_type: SourceType | None
        if isinstance(command, CreateSourceVersion):
            source_type = command.source_type
        else:
            result = await connection.execute(
                source_type_select_statement(command.workspace_id, command.source_id)
            )
            stored = result.scalar_one_or_none()
            source_type = SourceType(str(stored)) if stored is not None else None
        normalized_locator_value: str | None = None
        if bound_locator is not None:
            normalized_locator_value = bound_locator.value
        return PolicySubject(
            workspace_id=command.workspace_id,
            source_id=command.source_id,
            normalized_locator=normalized_locator_value,
            source_type=source_type,
            media_type=receipt.media_type,
            size_bytes=receipt.size_bytes,
        )

    async def _create_transition(
        self,
        connection: AsyncConnection,
        command: CreateSourceVersion,
        request_fingerprint: RequestFingerprint,
        receipt: VerifiedObjectReceipt,
        diagnostic_context: DiagnosticContext,
        identities: SourceCreateIdentities,
    ) -> tuple[_PendingRejection | None, SourceVersionPublicationResult | None]:
        """Execute the create state transition under both advisory locks.

        The initial ``source_locators`` row lands AFTER the create event and
        BEFORE the projection intents and the succeeded audit, so a duplicate
        active locator rejection rolls the whole create graph back. A bound
        locator whose path already has a foreign ACTIVE locator is rejected by
        the guarded pre-check before that insert — the typed, non-retryable
        ``source_locator_conflict`` (a permanent business conflict for this
        event's identity) instead of the unclassified integrity violation the
        partial unique index would raise, which the caller must never see as a
        retryable outcome-unknown loop.
        """
        if await self._select_source_workspace_id(connection, command.source_id) is not None:
            # ``source_id`` is a global primary key: any existing row, in the
            # requested workspace or another, rejects without tenant disclosure.
            return (
                _PendingRejection(
                    reason_code=REASON_SOURCE_ALREADY_EXISTS,
                    error=SourcePublicationError(
                        ErrorCode.SOURCE_ALREADY_EXISTS,
                        safe_details={"source_id": command.source_id},
                    ),
                ),
                None,
            )
        content_object_row = await self._insert_content_object(
            connection, identities.content_object_id, receipt
        )
        if content_object_row is None:
            return self._invariant_rejection(command), None
        if not content_object_metadata_matches(
            receipt,
            object_key=content_object_row.object_key,
            byte_size=content_object_row.byte_size,
            media_type=content_object_row.media_type,
        ):
            return (
                _PendingRejection(
                    reason_code=REASON_CONTENT_OBJECT_CONFLICT,
                    error=SourcePublicationError(
                        ErrorCode.SOURCE_CONTENT_OBJECT_CONFLICT,
                        safe_details={"source_id": command.source_id},
                    ),
                ),
                None,
            )
        await self._insert_pending_source(connection, command)
        await self._insert_version_one(
            connection,
            command,
            identities.source_version_id,
            content_object_row.content_object_id,
        )
        pointer_rejection = await self._activate_source_pointer(
            connection, command, identities.source_version_id
        )
        if pointer_rejection is not None:
            return pointer_rejection, None
        event_sequence, committed_at = await self._insert_create_event(
            connection, command, request_fingerprint, identities.source_version_id
        )
        if command.initial_locator is not None:
            conflicting_locator_source_id = await self._select_foreign_active_locator_source_id(
                connection, command, command.initial_locator
            )
            if conflicting_locator_source_id is not None:
                # Same transaction and advisory locks as the insert itself:
                # the pre-check shares the transition's locking discipline, so
                # no separate lookup window exists between check and insert.
                # The registry admits no safe detail field for this code; the
                # rejected source identity rides the audit row's target and
                # the diagnostic event fields instead.
                return (
                    _PendingRejection(
                        reason_code=REASON_SOURCE_LOCATOR_CONFLICT,
                        error=SourcePublicationError(ErrorCode.SOURCE_LOCATOR_CONFLICT),
                    ),
                    None,
                )
            await self._insert_initial_locator(
                connection,
                command,
                identities.source_locator_id,
                event_sequence,
                command.initial_locator,
            )
        await self._insert_projection_intent(
            connection,
            command,
            identities.source_version_id,
            identities.qdrant_intent_id,
            PROJECTION_KIND_QDRANT,
        )
        await self._insert_projection_intent(
            connection,
            command,
            identities.source_version_id,
            identities.neo4j_intent_id,
            PROJECTION_KIND_NEO4J,
        )
        await self._insert_success_audit(
            connection,
            command,
            receipt,
            diagnostic_context,
            identities.audit_event_id,
        )
        return None, SourceVersionPublicationResult(
            source_id=command.source_id,
            source_version_id=identities.source_version_id,
            content_version=CONTENT_VERSION_ONE,
            event_id=command.event_id,
            event_sequence=event_sequence,
            content_digest=receipt.content_digest,
            outcome=PublicationOutcome.PUBLISHED,
            committed_at=committed_at,
        )

    async def _update_transition(
        self,
        connection: AsyncConnection,
        command: UpdateSourceVersion,
        request_fingerprint: RequestFingerprint,
        receipt: VerifiedObjectReceipt,
        diagnostic_context: DiagnosticContext,
        identities: SourceUpdateIdentities,
    ) -> tuple[_PendingRejection | None, SourceVersionPublicationResult | None]:
        """Execute the update state transition under both advisory locks.

        The source, current version and current object rows are selected
        ``FOR UPDATE``; only ``active`` and ``stored_not_indexed`` sources may
        publish. The base comparison (design 8.6) precedes the content
        comparison: a stale base conflicts even when the proposed bytes equal
        the current object. Equal digest/key/size/media type writes only the
        no-change event and audit (design 8.7); anything else commits the
        changed graph (design 8.8) without touching type, title or state.
        """
        source_row = await self._select_locked_source(connection, command)
        if source_row is None:
            return (
                _PendingRejection(
                    reason_code=REASON_SOURCE_NOT_FOUND,
                    error=SourcePublicationError(
                        ErrorCode.SOURCE_NOT_FOUND,
                        safe_details={"source_id": command.source_id},
                    ),
                ),
                None,
            )
        if source_row.deleted_at is not None or source_row.sync_state not in (
            _UPDATEABLE_SOURCE_STATES
        ):
            return self._state_invalid_rejection(command, source_row.sync_state), None
        if source_row.current_version_id is None:
            # A publishable state with a null pointer cannot exist; the
            # invariant-failure rejection audits with a null reason.
            return self._invariant_rejection(command), None
        current = await self._select_locked_current_version(
            connection, command, source_row.current_version_id
        )
        if current is None or current.content_version < 1:
            return self._invariant_rejection(command), None
        # Base comparison BEFORE content comparison (design 8.6): a stale base
        # conflicts even when the proposed bytes equal the current object.
        if command.base_version_id != current.source_version_id:
            return (
                _PendingRejection(
                    reason_code=REASON_VERSION_CONFLICT,
                    error=SourcePublicationError(
                        ErrorCode.SOURCE_VERSION_CONFLICT,
                        safe_details={
                            "source_id": command.source_id,
                            "current_version_id": current.source_version_id,
                            "content_version": current.content_version,
                        },
                    ),
                ),
                None,
            )
        if self._receipt_matches_current_object(receipt, current):
            return await self._no_change_update_transition(
                connection,
                command,
                request_fingerprint,
                receipt,
                diagnostic_context,
                identities,
                current,
            )
        return await self._changed_update_transition(
            connection,
            command,
            request_fingerprint,
            receipt,
            diagnostic_context,
            identities,
            current,
        )

    def _state_invalid_rejection(
        self, command: UpdateSourceVersion, sync_state: str
    ) -> _PendingRejection:
        """Build the non-publishable-state rejection with the closed state token."""
        state_token = _SOURCE_STATE_TOKENS_BY_VALUE.get(sync_state)
        if state_token is None:
            # Impossible by the CHECK constraint; fail closed as an invariant.
            return self._invariant_rejection(command)
        return _PendingRejection(
            reason_code=REASON_SOURCE_STATE_INVALID,
            error=SourcePublicationError(
                ErrorCode.SOURCE_STATE_INVALID,
                safe_details={"source_id": command.source_id, "source_state": state_token},
            ),
        )

    @staticmethod
    def _receipt_matches_current_object(
        receipt: VerifiedObjectReceipt, current: LockedCurrentVersionRow
    ) -> bool:
        """Compare the receipt against the current object exactly (design 8.7)."""
        return (
            current.content_hash == receipt.content_digest.hexadecimal
            and content_object_metadata_matches(
                receipt,
                object_key=current.object_key,
                byte_size=current.byte_size,
                media_type=current.media_type,
            )
        )

    async def _select_locked_source(
        self, connection: AsyncConnection, command: UpdateSourceVersion
    ) -> LockedSourceRow | None:
        """Select the requested workspace's source row ``FOR UPDATE``.

        The workspace boundary is part of the match, so a source held by
        another workspace is indistinguishable from a missing one and nothing
        about the owning tenant is disclosed.
        """
        result = await connection.execute(
            sa.select(
                sources.c.sync_state,
                sources.c.current_version_id,
                sources.c.deleted_at,
            )
            .where(
                sources.c.source_id == command.source_id,
                sources.c.workspace_id == command.workspace_id,
            )
            .with_for_update()
        )
        row = result.one_or_none()
        if row is None:
            return None
        return LockedSourceRow(
            sync_state=row.sync_state,
            current_version_id=row.current_version_id,
            deleted_at=row.deleted_at,
        )

    async def _select_locked_current_version(
        self,
        connection: AsyncConnection,
        command: UpdateSourceVersion,
        current_version_id: UUID,
    ) -> LockedCurrentVersionRow | None:
        """Select the current version joined with its content object ``FOR UPDATE``."""
        result = await connection.execute(
            sa.select(
                source_versions.c.source_version_id,
                source_versions.c.content_version,
                source_versions.c.content_object_id,
                content_objects.c.content_hash,
                content_objects.c.object_key,
                content_objects.c.byte_size,
                content_objects.c.media_type,
            )
            .select_from(source_versions)
            .join(
                content_objects,
                content_objects.c.content_object_id == source_versions.c.content_object_id,
            )
            .where(
                source_versions.c.workspace_id == command.workspace_id,
                source_versions.c.source_id == command.source_id,
                source_versions.c.source_version_id == current_version_id,
            )
            .with_for_update()
        )
        row = result.one_or_none()
        if row is None:
            return None
        return LockedCurrentVersionRow(
            source_version_id=row.source_version_id,
            content_version=int(row.content_version),
            content_object_id=row.content_object_id,
            content_hash=row.content_hash,
            object_key=row.object_key,
            byte_size=int(row.byte_size),
            media_type=row.media_type,
        )

    async def _no_change_update_transition(
        self,
        connection: AsyncConnection,
        command: UpdateSourceVersion,
        request_fingerprint: RequestFingerprint,
        receipt: VerifiedObjectReceipt,
        diagnostic_context: DiagnosticContext,
        identities: SourceUpdateIdentities,
        current: LockedCurrentVersionRow,
    ) -> tuple[_PendingRejection | None, SourceVersionPublicationResult | None]:
        """Write only the no-change event and audit (design 8.7).

        No content object, version, intent, pointer, state or ``updated_at``
        change happens; ``base_version_id == committed_version_id`` is the
        persisted no-change marker.
        """
        event_sequence, committed_at = await self._insert_update_event(
            connection,
            command,
            request_fingerprint,
            base_version_id=current.source_version_id,
            committed_version_id=current.source_version_id,
        )
        await self._insert_publication_audit(
            connection,
            command,
            diagnostic_context,
            identities.audit_event_id,
            action=NO_CHANGE_AUDIT_ACTION,
            reason_code=REASON_CONTENT_UNCHANGED,
            safe_diff_hash=self._update_safe_diff_hash(command, receipt, current),
        )
        return None, SourceVersionPublicationResult(
            source_id=command.source_id,
            source_version_id=current.source_version_id,
            content_version=current.content_version,
            event_id=command.event_id,
            event_sequence=event_sequence,
            content_digest=receipt.content_digest,
            outcome=PublicationOutcome.NO_CHANGE,
            committed_at=committed_at,
        )

    async def _changed_update_transition(
        self,
        connection: AsyncConnection,
        command: UpdateSourceVersion,
        request_fingerprint: RequestFingerprint,
        receipt: VerifiedObjectReceipt,
        diagnostic_context: DiagnosticContext,
        identities: SourceUpdateIdentities,
        current: LockedCurrentVersionRow,
    ) -> tuple[_PendingRejection | None, SourceVersionPublicationResult | None]:
        """Write the changed-update graph (design 8.8).

        Content object upsert/reuse, version ``n+1`` with the current parent,
        the guarded pointer advance, the update event, two upsert intents and
        the succeeded audit. Source type/title stay untouched and the existing
        sync state is preserved.
        """
        content_object_row = await self._insert_content_object(
            connection, identities.content_object_id, receipt
        )
        if content_object_row is None:
            return self._invariant_rejection(command), None
        if not content_object_metadata_matches(
            receipt,
            object_key=content_object_row.object_key,
            byte_size=content_object_row.byte_size,
            media_type=content_object_row.media_type,
        ):
            return (
                _PendingRejection(
                    reason_code=REASON_CONTENT_OBJECT_CONFLICT,
                    error=SourcePublicationError(
                        ErrorCode.SOURCE_CONTENT_OBJECT_CONFLICT,
                        safe_details={"source_id": command.source_id},
                    ),
                ),
                None,
            )
        next_ordinal = current.content_version + 1
        await self._insert_next_version(
            connection,
            command,
            identities.source_version_id,
            content_object_row.content_object_id,
            parent_version_id=current.source_version_id,
            content_version=next_ordinal,
        )
        pointer_rejection = await self._advance_current_pointer(
            connection, command, identities.source_version_id
        )
        if pointer_rejection is not None:
            return pointer_rejection, None
        event_sequence, committed_at = await self._insert_update_event(
            connection,
            command,
            request_fingerprint,
            base_version_id=command.base_version_id,
            committed_version_id=identities.source_version_id,
        )
        await self._insert_projection_intent(
            connection,
            command,
            identities.source_version_id,
            identities.qdrant_intent_id,
            PROJECTION_KIND_QDRANT,
        )
        await self._insert_projection_intent(
            connection,
            command,
            identities.source_version_id,
            identities.neo4j_intent_id,
            PROJECTION_KIND_NEO4J,
        )
        await self._insert_publication_audit(
            connection,
            command,
            diagnostic_context,
            identities.audit_event_id,
            action=SUCCESS_AUDIT_ACTION,
            reason_code=None,
            safe_diff_hash=self._update_safe_diff_hash(command, receipt, current),
        )
        return None, SourceVersionPublicationResult(
            source_id=command.source_id,
            source_version_id=identities.source_version_id,
            content_version=next_ordinal,
            event_id=command.event_id,
            event_sequence=event_sequence,
            content_digest=receipt.content_digest,
            outcome=PublicationOutcome.PUBLISHED,
            committed_at=committed_at,
        )

    @staticmethod
    def _update_safe_diff_hash(
        command: UpdateSourceVersion,
        receipt: VerifiedObjectReceipt,
        current: LockedCurrentVersionRow,
    ) -> SafeDiffHash:
        """Compute the update safe diff hash over the locked current object."""
        return compute_safe_diff_hash(
            command.source_id,
            current.source_version_id,
            ContentDigest.parse(current.content_hash),
            receipt.content_digest,
        )

    async def _insert_next_version(
        self,
        connection: AsyncConnection,
        command: UpdateSourceVersion,
        source_version_id: UUID,
        content_object_id: UUID,
        *,
        parent_version_id: UUID,
        content_version: int,
    ) -> None:
        """Insert version ``n+1`` with the current parent and actor author."""
        await connection.execute(
            sa.insert(source_versions).values(
                source_version_id=source_version_id,
                workspace_id=command.workspace_id,
                source_id=command.source_id,
                content_object_id=content_object_id,
                content_version=content_version,
                parent_version_id=parent_version_id,
                author_kind=command.actor.actor_kind.value,
                author_id=command.actor.actor_id,
                client_timestamp=command.client_timestamp,
            )
        )

    async def _advance_current_pointer(
        self,
        connection: AsyncConnection,
        command: UpdateSourceVersion,
        source_version_id: UUID,
    ) -> _PendingRejection | None:
        """Advance the current pointer through the guarded transition.

        The guard matches the workspace/source pair whose pointer still equals
        the requested base; any other rowcount is the invariant failure. The
        existing sync state is preserved — only the pointer and ``updated_at``
        change.
        """
        guarded = await connection.execute(
            sa.update(sources)
            .values(
                current_version_id=source_version_id,
                updated_at=sa.text("CURRENT_TIMESTAMP"),
            )
            .where(
                sources.c.source_id == command.source_id,
                sources.c.workspace_id == command.workspace_id,
                sources.c.current_version_id == command.base_version_id,
            )
        )
        if guarded.rowcount != 1:
            return self._invariant_rejection(command)
        return None

    async def _insert_update_event(
        self,
        connection: AsyncConnection,
        command: UpdateSourceVersion,
        request_fingerprint: RequestFingerprint,
        *,
        base_version_id: UUID,
        committed_version_id: UUID,
    ) -> tuple[int, datetime]:
        """Insert the update event; PostgreSQL owns the sequence and time."""
        actor: SourceActor = command.actor
        statement = (
            sa.insert(sync_events)
            .values(
                event_id=command.event_id,
                workspace_id=command.workspace_id,
                source_id=command.source_id,
                device_id=actor.actor_id if actor.actor_kind is ActorKind.DEVICE else None,
                committed_version_id=committed_version_id,
                base_version_id=base_version_id,
                idempotency_key=command.idempotency_key.value,
                request_fingerprint=request_fingerprint.hexadecimal,
                event_type=UPDATE_EVENT_TYPE,
                client_timestamp=command.client_timestamp,
            )
            .returning(sync_events.c.event_sequence, sync_events.c.committed_at)
        )
        row = (await connection.execute(statement)).one()
        return int(row.event_sequence), row.committed_at

    @staticmethod
    def _invariant_rejection(command: SourceVersionCommand) -> _PendingRejection:
        """Build the invariant-failure rejection (audited with a null reason)."""
        return _PendingRejection(
            reason_code=None,
            error=SourcePublicationError(
                ErrorCode.SOURCE_CONCURRENCY_INVARIANT_FAILED,
                safe_details={"source_id": command.source_id},
            ),
        )

    async def _select_source_workspace_id(
        self, connection: AsyncConnection, source_id: UUID
    ) -> UUID | None:
        result = await connection.execute(
            sa.select(sources.c.workspace_id).where(sources.c.source_id == source_id)
        )
        return result.scalar_one_or_none()

    async def _select_foreign_active_locator_source_id(
        self, connection: AsyncConnection, command: CreateSourceVersion, locator: NormalizedLocator
    ) -> UUID | None:
        """Select the foreign owner of an ACTIVE locator at the create's bound path.

        Mirrors the partial unique active-locator index
        ``(workspace_id, normalized_locator) WHERE closed_event_id IS NULL``:
        any such row owned by a different source makes the bound initial
        locator a permanent business conflict for this create's identity, so
        the guarded pre-check inside the locked transition rejects with the
        typed, non-retryable conflict before the insert. The index remains the
        final arbiter for the residual two-simultaneous-creates race (the
        loser's violation surfaces as before and the next sanctioned retry
        finds the winner's row here).
        """
        result = await connection.execute(
            sa.select(source_locators.c.source_id).where(
                source_locators.c.workspace_id == command.workspace_id,
                source_locators.c.normalized_locator == locator.value,
                source_locators.c.closed_event_id.is_(None),
                source_locators.c.source_id != command.source_id,
            )
        )
        return result.scalar_one_or_none()

    async def _insert_content_object(
        self,
        connection: AsyncConnection,
        content_object_id: UUID,
        receipt: VerifiedObjectReceipt,
    ) -> ContentObjectLookupRow | None:
        """Upsert the exact content object and return the row to reuse.

        The insert is a no-op when the hash already exists, so the first
        ``verified_at`` and content-object identity survive every later
        deduplication. A missing row after the upsert is an impossible state
        reported as ``None`` for the caller's invariant rejection.
        """
        await connection.execute(content_object_upsert_statement(content_object_id, receipt))
        result = await connection.execute(
            content_object_by_hash_statement(receipt.content_digest.hexadecimal)
        )
        row = result.one_or_none()
        if row is None:
            return None
        return ContentObjectLookupRow(
            content_object_id=row.content_object_id,
            object_key=row.object_key,
            byte_size=int(row.byte_size),
            media_type=row.media_type,
        )

    async def _insert_pending_source(
        self, connection: AsyncConnection, command: CreateSourceVersion
    ) -> None:
        """Insert the source as ``pending`` with a null current pointer."""
        await connection.execute(
            sa.insert(sources).values(
                source_id=command.source_id,
                workspace_id=command.workspace_id,
                source_type=command.source_type.value,
                title=command.title.value,
            )
        )

    async def _insert_initial_locator(
        self,
        connection: AsyncConnection,
        command: CreateSourceVersion,
        source_locator_id: UUID,
        opened_sequence: int,
        locator: NormalizedLocator,
    ) -> None:
        """Insert the bound initial ``source_locators`` row for one create.

        The opening event and sequence come from the just-committed create
        event; the display locator mirrors the canonical normalized value
        until the lifecycle mutates it. The unique active locator index
        raises a constraint violation if the same workspace already has an
        active locator at the same path, rolling the whole create back; the
        foreign-active case is normally caught first by the guarded pre-check
        in :meth:`_create_transition`, which rejects with the typed conflict
        so the index violation stays the race-only final arbiter.
        """
        await connection.execute(
            sa.insert(source_locators).values(
                source_locator_id=source_locator_id,
                workspace_id=command.workspace_id,
                source_id=command.source_id,
                normalized_locator=locator.value,
                display_locator=locator.value,
                opened_event_id=command.event_id,
                opened_sequence=opened_sequence,
            )
        )

    async def _insert_version_one(
        self,
        connection: AsyncConnection,
        command: CreateSourceVersion,
        source_version_id: UUID,
        content_object_id: UUID,
    ) -> None:
        """Insert version 1 with a null parent and the actor-derived author."""
        await connection.execute(
            sa.insert(source_versions).values(
                source_version_id=source_version_id,
                workspace_id=command.workspace_id,
                source_id=command.source_id,
                content_object_id=content_object_id,
                content_version=CONTENT_VERSION_ONE,
                parent_version_id=None,
                author_kind=command.actor.actor_kind.value,
                author_id=command.actor.actor_id,
                client_timestamp=command.client_timestamp,
            )
        )

    async def _activate_source_pointer(
        self,
        connection: AsyncConnection,
        command: CreateSourceVersion,
        source_version_id: UUID,
    ) -> _PendingRejection | None:
        """Set the active current pointer through the guarded transition.

        The guard matches exactly the just-inserted ``pending`` row with a
        null pointer; any other rowcount is the invariant failure.
        """
        guarded = await connection.execute(
            sa.update(sources)
            .values(
                current_version_id=source_version_id,
                sync_state=_SOURCE_STATE_ACTIVE,
                updated_at=sa.text("CURRENT_TIMESTAMP"),
            )
            .where(
                sources.c.source_id == command.source_id,
                sources.c.workspace_id == command.workspace_id,
                sources.c.sync_state == _SOURCE_STATE_PENDING,
                sources.c.current_version_id.is_(None),
            )
        )
        if guarded.rowcount != 1:
            return self._invariant_rejection(command)
        return None

    async def _insert_create_event(
        self,
        connection: AsyncConnection,
        command: CreateSourceVersion,
        request_fingerprint: RequestFingerprint,
        source_version_id: UUID,
    ) -> tuple[int, datetime]:
        """Insert the create event; PostgreSQL owns the sequence and time."""
        actor: SourceActor = command.actor
        statement = (
            sa.insert(sync_events)
            .values(
                event_id=command.event_id,
                workspace_id=command.workspace_id,
                source_id=command.source_id,
                device_id=actor.actor_id if actor.actor_kind is ActorKind.DEVICE else None,
                committed_version_id=source_version_id,
                base_version_id=None,
                idempotency_key=command.idempotency_key.value,
                request_fingerprint=request_fingerprint.hexadecimal,
                event_type=CREATE_EVENT_TYPE,
                client_timestamp=command.client_timestamp,
            )
            .returning(sync_events.c.event_sequence, sync_events.c.committed_at)
        )
        row = (await connection.execute(statement)).one()
        return int(row.event_sequence), row.committed_at

    async def _insert_projection_intent(
        self,
        connection: AsyncConnection,
        command: SourceVersionCommand,
        source_version_id: UUID,
        projection_intent_id: UUID,
        projection_kind: str,
    ) -> None:
        """Insert one durable ``upsert`` dispatch intent for the event."""
        await connection.execute(
            sa.insert(projection_intents).values(
                projection_intent_id=projection_intent_id,
                workspace_id=command.workspace_id,
                event_id=command.event_id,
                source_id=command.source_id,
                source_version_id=source_version_id,
                projection_kind=projection_kind,
                operation=PROJECTION_OPERATION_UPSERT,
            )
        )

    async def _insert_success_audit(
        self,
        connection: AsyncConnection,
        command: CreateSourceVersion,
        receipt: VerifiedObjectReceipt,
        diagnostic_context: DiagnosticContext,
        audit_event_id: UUID,
    ) -> None:
        """Insert the in-transaction succeeded audit of a changed create."""
        safe_diff_hash = compute_safe_diff_hash(
            command.source_id, None, None, receipt.content_digest
        )
        await self._insert_publication_audit(
            connection,
            command,
            diagnostic_context,
            audit_event_id,
            action=SUCCESS_AUDIT_ACTION,
            reason_code=None,
            safe_diff_hash=safe_diff_hash,
        )

    async def _insert_publication_audit(
        self,
        connection: AsyncConnection,
        command: SourceVersionCommand,
        diagnostic_context: DiagnosticContext,
        audit_event_id: UUID,
        *,
        action: str,
        reason_code: str | None,
        safe_diff_hash: SafeDiffHash,
    ) -> None:
        """Insert the in-transaction succeeded audit with the safe diff hash.

        A changed create/update audits ``source.version_published`` with a
        null reason; a no-change update audits ``source.version_no_change``
        with ``content_unchanged`` (design 10.1). A replay never reaches this
        insert.
        """
        await connection.execute(
            sa.insert(audit_events).values(
                audit_event_id=audit_event_id,
                workspace_id=command.workspace_id,
                actor_kind=command.actor.actor_kind.value,
                actor_id=command.actor.actor_id,
                actor_reference=None,
                action=action,
                target_kind=AUDIT_TARGET_KIND_SOURCE,
                target_id=command.source_id,
                request_id=diagnostic_context.request_id,
                client_request_id=diagnostic_context.client_request_id,
                trace_id=diagnostic_context.trace.trace_id.value,
                result=AUDIT_RESULT_SUCCEEDED,
                reason_code=reason_code,
                safe_diff_hash=safe_diff_hash.hexadecimal,
            )
        )

    async def _resolve_committed_once(
        self,
        command: SourceVersionCommand,
        request_fingerprint: RequestFingerprint,
        diagnostic_context: DiagnosticContext,
    ) -> SourceVersionPublicationResult | None:
        result: SourceVersionPublicationResult | None = None
        rejection: _PendingRejection | None = None
        async with (
            self._engine.connect() as connection,
            connection.begin(),
        ):
            await apply_transaction_bounds(connection)
            is_workspace_trusted = await self._select_workspace_is_active(
                connection, command.workspace_id
            )
            if not is_workspace_trusted:
                # Before the trust boundary: typed diagnostics only, no audit.
                raise SourcePublicationError(
                    ErrorCode.SOURCE_PUBLISH_INPUT_INVALID,
                    safe_details={"reason": ACTOR_INVALID},
                )
            if not await self._is_actor_valid(connection, command):
                rejection = _PendingRejection(
                    reason_code=REASON_ACTOR_INVALID,
                    error=SourcePublicationError(
                        ErrorCode.SOURCE_PUBLISH_INPUT_INVALID,
                        safe_details={"reason": ACTOR_INVALID},
                    ),
                )
            else:
                rejection, result = await self._resolve_identity(
                    connection, command, request_fingerprint
                )
        if rejection is not None:
            await self._write_rejection_audit(command, diagnostic_context, rejection.reason_code)
            raise rejection.error
        return result

    async def _resolve_identity(
        self,
        connection: AsyncConnection,
        command: SourceVersionCommand,
        request_fingerprint: RequestFingerprint,
    ) -> tuple[_PendingRejection | None, SourceVersionPublicationResult | None]:
        key_row = await self._fetch_replay_row(
            connection,
            replay_lookup_by_key_statement(command.workspace_id, command.idempotency_key),
        )
        if key_row is not None:
            if (
                key_row.event_id != command.event_id
                or key_row.request_fingerprint != request_fingerprint.hexadecimal
            ):
                return (
                    _PendingRejection(
                        reason_code=REASON_IDEMPOTENCY_MISMATCH,
                        error=SourcePublicationError(
                            ErrorCode.SOURCE_IDEMPOTENCY_MISMATCH,
                            safe_details={"source_id": command.source_id},
                        ),
                    ),
                    None,
                )
            try:
                return None, hydrate_replay_result(key_row, command)
            except SourcePublicationError as invariant_cause:
                invariant_error = SourcePublicationError(
                    ErrorCode.SOURCE_CONCURRENCY_INVARIANT_FAILED,
                    safe_details={"source_id": command.source_id},
                )
                invariant_error.__cause__ = invariant_cause
                return _PendingRejection(reason_code=None, error=invariant_error), None
        event_row = await self._fetch_replay_row(
            connection, replay_lookup_by_event_statement(command.event_id)
        )
        if event_row is not None:
            # The key lookup already missed, so this event identity is held by
            # another key or workspace; only the requested IDs are disclosed.
            return (
                _PendingRejection(
                    reason_code=REASON_EVENT_IDENTITY_MISMATCH,
                    error=SourcePublicationError(
                        ErrorCode.SOURCE_EVENT_IDENTITY_MISMATCH,
                        safe_details={
                            "source_id": command.source_id,
                            "event_id": command.event_id,
                        },
                    ),
                ),
                None,
            )
        return None, None

    async def _fetch_replay_row(
        self, connection: AsyncConnection, statement: sa.Select[tuple[Any, ...]]
    ) -> ReplayLookupRow | None:
        result = await connection.execute(statement)
        row = result.one_or_none()
        return None if row is None else ReplayLookupRow.from_result_row(row)

    async def _select_workspace_is_active(
        self, connection: AsyncConnection, workspace_id: UUID
    ) -> bool:
        result = await connection.execute(
            sa.select(workspaces.c.status).where(workspaces.c.workspace_id == workspace_id)
        )
        return result.scalar_one_or_none() == _WORKSPACE_STATUS_ACTIVE

    async def _is_actor_valid(
        self, connection: AsyncConnection, command: SourceVersionCommand
    ) -> bool:
        actor: SourceActor = command.actor
        if actor.actor_kind is ActorKind.USER:
            result = await connection.execute(
                sa.select(workspaces.c.owner_user_id).where(
                    workspaces.c.workspace_id == command.workspace_id
                )
            )
            return result.scalar_one_or_none() == actor.actor_id
        if actor.actor_kind is ActorKind.DEVICE:
            result = await connection.execute(
                sa.select(devices.c.status).where(
                    devices.c.workspace_id == command.workspace_id,
                    devices.c.device_id == actor.actor_id,
                )
            )
            return result.scalar_one_or_none() == _DEVICE_STATUS_ACTIVE
        return True

    async def _write_rejection_audit(
        self,
        command: SourceVersionCommand,
        diagnostic_context: DiagnosticContext,
        reason_code: _RejectionReasonCode,
    ) -> None:
        statement = sa.insert(audit_events).values(
            audit_event_id=uuid4(),
            workspace_id=command.workspace_id,
            actor_kind=command.actor.actor_kind.value,
            actor_id=command.actor.actor_id,
            actor_reference=None,
            action=REJECTION_AUDIT_ACTION,
            target_kind=REJECTION_AUDIT_TARGET_KIND,
            target_id=command.source_id,
            request_id=diagnostic_context.request_id,
            client_request_id=diagnostic_context.client_request_id,
            trace_id=diagnostic_context.trace.trace_id.value,
            result=AUDIT_RESULT_REJECTED,
            reason_code=reason_code,
            safe_diff_hash=None,
        )
        async with (
            self._engine.connect() as connection,
            connection.begin(),
        ):
            await apply_transaction_bounds(connection)
            await connection.execute(statement)
