"""Authorization-aware idempotency preflight, replay hydration and atomic create.

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

``commit_create`` runs the canonical create transaction (design sections
8.3-8.5 and 10.1) inside one ``READ COMMITTED`` transaction: the pinned
``SET LOCAL`` bounds, the idempotency advisory lock, the trusted
workspace/actor revalidation, the replay/mismatch recheck, the source
advisory lock, the global source-existence rejection, the exact
content-object upsert/select/compare, the pending source, version 1 with a
null parent, the guarded active-pointer transition, the create event with a
null base, the two upsert intents, the succeeded audit with the safe diff
hash, and the commit. Backend UUIDv7 identities are allocated once per
service invocation and reused through bounded transaction attempts;
PostgreSQL owns the event identity sequence and the transaction timestamps.

Every statement is schema-qualified through the Task 6 Core metadata and
parameter-bound; driver failures are routed through
:mod:`postgresql_source_store.error_mapping` so SQLSTATE, SQL, parameters and
driver text never leave the adapter. The update commit lands in a later task.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final
from uuid import UUID, uuid4, uuid7

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from personal_os.diagnostics.context import DiagnosticContext
from personal_os.error_contracts.codes import ErrorCode
from personal_os.object_storage import VerifiedObjectReceipt
from personal_os.object_storage.keys import ContentDigest
from personal_os.sources.actors import ActorKind, SourceActor
from personal_os.sources.commands import (
    CreateSourceVersion,
    IdempotencyKey,
    UpdateSourceVersion,
)
from personal_os.sources.errors import ACTOR_INVALID, SourcePublicationError
from personal_os.sources.fingerprint import (
    RequestFingerprint,
    SourceVersionCommand,
    compute_safe_diff_hash,
)
from personal_os.sources.results import PublicationOutcome, SourceVersionPublicationResult
from postgresql_source_store.engine import apply_transaction_bounds
from postgresql_source_store.error_mapping import DatabaseRetryPolicy
from postgresql_source_store.locks import idempotency_lock_statement, source_lock_statement
from postgresql_source_store.tables import (
    audit_events,
    content_objects,
    devices,
    projection_intents,
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

#: Audit constants for the in-transaction success audit of a changed create.
SUCCESS_AUDIT_ACTION: Final[str] = "source.version_published"
AUDIT_TARGET_KIND_SOURCE: Final[str] = "source"
AUDIT_RESULT_SUCCEEDED: Final[str] = "succeeded"

#: Canonical create-transition literals.
CREATE_EVENT_TYPE: Final[str] = "create"
CONTENT_VERSION_ONE: Final[int] = 1
PROJECTION_KIND_QDRANT: Final[str] = "qdrant"
PROJECTION_KIND_NEO4J: Final[str] = "neo4j"
PROJECTION_OPERATION_UPSERT: Final[str] = "upsert"

#: Workspace, device and source lifecycle states referenced by the transitions.
_WORKSPACE_STATUS_ACTIVE: Final[str] = "active"
_DEVICE_STATUS_ACTIVE: Final[str] = "active"
_SOURCE_STATE_PENDING: Final[str] = "pending"
_SOURCE_STATE_ACTIVE: Final[str] = "active"

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


@dataclass(frozen=True, slots=True)
class SourceCreateIdentities:
    """Backend UUIDv7 identities for one create service invocation.

    The five generated identities are allocated once per service invocation
    and reused through the bounded transaction attempts, so a retry rewrites
    the same canonical identity rather than leaking a new one per attempt.
    The source and event identities come from the command, and the event
    sequence and every timestamp stay PostgreSQL-owned.
    """

    content_object_id: UUID
    source_version_id: UUID
    qdrant_intent_id: UUID
    neo4j_intent_id: UUID
    audit_event_id: UUID

    @classmethod
    def allocate(cls) -> SourceCreateIdentities:
        """Allocate the five fresh time-ordered UUIDv7 identities."""
        return cls(
            content_object_id=uuid7(),
            source_version_id=uuid7(),
            qdrant_intent_id=uuid7(),
            neo4j_intent_id=uuid7(),
            audit_event_id=uuid7(),
        )


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
    LOCAL`` bounds and the bounded contention retry; ``commit_create`` runs
    the same locked prefix plus the canonical create transition. The update
    commit arrives with the later update task.
    """

    def __init__(self, engine: AsyncEngine, *, retry: DatabaseRetryPolicy | None = None) -> None:
        self._engine = engine
        self._retry = retry if retry is not None else DatabaseRetryPolicy()

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
    ) -> SourceVersionPublicationResult:
        identities = SourceCreateIdentities.allocate()
        return await self._retry.run(
            lambda _attempt: self._commit_create_once(
                command, request_fingerprint, receipt, diagnostic_context, identities
            ),
            source_id=command.source_id,
        )

    async def commit_update(
        self,
        command: UpdateSourceVersion,
        request_fingerprint: RequestFingerprint,
        receipt: VerifiedObjectReceipt,
        diagnostic_context: DiagnosticContext,
    ) -> SourceVersionPublicationResult:
        raise NotImplementedError("commit_update lands with the update transaction task")

    async def _commit_create_once(
        self,
        command: CreateSourceVersion,
        request_fingerprint: RequestFingerprint,
        receipt: VerifiedObjectReceipt,
        diagnostic_context: DiagnosticContext,
        identities: SourceCreateIdentities,
    ) -> SourceVersionPublicationResult:
        result: SourceVersionPublicationResult | None = None
        rejection: _PendingRejection | None = None
        async with (
            self._engine.connect() as connection,
            connection.begin(),
        ):
            await apply_transaction_bounds(connection)
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
                if rejection is None and result is None:
                    await connection.execute(source_lock_statement(command.source_id))
                    rejection, result = await self._create_transition(
                        connection,
                        command,
                        request_fingerprint,
                        receipt,
                        diagnostic_context,
                        identities,
                    )
        if rejection is not None:
            await self._write_rejection_audit(command, diagnostic_context, rejection.reason_code)
            raise rejection.error
        if result is None:
            raise SourcePublicationError(
                ErrorCode.SOURCE_CONCURRENCY_INVARIANT_FAILED,
                safe_details={"source_id": command.source_id},
            )
        return result

    async def _create_transition(
        self,
        connection: AsyncConnection,
        command: CreateSourceVersion,
        request_fingerprint: RequestFingerprint,
        receipt: VerifiedObjectReceipt,
        diagnostic_context: DiagnosticContext,
        identities: SourceCreateIdentities,
    ) -> tuple[_PendingRejection | None, SourceVersionPublicationResult | None]:
        """Execute the create state transition under both advisory locks."""
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
        command: CreateSourceVersion,
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
        """Insert the in-transaction succeeded audit with the safe diff hash."""
        safe_diff_hash = compute_safe_diff_hash(
            command.source_id, None, None, receipt.content_digest
        )
        await connection.execute(
            sa.insert(audit_events).values(
                audit_event_id=audit_event_id,
                workspace_id=command.workspace_id,
                actor_kind=command.actor.actor_kind.value,
                actor_id=command.actor.actor_id,
                actor_reference=None,
                action=SUCCESS_AUDIT_ACTION,
                target_kind=AUDIT_TARGET_KIND_SOURCE,
                target_id=command.source_id,
                request_id=diagnostic_context.request_id,
                client_request_id=diagnostic_context.client_request_id,
                trace_id=diagnostic_context.trace.trace_id.value,
                result=AUDIT_RESULT_SUCCEEDED,
                reason_code=None,
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
